import csv
import tempfile
import unittest
from pathlib import Path

from annotate_chat_reads import annotate_rows, similarity
from narrative_edit import refine_narrative_edit_ranges
from virtuareal_glossary import compact_paid_thanks


class NarrativeCleanupTests(unittest.TestCase):
    def test_tightens_edges_and_removes_long_inter_sentence_silence(self):
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "transcript.csv"
            with transcript.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=("start_seconds", "end_seconds", "text")
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {"start_seconds": 5.0, "end_seconds": 7.0, "text": "第一句"},
                        {"start_seconds": 10.5, "end_seconds": 12.0, "text": "第二句"},
                    ]
                )
            items = [{
                "clip_id": "001",
                "content_type": "narrative",
                "timestamps": [{"start_seconds": 0.0, "end_seconds": 18.0}],
            }]
            refined, audit = refine_narrative_edit_ranges(items, transcript)
            self.assertEqual(
                refined[0]["timestamps"],
                [
                    {"start_seconds": 4.88, "end_seconds": 7.18},
                    {"start_seconds": 10.32, "end_seconds": 12.28},
                ],
            )
            self.assertEqual(items[0]["timestamps"], [{"start_seconds": 0.0, "end_seconds": 18.0}])
            self.assertGreater(audit[0]["long_silence_removed_seconds"], 3.0)

    def test_short_natural_pause_is_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "transcript.csv"
            with transcript.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=("start_seconds", "end_seconds", "text")
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {"start_seconds": 1.0, "end_seconds": 2.0, "text": "前句"},
                        {"start_seconds": 3.5, "end_seconds": 5.0, "text": "后句"},
                    ]
                )
            refined, _audit = refine_narrative_edit_ranges(
                [{
                    "clip_id": "001",
                    "content_type": "narrative",
                    "timestamps": [{"start_seconds": 0.0, "end_seconds": 6.0}],
                }],
                transcript,
            )
            self.assertEqual(len(refined[0]["timestamps"]), 1)


class ReadMessageAnnotationTests(unittest.TestCase):
    def test_nearby_sc_has_priority_and_marks_the_transcript(self):
        rows = [{
            "start_seconds": "100.000",
            "end_seconds": "103.000",
            "text": "今天晚上吃什么火锅",
        }]
        danmaku = [{"time": 91.0, "text": "今天晚上吃什么火锅"}]
        superchats = [{"time": 102.0, "text": "今天晚上吃什么火锅"}]
        annotated, audit = annotate_rows(rows, danmaku, superchats)
        self.assertEqual(annotated[0]["text"], "【读SC】今天晚上吃什么火锅")
        self.assertEqual(audit[0]["label"], "读SC")

    def test_outside_twenty_seconds_and_generic_chat_do_not_mark(self):
        rows = [{
            "start_seconds": "100.000",
            "end_seconds": "103.000",
            "text": "今天晚上吃什么火锅",
        }]
        annotated, audit = annotate_rows(
            rows,
            [{"time": 70.0, "text": "今天晚上吃什么火锅"}, {"time": 101.0, "text": "哈哈哈"}],
            [],
        )
        self.assertEqual(annotated[0]["text"], "今天晚上吃什么火锅")
        self.assertEqual(audit, [])
        self.assertEqual(similarity("哈哈哈哈", "哈哈哈"), 0.0)


class PaidMessageContextTests(unittest.TestCase):
    def test_bare_sc_terms_are_not_forced_into_thanks(self):
        for source in ("我们在讨论SC机制", "游戏叫Super Cat", "A的SC是多少钱", "提到刚镚"):
            with self.subTest(source=source):
                self.assertNotIn("（谢谢SC）", compact_paid_thanks(source))

    def test_acknowledgement_context_is_still_compacted(self):
        cases = {
            "谢谢老板的super chat": "（谢谢SC）",
            "小王送的钢蹦儿谢谢": "（谢谢SC）",
            "收到钢本了": "（谢谢SC）",
            "super cat来了": "（谢谢SC）",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(compact_paid_thanks(source), expected)


if __name__ == "__main__":
    unittest.main()
