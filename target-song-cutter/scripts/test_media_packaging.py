"""Contract and real FFmpeg tests use tiny synthetic media only."""
from __future__ import annotations

import json
import csv
import importlib.util
import sys
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import burn_ass_subtitles as burn
import content_slicer
import media_packaging as media
import workflow_app_core as core
from windows_process import hidden_subprocess_kwargs


def load_standalone_renderer():
    name = "media_packaging_test_renderer"
    if name not in sys.modules:
        path = core.WORKSPACE_ROOT / "json-highlight-renderer" / "render_highlights.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class MediaContractTests(unittest.TestCase):
    def test_disabled_defaults_missing_global_and_explicit_project_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "media-packaging.json"
            self.assertEqual(media.load_media_packaging(path), {"enabled": False, "intro_path": "", "outro_path": ""})
            intro = root / "intro.mp4"
            intro.write_bytes(b"fixture")
            media.save_media_packaging(path, {"enabled": True, "intro_path": "intro.mp4"})
            with patch.object(core, "WORKSPACE_ROOT", root):
                self.assertEqual(core.effective_media_packaging({"config": {}}, root)["intro_path"], str(intro))
                self.assertFalse(core.effective_media_packaging({"config": {"media_packaging": {"enabled": False}}}, root)["enabled"])
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "不存在"):
                media.save_media_packaging(path, {"enabled": True, "intro_path": "missing.mp4"})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(media.normalize_media_packaging({"enabled": True})["intro_path"], "")

    def test_explicit_and_legacy_callback_round_trip_preserve_order(self):
        raw = {"标题": "枝堇谈完失败原因后给出了完整答案", "副标题": "冷开场说明结果｜最短前情说明原因",
               "时间戳": [[100, 105], [10, 35], [105, 110]]}
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                payload = dict(raw)
                if explicit:
                    payload["叙事结构"] = {"类型": "倒叙", "段落角色": ["冷开场", "前情", "回归"],
                                          "转场秒数": 0.5, "理由": "反应提出问题，前情解释原因"}
                items = core.normalize_selection([payload], "sumire", "narrative")
                self.assertEqual([r["start_seconds"] for r in items[0]["timestamps"]], [100, 10, 105])
                self.assertEqual(items[0]["narrative_structure"]["roles"], ["cold_open", "setup", "return"])
                self.assertEqual(core.normalize_selection(core.canonical_selection(items), "sumire", "narrative"), items)

    def test_cleanup_splits_roles_and_removed_cold_open_demotes_to_chronological(self):
        raw = {"标题": "枝堇解释前因后补上结果", "副标题": "测试冷开场｜最短前情说明",
               "时间戳": [[100, 106], [10, 30], [106, 120]],
               "叙事结构": {"类型": "倒叙", "段落角色": ["冷开场", "前情", "回归"], "理由": "同一事件"}}
        item = core.normalize_selection([raw], "sumire", "narrative")[0]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gift.csv"
            def write_exclusions(rows):
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=["start_seconds", "end_seconds", "text"])
                    writer.writeheader()
                    for start, end in rows:
                        writer.writerow({"start_seconds": start, "end_seconds": end, "text": "谢谢礼物"})
            write_exclusions([(15, 20)])
            refined, _ = core.trim_items_around_gift_acknowledgements([item], path)
            self.assertEqual(refined[0]["narrative_structure"]["roles"], ["cold_open", "setup", "setup", "return"])
            self.assertEqual(len(refined[0]["timestamps"]), 4)
            write_exclusions([(99, 106)])
            refined, _ = core.trim_items_around_gift_acknowledgements([item], path)
            self.assertEqual(refined[0]["narrative_structure"]["type"], "chronological")
            self.assertEqual(refined[0]["narrative_structure"]["roles"], ["body", "body"])
            self.assertIn("自动恢复顺叙", refined[0]["narrative_structure"]["reason"])
            transcript = Path(temporary) / "transcript.csv"
            with transcript.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["start_seconds", "end_seconds", "text"])
                writer.writeheader()
                for start, end in ((100,106), (10,14), (20,30), (106,120)):
                    writer.writerow({"start_seconds": start, "end_seconds": end, "text": "test speech"})
            refined, _ = core.refine_narrative_edit_ranges([item], transcript)
            self.assertEqual(refined[0]["narrative_structure"]["roles"], ["cold_open", "setup", "setup", "return"])
        with self.assertRaisesRegex(ValueError, "冷开场"):
            media.remap_narrative_structure(
                {"type": "callback", "roles": ["cold_open", "setup", "return"]},
                [{"start_seconds": 100, "end_seconds": 106}, {"start_seconds": 30, "end_seconds": 40}, {"start_seconds": 10, "end_seconds": 20}],
                [{"start_seconds": 30, "end_seconds": 40}, {"start_seconds": 10, "end_seconds": 20}],
            )

    def test_standalone_renderer_legacy_input_tail_padding_and_cache_invalidation(self):
        renderer = load_standalone_renderer()
        item = renderer.normalize_renderer_items([{"segment": "recording", "timestamp": "00:00:30-00:01:05",
                                                  "title": "legacy title", "cover_text_1": "legacy cover"}])[0]
        self.assertEqual(item["timestamps"], [{"start_seconds": 30, "end_seconds": 65}])
        self.assertEqual(renderer.callback_safe_tail_padding([(100,110), (70,98)], 5, 200), 2)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "result.mp4"
            destination.write_bytes(b"existing video")
            request = {"version": 1, "type": "chronological"}
            cache = destination.with_suffix(".render-request.json")
            cache.write_text(json.dumps(request), encoding="utf-8")
            self.assertEqual(renderer.require_matching_render_cache(destination, request), cache)
            with self.assertRaisesRegex(ValueError, "overwrite"):
                renderer.require_matching_render_cache(destination, {"version": 1, "type": "callback"})
            self.assertEqual(destination.read_bytes(), b"existing video")
            self.assertEqual(renderer.require_matching_render_cache(destination, {"changed": True}, True), cache)

    def test_standalone_srt_converts_actual_ass_line_break_marker(self):
        renderer = load_standalone_renderer()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "subtitles.srt"
            renderer.write_srt(output, [(100, 2500, "Default", r"第一行\N第二行")])
            self.assertEqual(output.read_text(encoding="utf-8-sig"),
                             "1\n00:00:00,100 --> 00:00:02,500\n第一行\n第二行\n\n")

    def test_callback_rejects_overlap_and_song_reordering(self):
        with self.assertRaisesRegex(ValueError, "重叠"):
            media.normalize_narrative_structure(None, [
                {"start_seconds": 50, "end_seconds": 60}, {"start_seconds": 0, "end_seconds": 55}])
        with self.assertRaisesRegex(ValueError, "歌切"):
            media.normalize_narrative_structure(None, [
                {"start_seconds": 50, "end_seconds": 60}, {"start_seconds": 0, "end_seconds": 20}], "song")
        with self.assertRaisesRegex(ValueError, "数量"):
            media.normalize_narrative_structure({"段落角色": []}, [{"start_seconds": 0, "end_seconds": 30}])

    def test_only_callback_and_return_jumps_get_half_second_transition(self):
        transitions = media.narrative_transitions([(100, 105), (10, 20), (25, 35), (105, 110)])
        self.assertEqual([item["at_seconds"] for item in transitions], [5, 25])
        self.assertTrue(all(item["duration_seconds"] == 0.5 for item in transitions))
        self.assertEqual(media.narrative_transitions([(10, 20), (25, 35)]), [])
        self.assertEqual(media.narrative_transitions([(100, 105), (10, 35)], content_type="song"), [])

    def test_default_burn_keeps_audio_copy_and_no_extra_inputs(self):
        command, metadata = media.build_burn_command("ffmpeg", Path("clip.mp4"), "null", Path("out.mp4"))
        self.assertEqual(command.count("-i"), 1)
        self.assertNotIn("-filter_complex", command)
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertEqual(metadata["intro_seconds"], 0)

    def test_transition_boundaries_follow_applied_review_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "clip.mp4"
            metadata = {"regions": [
                {"source_start": 100, "source_end": 103, "exact_start": 0},
                {"source_start": 20, "source_end": 24, "exact_start": 3},
                {"source_start": 90, "source_end": 93, "exact_start": 7},
            ]}
            (root / "narrative-structures.json").write_text(json.dumps({"001": {
                "type": "callback", "roles": ["cold_open", "setup", "return"], "transition_seconds": .5,
                "source_ranges": [{"start_seconds": 100, "end_seconds": 103},
                                  {"start_seconds": 20, "end_seconds": 24},
                                  {"start_seconds": 90, "end_seconds": 93}],
            }}), encoding="utf-8")
            with patch.object(core.review_workspace, "review_proxy_metadata", return_value=metadata):
                result = core.delivery_narrative_transitions(root, root, [{"video": video.name, "clip_id": "001"}])
            self.assertEqual([item["at_seconds"] for item in result[video.name]], [3, 7])


class SyntheticMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = core.WORKSPACE_ROOT / "tools" / "ffmpeg" / "ffmpeg.exe"
        if not cls.ffmpeg.is_file():
            raise unittest.SkipTest("Bundled FFmpeg not available")
        cls.temporary = tempfile.TemporaryDirectory(prefix="synthetic-media-packaging-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / "source.mp4"
        cls.run_ffmpeg([str(cls.ffmpeg), "-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=20:duration=6",
                        "-f", "lavfi", "-i", "aevalsrc=0.15*sin(2*PI*(220*t+30*t*t)):s=48000:d=6",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.source)])
        cls.parts = [content_slicer.EditRange("001", i+1, start, end, "fixture", "", "", "")
                     for i, (start, end) in enumerate(((4, 5), (0, 1), (5, 6)))]
        cls.clip = cls.root / "clip.mp4"
        cls.run_ffmpeg(content_slicer.build_slice_command(cls.ffmpeg, cls.source, cls.source, cls.parts, cls.clip, "ultrafast", 18))
        cls.ass = cls.root / "clip.ass"
        cls.ass.write_text("""[Script Info]
ScriptType: v4.00+
PlayResX: 160
PlayResY: 90
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,14,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,2,2,4,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.20,0:00:00.80,Default,,0,0,0,,COLD
Dialogue: 0,0:00:01.20,0:00:01.80,Default,,0,0,0,,SETUP
Dialogue: 0,0:00:02.20,0:00:02.80,Default,,0,0,0,,RETURN
""", encoding="utf-8")
        cls.intro, cls.outro = cls.root / "intro.mp4", cls.root / "outro.mp4"
        cls.run_ffmpeg([str(cls.ffmpeg), "-y", "-f", "lavfi", "-i", "color=c=red:size=100x100:rate=25:duration=1",
                        "-c:v", "libx264", "-preset", "ultrafast", str(cls.intro)])
        cls.run_ffmpeg([str(cls.ffmpeg), "-y", "-f", "lavfi", "-i", "color=c=blue:size=200x80:rate=20:duration=0.7",
                        "-f", "lavfi", "-i", "sine=frequency=880:duration=0.7", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(cls.outro)])
        cls.transitions = media.narrative_transitions([(4, 5), (0, 1), (5, 6)])
        cls.plain, cls.blurred, cls.packaged = (cls.root / name for name in ("plain.mp4", "blurred.mp4", "packaged.mp4"))
        subtitle_filter = f"ass='{burn.filter_path(cls.ass)}'"
        for target, transitions, packaging in (
            (cls.plain, [], None), (cls.blurred, cls.transitions, None),
            (cls.packaged, cls.transitions, {"enabled": True, "intro_path": str(cls.intro), "outro_path": str(cls.outro)}),
        ):
            command, metadata = media.build_burn_command(cls.ffmpeg, cls.clip, subtitle_filter, target, packaging, transitions)
            cls.run_ffmpeg(command)
            if target == cls.packaged:
                cls.packaging_report = metadata
        cls.shifted = cls.root / "packaged.ass"
        media.shift_ass_file(cls.ass, cls.shifted, cls.packaging_report["intro_seconds"])

    @classmethod
    def run_ffmpeg(cls, command):
        command = [command[0], "-hide_banner", "-loglevel", "error", *command[1:]]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **hidden_subprocess_kwargs())
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace")[-5000:])
        return result.stdout

    def frame(self, path, seconds):
        raw = self.run_ffmpeg([str(self.ffmpeg), "-ss", str(seconds), "-i", str(path), "-frames:v", "1",
                               "-f", "rawvideo", "-pix_fmt", "gray", "-"])
        return np.frombuffer(raw, dtype=np.uint8).astype(float).reshape((90, 160))

    @staticmethod
    def edge_energy(frame):
        return float(np.mean(np.diff(frame, axis=0) ** 2) + np.mean(np.diff(frame, axis=1) ** 2))

    def test_ordered_export_preserves_duration_audio_order_and_subtitle_mapping(self):
        self.assertAlmostEqual(media.probe_media(self.ffmpeg, self.clip)["duration"], 3, delta=0.055)
        raw = self.run_ffmpeg([str(self.ffmpeg), "-i", str(self.clip), "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"])
        audio = np.frombuffer(raw, dtype=np.float32)
        frequencies = []
        for center in (0.5, 1.5, 2.5):
            sample = audio[int((center-.15)*48000):int((center+.15)*48000)]
            spectrum = abs(np.fft.rfft(sample * np.hanning(len(sample))))
            frequencies.append(np.argmax(spectrum) * 48000 / len(sample))
        for actual, expected in zip(frequencies, (490, 250, 550)):
            self.assertAlmostEqual(actual, expected, delta=12)
        mapped = content_slicer._map_segments_to_parts([
            {"start_seconds": t+.2, "end_seconds": t+.8, "text": text}
            for t, text in ((0, "SETUP"), (4, "COLD"), (5, "RETURN"))], self.parts)
        self.assertEqual([row["text"] for row in mapped], ["COLD", "SETUP", "RETURN"])
        self.assertEqual([row["start_seconds"] for row in mapped], [.2, 1.2, 2.2])

    def test_gaussian_transition_is_half_second_and_does_not_change_audio_or_duration(self):
        self.assertAlmostEqual(media.probe_media(self.ffmpeg, self.blurred)["duration"], 3, delta=0.055)
        for center in (1, 2):
            clear_energy = self.edge_energy(self.frame(self.plain, center))
            blurred_energy = self.edge_energy(self.frame(self.blurred, center))
            self.assertLess(blurred_energy, clear_energy * .35)
            for outside in (center-.3, center+.3):
                energy = self.edge_energy(self.frame(self.plain, outside))
                self.assertAlmostEqual(self.edge_energy(self.frame(self.blurred, outside)), energy, delta=energy*.15)
        def audio(path):
            return self.run_ffmpeg([str(self.ffmpeg), "-i", str(path), "-vn", "-c:a", "copy", "-f", "adts", "-"])
        self.assertEqual(audio(self.plain), audio(self.blurred))

    def test_standalone_json_renderer_cli_uses_business_contract_and_real_transitions(self):
        source = self.root / "standalone-source.mp4"
        self.run_ffmpeg([str(self.ffmpeg), "-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=36",
                         "-f", "lavfi", "-i", "sine=frequency=440:duration=36", "-c:v", "libx264", "-preset", "ultrafast",
                         "-c:a", "aac", str(source)])
        selection = self.root / "standalone.json"
        selection.write_text(json.dumps([{
            "标题": "【枝堇Sumire】枝堇先解释失败再补上完整结果", "副标题": "测试冷开场｜测试前情说明",
            "时间戳": [[30, 33], [0, 25], [33, 36]],
            "叙事结构": {"类型": "倒叙", "段落角色": ["冷开场", "前情", "回归"], "转场秒数": .5, "理由": "测试同一事件的前因后果"},
        }], ensure_ascii=False), encoding="utf-8")
        transcript = self.root / "standalone-transcript.csv"
        with transcript.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["start_seconds", "end_seconds", "text"])
            writer.writeheader()
            for start, text in ((1.2, "SETUP"), (30.2, "COLD"), (34.2, "RETURN")):
                writer.writerow({"start_seconds": start, "end_seconds": start+.6, "text": text})
        output = self.root / "standalone-output"
        disabled = self.root / "disabled-packaging.json"
        media.save_media_packaging(disabled, {"enabled": False})
        command = [sys.executable, "-X", "utf8", str(core.WORKSPACE_ROOT / "json-highlight-renderer" / "render_highlights.py"),
                   "--highlights", str(selection), "--transcript", str(transcript), "--source", str(source),
                   "--output", str(output), "--ffmpeg", str(self.ffmpeg), "--tail-padding", "0",
                   "--media-packaging", str(disabled)]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **hidden_subprocess_kwargs())
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace")[-5000:])
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))[0]
        report = json.loads(Path(manifest["clip"]).with_suffix(".packaging.json").read_text(encoding="utf-8"))
        self.assertEqual([item["at_seconds"] for item in report["transitions"]], [3, 28])
        self.assertAlmostEqual(media.probe_media(self.ffmpeg, Path(manifest["clip"]))["duration"], 31, delta=.11)
        subtitle_text = Path(manifest["srt"]).read_text(encoding="utf-8-sig")
        self.assertLess(subtitle_text.index("COLD"), subtitle_text.index("SETUP"))
        self.assertLess(subtitle_text.index("SETUP"), subtitle_text.index("RETURN"))
        self.assertIn("00:00:00,300", subtitle_text)
        self.assertIn("00:00:04,300", subtitle_text)
        self.assertIn("00:00:29,300", subtitle_text)

    def test_disabled_reburn_removes_previous_packaging_sidecar(self):
        output = self.root / "reburn"
        output.mkdir()
        destination = output / "clip.mp4"
        shutil.copy2(self.packaged, destination)
        report = destination.with_suffix(".packaging.json")
        report.write_text(json.dumps(self.packaging_report), encoding="utf-8")
        disabled = self.root / "reburn-disabled.json"
        media.save_media_packaging(disabled, {"enabled": False})
        command = [sys.executable, "-X", "utf8", str(core.SCRIPTS / "burn_ass_subtitles.py"),
                   "--clips", str(self.root), "--ass", str(self.root), "--output", str(output),
                   "--ffmpeg", str(self.ffmpeg), "--prefix", "clip", "--media-packaging", str(disabled)]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **hidden_subprocess_kwargs())
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace")[-5000:])
        self.assertFalse(report.exists())
        self.assertAlmostEqual(media.probe_media(self.ffmpeg, destination)["duration"], 3, delta=.055)

    def test_bumpers_add_measured_duration_and_shift_only_delivery_subtitles(self):
        self.assertAlmostEqual(self.packaging_report["intro_seconds"], 1, places=4)
        self.assertAlmostEqual(self.packaging_report["outro_seconds"], .7, places=4)
        self.assertAlmostEqual(media.probe_media(self.ffmpeg, self.packaged)["duration"], 4.7, delta=.01)
        self.assertIn("0:00:01.20,0:00:01.80", self.shifted.read_text(encoding="utf-8-sig"))
        self.assertIn("0:00:00.20,0:00:00.80", self.ass.read_text(encoding="utf-8-sig"))
        raw = self.run_ffmpeg([str(self.ffmpeg), "-i", str(self.packaged), "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"])
        audio = np.frombuffer(raw, dtype=np.float32)
        self.assertLess(float(np.sqrt(np.mean(audio[12000:36000] ** 2))), .0001)
        self.assertGreater(float(np.sqrt(np.mean(audio[60000:84000] ** 2))), .05)


if __name__ == "__main__":
    unittest.main()
