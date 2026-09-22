"""Bookend settings and current publication diagnostics, without posting."""
import json
from pathlib import Path
import queue
import unittest
from unittest.mock import patch, Mock

import test_workflow_dashboard as dashboard_fixture
import test_workbench_review_layout as layout_fixture
import test_workflow_command_queue as command_fixture
import workflow_app


class WorkbenchPackagingSettingsTests(unittest.TestCase):
    setUp = dashboard_fixture.WorkflowDashboardLayoutTests.setUp
    tearDown = dashboard_fixture.WorkflowDashboardLayoutTests.tearDown

    def test_shared_settings_save_and_reopen_without_starting_a_process(self):
        bench = self.bench
        intro = workflow_app.core.WORKSPACE_ROOT / "片头.mp4"
        intro.write_bytes(b"local fixture")
        window = bench.open_media_packaging_settings()
        window.attributes("-alpha", 0.0)
        fields = bench._media_packaging_fields
        self.assertFalse(fields["enabled"].get())
        fields["enabled"].set(True)
        fields["intro_path"].set(str(intro))
        bench._media_packaging_save_button.invoke()
        path = workflow_app.core.WORKSPACE_ROOT / "media-packaging.json"
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        self.assertTrue(value["enabled"])
        self.assertEqual(value["intro_path"], str(intro.resolve()))
        self.assertEqual(value["outro_path"], "")
        self.assertIn("已保存", fields["status"].get())
        window.destroy()
        reopened = bench.open_media_packaging_settings()
        reopened.attributes("-alpha", 0.0)
        self.assertTrue(bench._media_packaging_fields["enabled"].get())
        self.assertEqual(bench._media_packaging_fields["intro_path"].get(), str(intro.resolve()))
        self.launch_process.assert_not_called()

    def test_missing_enabled_asset_reports_failure_and_preserves_saved_config(self):
        bench = self.bench
        window = bench.open_media_packaging_settings()
        window.attributes("-alpha", 0.0)
        bench._media_packaging_save_button.invoke()
        path = workflow_app.core.WORKSPACE_ROOT / "media-packaging.json"
        before = path.read_bytes()
        bench._media_packaging_fields["enabled"].set(True)
        bench._media_packaging_fields["intro_path"].set(str(path.parent / "missing.mp4"))
        with patch.object(workflow_app.messagebox, "showerror") as error:
            bench._media_packaging_save_button.invoke()
        error.assert_called_once()
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("保存失败", bench._media_packaging_fields["status"].get())

    def test_settings_save_button_and_toolbar_entry_are_visible(self):
        bench = self.bench
        window = bench.open_media_packaging_settings()
        window.attributes("-alpha", 0.0)
        window.geometry("740x360")
        bench.update()
        button = bench._media_packaging_save_button
        self.assertTrue(button.winfo_ismapped())
        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), window.winfo_rooty() + window.winfo_height())
        self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), window.winfo_rootx() + window.winfo_width())
        entries = [widget for widget in dashboard_fixture.WorkflowDashboardLayoutTests.descendants(bench.dashboard_toolbar)
                   if isinstance(widget, workflow_app.ttk.Button) and widget.cget("text") == "片头片尾"]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].winfo_ismapped())


class WorkbenchPackagingHighDpiTests(WorkbenchPackagingSettingsTests):
    tk_scaling = 2.0
    setUp = layout_fixture.WorkbenchReviewLayoutTests.setUp


class WorkbenchPublicationDisplayTests(unittest.TestCase):
    def test_live_snapshot_updates_display_copies_without_rewriting_queue(self):
        job = {"id": "retry", "status": "running", "current_stage": "执行失败", "detail": "退出码1", "progress_percent": 100}
        snapshot = {"current_stage": "投稿限流冷却", "detail": "137022，等待冷却后自动继续", "progress_percent": 95}
        with patch.object(workflow_app.auto, "job_progress_snapshot", return_value=snapshot):
            display = workflow_app.auto_jobs_with_progress([job])
        self.assertEqual(job["current_stage"], "执行失败")
        self.assertEqual(display[0]["status"], "running")
        self.assertEqual(display[0]["current_stage"], "投稿限流冷却")
        self.assertIn("137022", display[0]["detail"])
        # The same display copies are consumed by the table and sidebar.
        from workflow_workbench_ui import build_execution_snapshot
        self.assertEqual(build_execution_snapshot({}, [], display)["running"][0]["detail"], "投稿限流冷却")

    def test_failure_footer_log_explains_platform_error_and_next_task_continues(self):
        bench = command_fixture.fake_workbench()
        bench.run_command(command_fixture.publish_command("first"))
        bench.run_command(command_fixture.publish_command("second"))
        record = bench.launches[0]
        record["output_tail"] = 'ResponseData { code: 137022, message: "投稿过于频繁，请稍后再试" }'
        bench._finish_command(record["id"], code=1)
        messages = "".join(call.args[0] for call in bench.append_log.call_args_list)
        self.assertIn("137022", messages)
        self.assertIn("退出码 1", messages)
        self.assertIn("冷却", messages)
        self.assertEqual(len(bench.launches), 2)

    def test_task_output_retains_bounded_tail_for_failure_diagnosis(self):
        bench = command_fixture.fake_workbench()
        bench.run_command(command_fixture.publish_command("first"))
        record = bench.launches[0]
        bench.output_queue = queue.Queue()
        bench.after = Mock()
        bench.report_callback_exception = Mock()
        bench.output_queue.put(("task_line", {"id": record["id"], "flow_key": record["flow_key"], "line": "x" * 150000}))
        bench.output_queue.put(("task_line", {"id": record["id"], "flow_key": record["flow_key"], "line": "ValueError: 片头文件不存在\n"}))
        bench._drain_queue()
        self.assertEqual(len(record["output_tail"]), 131072)
        self.assertTrue(record["output_tail"].endswith("ValueError: 片头文件不存在\n"))
        bench.report_callback_exception.assert_not_called()


if __name__ == "__main__":
    unittest.main()
