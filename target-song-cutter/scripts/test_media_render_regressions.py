"""Regression coverage for real FLV rates and wide inputs to the radio layout."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import media_packaging as media
import render_vertical_radio_layout as radio


class MediaFrameRateTests(unittest.TestCase):
    def probe(self, average, nominal):
        stream = {"codec_type": "video", "width": 1200, "height": 960,
                  "duration": "1.0", "avg_frame_rate": average, "r_frame_rate": nominal}
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps({"streams": [stream], "format": {"duration": "1"}}))
        with patch.object(media.subprocess, "run", return_value=result):
            return media.probe_media(Path("ffmpeg.exe"), Path("fixture.flv"))

    def test_real_flv_clock_does_not_become_1000_fps(self):
        self.assertEqual(self.probe("30/1", "1000/1")["fps"], 30)
        self.assertAlmostEqual(self.probe("30000/1001", "60/1")["fps"], 30000 / 1001)

    def test_missing_or_malformed_average_uses_reasonable_nominal(self):
        for average in (None, "", "N/A", "0/0", "0/1", "NaN", "inf", "1000/1", "30/", "1/0"):
            with self.subTest(average=average):
                self.assertEqual(self.probe(average, "60/1")["fps"], 60)
        self.assertEqual(self.probe(None, "24")["fps"], 24)

    def test_no_reasonable_rate_uses_safe_default(self):
        for average, nominal in ((None, "1000/1"), ("NaN", "0/0"), (None, "-30/1"), (None, "garbage")):
            with self.subTest(average=average, nominal=nominal):
                self.assertEqual(self.probe(average, nominal)["fps"], 30)
        self.assertEqual(self.probe("240/1", "1000/1")["fps"], 240)


class RadioSubtitleColorTests(unittest.TestCase):
    def test_hash_prefixed_default_and_mixed_case_are_normalized(self):
        self.assertEqual(radio.rgb_to_ass_bgr("#111318"), "&H00181311")
        self.assertEqual(radio.rgb_to_ass_bgr(" aAbBcC "), "&H00CCBBAA")
        self.assertEqual(radio.rgb_to_ass_bgr("FFFFFF"), "&H00FFFFFF")

    def test_invalid_color_is_rejected_before_writing_ass(self):
        for value in ("123", "#ZZ1111", "12345678"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                radio.rgb_to_ass_bgr(value)

    def test_converted_ass_has_valid_default_outline_and_preserves_dialogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = Path(tmp) / "input.ass", Path(tmp) / "output.ass"
            source.write_text("Dialogue: 0,0:00:00.00,0:00:01.00,Old,Original,0,0,0,,原句没有改变。\n", encoding="utf-8")
            radio.convert_ass(source, destination, rgb="#123456", subtitle_rgb="#abcdef", subtitle_outline_rgb="#111318",
                              font="Microsoft YaHei", font_size=65, style_name="Radio", speaker="主播", line_chars=14)
            output = destination.read_text(encoding="utf-8-sig")
            style = next(line for line in output.splitlines() if line.startswith("Style:"))
            self.assertIn("&H00EFCDAB,&H00EFCDAB,&H00181311", style)
            self.assertNotIn("#", style)
            self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.00,Radio,主播,0,0,0,,原句没有改变。", output)


class RadioContainRenderTests(unittest.TestCase):
    def test_real_landscape_and_portrait_sources_fit_centered_left_column(self):
        ffmpeg = Path(__file__).resolve().parents[2] / "tools" / "ffmpeg" / "ffmpeg.exe"
        if not ffmpeg.is_file():
            self.skipTest("Workspace FFmpeg unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for width, height, expected_width, expected_height in ((1200, 960, 624, 498), (540, 960, 608, 1080)):
                with self.subTest(source=(width, height)):
                    source = root / f"{width}.mp4"
                    destination = root / f"{width}-radio.mp4"
                    subprocess.run([str(ffmpeg), "-v", "error", "-f", "lavfi", "-i", f"color=c=blue:s={width}x{height}:r=30:d=0.04",
                                    "-frames:v", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(source)], check=True, capture_output=True)
                    radio.render_video(ffmpeg, source, destination, "#123456")
                    decoded = subprocess.run([str(ffmpeg), "-v", "error", "-i", str(destination), "-frames:v", "1",
                                              "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"], check=True, capture_output=True).stdout
                    frame = np.frombuffer(decoded, dtype=np.uint8).reshape(1080, 1920, 3)
                    clear_blue = (frame[:, :, 0] < 40) & (frame[:, :, 1] < 40) & (frame[:, :, 2] > 200)
                    ys, xs = np.where(clear_blue)
                    self.assertGreater(len(xs), 100000)
                    self.assertAlmostEqual(int(xs.max() - xs.min() + 1), expected_width, delta=3)
                    self.assertAlmostEqual(int(ys.max() - ys.min() + 1), expected_height, delta=3)
                    self.assertAlmostEqual((int(xs.min()) + int(xs.max())) / 2, 328, delta=2)
                    self.assertAlmostEqual((int(ys.min()) + int(ys.max())) / 2, 540, delta=2)
                    self.assertFalse(clear_blue[:, 666:].any(), "Unblurred source must not cover right subtitle panel")


if __name__ == "__main__":
    unittest.main()
