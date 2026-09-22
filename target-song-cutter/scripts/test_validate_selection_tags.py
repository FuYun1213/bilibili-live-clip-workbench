import unittest

from validate_selection_tags import merge_tags, validate_tag_row


class SelectionTagTests(unittest.TestCase):
    BASE = ["昼夜_Chilly", "虚拟主播", "虚拟Singer", "直播切片", "VUP"]

    def test_grounded_dynamic_tags_reach_eight_total(self):
        row = {
            "tags": "县城爱情,学历差距,初恋故事",
            "tag_evidence": "00:42谈县城；01:10谈学历；02:00谈初恋",
        }
        self.assertEqual(validate_tag_row(row, "chilly", self.BASE), [])
        self.assertEqual(len(merge_tags(self.BASE, row["tags"].split(","))), 8)

    def test_missing_selection_tags_is_blocked(self):
        errors = validate_tag_row({"tag_evidence": "有证据"}, "chilly", self.BASE)
        self.assertTrue(any("at least 1" in error for error in errors))

    def test_manual_two_tags_are_upload_safe_but_not_model_quality_output(self):
        row = {
            "tags": "刘备文学,皇叔.得意",
            "tag_evidence": "20:24-21:40围绕刘备文学和相关称呼展开",
        }
        self.assertEqual(validate_tag_row(row, "sumire", ["VirtuaReal", "枝堇Sumire"]), [])
        errors = validate_tag_row(
            row, "sumire", ["VirtuaReal", "枝堇Sumire"], strict_quality=True
        )
        self.assertTrue(any("exactly 5" in error for error in errors))

    def test_chilly_vr_tag_requires_explicit_topic_confirmation(self):
        row = {
            "tags": "VR游戏,直播趣事,设备体验",
            "tag_evidence": "实际讨论VR",
        }
        self.assertTrue(any("vr_topic=yes" in error for error in validate_tag_row(row, "chilly", self.BASE)))
        row["vr_topic"] = "yes"
        self.assertEqual(validate_tag_row(row, "chilly", self.BASE), [])


if __name__ == "__main__":
    unittest.main()

