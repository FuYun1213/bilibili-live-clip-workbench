from __future__ import annotations

from tkinter import font as tkfont, ttk
import unicodedata
import unittest
from unittest.mock import patch

import workflow_app
import test_workflow_dashboard as dashboard_fixture


class QueueDetailWrappingTests(unittest.TestCase):
    def test_long_mixed_detail_retains_every_character(self):
        detail = (
            "流程 0 校验完成；源文件 D:\\直播素材\\session_20260905_part012.flv  "
            "接受 18 个分段，跳过 2 个坏分段；等待后续完整转写与人工审核。"
        ) * 4

        lines = workflow_app.split_monitor_detail(detail, 18)

        self.assertGreater(len(lines), 2)
        self.assertEqual("".join(lines), detail)
        self.assertFalse(any("…" in line for line in lines))
        for line in lines:
            self.assertLessEqual(sum(
                2 if unicodedata.east_asian_width(char) in {"W", "F", "A"} else 1
                for char in line
            ), 18)

    def test_explicit_line_breaks_blank_lines_and_spaces_are_preserved(self):
        self.assertEqual(
            workflow_app.split_monitor_detail("  第一行  \n\n第二行 English  \n", 80),
            ["  第一行  ", "", "第二行 English  ", ""],
        )
        self.assertEqual(workflow_app.split_monitor_detail("", 12), [""])

    def test_narrow_column_and_preexisting_ellipsis_do_not_lose_text(self):
        detail = "中 A…B 终"
        lines = workflow_app.split_monitor_detail(detail, 1)

        self.assertEqual("".join(lines), detail)
        self.assertEqual(sum(line.count("…") for line in lines), 1)
        self.assertTrue(all(len(line) == 1 for line in lines))


class QueueDetailRenderingTests(unittest.TestCase):
    # Reuse its isolated real-Tk setup without inheriting the dashboard tests.
    setUp = dashboard_fixture.WorkflowDashboardLayoutTests.setUp
    tearDown = dashboard_fixture.WorkflowDashboardLayoutTests.tearDown

    def set_rows(self, details, *, width=200):
        bench = self.bench
        bench.auto_job_tree.column("detail", width=width, stretch=False)
        bench._auto_job_display_rows = [
            (
                f"job-{index}",
                ("2026-09-05 12:00", "测试主播", f"测试场次 {index}",
                 "等待人工审核", "65%", "等待审核", detail),
                "waiting",
            )
            for index, detail in enumerate(details)
        ]
        bench._render_auto_job_rows()
        bench.update_idletasks()

    def detail_rows(self, job_id="job-0"):
        bench = self.bench
        return [
            str(row)
            for row in bench.auto_job_tree.get_children()
            if bench.auto_job_row_to_job.get(str(row)) == job_id
        ]

    def detail_text(self, job_id="job-0"):
        tree = self.bench.auto_job_tree
        return "".join(str(tree.set(row, "detail")) for row in self.detail_rows(job_id))

    def tree_font(self):
        tree = self.bench.auto_job_tree
        style = str(tree.cget("style") or "Treeview")
        font = ttk.Style(self.bench).lookup(style, "font") or "TkDefaultFont"
        return tkfont.Font(root=self.bench, font=font)

    def first_visible_row(self):
        tree = self.bench.auto_job_tree
        return next(
            str(row)
            for y in range(tree.winfo_height())
            if (row := tree.identify_row(y))
        )

    def test_font_measure_wraps_all_text_inside_actual_column(self):
        detail = (
            "自动队列完整说明：Wide WWW narrow iii，中文 English "
            "D:\\直播素材\\long_source_path_20260905.flv；尾句必须可读。"
        ) * 3
        self.set_rows([detail], width=185)

        tree = self.bench.auto_job_tree
        font = self.tree_font()
        self.assertGreater(len(self.detail_rows()), 2)
        self.assertEqual(self.detail_text(), detail)
        available = int(tree.column("detail", "width")) - 12
        for row in self.detail_rows():
            self.assertLessEqual(font.measure(tree.set(row, "detail")), available)

        measured = workflow_app.split_monitor_detail(detail, available, measure=font.measure)
        self.assertEqual("".join(measured), detail)
        self.assertTrue(all(font.measure(line) <= available for line in measured))

    def test_column_resize_reflows_full_detail_in_both_directions(self):
        detail = "列宽改变后需要完整保留中文说明及 path/file-name-0123.flv；最后一句。" * 4
        self.set_rows([detail], width=150)
        narrow_count = len(self.detail_rows())

        self.bench.auto_job_tree.column("detail", width=420)
        self.bench._render_auto_job_rows()

        self.assertLess(len(self.detail_rows()), narrow_count)
        self.assertEqual(self.detail_text(), detail)
        self.bench.auto_job_tree.column("detail", width=150)
        self.bench._render_auto_job_rows()
        self.assertEqual(len(self.detail_rows()), narrow_count)
        self.assertEqual(self.detail_text(), detail)

    def test_later_continuation_rows_select_one_task(self):
        detail = "等待审核的最近说明与完整源文件路径；" * 15
        self.set_rows([detail, "另一个任务"], width=150)
        rows = self.detail_rows()
        self.assertGreater(len(rows), 4)

        self.bench.auto_job_tree.selection_set(rows[2], rows[3], rows[-1])

        self.assertEqual(self.bench.selected_auto_job_ids(), ["job-0"])
        self.assertEqual(
            self.bench.auto_job_row_to_job[rows[-1]], "job-0"
        )
        self.bench.auto_job_tree.selection_add("job-1")
        self.assertEqual(self.bench.selected_auto_job_ids(), ["job-0", "job-1"])

    def test_drag_and_control_selection_treat_all_continuations_as_one_task(self):
        self.set_rows(["完整最近说明与路径片段；" * 12] * 3, width=160)
        tree = self.bench.auto_job_tree
        rows = [self.detail_rows(f"job-{index}") for index in range(3)]
        cases = (
            ("replace", rows[0][3], rows[1][2], set(), ["job-0", "job-1"]),
            ("toggle", rows[0][3], rows[0][3], set(rows[0]), []),
            ("toggle", rows[0][3], rows[1][2],
             {rows[0][2], rows[2][-1]}, ["job-1", "job-2"]),
            ("add", rows[1][3], rows[1][3], {rows[0][2]}, ["job-0", "job-1"]),
        )
        for mode, anchor, target, base, expected in cases:
            with self.subTest(mode=mode, expected=expected):
                self.bench._auto_job_drag = {
                    "anchor": anchor, "target": anchor, "base": base,
                    "mode": mode, "start_y": 40, "last_y": 80, "moved": False,
                }

                self.bench._set_auto_job_drag_selection(target)

                self.assertEqual(self.bench.selected_auto_job_ids(), expected)
                self.assertEqual(set(tree.selection()), {
                    row
                    for job_id in expected
                    for row in self.detail_rows(job_id)
                })
        self.bench._auto_job_drag = None

    def test_explicit_blank_lines_remain_visible_and_map_to_their_task(self):
        self.set_rows(["  第一行  \n\n末行  \n"], width=250)
        tree = self.bench.auto_job_tree

        self.assertEqual(
            [tree.set(row, "detail") for row in self.detail_rows()],
            ["  第一行  ", "", "末行  ", ""],
        )
        self.assertTrue(all(
            self.bench.auto_job_row_to_job[row] == "job-0"
            for row in self.detail_rows()
        ))

    def test_identical_refresh_does_not_rebuild_rows(self):
        self.set_rows(["重复刷新仍需保持阅读位置；" * 30], width=180)
        tree = self.bench.auto_job_tree
        with patch.object(tree, "delete", wraps=tree.delete) as delete, patch.object(
            tree, "insert", wraps=tree.insert
        ) as insert:
            self.bench._render_auto_job_rows()

        delete.assert_not_called()
        insert.assert_not_called()

    def test_changed_refresh_preserves_reading_position_selection_and_focus(self):
        details = [f"任务 {index} 完整最近说明；" * 6 for index in range(20)]
        self.set_rows(details, width=190)
        tree = self.bench.auto_job_tree
        focused = self.detail_rows("job-8")[2]
        tree.selection_set(focused)
        tree.focus(focused)
        tree.yview_moveto(0.45)
        self.bench.update_idletasks()
        top = self.first_visible_row()
        self.assertGreater(tree.yview()[0], 0)

        details[-1] += " 刚刚更新的状态仍应显示完整。"
        self.set_rows(details, width=190)

        self.assertEqual(self.first_visible_row(), top)
        self.assertEqual(tree.focus(), focused)
        self.assertEqual(self.bench.selected_auto_job_ids(), ["job-8"])
        self.assertEqual(self.detail_text("job-19"), details[-1])

    def test_dragging_defers_reflow_until_selection_finishes(self):
        original = "拖动多选过程中保持行稳定；" * 15
        self.set_rows([original], width=190)
        tree = self.bench.auto_job_tree
        old_rows = tuple(tree.get_children())
        self.bench._auto_job_drag = {"anchor": old_rows[0], "target": old_rows[-1]}
        updated = original + "新增的末尾说明。"

        self.set_rows([updated], width=120)

        self.assertEqual(tuple(tree.get_children()), old_rows)
        self.assertEqual(self.detail_text(), original)
        self.bench._auto_job_drag = None
        self.bench._render_auto_job_rows()
        self.assertEqual(self.detail_text(), updated)
        self.assertGreater(len(self.detail_rows()), len(old_rows))


if __name__ == "__main__":
    unittest.main()
