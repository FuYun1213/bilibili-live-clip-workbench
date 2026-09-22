import unittest

from bilibili_gifts import detect_gift_ack, mark_gift_blocks


GIFTS = {"爱心抱枕", "棉花糖", "粉丝团灯牌", "打call"}


class BilibiliGiftTests(unittest.TestCase):
    def test_detects_explicit_gift_thanks(self):
        match = detect_gift_ack("谢谢清晰送的两个爱心抱枕", GIFTS)
        self.assertTrue(match.explicit)
        self.assertIn("爱心抱枕", match.gifts)

    def test_preserves_normal_thanks(self):
        self.assertFalse(detect_gift_ack("谢谢你愿意陪我聊这些", GIFTS).explicit)

    def test_removes_standalone_gift_thanks(self):
        self.assertTrue(detect_gift_ack("谢谢清晰宝宝", GIFTS).explicit)

    def test_paid_message_requires_editorial_review(self):
        match = detect_gift_ack("谢谢清晰的SC，刚才那段为什么会唱错？", GIFTS)
        self.assertFalse(match.explicit)
        self.assertTrue(match.paid_message)
        self.assertIn("paid-message-review", match.reason)

    def test_gangbeng_requires_editorial_review(self):
        match = detect_gift_ack("谢谢清晰的钢镚，能再唱一次副歌吗？", GIFTS)
        self.assertFalse(match.explicit)
        self.assertTrue(match.paid_message)

    def test_extends_gift_block_to_adjacent_short_thanks(self):
        rows = [
            {"start_seconds": "10", "end_seconds": "12", "text": "谢谢甲送的棉花糖"},
            {"start_seconds": "13", "end_seconds": "14", "text": "谢谢宝宝"},
            {"start_seconds": "30", "end_seconds": "33", "text": "谢谢你愿意听我说"},
        ]
        kept, removed = mark_gift_blocks(rows, GIFTS)
        self.assertEqual([row["text"] for row in removed], ["谢谢甲送的棉花糖", "谢谢宝宝"])
        self.assertEqual([row["text"] for row in kept], ["谢谢你愿意听我说"])


if __name__ == "__main__":
    unittest.main()
