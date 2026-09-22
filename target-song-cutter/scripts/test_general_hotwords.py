import json
import tempfile
import unittest
from pathlib import Path

from general_hotwords import (
    build_funasr_hotwords,
    build_whisper_prompt,
    load_general_hotwords,
    merge_terms,
)


class GeneralHotwordsTests(unittest.TestCase):
    def test_default_hotwords_contain_foot_and_leg(self):
        terms = load_general_hotwords()
        self.assertEqual(terms[:2], ["脚", "腿"])
        self.assertEqual(len(terms), len({term.casefold() for term in terms}))

    def test_vocabulary_is_never_injected_into_whisper_or_funasr(self):
        self.assertTrue(load_general_hotwords())
        self.assertIsNone(build_whisper_prompt("柚雨，Girimi"))
        self.assertEqual(build_funasr_hotwords("柚雨 Girimi"), "")

    def test_merge_terms_deduplicates_case_insensitively(self):
        self.assertEqual(merge_terms(["腿", "Girimi"], ["腿", "girimi", "脚"]), ["腿", "Girimi", "脚"])

    def test_rejects_duplicate_persistent_terms(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hotwords.json"
            path.write_text(
                json.dumps(
                    {"schema_version": 1, "updated_at": "2026-08-09", "terms": ["脚", "脚"]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Duplicate general hotword"):
                load_general_hotwords(path)


if __name__ == "__main__":
    unittest.main()
