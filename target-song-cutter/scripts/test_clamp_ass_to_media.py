from __future__ import annotations

import unittest

import clamp_ass_to_media as clamp


class ClampAssToMediaTests(unittest.TestCase):
    def test_rounding_cannot_leave_zero_length_tail_cue(self):
        lines = [
            "[Events]",
            "Dialogue: 0,0:00:56.97,0:00:58.15,Main,主播,0,0,0,,保留",
            "Dialogue: 0,0:00:58.17,0:00:58.37,Main,主播,0,0,0,,删除",
        ]

        output, changed = clamp.clamp_ass_lines(
            lines, duration=58.421354, tail=0.25
        )

        self.assertEqual(changed, 1)
        self.assertIn(lines[1], output)
        self.assertNotIn(lines[2], output)

    def test_long_tail_cue_is_clamped_to_last_valid_centisecond(self):
        line = "Dialogue: 0,0:00:57.00,0:00:59.00,Main,主播,0,0,0,,截断"

        output, changed = clamp.clamp_ass_lines(
            [line], duration=58.421354, tail=0.25
        )

        self.assertEqual(changed, 1)
        self.assertIn(",0:00:57.00,0:00:58.17,", output[0])


if __name__ == "__main__":
    unittest.main()
