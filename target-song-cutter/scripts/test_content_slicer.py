import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import content_slicer as app

from content_slicer import (
    apply_tail_padding,
    group_plan,
    load_plan,
    reject_gift_overlaps,
    safe_name,
    srt_timestamp,
)


class ContentSlicerTests(unittest.TestCase):
    def write_plan(self, rows):
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / "plan.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "slice_id", "order", "start_seconds", "end_seconds", "title",
                "outline", "hook", "reason", "keep",
            ])
            writer.writerows(rows)
        return temporary, path

    def test_loads_only_kept_rows_and_groups_non_contiguous_parts(self):
        temporary, path = self.write_plan([
            ["story-1", 2, 50, 60, "Title", "Outline", "Hook", "Payoff", "yes"],
            ["story-1", 1, 10, 20, "Title", "Outline", "Hook", "Setup", "1"],
            ["story-2", 1, 70, 80, "Skip", "", "", "", "no"],
        ])
        self.addCleanup(temporary.cleanup)
        ranges = load_plan(path, duration=100)
        self.assertEqual([item.start for item in ranges], [10, 50])
        self.assertEqual(len(group_plan(ranges)["story-1"]), 2)

    def test_rejects_duplicate_order(self):
        temporary, path = self.write_plan([
            ["story", 1, 0, 5, "", "", "", "", "yes"],
            ["story", 1, 10, 15, "", "", "", "", "yes"],
        ])
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ValueError, "Duplicate order"):
            load_plan(path)

    def test_media_duration_retries_windows_dll_start_failure(self):
        calls = []
        results = iter(
            [
                type("Result", (), {"returncode": -1073741502, "stderr": ""})(),
                type(
                    "Result",
                    (),
                    {
                        "returncode": 1,
                        "stderr": "Duration: 02:36:12.716, start: 0.000000",
                    },
                )(),
            ]
        )

        original_run = app.subprocess.run
        original_ffmpeg = app.ensure_ffmpeg
        original_sleep = app.time.sleep
        try:
            app.ensure_ffmpeg = lambda: "ffmpeg"
            app.subprocess.run = lambda command, **kwargs: (
                calls.append((command, kwargs)) or next(results)
            )
            app.time.sleep = lambda _seconds: None
            duration = app.media_duration(Path("直播.flv"))
        finally:
            app.subprocess.run = original_run
            app.ensure_ffmpeg = original_ffmpeg
            app.time.sleep = original_sleep
        self.assertAlmostEqual(duration, 9372.716)
        self.assertEqual(len(calls), 2)

    def test_local_model_ready_rejects_partial_weight_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "model"
            model.mkdir()
            (model / "config.json").write_text("{}", encoding="utf-8")
            with (model / "model.safetensors").open("wb") as handle:
                handle.truncate(8)
            self.assertFalse(
                app._local_model_ready(model, (("config.json", 1), ("model.safetensors", 9)))
            )
            self.assertTrue(
                app._local_model_ready(model, (("config.json", 1), ("model.safetensors", 8)))
            )

    def test_helpers(self):
        self.assertEqual(srt_timestamp(3661.234), "01:01:01,234")
        self.assertEqual(safe_name('a:b/c*'), "a_b_c_")

    def test_failed_same_source_transcript_can_resume_gap_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "continuous-source.mkv"
            source.write_bytes(b"media")
            output = root / "transcript"
            output.mkdir()
            payload = {
                "source": str(source.resolve()),
                "authoritative": True,
                "language": "zh",
                "segments": [{
                    "start_seconds": 1.0,
                    "end_seconds": 2.0,
                    "text": "已有字幕",
                    "words": [],
                }],
            }
            (output / "transcript.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            (output / "transcript-completeness.json").write_text(
                json.dumps({
                    "status": "FAIL",
                    "unresolved_windows": [{"core_start": 4.0, "core_end": 6.0}],
                }),
                encoding="utf-8",
            )
            resumed = app.resumable_failed_transcript(output, source)
            self.assertIsNotNone(resumed)
            self.assertEqual(resumed["segments"][0]["text"], "已有字幕")

    def test_passed_or_different_source_transcript_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mkv"
            source.write_bytes(b"media")
            output = root / "transcript"
            output.mkdir()
            (output / "transcript.json").write_text(
                json.dumps({
                    "source": str(root / "different.mkv"),
                    "segments": [{"text": "旧字幕"}],
                }),
                encoding="utf-8",
            )
            (output / "transcript-completeness.json").write_text(
                json.dumps({
                    "status": "FAIL",
                    "unresolved_windows": [{"core_start": 4.0, "core_end": 6.0}],
                }),
                encoding="utf-8",
            )
            self.assertIsNone(app.resumable_failed_transcript(output, source))

    def test_authoritative_csv_correction_cannot_be_overwritten_by_stale_asr_words(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript_json = root / "transcript.json"
            transcript_csv = root / "transcript.csv"
            transcript_json.write_text(
                json.dumps({
                    "authoritative": True,
                    "segments": [{
                        "start_seconds": 10.0,
                        "end_seconds": 12.0,
                        "text": "小鹿来了",
                        "avg_logprob": -0.2,
                        "no_speech_prob": 0.01,
                        "words": [{
                            "start_seconds": 10.0,
                            "end_seconds": 12.0,
                            "word": "小鹿来了",
                            "probability": 0.9,
                        }],
                    }],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            with transcript_csv.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["start_seconds", "end_seconds", "text"]
                )
                writer.writeheader()
                writer.writerow({
                    "start_seconds": 10,
                    "end_seconds": 12,
                    "text": "小路来了",
                })
            segments = app.load_authoritative_segments(transcript_json, transcript_csv)
            self.assertEqual(segments[0]["text"], "小路来了")
            self.assertEqual(segments[0]["words"], [])
            partial = app.EditRange("corrected", 1, 10, 10.5, "", "", "", "")
            with self.assertRaisesRegex(ValueError, "Split/align the source"):
                app._map_segments_to_parts(segments, [partial])

    def test_authoritative_words_follow_non_contiguous_edit_plan_without_retranscription(self):
        segments = [{
            "start_seconds": 10.0,
            "end_seconds": 31.0,
            "text": "甲乙",
            "avg_logprob": -0.1,
            "no_speech_prob": 0.0,
            "words": [
                {"start_seconds": 10.5, "end_seconds": 11.0, "word": "甲", "probability": 0.9},
                {"start_seconds": 30.5, "end_seconds": 31.0, "word": "乙", "probability": 0.9},
            ],
        }]
        parts = [
            app.EditRange("story", 1, 10, 12, "", "", "", ""),
            app.EditRange("story", 2, 30, 32, "", "", "", ""),
        ]
        mapped = app._map_segments_to_parts(segments, parts)
        self.assertEqual([item["text"] for item in mapped], ["甲", "乙"])
        self.assertAlmostEqual(mapped[0]["start_seconds"], 0.5)
        self.assertAlmostEqual(mapped[1]["start_seconds"], 2.5)
    def test_slice_authoritative_writes_provenance_and_ass_without_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript_csv = root / "transcript.csv"
            with transcript_csv.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["start_seconds", "end_seconds", "text"]
                )
                writer.writeheader()
                writer.writerow({"start_seconds": 10, "end_seconds": 11, "text": "权威字幕"})
            transcript_json = root / "transcript.json"
            transcript_json.write_text(
                json.dumps({
                    "authoritative": True,
                    "segments": [{
                        "start_seconds": 10,
                        "end_seconds": 11,
                        "text": "权威字幕",
                        "avg_logprob": -0.1,
                        "no_speech_prob": 0.0,
                        "words": [{
                            "start_seconds": 10,
                            "end_seconds": 11,
                            "word": "权威字幕",
                            "probability": 0.95,
                        }],
                    }],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            completeness = root / "transcript-completeness.json"
            completeness.write_text(
                json.dumps({"status": "PASS", "authoritative": True}),
                encoding="utf-8",
            )
            speech = root / "speech-activity.json"
            speech.write_text(
                json.dumps({"regions": [{"start_seconds": 10, "end_seconds": 11}]}),
                encoding="utf-8",
            )
            plan_temporary, plan = self.write_plan([
                ["story", 1, 10, 12, "Title", "", "", "", "1"],
            ])
            self.addCleanup(plan_temporary.cleanup)
            manifest = root / "slices.csv"
            with manifest.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["file", "slice_id"])
                writer.writeheader()
                writer.writerow({"file": "001_Title.mp4", "slice_id": "story"})
            template = root / "template.ass"
            template.write_text(
                "[Script Info]\nWrapStyle: 0\n[V4+ Styles]\n"
                "Style: Regular,Arial,70\n[Events]\n",
                encoding="utf-8",
            )
            output = root / "axis"
            ass_output = root / "clips"
            result = app.slice_authoritative(SimpleNamespace(
                transcript=transcript_csv,
                transcript_json=transcript_json,
                completeness_report=completeness,
                speech_json=speech,
                plan=plan,
                manifest=manifest,
                output=output,
                ass_output=ass_output,
                ass_template=template,
                ass_style="Regular",
                ass_name="测试",
                ass_font="",
                ass_font_size=None,
                ass_color="",
                content_types_csv=None,
                subtitle_delay=0.0,
                subtitle_vad_lead=0.0,
                subtitle_vad_tail=0.0,
                subtitle_vad_min_overlap=0.08,
                tail_padding=0.0,
            ))
            self.assertEqual(result, 0)
            provenance = json.loads((output / "provenance.json").read_text(encoding="utf-8"))
            self.assertFalse(provenance["clip_local_transcription"])
            self.assertIn("权威字幕", (ass_output / "001_Title.ass").read_text(encoding="utf-8-sig"))
    def test_unaligned_partial_source_paragraph_requires_source_repair(self):
        segment = {"start_seconds": 100, "end_seconds": 130,
                   "text": "一个包含下一话题的完整长段落", "words": []}
        for start, end in [(100, 114), (129.8, 140), (105, 135)]:
            with self.subTest(start=start, end=end):
                part = app.EditRange("long-row", 1, start, end, "", "", "", "")
                with self.assertRaisesRegex(ValueError, "Split/align the source"):
                    app._map_segments_to_parts([segment], [part])
        full = app.EditRange("full", 1, 99, 131, "", "", "", "")
        self.assertEqual(app._map_segments_to_parts([segment], [full])[0]["text"], segment["text"])

    def test_punctuation_only_source_row_does_not_create_flash_subtitle(self):
        part = app.EditRange("punctuation", 1, 10, 12, "", "", "", "")
        rows = [{"start_seconds": 10, "end_seconds": 11, "text": "……！"}]
        self.assertEqual(app._map_segments_to_parts(rows, [part]), [])

    def test_unaligned_rows_allow_rounding_but_ignore_subframe_edge(self):
        segment = {"start_seconds": 10, "end_seconds": 12, "text": "完整一句"}
        rounded = app.EditRange("rounded", 1, 10.04, 11.96, "", "", "", "")
        self.assertEqual(app._map_segments_to_parts([segment], [rounded])[0]["text"], "完整一句")
        edge = app.EditRange("edge", 1, 11.99, 15, "", "", "", "")
        self.assertEqual(app._map_segments_to_parts([segment], [edge]), [])

    def test_native_caption_policy_survives_source_loading_without_overlay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            j, c = root / "transcript.json", root / "transcript.csv"
            j.write_text(json.dumps({"segments": [{"start_seconds": 10, "end_seconds": 40,
                         "text": "原片已有的旁白字幕", "render_policy": "native-captions"}]}), encoding="utf-8")
            c.write_text("start_seconds,end_seconds,text,render_policy\n10,40,原片已有的旁白字幕,native-captions\n", encoding="utf-8-sig")
            loaded = app.load_authoritative_segments(j, c)
            self.assertEqual(loaded[0]["text"], "原片已有的旁白字幕")
            part = app.EditRange("native", 1, 12, 15, "", "", "", "")
            self.assertEqual(app._map_segments_to_parts(loaded, [part]), [])

    def test_later_invalid_clip_does_not_overwrite_earlier_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            c, j = root / "transcript.csv", root / "transcript.json"
            c.write_text("start_seconds,end_seconds,text\n10,11,第一句\n20,50,不可截断的长段落\n", encoding="utf-8-sig")
            j.write_text(json.dumps({"segments": []}), encoding="utf-8")
            complete, speech = root / "complete.json", root / "speech.json"
            complete.write_text(json.dumps({"status": "PASS", "authoritative": True}), encoding="utf-8")
            speech.write_text(json.dumps({"regions": []}), encoding="utf-8")
            plan_temp, plan = self.write_plan([
                ["first", 1, 9, 12, "First", "", "", "", "1"],
                ["second", 1, 20, 30, "Second", "", "", "", "1"],
            ])
            self.addCleanup(plan_temp.cleanup)
            manifest = root / "slices.csv"
            manifest.write_text("file,slice_id\n001_First.mp4,first\n002_Second.mp4,second\n", encoding="utf-8-sig")
            output, clips = root / "axis", root / "clips"
            (output / "001_First").mkdir(parents=True)
            clips.mkdir()
            old_axis, old_ass = output / "001_First/transcript.json", clips / "001_First.ass"
            old_axis.write_bytes(b"original axis")
            old_ass.write_bytes(b"user edited ASS")
            args = SimpleNamespace(transcript=c, transcript_json=j, completeness_report=complete,
                speech_json=speech, plan=plan, manifest=manifest, output=output,
                ass_output=clips, tail_padding=0, content_types_csv=None)
            with self.assertRaisesRegex(ValueError, "Split/align the source"):
                app.slice_authoritative(args)
            self.assertEqual(old_axis.read_bytes(), b"original axis")
            self.assertEqual(old_ass.read_bytes(), b"user edited ASS")
            self.assertFalse((output / "002_Second").exists())

    def test_tail_padding_only_extends_last_part_of_each_slice(self):
        temporary, path = self.write_plan([
            ["story", 1, 10, 20, "", "", "", "", "yes"],
            ["story", 2, 50, 60, "", "", "", "", "yes"],
            ["other", 1, 95, 99, "", "", "", "", "yes"],
        ])
        self.addCleanup(temporary.cleanup)
        padded = apply_tail_padding(load_plan(path), padding=5, duration=100)
        by_slice = group_plan(padded)
        self.assertEqual([(item.start, item.end) for item in by_slice["story"]], [(10, 20), (50, 65)])
        self.assertEqual([(item.start, item.end) for item in by_slice["other"]], [(95, 100)])

    def test_padded_tail_cannot_run_into_gift_acknowledgement(self):
        temporary, path = self.write_plan([
            ["story", 1, 50, 60, "", "", "", "", "yes"],
        ])
        self.addCleanup(temporary.cleanup)
        gift_path = Path(temporary.name) / "gifts.csv"
        with gift_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["start_seconds", "end_seconds", "text"])
            writer.writerow([62, 63, "谢谢甲的棉花糖"])
        padded = apply_tail_padding(load_plan(path), padding=5)
        with self.assertRaisesRegex(ValueError, "gift acknowledgements"):
            reject_gift_overlaps(padded, gift_path)


if __name__ == "__main__":
    unittest.main()
