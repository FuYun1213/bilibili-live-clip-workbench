from __future__ import annotations
from collections import deque
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import workflow_auto as auto
from workflow_workbench_ui import WorkbenchUiMixin, build_execution_snapshot, command_arguments


def command(action, *args):
    return ["python", "-X", "utf8", "workflow_auto.py", "--config", "settings.json", action, *args]


def record(task_id, action, job_id, **kwargs):
    return {"id": task_id, "flow_key": "manual:flow5", "command": command(action, "--job-id", job_id, *(["--allow-upload"] if action == "run-job" else [])), **kwargs}


class WorkbenchQueueViewTests(unittest.TestCase):
    def test_pending_republishes_keep_every_task_and_submission_order(self):
        jobs = [
            {"id": "one", "title": "第一场长录播", "current_stage": "上传失败", "status": "failed"},
            {"id": "two", "title": "第二场录播", "current_stage": "等待投稿", "status": "ready_to_publish"},
        ]
        pending = deque([record(8, "replace-published-job", "two"), record(9, "run-job", "one", label="重试发布")])
        snapshot = build_execution_snapshot({}, pending, jobs)
        self.assertEqual([item["id"] for item in snapshot["pending"]], [8, 9])
        self.assertEqual([item["title"] for item in snapshot["pending"]], ["第二场录播", "第一场长录播"])
        self.assertEqual([item["action"] for item in snapshot["pending"]], ["重新发布", "重试发布"])
        self.assertEqual(snapshot["pending"][1]["detail"], "等待再次投递")
        self.assertEqual(list(pending)[0]["id"], 8)
        self.assertEqual(snapshot["queued"], [])

    def test_active_republish_waiting_on_real_queue_lock_does_not_show_old_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "queue.json"
            job = {"id": "retry", "title": "再次投稿", "status": "failed",
                   "current_stage": "B站限制投稿频率", "publish_error_code": "137022"}
            state_path.write_text(json.dumps({"jobs": {"retry": job}}), encoding="utf-8")
            lock = state_path.with_name(state_path.name + ".lock")
            lock.write_text(str(os.getpid()), encoding="ascii")
            active = record(1, "run-job", "retry", output_tail=(
                "手动操作已进入优先队列；若自动监控正在收尾当前安全阶段，这里会等待它让位。\n"
            ))
            before = state_path.read_bytes()
            snapshots = []

            def during_lock_wait(_seconds):
                current = json.loads(state_path.read_text(encoding="utf-8"))["jobs"]
                snapshots.append(build_execution_snapshot({"publish": active}, [], current))
                lock.unlink()

            with patch.object(auto.time, "sleep", side_effect=during_lock_wait):
                with auto.queue_lock(state_path, wait_seconds=None):
                    pass
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["running"][0]["detail"], "准备再次投递，等待任务状态更新")
            self.assertEqual(state_path.read_bytes(), before)
            self.assertEqual(active["output_tail"].count("手动操作"), 1)

    def test_pending_and_just_started_republishes_do_not_mutate_failure_records(self):
        jobs = [{"id": "retry", "status": "failed", "current_stage": "再次投递失败"}]
        original = copy.deepcopy(jobs)
        for action in ("run-job", "replace-published-job", "retry-publish-job"):
            with self.subTest(action=action):
                request = record(1, action, "retry")
                self.assertEqual(build_execution_snapshot({}, [request], jobs)["pending"][0]["detail"], "等待再次投递")
                self.assertEqual(build_execution_snapshot({"publish": request}, [], jobs)["running"][0]["detail"], "准备再次投递，等待任务状态更新")
        self.assertEqual(jobs, original)

    def test_started_attempt_preserves_current_cooldown_and_new_failure(self):
        request = record(1, "run-job", "retry", output_tail='FLOW5_PROGRESS {"phase":"failed","code":"137022"}\n')
        for status, stage in (("running", "B站限制投稿频率，正在冷却"), ("failed", "B站限制投稿频率")):
            with self.subTest(status=status):
                jobs = [{"id": "retry", "status": status, "current_stage": stage}]
                result = build_execution_snapshot({"publish": request}, [], jobs)
                self.assertEqual(result["running"][0]["detail"], stage)

    def test_media_only_command_does_not_claim_to_be_republishing(self):
        request = {"id": 1, "command": command("run-job", "--job-id", "failed", "--allow-burn")}
        jobs = [{"id": "failed", "status": "failed", "current_stage": "烧录失败"}]
        self.assertEqual(build_execution_snapshot({}, [request], jobs)["pending"][0]["detail"], "烧录失败")
        self.assertEqual(build_execution_snapshot({"media": request}, [], jobs)["running"][0]["detail"], "烧录失败")

    def test_auto_queue_uses_scheduler_priority_and_excludes_paused_failed(self):
        jobs = [
            {"id": "new", "title": "新任务", "status": "queued", "queue_priority": 0, "created_at": "2026-09-07"},
            {"id": "urgent", "title": "优先任务", "status": "queued", "queue_priority": -10, "created_at": "2026-09-07"},
            {"id": "old", "title": "旧任务", "status": "queued", "queue_priority": 0, "created_at": "2026-09-06"},
            {"id": "paused", "status": "paused", "queue_priority": -20},
            {"id": "failed", "status": "failed"},
        ]
        snapshot = build_execution_snapshot({}, [], jobs)
        self.assertEqual([item["id"] for item in snapshot["queued"]], ["urgent", "old", "new"])
        self.assertEqual([job["id"] for job in jobs], ["new", "urgent", "old", "paused", "failed"])

    def test_local_active_and_pending_jobs_are_not_counted_again_as_automatic(self):
        jobs = [
            {"id": name, "title": name, "status": "queued", "queue_priority": priority}
            for name, priority in (("active", -20), ("pending-a", -10),
                                   ("pending-b", -10), ("next", 0), ("later", 10))
        ]
        active = {"publish": record(1, "run-job", "active")}
        pending = [{"id": 2, "command": command(
            "run-jobs", "--job-id", "pending-a", "--job-id=pending-b") }]
        snapshot = build_execution_snapshot(active, pending, jobs)
        self.assertEqual([item["job_ids"] for item in snapshot["running"]], [["active"]])
        self.assertEqual([item["job_ids"] for item in snapshot["pending"]], [["pending-a", "pending-b"]])
        self.assertEqual([item["id"] for item in snapshot["queued"]], ["next", "later"])
        self.assertTrue(all(job["status"] == "queued" for job in jobs))

    def test_queued_job_returns_to_automatic_view_when_local_request_is_removed(self):
        jobs = [{"id": "a", "title": "queued job", "status": "queued"}]
        pending = deque([record(1, "run-job", "a")])
        self.assertEqual(build_execution_snapshot({}, pending, jobs)["queued"], [])
        pending.clear()
        self.assertEqual([item["id"] for item in build_execution_snapshot({}, pending, jobs)["queued"]], ["a"])

    def test_actual_monitor_job_visible_and_same_active_job_not_duplicated(self):
        jobs = [{"id": "a", "title": "当前投稿", "status": "running", "current_stage": "上传 50%"}, {"id": "b", "title": "监控转写", "status": "running", "current_stage": "转写中"}]
        snapshot = build_execution_snapshot({"flow5": record(1, "run-job", "a")}, [], jobs)
        self.assertEqual(len(snapshot["running"]), 2)
        self.assertEqual(snapshot["running"][0]["detail"], "上传 50%")
        self.assertEqual(snapshot["running"][1]["title"], "监控转写")

    def test_batch_command_lists_all_selected_job_titles(self):
        records = [{"id": 1, "command": command("run-jobs", "--job-id", "a", "--job-id=b")}]
        snapshot = build_execution_snapshot({}, records, {"a": {"id": "a", "title": "甲"}, "b": {"id": "b", "title": "乙"}})
        self.assertEqual(snapshot["pending"][0]["title"], "甲\n乙")
        self.assertEqual(command_arguments(records[0]["command"], "--job-id"), ["a", "b"])

    def test_missing_job_uses_identifier_and_manual_command_uses_project_name(self):
        snapshot = build_execution_snapshot({}, [record(1, "run-job", "unknown"), {"id": 2, "command": ["python", "workflow_app_core.py", "step4", "--project", "D:\\切片项目\\测试项目"]}], [])
        self.assertEqual(snapshot["pending"][0]["title"], "unknown")
        self.assertEqual(snapshot["pending"][1]["title"], "测试项目")
        self.assertEqual(snapshot["pending"][1]["action"], "烧录与审计")

    def test_refresh_without_tk_can_cache_and_replace_job_state(self):
        bench = WorkbenchUiMixin()
        bench.active_commands = {}
        bench.pending_commands = deque([record(1, "replace-published-job", "a")])
        snapshot = bench.refresh_execution_panel([{"id": "a", "title": "重新发布任务", "status": "failed"}])
        self.assertEqual(snapshot["pending"][0]["title"], "重新发布任务")
        self.assertEqual(bench.refresh_execution_panel(), snapshot)
        self.assertEqual(bench.refresh_execution_panel([])["pending"][0]["title"], "a")


import test_workflow_dashboard as dashboard_fixture


class WorkbenchExecutionPanelLayoutTests(unittest.TestCase):
    setUp = dashboard_fixture.WorkflowDashboardLayoutTests.setUp
    tearDown = dashboard_fixture.WorkflowDashboardLayoutTests.tearDown
    assert_visible_inside_window = dashboard_fixture.WorkflowDashboardLayoutTests.assert_visible_inside_window

    def test_execution_panel_stays_visible_at_three_window_sizes(self):
        bench = self.bench
        bench.active_commands = {"active": record(1, "replace-published-job", "active")}
        bench.pending_commands = deque(record(index + 2, "replace-published-job", f"job-{index}") for index in range(8))
        jobs = [{"id": "active", "title": "当前正在重新发布的文件", "current_stage": "上传中", "status": "running"}]
        jobs += [{"id": f"job-{index}", "title": f"等待重新发布的第 {index + 1} 个视频文件", "current_stage": "等待发布", "status": "ready_to_publish"} for index in range(8)]
        jobs += [{"id": "auto", "title": "接下来自动转写的录播", "status": "queued"}]
        bench.refresh_execution_panel(jobs)
        for size in ("1280x760", "1440x900", "1720x980"):
            bench.geometry(size)
            for panel in ("", "manual", "settings", "batch"):
                with self.subTest(size=size, panel=panel):
                    bench.show_dashboard_panel(panel)
                    bench.update()
                    self.assert_visible_inside_window(bench.execution_panel)
                    self.assert_visible_inside_window(bench.execution_canvas)
                    self.assertGreaterEqual(bench.execution_panel.winfo_width(), 300)
                    self.assertGreaterEqual(bench.execution_canvas.winfo_height(), 50)
                    self.assertLess(bench.execution_canvas.yview()[1] - bench.execution_canvas.yview()[0], 1)
                    self.assertIn("待执行 8", bench.execution_summary_var.get())
        bench.execution_canvas.yview_moveto(1.0)
        bench.update_idletasks()
        self.assertAlmostEqual(bench.execution_canvas.yview()[1], 1.0, places=2)

    def test_summary_counts_each_job_in_its_current_display_section(self):
        bench = self.bench
        bench.active_commands = {"publish": record(1, "run-job", "active")}
        bench.pending_commands = deque([record(2, "run-job", "pending")])
        jobs = [{"id": name, "title": name, "status": "queued"}
                for name in ("active", "pending", "automatic")]
        bench.refresh_execution_panel(jobs)
        bench.update_idletasks()
        self.assertEqual(bench.execution_summary_var.get(), "运行 1  ·  待执行 1  ·  自动接续 1")
        automatic_cards = bench._execution_snapshot["queued"]
        self.assertEqual([item["title"] for item in automatic_cards], ["automatic"])

    def test_refresh_updates_card_text_and_preserves_long_task_names(self):
        bench = self.bench
        title = "长任务标题含完整文件名、分段与中文 English 123：" * 6
        bench.pending_commands = deque([record(1, "replace-published-job", "long")])
        bench.refresh_execution_panel([{"id": "long", "title": title, "status": "failed", "current_stage": "投稿失败待重试"}])
        bench.update_idletasks()
        self.assertIn(title, [str(label.cget("text")) for label in bench._execution_label_widgets])
        self.assertIn("等待再次投递", [str(label.cget("text")) for label in bench._execution_label_widgets])
        for label in bench._execution_label_widgets:
            self.assertLessEqual(label.winfo_width(), bench.execution_canvas.winfo_width())
        bench.pending_commands.clear()
        bench.refresh_execution_panel([])
        self.assertIn("本地执行队列为空", [str(label.cget("text")) for label in bench._execution_label_widgets])


if __name__ == "__main__":
    unittest.main()

