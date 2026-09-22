from __future__ import annotations

import json
import re
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import cover_layout
import local_publish


class CoverLayoutTests(unittest.TestCase):
    def render(self, primary, secondary, *, source_region="auto", has_emote=False):
        canvas = Image.new("RGBA", cover_layout.CANVAS)
        draw = Mock()
        with patch.object(cover_layout, "draw_cover_emoji"):
            result = cover_layout.render_cover_regions(
                canvas, primary, secondary, {},
                source_region=source_region, has_emote=has_emote,
                fit_font=local_publish.fit_autoclip_font, draw_text=draw,
            )
        self.assertIsNotNone(result)
        sizes = {call.args[3].size for call in draw.call_args_list}
        self.assertEqual(len(sizes), 1, "Every rendered line must use the same font size")
        regions = [region for region in result["regions"].values() if region]
        self.assertLessEqual(sum(len(region["lines"]) for region in regions), 4)
        left, right = result["safe_bounds"]
        for region in regions:
            self.assertLessEqual(len(region["lines"]), 2)
            boxes = region["boxes"]
            for box in boxes + ([region["emoji_box"]] if region["emoji_box"] else []):
                self.assertGreaterEqual(box[0], left)
                self.assertLessEqual(box[2], right)
                self.assertGreaterEqual(box[1], 0)
                self.assertLessEqual(box[3], cover_layout.CANVAS[1])
            for first, second in zip(boxes, boxes[1:]):
                self.assertLess(first[3], second[1], "Rows must not overlap")
        json.dumps(result)  # Exportable diagnostics must not be self-referential.
        return result

    def readable(self, value):
        return re.sub(r"[\s，。！？、；：,.!?;:｜|😅]", "", value)

    def test_unequal_short_phrases_share_one_font(self):
        result = self.render("认真猜名言", "答案全部说反了")
        self.assertEqual(result["regions"]["primary"]["font_size"], 158)
        self.assertEqual(result["regions"]["secondary"]["font_size"], 158)

    def test_three_long_phrases_never_make_four_lower_rows(self):
        primary = "观众说文章自己看着还行"
        secondary = "听枝堇念出来却觉得尴尬｜枝堇这才懂自己小作文有多好笑"
        result = self.render(primary, secondary)
        upper, lower = result["regions"]["primary"], result["regions"]["secondary"]
        self.assertEqual(upper["lines"], [primary])
        self.assertEqual(lower["lines"], ["听枝堇念出来却觉得尴尬，", "枝堇这才懂自己小作文有多好笑"])
        self.assertEqual(self.readable("".join(lower["lines"])), self.readable(secondary))

    def test_lopsided_lower_phrases_reflow_without_losing_copy(self):
        secondary = "先说好｜后来才发现原来整段内容都没有认真看过"
        result = self.render("到底有没有认真看", secondary)
        lines = result["regions"]["secondary"]["lines"]
        self.assertEqual(self.readable("".join(lines)), self.readable(secondary))
        font = local_publish.load_font(result["font_size"])
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        widths = [probe.textlength(line, font=font) for line in lines]
        self.assertLessEqual(max(widths) - min(widths), font.size * 2.5)

    def test_legacy_manual_lower_wraps_are_compacted_without_losing_copy(self):
        secondary = "听枝堇念出来\n却觉得尴尬｜枝堇这才懂自己\n小作文有多好笑"
        result = self.render("观众说文章\n自己看着还行", secondary)
        self.assertEqual(result["regions"]["primary"]["lines"], ["观众说文章", "自己看着还行"])
        self.assertEqual(self.readable("".join(result["lines"])), self.readable(secondary))

    def test_long_copy_stays_safe_with_emoji_radio_and_sticker(self):
        primary = "十二点路灯全关四周一个人都没有"
        secondary = "枝堇抱着凳子走回家｜路上还得求弹幕一直陪她说话😅"
        for region in ("auto", "radio-left"):
            for sticker in (False, True):
                with self.subTest(region=region, sticker=sticker):
                    result = self.render(primary, secondary, source_region=region, has_emote=sticker)
                    self.assertEqual(result["emoji"], "😅")
                    self.assertIsNotNone(result["emoji_box"])
                    self.assertEqual(self.readable("".join(result["lines"])), self.readable(secondary))

    def test_single_phrase_manual_breaks_remain_explicit(self):
        result = self.render("直播途中\r\n妹妹进门", "突然发现\n没带零食")
        self.assertEqual(result["regions"]["primary"]["lines"], ["直播途中", "妹妹进门"])
        self.assertEqual(result["regions"]["secondary"]["lines"], ["突然发现", "没带零食"])

    def test_measured_breaks_balance_mixed_width_text(self):
        sentence = "WWWW观众说这次真的很好iiiiiiii"
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        font = local_publish.load_font(100)
        lines = cover_layout.wrap_cover_region(sentence, measure=lambda value: probe.textlength(value, font=font))
        self.assertEqual("".join(lines), sentence)
        self.assertTrue(any("WWWW" in line for line in lines))
        self.assertTrue(any("iiiiiiii" in line for line in lines))
        widths = [probe.textlength(line, font=font) for line in lines]
        self.assertLessEqual(max(widths) - min(widths), font.size * 2)

    def test_extra_manual_lines_fail_instead_of_silently_dropping_text(self):
        with self.assertRaisesRegex(ValueError, "不能截断"):
            cover_layout.wrap_cover_region("第一句\n第二句\n第三句")

    def test_more_than_three_phrases_fail_before_any_text_is_drawn(self):
        draw = Mock()
        with self.assertRaises(ValueError):
            cover_layout.render_cover_regions(
                Image.new("RGBA", cover_layout.CANVAS), "第一句", "第二句｜第三句｜第四句", {},
                source_region="auto", has_emote=False,
                fit_font=local_publish.fit_autoclip_font, draw_text=draw,
            )
        draw.assert_not_called()


if __name__ == "__main__":
    unittest.main()

