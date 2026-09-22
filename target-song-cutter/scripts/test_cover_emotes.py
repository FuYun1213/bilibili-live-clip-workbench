#!/usr/bin/env python3

from __future__ import annotations

import inspect
import unittest

from PIL import Image

import cover_emotes
import cover_layout


class CoverEmoteTests(unittest.TestCase):
    def test_cover_copy_font_starts_are_large_enough_for_review_cards(self) -> None:
        self.assertGreaterEqual(cover_layout.PRIMARY_FONT_START, 158)
        self.assertGreaterEqual(cover_layout.SECONDARY_FONT_START, 140)
        self.assertEqual(
            (cover_layout.SAFE_4_3_LEFT, cover_layout.SAFE_4_3_RIGHT),
            (240, 1680),
        )

    def test_catalog_contains_every_reviewed_wushen_emote(self) -> None:
        catalog = cover_emotes.load_catalog(creator="wushen")
        self.assertEqual(len(catalog), 48)
        self.assertEqual(
            {row["category"] for row in catalog},
            set(cover_emotes.EMOTION_CATEGORIES),
        )
        self.assertTrue(all(row["path"].is_file() for row in catalog))

    def test_phrase_split_preserves_three_to_five_piece_copy(self) -> None:
        self.assertEqual(
            cover_emotes.split_cover_phrases(
                "本音被嫌弃", "命令观众上舰｜一句好的｜当场被骗"
            ),
            ["本音被嫌弃", "命令观众上舰", "一句好的", "当场被骗"],
        )

    def test_auto_emotion_and_selection_are_deterministic(self) -> None:
        copy = "没想到一句好的就把主播骗到了"
        self.assertEqual(cover_emotes.infer_emotion(copy), "吐槽得意")
        first = cover_emotes.select_emote(copy, creator="wushen")
        second = cover_emotes.select_emote(copy, creator="wushen")
        self.assertIsNotNone(first)
        self.assertEqual(first["label"], second["label"])
        self.assertEqual(first["category"], "吐槽得意")

    def test_no_emote_override_disables_overlay(self) -> None:
        self.assertIsNone(
            cover_emotes.select_emote(
                "开心", creator="wushen", emotion=cover_emotes.NO_EMOTE
            )
        )

    def test_missing_creator_catalog_never_falls_back_to_wushen(self) -> None:
        self.assertEqual(cover_emotes.load_catalog(creator="kioi"), [])
        self.assertIsNone(cover_emotes.select_emote("笑死了", creator="kioi"))
        self.assertIsNone(cover_emotes.select_emote("笑死了"))

    def test_approved_emoji_is_extracted_for_color_rendering(self) -> None:
        self.assertEqual(
            cover_emotes.extract_cover_emoji("柚雨当场无语了😅"),
            ("柚雨当场无语了", "😅"),
        )

    def test_complete_sentence_layout_has_no_badge_rectangle(self) -> None:
        source = inspect.getsource(cover_layout.render_phrase_cluster)
        self.assertNotIn("rounded_rectangle", source)
        sentence = "羽啾先说自己没犯蠢，下一秒就把答案念反了"
        lines = cover_layout.wrap_cover_sentence(sentence)
        self.assertGreaterEqual(len(lines), 2)
        self.assertEqual("".join(lines), sentence)

    def test_each_cover_region_accepts_one_manual_break_and_22_chars(self) -> None:
        primary, secondary = cover_emotes.validate_cover_lines(
            "十二点路灯全关\n四周一个人都没有",
            "枝堇抱着凳子走\n求弹幕陪她说话😅",
        )
        self.assertEqual(primary.count("\n"), 1)
        self.assertEqual(secondary.count("\n"), 1)
        with self.assertRaisesRegex(ValueError, "最多手动换成 2 行"):
            cover_emotes.validate_cover_lines("一二三\n四五六\n七八九", "下区有内容")
        with self.assertRaisesRegex(ValueError, "3–22"):
            cover_emotes.validate_cover_lines("一" * 23, "下区有内容")

    def test_auto_wrap_keeps_each_region_to_at_most_two_lines(self) -> None:
        lines = cover_layout.wrap_cover_region("直播途中妹妹进来要吃的姐姐")
        self.assertEqual(len(lines), 2)
        self.assertEqual("".join(lines), "直播途中妹妹进来要吃的姐姐")
        self.assertEqual(
            cover_layout.wrap_cover_region("直播途中\n妹妹进来要吃的"),
            ["直播途中", "妹妹进来要吃的"],
        )

    def test_color_emoji_renderer_draws_visible_pixels(self) -> None:
        canvas = Image.new("RGBA", cover_layout.CANVAS, (0, 0, 0, 0))
        cover_layout.draw_cover_emoji(canvas, "🤣", center=(300, 300), size=140)
        self.assertIsNotNone(canvas.getchannel("A").getbbox())


if __name__ == "__main__":
    unittest.main()
