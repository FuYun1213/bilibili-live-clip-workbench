from __future__ import annotations

from pathlib import Path
import tempfile
from tkinter import TclError
import unittest
from unittest.mock import Mock, patch

import workflow_app


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class WorkflowDashboardSaveTests(unittest.TestCase):
    def make_workbench(self, panel="", tab="dashboard"):
        # Do not initialize Tk or any of the startup monitor/load callbacks.
        bench = object.__new__(workflow_app.Workbench)
        bench.dashboard_tab = "dashboard"
        bench.review_tab = "review"
        bench.dashboard_panel = panel
        bench.notebook = Mock(select=Mock(return_value=tab))
        bench.vars = {"status": Value("unsaved")}
        bench.append_log = Mock()
        bench.save_project = Mock(return_value=True)
        bench.save_auto_config = Mock(return_value=Path("config.json"))
        bench.current_review_video = Path("review/clip.mp4")
        bench.current_review_ass = Path("review/clip.ass")
        bench.save_current_review_copy = Mock(return_value=True)
        bench.save_current_subtitle = Mock(return_value=True)
        bench.review_subtitle_tree = Mock(selection=Mock(return_value=("line-1",)))
        return bench

    def test_manual_context_saves_only_its_project(self):
        for saved in (True, False):
            with self.subTest(saved=saved):
                bench = self.make_workbench(panel="manual")
                bench.save_project.return_value = saved

                self.assertIs(bench.save_workbench(), saved)

                bench.save_project.assert_called_once_with()
                bench.save_auto_config.assert_not_called()
                bench.save_current_review_copy.assert_not_called()
                self.assertEqual(
                    bench.vars["status"].get(), "保存完成" if saved else "unsaved"
                )

    def test_dashboard_saves_auto_settings_when_manual_panel_is_closed(self):
        for panel in ("", "settings", "batch"):
            with self.subTest(panel=panel):
                bench = self.make_workbench(panel=panel)

                self.assertTrue(bench.save_workbench())

                bench.save_auto_config.assert_called_once_with()
                bench.save_project.assert_not_called()
                bench.save_current_review_copy.assert_not_called()
                self.assertEqual(bench.vars["status"].get(), "保存完成")

    def test_failed_auto_save_does_not_report_success_or_fall_back_to_project(self):
        for panel in ("", "settings", "batch"):
            with self.subTest(panel=panel):
                bench = self.make_workbench(panel=panel)
                bench.save_auto_config.side_effect = ValueError("invalid settings")

                with patch.object(workflow_app.messagebox, "showerror") as showerror:
                    self.assertFalse(bench.save_workbench())

                showerror.assert_called_once_with(workflow_app.APP_TITLE, "invalid settings")
                bench.save_project.assert_not_called()
                bench.append_log.assert_not_called()
                self.assertEqual(bench.vars["status"].get(), "unsaved")

    def test_review_save_keeps_priority_over_open_manual_panel(self):
        bench = self.make_workbench(panel="manual", tab="review")

        self.assertTrue(bench.save_workbench())

        bench.save_current_review_copy.assert_called_once_with(silent=True, draft=True)
        bench.save_current_subtitle.assert_called_once_with(silent=True)
        bench.save_auto_config.assert_not_called()
        bench.save_project.assert_not_called()

    def test_review_failure_stops_save_and_preserves_error_state(self):
        for failing_step in ("metadata", "subtitle"):
            with self.subTest(failing_step=failing_step):
                bench = self.make_workbench(panel="manual", tab="review")
                bench.save_current_review_copy.return_value = failing_step != "metadata"
                bench.save_current_subtitle.return_value = failing_step != "subtitle"

                self.assertFalse(bench.save_workbench())

                self.assertEqual(bench.vars["status"].get(), "unsaved")
                bench.append_log.assert_not_called()
                bench.save_project.assert_not_called()
                bench.save_auto_config.assert_not_called()
                self.assertEqual(
                    bench.save_current_subtitle.call_count, int(failing_step == "subtitle")
                )

    def test_empty_review_does_not_save_hidden_dashboard_context(self):
        bench = self.make_workbench(panel="manual", tab="review")
        bench.current_review_video = None

        self.assertTrue(bench._save_active_context())

        bench.save_project.assert_not_called()
        bench.save_auto_config.assert_not_called()
        bench.save_current_review_copy.assert_not_called()
        bench.save_current_subtitle.assert_not_called()


class WorkflowDashboardLayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(workflow_app.core, "WORKSPACE_ROOT", Path(temporary)))
        self.enterContext(patch.object(
            workflow_app.core, "load_profiles",
            return_value={"sumire": {"display_name": "测试主播"}},
        ))
        self.enterContext(patch.object(workflow_app.glossary, "load_correction_dictionary"))
        self.enterContext(patch.object(workflow_app.Workbench, "_load_auto_config"))
        self.enterContext(patch.object(workflow_app.Workbench, "_configure_icon"))
        self.enterContext(patch.object(workflow_app.Workbench, "after", return_value="disabled"))
        self.enterContext(patch.object(workflow_app.Workbench, "after_idle", return_value="disabled"))
        self.enterContext(patch.object(
            workflow_app.Workbench, "flow2_prompt_description", return_value="测试提示词",
        ))
        self.launch_process = self.enterContext(patch.object(
            workflow_app.subprocess, "Popen",
            side_effect=AssertionError("UI construction must not launch a process"),
        ))
        try:
            self.bench = workflow_app.Workbench()
        except TclError as exc:
            if "no display" in str(exc).lower() or "couldn't connect to display" in str(exc).lower():
                self.skipTest(f"Tk display is unavailable: {exc}")
            raise
        self.addCleanup(self.bench.destroy)
        # Lay out real widgets without showing a test application window.
        self.bench.attributes("-alpha", 0.0)
        self.bench.geometry("1280x760")
        self.bench.update()

    def tearDown(self):
        self.launch_process.assert_not_called()

    @staticmethod
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from WorkflowDashboardLayoutTests.descendants(child)

    def assert_visible_inside_window(self, widget):
        bench = self.bench
        self.assertTrue(widget.winfo_ismapped(), str(widget))
        self.assertGreater(widget.winfo_width(), 1, str(widget))
        self.assertGreater(widget.winfo_height(), 1, str(widget))
        self.assertGreaterEqual(widget.winfo_rootx(), bench.winfo_rootx(), str(widget))
        self.assertGreaterEqual(widget.winfo_rooty(), bench.winfo_rooty(), str(widget))
        self.assertLessEqual(
            widget.winfo_rootx() + widget.winfo_width(),
            bench.winfo_rootx() + bench.winfo_width(), str(widget),
        )
        self.assertLessEqual(
            widget.winfo_rooty() + widget.winfo_height(),
            bench.winfo_rooty() + bench.winfo_height(), str(widget),
        )

    def test_initial_layout_has_two_pages_and_collapsed_auxiliary_panels(self):
        bench = self.bench
        self.assertEqual(
            [bench.notebook.tab(tab, "text") for tab in bench.notebook.tabs()],
            ["任务与流程", "切片审核台"],
        )
        self.assertEqual(bench.notebook.select(), str(bench.dashboard_tab))
        self.assertEqual(bench.dashboard_panel, "")
        self.assertFalse(bench.dashboard_panel_host.winfo_ismapped())
        self.assert_visible_inside_window(bench.auto_job_tree)
        self.assert_visible_inside_window(bench.console_panel)

    def test_auxiliary_panels_preserve_queue_actions_and_log_at_supported_sizes(self):
        bench = self.bench
        actions = [widget for widget in self.descendants(bench.auto_job_tree.master.master)
                   if isinstance(widget, (workflow_app.ttk.Button, workflow_app.ttk.Menubutton))
                   and widget.cget("text") in (
                       "打开审核台", "暂停 / 继续", "移到队首", "烧录并上传",
                       "打开交付文件夹", "更多操作", "从这里重做",
                   )]
        self.assertEqual(len(actions), 7)
        for size in ("1280x760", "1720x980"):
            bench.geometry(size)
            for name in ("manual", "settings", "batch", ""):
                with self.subTest(size=size, panel=name):
                    bench.show_dashboard_panel(name)
                    bench.update()
                    self.assertEqual(bench.dashboard_panel, name)
                    self.assertEqual(bool(bench.dashboard_panel_host.winfo_ismapped()), bool(name))
                    for other_name, panel in bench.dashboard_panels.items():
                        self.assertEqual(bool(panel.winfo_ismapped()), other_name == name)
                    self.assert_visible_inside_window(bench.auto_job_tree)
                    self.assertGreaterEqual(bench.auto_job_tree.winfo_height(), 64)
                    self.assert_visible_inside_window(bench.console_panel)
                    for action in actions:
                        self.assert_visible_inside_window(action)

    def test_shared_mode_and_asr_controls_exist_only_once_in_settings_panel(self):
        bench = self.bench
        settings_widgets = set(self.descendants(bench.dashboard_panels["settings"]))
        widgets = list(self.descendants(bench.dashboard_tab))
        for name in ("mode", "model", "device", "compute_type"):
            with self.subTest(setting=name):
                controls = [
                    widget for widget in widgets
                    if isinstance(widget, (workflow_app.ttk.Entry, workflow_app.ttk.Combobox))
                    and str(widget.cget("textvariable")) == str(bench.vars[name])
                ]
                self.assertEqual(len(controls), 1)
                self.assertIn(controls[0], settings_widgets)


    def test_long_monitor_summary_keeps_task_rows_visible(self):
        bench = self.bench
        summary = (
            "监控运行中｜手动流程优先，自动重任务暂停｜录播姬状态直读｜共 128 项｜"
            "直播中 12｜处理中 3｜断线观察 10｜等待 56｜异常/暂停 15｜坏分段 2｜完成 30"
        )
        for size in ("1280x760", "1720x980"):
            bench.geometry(size)
            for panel in ("manual", "settings", "batch"):
                with self.subTest(size=size, panel=panel):
                    bench.show_dashboard_panel(panel)
                    bench.update()
                    bench.vars["auto_status"].set(summary)
                    bench.update()
                    self.assert_visible_inside_window(bench.auto_job_tree)
                    self.assertGreaterEqual(bench.auto_job_tree.winfo_height(), 64)
                    bench.vars["auto_status"].set("监控未启动")
                    bench.update()

    def test_panel_controls_fit_canvas_width_at_supported_sizes(self):
        bench = self.bench
        for size in ("1280x760", "1720x980"):
            bench.geometry(size)
            for panel in ("manual", "settings", "batch"):
                with self.subTest(size=size, panel=panel):
                    bench.show_dashboard_panel(panel)
                    bench.update()
                    canvas_left = bench.dashboard_canvas.winfo_rootx()
                    canvas_right = canvas_left + bench.dashboard_canvas.winfo_width()
                    for widget in self.descendants(bench.dashboard_panels[panel]):
                        if widget.winfo_ismapped():
                            self.assertGreaterEqual(widget.winfo_rootx(), canvas_left, str(widget))
                            self.assertLessEqual(
                                widget.winfo_rootx() + widget.winfo_width(), canvas_right, str(widget),
                            )

if __name__ == "__main__":
    unittest.main()



