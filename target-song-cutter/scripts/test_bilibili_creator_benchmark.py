import hashlib
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bilibili_creator_benchmark as benchmark


class BilibiliCreatorBenchmarkTests(unittest.TestCase):
    def test_signed_query_sorts_and_sanitizes_values(self) -> None:
        mixin = "0" * 32
        base = "a=x+y&wts=1&z=ab"
        expected = hashlib.md5((base + mixin).encode("utf-8")).hexdigest()
        self.assertEqual(
            benchmark.signed_query({"z": "a!b", "a": "x y", "wts": 1}, mixin),
            f"{base}&w_rid={expected}",
        )

    def test_clean_text_removes_highlight_markup(self) -> None:
        self.assertEqual(
            benchmark.clean_text("【<em class=\"keyword\">羽啾</em>】&quot;测试&quot;"),
            "【羽啾】\"测试\"",
        )

    def test_normalize_row_rejects_description_only_match(self) -> None:
        row = {
            "bvid": "BV1test",
            "title": "无关标题",
            "description": "简介提到枝堇",
            "play": 100,
            "pubdate": 1_700_000_000,
        }
        result = benchmark.normalize_row(
            "sumire",
            benchmark.CREATORS["sumire"],
            row,
            datetime(2026, 8, 17, tzinfo=timezone.utc),
        )
        self.assertIsNone(result)

    def test_content_type_hint_separates_replay_and_song(self) -> None:
        self.assertEqual(benchmark.content_type_hint("直播回放：杂谈"), "replay")
        self.assertEqual(benchmark.content_type_hint("【歌切】翻唱《遇见》"), "song")
        self.assertEqual(benchmark.content_type_hint("被弹幕钓到当场破防"), "narrative")


if __name__ == "__main__":
    unittest.main()