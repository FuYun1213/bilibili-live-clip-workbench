"""Real Tk regressions for review controls, including smaller/high-DPI windows.

The dashboard fixture redirects workspace state and forbids subprocesses. No
review files are loaded and no publishing or production writes are possible.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import workflow_app
import test_workflow_dashboard as dashboard_fixture


class WorkbenchReviewLayoutTests(unittest.TestCase):
    tk_scaling = 4.0 / 3.0

    def setUp(self):
        original_init = workflow_app.Tk.__init__
        original_scaling = []

        def initialize_with_scaled_text(root, *args, **kwargs):
            original_init(root, *args, **kwargs)
            original_scaling.append(root.tk.call("tk", "scaling"))
            root.tk.call("tk", "scaling", self.tk_scaling)

        self.enterContext(patch.object(workflow_app.Tk, "__init__", initialize_with_scaled_text))
        dashboard_fixture.WorkflowDashboardLayoutTests.setUp(self)
        self.addCleanup(self.bench.tk.call, "tk", "scaling", original_scaling[0])

    tearDown = dashboard_fixture.WorkflowDashboardLayoutTests.tearDown
    descendants = staticmethod(dashboard_fixture.WorkflowDashboardLayoutTests.descendants)
    assert_visible_inside_window = dashboard_fixture.WorkflowDashboardLayoutTests.assert_visible_inside_window
    sizes = ("1280x760", "1440x900", "1720x980")

    def open_review(self, size):
        bench = self.bench
        bench.geometry(size)
        bench.notebook.select(bench.review_tab)
        if "review_editor_notebook" in bench.__dict__:
            bench.review_editor_notebook.select(bench.review_subtitle_tab)
        bench.update()

    def assert_fully_visible(self, widget):
        self.assert_visible_inside_window(widget)
        # A mapped widget can still be clipped by a smaller parent frame.
        parent = widget.master
        while parent is not None and parent is not self.bench:
            self.assertGreaterEqual(widget.winfo_rootx(), parent.winfo_rootx(), str(widget))
            self.assertGreaterEqual(widget.winfo_rooty(), parent.winfo_rooty(), str(widget))
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                 parent.winfo_rootx() + parent.winfo_width(), str(widget))
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 parent.winfo_rooty() + parent.winfo_height(), str(widget))
            parent = parent.master

    def load_subtitle_rows(self, count=80):
        bench = self.bench
        self.enterContext(patch.object(bench, "_flush_pending_subtitle_autosave", return_value=True))
        self.enterContext(patch.object(bench, "save_current_subtitle", return_value=True))
        self.enterContext(patch.object(bench, "seek_timeline_seconds"))
        bench.review_dialogue_rows = [
            {"line_number": index, "name": "测试主播", "start": "0:00:01.00",
             "end": "0:00:02.00", "text": f"第 {index} 条测试字幕，末尾用于验证完整文本"}
            for index in range(1, count + 1)
        ]
        bench.filter_review_subtitles()
        bench.update()

    def test_core_review_controls_and_at_least_three_subtitle_rows_fit(self):
        bench = self.bench
        self.load_subtitle_rows()
        for size in self.sizes:
            with self.subTest(size=size):
                self.open_review(size)
                for name in ("review_clip_tree", "review_player_panel", "review_waveform",
                             "review_subtitle_tree", "review_text_editor", "review_speaker_box",
                             "review_approve_button", "review_skip_button"):
                    self.assert_fully_visible(getattr(bench, name))
                tree = bench.review_subtitle_tree
                tree.yview_moveto(0.0)
                bench.update()
                for line in ("1", "2", "3"):
                    bbox = tree.bbox(line)
                    self.assertTrue(bbox, f"subtitle {line} hidden at {size}")
                    self.assertLessEqual(bbox[1] + bbox[3], tree.winfo_height())

    def test_last_subtitle_can_be_scrolled_selected_and_loaded_at_each_size(self):
        bench = self.bench
        self.load_subtitle_rows()
        tree = bench.review_subtitle_tree
        for size in self.sizes:
            with self.subTest(size=size):
                self.open_review(size)
                tree.yview_moveto(1.0)
                tree.selection_set("80")
                tree.focus("80")
                tree.see("80")
                bench.review_subtitle_selected()
                bench.update()
                self.assert_fully_visible(tree)
                self.assertTrue(tree.bbox("80"), f"final subtitle is inaccessible at {size}")
                self.assertEqual(tree.selection(), ("80",))
                self.assertIn("第 80 条", bench.review_text_editor.get("1.0", "end"))
                self.assertAlmostEqual(tree.yview()[1], 1.0, places=2)

    def test_subtitle_search_and_clear_restore_the_full_selectable_list(self):
        bench = self.bench
        self.load_subtitle_rows()
        self.open_review("1280x760")
        bench.vars["review_subtitle_query"].set("第 80 条")
        bench.update()
        self.assertEqual(bench.review_subtitle_tree.get_children(), ("80",))
        self.assertIn("1 / 80", bench.vars["review_subtitle_match_status"].get())
        bench.select_next_subtitle_match()
        self.assertEqual(bench.review_subtitle_tree.selection(), ("80",))
        bench.vars["review_subtitle_query"].set("")
        bench.update()
        self.assertEqual(len(bench.review_subtitle_tree.get_children()), 80)
        self.assertEqual(bench.vars["review_subtitle_match_status"].get(), "全部 80 条")
        self.assertEqual(bench.review_subtitle_tree.selection(), ("80",))
        self.assert_fully_visible(bench.review_subtitle_tree)
        self.assert_fully_visible(bench.review_subtitle_search)

    def test_both_subtitle_scrollbars_are_accessible_and_connected(self):
        bench = self.bench
        self.load_subtitle_rows()
        self.open_review("1280x760")
        tree = bench.review_subtitle_tree
        for name in ("review_subtitle_scroll", "review_subtitle_horizontal"):
            self.assert_fully_visible(getattr(bench, name))
        self.assertTrue(tree.cget("xscrollcommand"))
        self.assertTrue(tree.cget("yscrollcommand"))
        # Columns may fit initially. Widen the text column as a user can do,
        # then verify that overflow remains reachable with the scrollbar.
        overflow_width = tree.winfo_width() + 300
        tree.column("text", width=overflow_width, minwidth=overflow_width)
        bench.update()
        self.assertLess(tree.xview()[1] - tree.xview()[0], 1.0)
        tree.xview_moveto(1.0)
        bench.update()
        self.assertGreater(tree.xview()[0], 0.0)
        self.assertAlmostEqual(tree.xview()[1], 1.0, places=2)
        self.assertAlmostEqual(bench.review_subtitle_horizontal.get()[1], 1.0, places=2)

    def test_expanded_terminal_does_not_hide_subtitle_list_or_decisions(self):
        bench = self.bench
        self.load_subtitle_rows()
        if not bench.log.winfo_manager():
            bench.toggle_console_log()
        for size in self.sizes:
            with self.subTest(size=size):
                self.open_review(size)
                for widget in (bench.review_subtitle_tree, bench.review_text_editor,
                               bench.review_approve_button, bench.review_redo_button):
                    self.assert_fully_visible(widget)
                tree = bench.review_subtitle_tree
                tree.yview_moveto(0.0)
                bench.update()
                bbox = tree.bbox("3")
                self.assertTrue(bbox, f"expanded terminal hides subtitle rows at {size}")
                self.assertLessEqual(bbox[1] + bbox[3], tree.winfo_height())

    def test_published_revision_action_full_text_and_neighboring_buttons_fit(self):
        bench = self.bench
        with patch.object(bench, "_current_review_decision_row", return_value={"status": "revise"}), \
             patch.object(bench, "_current_review_source_publication", return_value=(True, None)):
            bench.refresh_review_primary_action()
        self.assertEqual(bench.review_approve_button.cget("text"), "重新烧录并替换源")
        for size in self.sizes:
            with self.subTest(size=size):
                self.open_review(size)
                button = bench.review_approve_button
                font = workflow_app.tkfont.Font(
                    root=bench,
                    font=workflow_app.ttk.Style(bench).lookup(button.cget("style") or "TButton", "font"),
                )
                self.assertGreaterEqual(button.winfo_width(), font.measure(button.cget("text")) + 8)
                for action in (button, bench.review_undo_approval_button,
                               bench.review_skip_button, bench.review_redo_button):
                    self.assert_fully_visible(action)

    def test_review_sections_and_decisions_remain_accessible(self):
        bench = self.bench
        for size in self.sizes:
            self.open_review(size)
            for name in ("review_subtitle_tab", "review_metadata_tab", "review_feedback_tab"):
                with self.subTest(size=size, section=name):
                    tab = getattr(bench, name)
                    bench.review_editor_notebook.select(tab)
                    bench.update()
                    self.assertTrue(tab.winfo_ismapped())
                    for action in (bench.review_approve_button, bench.review_skip_button,
                                   bench.review_redo_button):
                        self.assert_fully_visible(action)


class WorkbenchReviewHighDpiLayoutTests(WorkbenchReviewLayoutTests):
    """150% text scaling must not hide the subtitle list or its controls."""
    tk_scaling = 2.0  # 144 pixels/inch / 72 points/inch


if __name__ == "__main__":
    unittest.main()
