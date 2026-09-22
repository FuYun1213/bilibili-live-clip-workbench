import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import review_workspace


ASS_TEXT = """[Script Info]
Title: test
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,第一段
Dialogue: 0,0:00:10.00,0:00:15.00,Default,,0,0,0,,第二段
"""


class ReviewProxyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.run = self.root / "runs" / "prepare-test"
        self.review = self.run / "export" / "clips"
        self.review.mkdir(parents=True)
        self.source = self.root / "source.mkv"
        self.source.write_bytes(b"source")
        (self.root / "workflow-project.json").write_text(
            json.dumps({"config": {"source": str(self.source)}, "radio_layout": False}),
            encoding="utf-8",
        )
        transcript_dir = self.root / "analysis" / "transcript"
        transcript_dir.mkdir(parents=True)
        with (transcript_dir / "transcript.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["start_seconds", "end_seconds", "text"]
            )
            writer.writeheader()
            writer.writerows([
                {"start_seconds": 92, "end_seconds": 96, "text": "前置上下文"},
                {"start_seconds": 102, "end_seconds": 106, "text": "核心整场旧字幕"},
                {"start_seconds": 111, "end_seconds": 114, "text": "段间上下文"},
                {"start_seconds": 124, "end_seconds": 128, "text": "后置上下文"},
            ])
        with (self.run / "edit-plan.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["slice_id", "order", "start_seconds", "end_seconds", "keep"],
            )
            writer.writeheader()
            writer.writerows([
                {"slice_id": "001", "order": 1, "start_seconds": 100, "end_seconds": 110, "keep": 1},
                {"slice_id": "001", "order": 2, "start_seconds": 115, "end_seconds": 120, "keep": 1},
            ])
        self.video = self.review / "001_test.mp4"
        self.video.write_bytes(b"exact-video")
        self.video.with_suffix(".ass").write_text(ASS_TEXT, encoding="utf-8-sig")
        with (self.review / "titles-and-covers.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["clip_id", "video", "title", "cover_text_primary", "cover_text_secondary"],
            )
            writer.writeheader()
            writer.writerow({
                "clip_id": "001",
                "video": self.video.name,
                "title": "标题",
                "cover_text_primary": "主",
                "cover_text_secondary": "副",
            })

    def tearDown(self):
        self.temporary.cleanup()

    def test_proxy_adds_context_without_modifying_exact_video(self):
        def fake_render(_source, destination, _segments, _ffmpeg):
            destination.write_bytes(b"proxy-video")

        with patch.object(review_workspace, "_media_duration_with_ffmpeg", return_value=200.0), patch.object(
            review_workspace, "render_review_proxy", side_effect=fake_render
        ):
            metadata = review_workspace.generate_review_proxy(self.video, 10.0, Path("ffmpeg"))

        self.assertEqual(self.video.read_bytes(), b"exact-video")
        self.assertEqual(review_workspace.review_media_path(self.video).read_bytes(), b"proxy-video")
        self.assertEqual(
            [segment["kind"] for segment in metadata["initial_segments"]],
            ["pre", "original", "context", "original", "post"],
        )
        proxy_ass = review_workspace.review_proxy_ass_path(self.video).read_text(encoding="utf-8-sig")
        self.assertIn("0:00:10.00,0:00:15.00", proxy_ass)
        self.assertIn("0:00:25.00,0:00:30.00", proxy_ass)
        self.assertIn("0:00:02.00,0:00:06.00", proxy_ass)
        self.assertIn("前置上下文", proxy_ass)
        self.assertIn("段间上下文", proxy_ass)
        self.assertIn("后置上下文", proxy_ass)
        self.assertNotIn("核心整场旧字幕", proxy_ass)
        self.assertEqual(metadata["context_subtitles"]["cue_count"], 3)

    def test_approval_maps_proxy_subtitles_back_to_exact_timeline(self):
        def fake_render(_source, destination, _segments, _ffmpeg):
            destination.write_bytes(b"proxy-video")

        with patch.object(review_workspace, "_media_duration_with_ffmpeg", return_value=200.0), patch.object(
            review_workspace, "render_review_proxy", side_effect=fake_render
        ):
            review_workspace.generate_review_proxy(self.video, 10.0, Path("ffmpeg"))
        self.assertTrue(review_workspace.sync_review_proxy_ass_to_original(self.video))
        exact_ass = self.video.with_suffix(".ass").read_text(encoding="utf-8-sig")
        self.assertIn("0:00:00.00,0:00:05.00", exact_ass)
        self.assertIn("0:00:10.00,0:00:15.00", exact_ass)
        self.assertNotIn("0:00:25.00,0:00:30.00", exact_ass)
        self.assertNotIn("前置上下文", exact_ass)
        self.assertNotIn("段间上下文", exact_ass)
        self.assertNotIn("后置上下文", exact_ass)

    def test_streaming_review_starts_with_only_original_clip_regions(self):
        with patch.object(
            review_workspace, "_media_duration_with_ffmpeg", return_value=200.0
        ):
            metadata = review_workspace.prepare_streaming_review(
                self.video, Path("ffmpeg")
            )

        self.assertTrue(metadata["streaming"])
        self.assertEqual(review_workspace.review_media_path(self.video), self.video)
        self.assertEqual(review_workspace.timeline_render_source(self.video), self.source)
        self.assertFalse(review_workspace.review_proxy_video_path(self.video).exists())
        self.assertEqual(
            [segment["kind"] for segment in metadata["initial_segments"]],
            ["original", "original"],
        )
        proxy_ass = review_workspace.review_proxy_ass_path(self.video).read_text(
            encoding="utf-8-sig"
        )
        self.assertNotIn("前置上下文", proxy_ass)
        self.assertNotIn("后置上下文", proxy_ass)

    def test_manual_pre_extension_keeps_original_default_playback_start(self):
        with patch.object(
            review_workspace, "_media_duration_with_ffmpeg", return_value=200.0
        ):
            metadata = review_workspace.prepare_streaming_review(
                self.video, Path("ffmpeg")
            )
        state = review_workspace.load_timeline_edit(
            self.video, float(metadata["source_duration"])
        )
        self.assertTrue(
            review_workspace.reveal_streaming_context(
                self.video, state, 100.0, chunk_seconds=4.0,
                direction="before",
            )
        )
        stored = review_workspace.review_proxy_metadata(self.video)
        self.assertEqual(
            float(stored["initial_segments"][0]["source_start"]), 100.0
        )
        self.assertEqual(float(state["segments"][0]["source_start"]), 96.0)
        self.assertEqual(
            review_workspace.review_default_source_start(self.video), 100.0
        )
    def test_streaming_waveform_uses_small_exact_clip_and_maps_source_ranges(self):
        with patch.object(
            review_workspace, "_media_duration_with_ffmpeg", return_value=200.0
        ):
            review_workspace.prepare_streaming_review(self.video, Path("ffmpeg"))
        spec = review_workspace.review_waveform_spec(self.video)
        self.assertEqual(spec["media"], self.video.resolve())
        self.assertEqual(spec["coordinate_space"], "exact_regions")
        data = {
            **spec,
            "duration": 15.0,
            "peaks": [0.0, 0.5, 1.0],
        }
        self.assertAlmostEqual(
            review_workspace.waveform_sample_seconds(data, 105.0), 5.0
        )
        self.assertAlmostEqual(
            review_workspace.waveform_sample_seconds(data, 117.0), 12.0
        )
        self.assertIsNone(
            review_workspace.waveform_sample_seconds(data, 112.0)
        )

    def test_streaming_review_extends_only_with_explicit_direction(self):
        with patch.object(
            review_workspace, "_media_duration_with_ffmpeg", return_value=200.0
        ):
            metadata = review_workspace.prepare_streaming_review(
                self.video, Path("ffmpeg")
            )
        state = review_workspace.load_timeline_edit(
            self.video, float(metadata["source_duration"])
        )

        self.assertFalse(
            review_workspace.reveal_streaming_context(
                self.video, state, 124.0, chunk_seconds=8.0
            )
        )
        self.assertEqual(len(state["segments"]), 2)

        self.assertTrue(
            review_workspace.reveal_streaming_context(
                self.video, state, 120.0, chunk_seconds=8.0,
                direction="after",
            )
        )
        after_click = review_workspace.review_proxy_ass_path(self.video).read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("后置上下文", after_click)
        self.assertNotIn("前置上下文", after_click)

        self.assertTrue(
            review_workspace.reveal_streaming_context(
                self.video, state, 100.0, chunk_seconds=8.0,
                direction="before",
            )
        )
        after_both_clicks = review_workspace.review_proxy_ass_path(self.video).read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("前置上下文", after_both_clicks)
        self.assertIn("后置上下文", after_both_clicks)
        self.assertEqual(
            [segment["source_start"] for segment in state["segments"]],
            sorted(segment["source_start"] for segment in state["segments"]),
        )
        self.assertEqual(state["segments"][0]["kind"], "manual-pre-context")
        self.assertEqual(state["segments"][-1]["kind"], "manual-post-context")

        rows = review_workspace.read_dialogues(
            review_workspace.review_proxy_ass_path(self.video)
        )
        source_starts = {}
        for row in rows:
            source, _index = review_workspace.edited_to_source(
                state["segments"],
                review_workspace.parse_ass_time(row["start"]),
            )
            source_starts[row["text"]] = source
        self.assertAlmostEqual(source_starts["第一段"], 100.0, places=2)
        self.assertAlmostEqual(source_starts["第二段"], 115.0, places=2)
        self.assertAlmostEqual(source_starts["前置上下文"], 92.0, places=2)
        self.assertAlmostEqual(source_starts["后置上下文"], 124.0, places=2)

    def test_streaming_review_can_restore_a_removed_middle_gap_with_subtitles(self):
        with patch.object(
            review_workspace, "_media_duration_with_ffmpeg", return_value=200.0
        ):
            metadata = review_workspace.prepare_streaming_review(
                self.video, Path("ffmpeg")
            )
        state = review_workspace.load_timeline_edit(
            self.video, float(metadata["source_duration"])
        )
        gap = review_workspace.restorable_timeline_gap(
            state["segments"], 10.0, tolerance_seconds=0.1
        )
        self.assertIsNotNone(gap)
        self.assertAlmostEqual(float(gap["duration"]), 5.0)

        restored = review_workspace.restore_streaming_gap(
            self.video, state, int(gap["left_index"])
        )

        self.assertEqual(restored["inserted_index"], 1)
        self.assertEqual(
            [segment["kind"] for segment in state["segments"]],
            ["original", "restored-middle", "original"],
        )
        rows = review_workspace.read_dialogues(
            review_workspace.review_proxy_ass_path(self.video)
        )
        by_text = {row["text"]: row for row in rows}
        self.assertIn("段间上下文", by_text)
        self.assertEqual(by_text["段间上下文"]["start"], "0:00:11.00")
        self.assertEqual(by_text["第二段"]["start"], "0:00:15.00")

    def test_legacy_auto_context_is_removed_but_manual_context_is_preserved(self):
        with patch.object(
            review_workspace, "_media_duration_with_ffmpeg", return_value=200.0
        ):
            metadata = review_workspace.prepare_streaming_review(
                self.video, Path("ffmpeg")
            )
        state = review_workspace.load_timeline_edit(
            self.video, float(metadata["source_duration"])
        )
        review_workspace.reveal_streaming_context(
            self.video, state, 100.0, chunk_seconds=8.0,
            direction="before",
        )
        state["segments"][0]["kind"] = "pre-context"

        removed = review_workspace.remove_automatic_streaming_context(
            self.video, state
        )

        self.assertEqual(removed, 1)
        self.assertEqual(
            [segment["kind"] for segment in state["segments"]],
            ["original", "original"],
        )
        proxy = review_workspace.review_proxy_ass_path(self.video).read_text(
            encoding="utf-8-sig"
        )
        self.assertNotIn("前置上下文", proxy)
    def test_sparse_full_session_context_cue_is_not_left_on_screen_for_minutes(self):

        start, end = review_workspace._bounded_context_span(
            0.0, 150.0, "怎么这么准时，因为今天提前了一点"
        )
        self.assertEqual(end, 150.0)
        self.assertGreater(start, 140.0)
        self.assertLessEqual(end - start, 5.0)

    def test_review_context_cue_never_stays_longer_than_five_seconds(self):
        start, end = review_workspace._bounded_context_span(10.0, 19.33, "这个家伙")
        self.assertEqual(end, 19.33)
        self.assertGreater(start, 14.33)
        self.assertLessEqual(end - start, 5.0)

    def test_legacy_padded_edit_migrates_to_full_source_coordinates(self):
        def fake_render(_source, destination, _segments, _ffmpeg):
            destination.write_bytes(b"legacy-proxy")

        with patch.object(
            review_workspace, "_media_duration_with_ffmpeg", return_value=200.0
        ), patch.object(
            review_workspace, "render_review_proxy", side_effect=fake_render
        ):
            review_workspace.generate_review_proxy(
                self.video, 10.0, Path("ffmpeg")
            )
            proxy_ass = review_workspace.review_proxy_ass_path(self.video)
            legacy_ass = proxy_ass.read_bytes()
            review_workspace.save_timeline_edit(
                self.video,
                {
                    "status": "pending",
                    "source_duration": 40.0,
                    "segments": [
                        {"source_start": 10.0, "source_end": 15.0},
                        {"source_start": 25.0, "source_end": 30.0},
                    ],
                },
            )
            metadata = review_workspace.prepare_streaming_review(
                self.video, Path("ffmpeg")
            )

        migrated = json.loads(
            review_workspace.timeline_edit_path(self.video).read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertTrue(metadata["streaming"])
        self.assertFalse(
            review_workspace.review_proxy_video_path(self.video).exists()
        )
        self.assertEqual(
            [
                (item["source_start"], item["source_end"])
                for item in migrated["segments"]
            ],
            [(100.0, 105.0), (115.0, 120.0)],
        )
        self.assertEqual(
            review_workspace.review_proxy_ass_path(self.video).read_bytes(),
            legacy_ass,
        )

if __name__ == "__main__":
    unittest.main()

