import tempfile
import unittest
from pathlib import Path

from virtuareal_glossary import (
    compact_paid_thanks,
    load_replacements,
    normalize_text,
    load_correction_dictionary,
    set_correction_status,
    upsert_correction,
)


class VirtuaRealGlossaryCorrectionTests(unittest.TestCase):
    def test_requested_recognition_corrections_are_persistent(self):
        replacements = load_replacements()
        self.assertEqual(
            normalize_text("小秋说NF外的寡人痴收到钢棒", replacements),
            "小啾说NFY的管人痴（谢谢SC）",
        )

    def test_paid_thanks_are_compacted_without_removing_sc_body(self):
        self.assertEqual(
            compact_paid_thanks("谢谢小明的SC，为什么只看一个主播？我觉得不要这样"),
            "（谢谢SC），为什么只看一个主播？我觉得不要这样",
        )
        self.assertEqual(
            compact_paid_thanks("感谢A的SC和B的钢镚 你们问的正文还在"),
            "（谢谢SC） 你们问的正文还在",
        )

    def test_every_paid_message_variant_has_one_marker_without_sender_or_thanks(self):
        cases = {
            "谢谢老板的super chat": "（谢谢SC）",
            "感谢用户_123发的醒目留言：问题正文": "（谢谢SC）：问题正文",
            "小王送的钢蹦儿谢谢": "（谢谢SC）",
            "多谢Alice的S C，正文保留": "（谢谢SC），正文保留",
            "（谢谢SC）": "（谢谢SC）",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(compact_paid_thanks(source), expected)

    def test_fuzzy_paid_message_variants_are_canonicalized(self):
        cases = ("谢谢老板的刚镚", "杠蹦儿谢谢", "收到钢本了", "super cat来了")
        for source in cases:
            with self.subTest(source=source):
                self.assertIn("（谢谢SC）", compact_paid_thanks(source))

    def test_requested_xiaolu_corrections_are_active(self):
        replacements = load_replacements()
        self.assertEqual(
            normalize_text("小鹿和四小路提到刚镚", replacements),
            "小路和四时小路提到钢镚",
        )

    def test_current_batch_fuzzy_terms_are_active(self):
        replacements = load_replacements()
        self.assertEqual(
            normalize_text(
                "阴道劲和波罗芬，储姬小粉元说欧米嘎，KPR还有SPSHAT",
                replacements,
            ),
            "阴道镜和布洛芬，筑基小粉螈说Omega，KPL还有Super Chat",
        )

    def test_sumire_name_variants_are_normalized(self):
        replacements = load_replacements()
        self.assertEqual(
            normalize_text("肢颈、织景、志敬和知景", replacements),
            "枝堇、枝堇、枝堇和枝堇",
        )

    def test_explicit_phrase_correction_is_active_without_edit_monitor(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "corrections.json"
            added = upsert_correction("盾上", "盾山", path=path)
            self.assertEqual(added["status"], "active")
            value = load_correction_dictionary(path)
            self.assertEqual(value["entries"][added["id"]]["status"], "active")
            self.assertNotIn("上␟山", value["entries"])

    def test_single_character_rule_cannot_be_enabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "corrections.json"
            with self.assertRaisesRegex(ValueError, "完整词组"):
                upsert_correction("上", "山", path=path)

    def test_manually_added_phrase_can_be_disabled_and_enabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "corrections.json"
            added = upsert_correction("盾上", "盾山", path=path)
            disabled = set_correction_status(added["id"], "disabled", path=path)
            self.assertEqual(disabled["status"], "disabled")
            enabled = set_correction_status(added["id"], "active", path=path)
            self.assertEqual(enabled["status"], "active")

    def test_sc_letters_inside_an_english_word_are_not_paid_messages(self):
        self.assertEqual(compact_paid_thanks("ASCII music"), "ASCII music")


if __name__ == "__main__":
    unittest.main()
