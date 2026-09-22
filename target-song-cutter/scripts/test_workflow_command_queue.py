"""Isolated UI dispatcher regressions; never starts workers or publishes media."""

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import workflow_app


def publish_command(job_id: str, *, replace: bool = False) -> list[str]:
    return [
        "python", "workflow_auto.py", "--config", "test-auto.json",
        "replace-published-job" if replace else "run-job", "--job-id", job_id,
        *([] if replace else ["--allow-burn", "--allow-upload"]),
    ]


def fake_workbench():
    bench = object.__new__(workflow_app.Workbench)
    bench.command_counter = 0
    bench.active_commands = {}
    bench.pending_commands = deque()
    bench.task_processes = {}
    bench.process = None
    bench.vars = {"status": Mock(), "auto_status": Mock()}
    bench.append_log = Mock()
    bench.refresh_status = Mock()
    bench.refresh_auto_status = Mock()
    bench._sync_manual_activity_marker = Mock()
    bench._refresh_execution_queue = Mock()
    bench._refresh_command_queue = Mock()
    bench._refresh_queue_panel = Mock()
    bench.refresh_execution_panel = Mock()
    bench.save_auto_config = Mock()
    bench.monitor_process = None
    bench._monitor_starting = False
    bench._restart_safe_monitor = Mock()
    bench._recover_interrupted_auto_jobs = Mock(return_value=0)
    bench._kill_tree = Mock(return_value=True)
    bench.launches = []

    def launch(record):
        bench.active_commands[record["flow_key"]] = record
        bench.launches.append(record)

    bench._launch_command = launch
    return bench


class WorkflowCommandQueueTests(unittest.TestCase):
    def test_new_upload_and_replacement_share_a_serial_publish_lane(self):
        commands = [
            publish_command("first"),
            publish_command("second", replace=True),
            ["python", "workflow_auto.py", "run-jobs", "--job-id", "third",
             "--job-id", "fourth", "--allow-upload"],
            ["python", "workflow_app_core.py", "upload", "--project", "test-project"],
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(workflow_app.command_flow_key(command), "manual:publish")
        self.assertNotEqual(
            workflow_app.command_flow_key(
                ["python", "workflow_auto.py", "run-job", "--job-id", "burn-only", "--allow-burn"]
            ),
            "manual:publish",
        )

    def test_multiple_selected_uploads_keep_order_and_continue_after_failure(self):
        bench = fake_workbench()
        succeeded = [Mock(), Mock(), Mock()]
        failed = [Mock(), Mock(), Mock()]
        commands = [publish_command("first"), publish_command("second", replace=True), publish_command("third")]
        for index, command in enumerate(commands):
            self.assertTrue(bench.run_command(
                command, after=succeeded[index], on_failure=failed[index], label=f"投稿 {index + 1}"
            ))
        self.assertEqual(len(bench.active_commands), 1)
        self.assertEqual([record["command"] for record in bench.pending_commands], commands[1:])

        bench._finish_command(bench.launches[-1]["id"], code=1)
        failed[0].assert_called_once()
        succeeded[0].assert_not_called()
        self.assertEqual(bench.launches[-1]["command"], commands[1])
        bench._finish_command(bench.launches[-1]["id"], code=0)
        succeeded[1].assert_called_once_with()
        failed[1].assert_not_called()
        self.assertEqual(bench.launches[-1]["command"], commands[2])
        bench._finish_command(bench.launches[-1]["id"], code=0)
        self.assertFalse(bench.active_commands)
        self.assertFalse(bench.pending_commands)

    def test_duplicate_active_or_pending_publication_is_not_queued_twice(self):
        bench = fake_workbench()
        first, second = publish_command("first"), publish_command("second")
        self.assertTrue(bench.run_command(first))
        self.assertFalse(bench.run_command(first))
        self.assertTrue(bench.run_command(second))
        self.assertFalse(bench.run_command(second))
        self.assertEqual(len(bench.launches), 1)
        self.assertEqual([record["command"] for record in bench.pending_commands], [second])

    def test_partial_duplicate_batch_keeps_new_jobs_and_runs_after_active_failure(self):
        bench = fake_workbench()
        bench.run_command(publish_command("active"))
        succeeded, failed = Mock(), Mock()
        requested = ["python", "workflow_auto.py", "run-jobs", "--job-id", "active",
                     "--job-id", "new-first", "--job-id", "new-second", "--allow-burn", "--allow-upload"]
        original = requested.copy()
        self.assertTrue(bench.run_command(requested, after=succeeded, on_failure=failed))
        queued = bench.pending_commands[0]
        self.assertEqual(workflow_app.command_arguments(queued["command"], "--job-id"),
                         ["new-first", "new-second"])
        self.assertIn("--allow-burn", queued["command"])
        self.assertIn("--allow-upload", queued["command"])
        self.assertEqual(requested, original)
        bench._finish_command(bench.launches[0]["id"], code=1)
        self.assertIs(bench.launches[-1], queued)
        bench._finish_command(queued["id"], code=0)
        succeeded.assert_called_once_with()
        failed.assert_not_called()
        self.assertFalse(bench.pending_commands)

    def test_partial_batch_filters_active_and_pending_ids_in_both_option_formats(self):
        bench = fake_workbench()
        active = ["python", "workflow_auto.py", "run-job", "--job-id=active", "--allow-upload"]
        bench.run_command(active)
        bench.run_command(publish_command("pending", replace=True))
        requested = ["python", "workflow_auto.py", "run-jobs", "--job-id", "active",
                     "--job-id=pending", "--job-id=new-first", "--job-id", "new-second", "--allow-upload"]
        self.assertTrue(bench.run_command(requested))
        self.assertEqual(len(bench.pending_commands), 2)
        self.assertEqual(workflow_app.command_arguments(bench.pending_commands[-1]["command"], "--job-id"),
                         ["new-first", "new-second"])
        self.assertFalse(bench.run_command(["python", "workflow_auto.py", "run-job", "--job-id=active", "--allow-upload"]))
        self.assertFalse(bench.run_command(publish_command("new-first")))
        self.assertEqual(len(bench.pending_commands), 2)

    def test_all_duplicate_batch_does_not_enqueue_an_empty_command(self):
        bench = fake_workbench()
        bench.run_command(publish_command("active"))
        bench.run_command(publish_command("pending"))
        counter = bench.command_counter
        self.assertFalse(bench.run_command([
            "python", "workflow_auto.py", "run-jobs", "--job-id=active",
            "--job-id", "pending", "--allow-upload",
        ]))
        self.assertEqual(bench.command_counter, counter)
        self.assertEqual(len(bench.launches), 1)
        self.assertEqual(len(bench.pending_commands), 1)

    def test_callback_error_does_not_strand_next_publication(self):
        bench = fake_workbench()
        bench.run_command(publish_command("first"), after=Mock(side_effect=RuntimeError("refresh callback failed")))
        second = publish_command("second")
        bench.run_command(second)
        bench._finish_command(bench.launches[0]["id"], code=0)
        self.assertEqual(bench.launches[-1]["command"], second)
        self.assertFalse(bench.pending_commands)

    def test_failed_callback_error_does_not_strand_next_publication(self):
        bench = fake_workbench()
        bench.run_command(publish_command("first"), on_failure=Mock(side_effect=RuntimeError("failure cleanup failed")))
        second = publish_command("second")
        bench.run_command(second)
        bench._finish_command(bench.launches[0]["id"], code=1)
        self.assertEqual(bench.launches[-1]["command"], second)
        self.assertFalse(bench.pending_commands)

    def test_status_refresh_error_does_not_strand_next_publication(self):
        bench = fake_workbench()
        bench.run_command(publish_command("first"))
        second = publish_command("second")
        bench.run_command(second)
        bench.refresh_status.side_effect = RuntimeError("state temporarily unavailable")
        bench._finish_command(bench.launches[0]["id"], code=0)
        self.assertEqual(bench.launches[-1]["command"], second)

    def test_start_failure_advances_without_modal_dialog(self):
        bench = fake_workbench()
        failed = Mock()
        bench.run_command(publish_command("first"), on_failure=failed)
        second = publish_command("second")
        bench.run_command(second)
        with patch.object(workflow_app.messagebox, "showerror") as showerror:
            bench._finish_command(bench.launches[0]["id"], error="executable missing")
        failed.assert_called_once()
        showerror.assert_not_called()
        self.assertEqual(bench.launches[-1]["command"], second)

    def test_scheduler_can_skip_busy_lane_and_launch_independent_work(self):
        bench = fake_workbench()
        bench.run_command(publish_command("first"))
        bench.run_command(publish_command("second"))
        bench.run_command(["python", "workflow_app_core.py", "step1", "--project", "one"])
        bench.run_command(["python", "workflow_app_core.py", "step3", "--project", "two"])
        self.assertEqual(len(bench.active_commands), workflow_app.MAX_PARALLEL_COMMANDS)
        running_transcription = bench.active_commands["manual:flow1"]
        bench._finish_command(running_transcription["id"], code=0)
        self.assertEqual(set(bench.active_commands), {"manual:publish", "manual:flow3"})
        self.assertEqual([record["command"] for record in bench.pending_commands], [publish_command("second")])

    def test_paused_monitor_resumes_on_exit_failure_or_start_failure(self):
        for error, code in (("", 1), ("executable missing", None)):
            with self.subTest(error=error, code=code):
                bench = fake_workbench()
                monitor = object()
                bench.monitor_process = monitor
                bench._run_selected_auto_command(
                    ["python", "workflow_auto.py", "--config", "test-auto.json", "doctor"],
                    action_name="环境检查",
                )
                bench._kill_tree.assert_called_once_with(monitor)
                self.assertIsNone(bench.monitor_process)
                with patch.object(workflow_app.messagebox, "showerror"):
                    bench._finish_command(bench.launches[0]["id"], code=code, error=error)
                bench._restart_safe_monitor.assert_called_once_with()

    def test_paused_monitor_resumes_if_success_ui_callback_raises(self):
        for failing_step in ("refresh", "callback"):
            with self.subTest(failing_step=failing_step):
                bench = fake_workbench()
                bench.monitor_process = object()
                callback = Mock()
                if failing_step == "refresh":
                    bench.refresh_auto_status.side_effect = RuntimeError("refresh unavailable")
                else:
                    callback.side_effect = RuntimeError("callback unavailable")
                bench._run_selected_auto_command(
                    ["python", "workflow_auto.py", "doctor"],
                    action_name="environment check", after=callback,
                )
                bench._finish_command(bench.launches[0]["id"], code=0)
                bench._restart_safe_monitor.assert_called_once_with()

    def test_waiting_manual_job_keeps_monitor_priority_marker_between_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            bench = fake_workbench()
            bench.vars["auto_output"] = Mock(get=Mock(return_value=temporary))
            bench.pending_commands.append({
                "id": 2, "flow_key": "manual:publish", "command": publish_command("second")
            })
            workflow_app.Workbench._sync_manual_activity_marker(bench)
            marker = Path(temporary) / workflow_app.auto.MANUAL_ACTIVITY_FILENAME
            self.assertTrue(marker.is_file(), "queued work must retain priority while the previous job finishes")
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["flows"], ["manual:publish"])

    def test_upload_keeps_monitor_running_while_joining_priority_queue(self):
        bench = fake_workbench()
        monitor = object()
        bench.monitor_process = monitor
        bench._run_selected_auto_command(publish_command("first"), action_name="再次投递失败稿件")
        self.assertIs(bench.monitor_process, monitor)
        bench._kill_tree.assert_not_called()
        self.assertIn("已排队", bench.vars["auto_status"].set.call_args.args[0])
        self.assertEqual(bench.launches[0]["label"], "再次投递失败稿件")


if __name__ == "__main__":
    unittest.main()
