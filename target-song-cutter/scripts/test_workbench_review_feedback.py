"""Real Tk review-feedback regressions isolated from production files and workers."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import review_workspace as rw
import workflow_app
import test_workflow_dashboard as dashboard_fixture


class WorkbenchReviewFeedbackTests(unittest.TestCase):
    def setUp(self):
        dashboard_fixture.WorkflowDashboardLayoutTests.setUp(self)
        bench = self.bench
        self.review = Path(workflow_app.core.WORKSPACE_ROOT) / "review-fixture"
        self.review.mkdir()
        self.video = self.review / "001_test.mp4"
        self.video.write_bytes(b"fixture video; never rendered or uploaded")
        rw.set_decision(self.review, self.video.name, "pending")
        bench.current_review_dir = self.review
        bench.current_review_video = self.video
        bench.current_review_ass = None
        bench.review_global_mode = False
        bench.review_item_map = {"fixture": {"video": self.video, "status": "pending"}}
        for method in ("save_current_review_copy", "_flush_pending_subtitle_autosave"):
            self.enterContext(patch.object(bench, method, return_value=True))
        for method in ("load_review_directory", "load_global_review_pool", "append_log",
                       "_schedule_review_batch_continuation", "_finish_approved_revision"):
            self.enterContext(patch.object(bench, method))
        self.warning = self.enterContext(patch.object(workflow_app.messagebox, "showwarning"))
        self.error = self.enterContext(patch.object(workflow_app.messagebox, "showerror"))
        bench.notebook.select(bench.review_tab)
        bench.vars["review_notes"].set("")
        bench.vars["review_suggestions"].set("")
        bench.update()

    tearDown = dashboard_fixture.WorkflowDashboardLayoutTests.tearDown

    def row(self):
        return rw.load_decisions(self.review, create=False)["clips"][self.video.name]

    def test_long_reason_remains_complete_readonly_and_can_be_replaced(self):
        bench = self.bench
        reason = "\n".join(f"第 {index} 项：需要核对原声、说话人和完整收尾。" for index in range(1, 61))
        bench.review_editor_notebook.select(bench.review_feedback_tab)
        bench.refresh_review_revision_reason({"status": "revise", "review_suggestions": reason})
        bench.update()
        display = bench.review_reason_display
        self.assertEqual(display.get("1.0", "end-1c"), reason)
        self.assertEqual(bench.vars["review_revision_reason"].get(), reason)
        self.assertEqual(str(display.cget("state")), "disabled")
        display.delete("1.0", "end")
        display.insert("1.0", "unwanted edit")
        self.assertEqual(display.get("1.0", "end-1c"), reason)
        display.see("end")
        bench.update()
        self.assertGreater(display.yview()[0], 0.0)
        self.assertEqual(display.yview()[1], 1.0)
        bench.refresh_review_revision_reason({"status": "revise", "notes": "下一条真实理由"})
        bench.update()
        self.assertEqual(display.get("1.0", "end-1c"), "下一条真实理由")
        self.assertEqual(str(display.cget("state")), "disabled")
        bench.refresh_review_revision_reason({"status": "approved", "notes": "旧理由"})
        self.assertEqual(display.get("1.0", "end-1c"), "当前切片未标记为人工重做")

    def test_save_feedback_persists_notes_and_advice_without_changing_status(self):
        bench = self.bench
        for status in ("pending", "revise", "approved"):
            with self.subTest(status=status):
                rw.set_decision(self.review, self.video.name, status, "旧备注")
                bench.vars["review_notes"].set("补充理由：末尾有半句。")
                bench.vars["review_suggestions"].set("核对 00:12 的人名；回源补全末尾。")
                self.assertTrue(bench.save_current_review_feedback())
                saved = self.row()
                self.assertEqual(saved["status"], status)
                self.assertEqual(saved["notes"], "补充理由：末尾有半句。")
                self.assertEqual(saved["review_suggestions"], "核对 00:12 的人名；回源补全末尾。")
                self.assertEqual(bench.vars["review_line_status"].get(), "审核理由与建议已保存")
                expected = rw.review_revision_reason(saved) or "当前切片未标记为人工重做"
                self.assertEqual(bench.review_reason_display.get("1.0", "end-1c"), expected)
        bench._schedule_review_batch_continuation.assert_not_called()
        bench._finish_approved_revision.assert_not_called()
        self.error.assert_not_called()

    def test_advice_save_failure_does_not_leave_a_previous_success_message(self):
        bench = self.bench
        rw.set_review_suggestions(self.review, self.video.name, "旧建议")
        bench.vars["review_notes"].set("新理由")
        bench.vars["review_suggestions"].set("新建议")
        bench.vars["review_line_status"].set("审核理由与建议已保存")
        with patch.object(rw, "set_review_suggestions", side_effect=OSError("disk full")):
            self.assertFalse(bench.save_current_review_feedback())
        self.assertEqual(self.row()["status"], "pending")
        self.assertEqual(self.row()["review_suggestions"], "旧建议")
        self.assertNotEqual(bench.vars["review_line_status"].get(), "审核理由与建议已保存")
        self.assertIn("失败", bench.vars["review_line_status"].get())

    def test_revise_without_reason_opens_feedback_and_does_not_write_a_decision(self):
        bench = self.bench
        bench.vars["review_notes"].set("  ")
        bench.vars["review_suggestions"].set("\n ")
        before = rw.decisions_path(self.review).read_bytes()
        with patch.object(bench.review_reason_editor, "focus_set") as focus, \
             patch.object(rw, "set_decision", wraps=rw.set_decision) as decide:
            bench.set_current_review_decision("revise")
        self.assertEqual(bench.review_editor_notebook.select(), str(bench.review_feedback_tab))
        focus.assert_called_once_with()
        self.warning.assert_called_once()
        decide.assert_not_called()
        bench.save_current_review_copy.assert_not_called()
        self.assertEqual(rw.decisions_path(self.review).read_bytes(), before)

    def test_revise_accepts_either_reason_field_and_saves_the_real_text(self):
        bench = self.bench
        for notes, suggestions in (("末尾不完整，需要回源延长。", ""), ("", "00:12 人名错听，需要核对。")):
            with self.subTest(notes=notes, suggestions=suggestions):
                rw.set_decision(self.review, self.video.name, "pending")
                rw.set_review_suggestions(self.review, self.video.name, "")
                bench.vars["review_notes"].set(notes)
                bench.vars["review_suggestions"].set(suggestions)
                bench.set_current_review_decision("revise")
                saved = self.row()
                self.assertEqual(saved["status"], "revise")
                self.assertEqual(saved["notes"], notes)
                self.assertEqual(saved["review_suggestions"], suggestions)
                self.assertEqual(rw.review_revision_reason(saved), suggestions or notes)
        self.assertEqual(bench.load_review_directory.call_count, 2)
        bench._schedule_review_batch_continuation.assert_not_called()
        bench._finish_approved_revision.assert_not_called()
        self.error.assert_not_called()

    def test_revise_does_not_change_status_when_reason_cannot_be_saved(self):
        bench = self.bench
        bench.vars["review_suggestions"].set("新建议")
        with patch.object(rw, "set_review_suggestions", side_effect=OSError("disk full")), \
             patch.object(rw, "set_decision", wraps=rw.set_decision) as decide:
            bench.set_current_review_decision("revise")
        self.assertEqual(self.row()["status"], "pending")
        decide.assert_not_called()
        bench.load_review_directory.assert_not_called()
        bench._schedule_review_batch_continuation.assert_not_called()

    def test_ctrl_s_handler_saves_both_reason_fields_and_keeps_decision(self):
        bench = self.bench
        self.assertTrue(bench.bind_all("<Control-s>"))
        rw.set_decision(self.review, self.video.name, "revise", "旧理由")
        bench.vars["review_notes"].set("Ctrl+S 保存的补充理由")
        bench.vars["review_suggestions"].set("Ctrl+S 保存的详细建议")
        self.assertEqual(bench.save_workbench(event=Mock()), "break")
        saved = self.row()
        self.assertEqual(saved["status"], "revise")
        self.assertEqual(saved["notes"], "Ctrl+S 保存的补充理由")
        self.assertEqual(saved["review_suggestions"], "Ctrl+S 保存的详细建议")
        self.assertEqual(bench.vars["status"].get(), "保存完成")

    def test_ctrl_s_notes_failure_stops_advice_and_clears_previous_success(self):
        bench = self.bench
        rw.set_decision(self.review, self.video.name, "revise", "先前保存的理由")
        rw.set_review_suggestions(self.review, self.video.name, "先前保存的建议")
        before = rw.decisions_path(self.review).read_bytes()
        bench.vars["review_notes"].set("尚未保存的新理由")
        bench.vars["review_suggestions"].set("尚未保存的新建议")
        bench.vars["status"].set("保存完成")
        with patch.object(rw, "set_review_notes", side_effect=OSError("disk full")), \
             patch.object(bench, "save_current_review_suggestions", wraps=bench.save_current_review_suggestions) as save_advice:
            self.assertFalse(bench.save_workbench())
        self.assertIn("失败", bench.vars["status"].get())
        self.assertNotEqual(bench.vars["status"].get(), "保存完成")
        self.assertIn("失败", bench.vars["review_line_status"].get())
        save_advice.assert_not_called()
        self.assertEqual(rw.decisions_path(self.review).read_bytes(), before)

    def test_standalone_advice_save_refreshes_readonly_reason_immediately(self):
        bench = self.bench
        rw.set_decision(self.review, self.video.name, "revise", "补充备注：末尾要留足余量。")
        rw.set_review_suggestions(self.review, self.video.name, "旧建议：检查第 1 句。")
        bench.refresh_review_revision_reason(self.row())
        before = bench.review_reason_display.get("1.0", "end-1c")
        bench.vars["review_suggestions"].set("新建议：第 8 句说话人需改为嘉宾。")
        self.assertTrue(bench.save_current_review_suggestions())
        saved = self.row()
        self.assertEqual(saved["status"], "revise")
        self.assertEqual(saved["review_suggestions"], "新建议：第 8 句说话人需改为嘉宾。")
        expected = rw.review_revision_reason(saved)
        self.assertNotEqual(expected, before)
        self.assertEqual(bench.vars["review_revision_reason"].get(), expected)
        self.assertEqual(bench.review_reason_display.get("1.0", "end-1c"), expected)
        self.assertEqual(str(bench.review_reason_display.cget("state")), "disabled")
        self.assertIn("补充备注：末尾要留足余量。", expected)
        self.assertEqual(bench.review_item_map["fixture"]["review_suggestions"], saved["review_suggestions"])
        self.error.assert_not_called()

    def test_ctrl_s_advice_failure_does_not_report_save_complete(self):
        bench = self.bench
        bench.vars["review_notes"].set("需要保存的理由")
        bench.vars["review_suggestions"].set("需要保存的建议")
        bench.vars["status"].set("保存完成")
        with patch.object(rw, "set_review_suggestions", side_effect=OSError("disk full")):
            self.assertFalse(bench.save_workbench())
        self.assertIn("失败", bench.vars["status"].get())
        self.assertEqual(self.row()["status"], "pending")


if __name__ == "__main__":
    unittest.main()
