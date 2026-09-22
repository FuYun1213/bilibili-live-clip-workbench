#!/usr/bin/env python3

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from make_word_fade_ass import Lyric, write_ass


class WordFadeProfileTests(unittest.TestCase):
    def test_style_and_speaker_are_profile_driven(self):
        args = SimpleNamespace(
            primary="#8EB056",
            outline="#272033",
            shadow="#000000",
            font="AR WeiBeiGBStd BD",
            font_size=46,
            spacing=0,
            outline_width=3.0,
            shadow_depth=1.5,
            margin_lr=140,
            margin_v=78,
            width=1920,
            height=1080,
            style_name="ViridisLyric",
            speaker_name="小松绿Viridis",
            japanese_font="Yuji Syuku",
            fade_ms=220,
            delay=0.1,
            hang=0.0,
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "song.ass"
            write_ass(
                output,
                "song",
                [Lyric("song", 1.0, 2.0, "测试歌词")],
                args,
            )
            text = output.read_text(encoding="utf-8-sig")
        self.assertIn("Style: ViridisLyric,AR WeiBeiGBStd BD", text)
        self.assertIn("ViridisLyric,小松绿Viridis", text)


if __name__ == "__main__":
    unittest.main()
