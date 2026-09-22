from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import review_workspace as rw


class ReviewRevisionReasonTests(unittest.TestCase):

    def test_save_notes_preserves_status_advice_replacement_metadata_and_undo(self):
        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary)
            video = review / "001.mp4"
            video.write_bytes(b"video")
            rw.set_decision(review, video.name, "revise", "原备注")
            rw.set_review_suggestions(review, video.name, "原独立建议")
            rw.set_decision(review, video.name, "approved", "通过备注", record_undo=True)
            state = rw.load_decisions(review, create=False)
            row = state["clips"][video.name]
            row.update({"replacement_requested": True, "replacement_action": "字幕",
                        "replacement_job_id": "existing-job", "custom_metadata": {"keep": 1}})
            rw._atomic_json(rw.decisions_path(review), state)
            before = dict(row)
            history = state[rw.DECISION_UNDO_HISTORY_KEY]

            saved = rw.set_review_notes(review, video.name, "  新重做理由  ")
            after = saved["clips"][video.name]

            self.assertEqual(after["notes"], "新重做理由")
            self.assertTrue(after["notes_updated_at"])
            self.assertEqual(
                {key: value for key, value in after.items() if key not in {"notes", "notes_updated_at"}},
                {key: value for key, value in before.items() if key not in {"notes", "notes_updated_at"}},
            )
            self.assertEqual(saved[rw.DECISION_UNDO_HISTORY_KEY], history)
            self.assertIsNotNone(rw.latest_undoable_approval(review))
            self.assertEqual(rw.load_decisions(review, create=False)["clips"][video.name], after)

    def test_clear_notes_does_not_change_revision_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary)
            video = review / "001.mp4"
            video.write_bytes(b"video")
            rw.set_decision(review, video.name, "revise", "旧理由")
            saved = rw.set_review_notes(review, video.name, "  ")
            self.assertEqual(saved["clips"][video.name]["notes"], "")
            self.assertEqual(saved["clips"][video.name]["status"], "revise")

    def test_save_notes_for_missing_video_does_not_write_decisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary)
            with self.assertRaises(FileNotFoundError):
                rw.set_review_notes(review, "missing.mp4", "理由")
            self.assertFalse(rw.decisions_path(review).exists())


    def test_detailed_advice_replaces_generic_pointer_note(self):
        row = {
            "status": "revise",
            "notes": "严重内容问题，请查看独立人工审核建议。",
            "review_suggestions": "需人工核对：收尾句被截断，需要回源恢复完整收尾。",
        }
        self.assertEqual(rw.review_revision_reason(row), row["review_suggestions"])

    def test_manual_supplement_remains_visible_beside_detailed_advice(self):
        row = {
            "status": "revise", "review_suggestions": "核对 00:12 的人名。",
            "notes": "补充重做理由：末尾还有半句话，需要回源补齐。",
        }
        self.assertEqual(
            rw.review_revision_reason(row),
            "核对 00:12 的人名。\n补充说明：补充重做理由：末尾还有半句话，需要回源补齐。",
        )

    def test_duplicate_finding_is_removed_but_timing_failure_is_preserved(self):
        row = {
            "status": "revise",
            "review_suggestions": "需人工核对：收尾 SC 不完整，需要回源确认。\n建议：修正后再通过。",
            "notes": "Codex 自动审核：严重问题：人工复核。收尾 SC 不完整，需要回源确认。 技术门禁：last cue leaves too little media tail。",
        }
        reason = rw.review_revision_reason(row)
        self.assertEqual(reason.count("收尾 SC 不完整，需要回源确认。"), 1)
        self.assertEqual(
            reason,
            row["review_suggestions"] + "\n补充说明：技术门禁：last cue leaves too little media tail。",
        )

    def test_exact_repeated_notes_are_not_shown_twice(self):
        for notes in ("收尾不完整。", "收尾不完整", " 收尾不完整。 "):
            with self.subTest(notes=notes):
                self.assertEqual(rw.review_revision_reason({
                    "status": "revise", "review_suggestions": "需人工核对：收尾不完整。",
                    "notes": notes,
                }), "需人工核对：收尾不完整。")

    def test_legacy_notes_remain_visible_without_advice(self):
        self.assertEqual(
            rw.review_revision_reason({
                "status": "revise", "notes": "  00:12 人名错听，需要对照原声。  ",
                "review_suggestions": None,
            }),
            "00:12 人名错听，需要对照原声。",
        )

    def test_missing_reason_is_explicit_and_does_not_invent_a_problem(self):
        row = {"status": "revise", "notes": None, "review_suggestions": "  "}
        self.assertEqual(
            rw.review_revision_reason(row),
            "未填写人工重做理由。请补充具体问题和需要修改的内容。",
        )
        self.assertEqual(row["notes"], None)
        self.assertNotIn("revision_reason", row)

    def test_replacement_action_is_labeled_and_not_used_as_a_content_finding(self):
        row = {
            "status": "revise", "replacement_requested": True,
            "replacement_action": "字幕", "notes": "",
        }
        self.assertEqual(
            rw.review_revision_reason(row),
            "原稿替换修订：字幕\n未填写具体重做理由。",
        )
        row["review_suggestions"] = "核对第 3 句的人名。"
        self.assertEqual(
            rw.review_revision_reason(row),
            "原稿替换修订：字幕\n核对第 3 句的人名。",
        )

    def test_stale_replacement_action_is_ignored(self):
        self.assertEqual(
            rw.review_revision_reason({
                "status": "revise", "replacement_requested": False,
                "replacement_action": "字幕", "notes": "收尾不完整",
            }),
            "收尾不完整",
        )

    def test_previous_rejection_notes_do_not_label_other_statuses_as_revisions(self):
        for status in ("pending", "approved", "skipped", ""):
            with self.subTest(status=status):
                self.assertEqual(
                    rw.review_revision_reason({
                        "status": status, "notes": "旧问题", "review_suggestions": "旧建议",
                    }),
                    "",
                )

    def test_active_pool_exposes_reason_without_rewriting_decisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review = root / "review"
            review.mkdir()
            video = review / "001.mp4"
            video.write_bytes(b"video")
            rw.set_decision(review, video.name, "revise", "请查看独立人工审核建议。")
            rw.set_review_suggestions(review, video.name, "多人联动说话人错位，需核对发言归属。")
            decision_path = rw.decisions_path(review)
            before = decision_path.read_bytes()

            items = rw.discover_review_items(root, statuses={"revise"})

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["revision_reason"], "多人联动说话人错位，需核对发言归属。")
            self.assertEqual(items[0]["revision_reason"], rw.review_revision_reason(items[0]))
            self.assertEqual(decision_path.read_bytes(), before)
            self.assertNotIn("revision_reason", json.loads(before)["clips"][video.name])

    def test_saved_notes_and_advice_recompute_reason_after_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            review = Path(temporary)
            video = review / "001.mp4"
            video.write_bytes(b"video")
            rw.set_decision(review, video.name, "revise", "先核对人名。")
            rw.set_review_suggestions(review, video.name, "00:12 的说话人是嘉宾，需要修改标记。")
            row = rw.load_decisions(review, create=False)["clips"][video.name]
            self.assertEqual(rw.review_revision_reason(row), "00:12 的说话人是嘉宾，需要修改标记。\n补充说明：先核对人名。")
            rw.set_review_suggestions(review, video.name, "")
            row = rw.load_decisions(review, create=False)["clips"][video.name]
            self.assertEqual(rw.review_revision_reason(row), "先核对人名。")


if __name__ == "__main__":
    unittest.main()
