"""Offline regression tests for visible Flow 5 state and native error details."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import publish_diagnostics as diagnostics
import workflow_app_core as core
import workflow_auto as auto


class PublishDiagnosticsTests(unittest.TestCase):
    def log(self, project, name, text, timestamp):
        path = project / "logs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.utime(path, (timestamp, timestamp))
        return path

    def test_legacy_failed_job_exposes_137022_without_rewriting_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self.log(project, "20260907-154343-flow5-publish-execute.log",
                     'RuntimeError: Unknown Error\nResponseData { code: 137022, data: None, message: "投稿过于频繁，请稍后再试" }', 1000)
            job = {"status": "failed", "project": str(project), "progress_percent": 100,
                   "current_stage": "再次投递失败", "detail": "flow5-publish-execute 失败，退出码 1"}
            before = copy.deepcopy(job)
            snapshot = auto.job_progress_snapshot(job)
            self.assertEqual(snapshot["publish_error_code"], "137022")
            self.assertIn("投稿过于频繁", snapshot["detail"])
            self.assertEqual(snapshot["current_stage"], "B站限制投稿频率")
            self.assertEqual(job, before)

    def test_current_retry_does_not_display_previous_failed_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self.log(project, "20260907-120000-flow5-publish-execute.log",
                     'ResponseData { code: 601, message: "您上传视频过快" }', 1000)
            newest = self.log(project, "20260907-160000-flow5-publish-execute.log",
                             '[1/1] 正在投稿 003：新的尝试', 2000)
            job = {"status": "running", "project": str(project), "progress_percent": 100,
                   "current_stage": "执行失败", "detail": "复用已有成片继续投递"}
            snapshot = auto.job_progress_snapshot(job)
            self.assertEqual(snapshot["publish_phase"], "uploading")
            self.assertEqual(snapshot["publish_error_code"], "")
            self.assertNotIn("失败", snapshot["current_stage"])
            self.assertIn(str(newest), snapshot["detail"])
            self.assertEqual(snapshot["progress_percent"], 99)

    def test_current_cooldown_exposes_deadline_and_platform_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            event = {"phase": "cooldown", "detail": "B站限制投稿频率，冷却后继续", "code": "137022", "cooldown_until": 1600}
            self.log(project, "20260907-160000-flow5-publish-execute.log",
                     diagnostics.PROGRESS_PREFIX + json.dumps(event, ensure_ascii=False), 1000)
            snapshot = auto.job_progress_snapshot({
                "status": "running", "project": str(project), "progress_percent": 90,
                "current_stage": "失败投稿再次投递", "detail": "旧详情",
            })
            self.assertEqual(snapshot["publish_phase"], "cooldown")
            self.assertEqual(snapshot["publish_cooldown_until"], 1600)
            self.assertEqual(snapshot["publish_error_code"], "137022")
            self.assertIn("正在冷却", snapshot["current_stage"])

    def test_new_attempt_ignores_old_cooldown_before_its_log_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self.log(project, "20260907-150000-flow5-publish-execute.log",
                     'B站限制投稿频率（137022），进入 10 分钟冷却；剩余 10:00，结束后自动继续。', 1000)
            (project / "workflow-project.json").write_text(json.dumps({
                "publish_attempts": [{"started_at": "1970-01-01T00:33:20+00:00", "status": "running"}],
            }), encoding="utf-8")
            result = diagnostics.describe_publish_job({"status": "running", "project": str(project)})
            self.assertEqual(result["phase"], "uploading")
            self.assertEqual(result["code"], "")
            self.assertEqual(result["cooldown_until"], 0)
            self.assertEqual(result["log_path"], "")

    def test_real_current_attempt_cooldown_survives_later_job_updated_at(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self.log(project, "20260907-160000-flow5-publish-execute.log",
                     'B站限制投稿频率（137022），进入 10 分钟冷却；剩余 10:00，结束后自动继续。', 1100)
            (project / "workflow-project.json").write_text(json.dumps({
                "publish_attempts": [{"started_at": "1970-01-01T00:16:40+00:00", "status": "running"}],
            }), encoding="utf-8")
            result = diagnostics.describe_publish_job({
                "status": "running", "project": str(project), "updated_at": "1970-01-01T00:33:20+00:00",
            })
            self.assertEqual(result["phase"], "cooldown")
            self.assertEqual(result["code"], "137022")
            self.assertEqual(result["cooldown_until"], 1700)

    def test_current_legacy_cooldown_is_recognized_without_invented_deadline(self):
        event = diagnostics.parse_progress_line('B站限制投稿频率（137022），进入 10 分钟冷却；剩余 10:00，结束后自动继续。')
        self.assertEqual(event["phase"], "cooldown")
        self.assertEqual(event["code"], "137022")
        self.assertEqual(event["cooldown_until"], 0)

    def test_legacy_cooldown_deadline_uses_actual_log_write_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self.log(project, "20260907-160000-flow5-publish-execute.log",
                     'B站限制投稿频率（137022），进入 10 分钟冷却；剩余 10:00，结束后自动继续。', 1000)
            with patch.object(diagnostics.time, "time", return_value=1120):
                result = diagnostics.describe_publish_job({"status": "running", "project": str(project)})
            self.assertEqual(result["cooldown_until"], 1600)
            self.assertIn("剩余 08:00", result["detail"])

    def test_new_preview_error_does_not_reuse_previous_upload_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self.log(project, "20260907-150000-flow5-publish-execute.log",
                     'ResponseData { code: 137022, message: "投稿过于频繁" }', 1000)
            preview = self.log(project, "20260907-160000-flow5-publish-preview.log",
                               'ValueError: cover image missing', 2000)
            result = diagnostics.describe_publish_job({
                "status": "failed", "project": str(project),
                "detail": f"flow5-publish-preview 失败，退出码 1。日志：{preview}",
            })
            self.assertEqual(result["phase"], "failed")
            self.assertEqual(result["log_path"], str(preview))
            self.assertIn("cover image missing", result["detail"])
            self.assertEqual(result["code"], "")

    def test_latest_error_replaces_recovered_earlier_rate_limit(self):
        output = 'ResponseData { code: 137022, message: "投稿过于频繁" }\nResponseData { code: 21032, message: "分区无效" }'
        summary = diagnostics.publish_error_summary(output)
        self.assertEqual(summary["code"], "21032")
        self.assertIn("分区无效", summary["detail"])
        self.assertNotIn("137022", summary["detail"])

    def test_json_api_error_is_not_hidden_by_unknown_error(self):
        output = ('RuntimeError: Unknown Error\n'
                  '{"code":-400,"data":null,"message":"请求错误","ttl":1}')
        summary = diagnostics.publish_error_summary(output)
        self.assertEqual(summary["code"], "-400")
        self.assertEqual(summary["detail"], "B站返回 -400：请求错误")

    def test_latest_json_error_replaces_earlier_rate_limit(self):
        output = ('ResponseData { code: 137022, message: "投稿过于频繁" }\n'
                  'RuntimeError: Unknown Error\n'
                  '{"code":-400,"data":null,"message":"请求错误","ttl":1}')
        summary = diagnostics.publish_error_summary(output)
        self.assertEqual(summary["code"], "-400")
        self.assertNotIn("137022", summary["detail"])

    def test_exception_details_are_redacted(self):
        result = diagnostics.publish_error_summary('RuntimeError: missing config cookie=secret-token')
        self.assertIn("missing config", result["detail"])
        self.assertNotIn("secret-token", result["detail"])

    def test_native_failure_is_in_external_error_without_custom_marker(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(core.WorkflowError) as caught:
                core.run_external(Path(temporary), "flow5-publish-execute", [
                    sys.executable, "-c",
                    'import sys; print(\'ResponseData { code: 137022, data: None, message: "投稿过于频繁，请稍后再试" }\'); print("RuntimeError: Unknown Error"); sys.exit(1)',
                ])
            self.assertIn("137022", str(caught.exception))
            self.assertIn("投稿过于频繁", str(caught.exception))

    def test_retry_stage_can_change_despite_previous_hundred_percent(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {"id": "retry", "status": "running", "progress_percent": 100, "current_stage": "执行失败"}
            config = {"_state_file": Path(temporary) / "state.json"}
            state = auto.empty_state()
            state["jobs"]["retry"] = job
            auto._set_job_progress(config, state, job, 90, "失败投稿再次投递", force_stage=True)
            self.assertEqual(job["current_stage"], "失败投稿再次投递")
            self.assertEqual(job["progress_percent"], 100)

    def check_upload_running(self, replace=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            delivery = root / "delivery"
            delivery.mkdir()
            path = root / "workflow-project.json"
            state = {"config": {"publish": {}}, "delivery_dir": str(delivery),
                     "publish_preview_digest": "digest", "replacement_preview_digest": "digest",
                     "steps": {"flow5": {"status": "failed", "detail": "旧的退出码 1"}}}
            observed = []
            def external(_root, _stage, _command, *, on_output):
                started = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(started["steps"]["flow5"]["status"], "running")
                self.assertNotIn("旧的", started["steps"]["flow5"]["detail"])
                event = {"phase": "cooldown", "detail": "B站限制投稿频率，等待冷却", "code": "137022", "cooldown_until": 1600}
                on_output(diagnostics.PROGRESS_PREFIX + json.dumps(event, ensure_ascii=False))
                saved = json.loads(path.read_text(encoding="utf-8"))["steps"]["flow5"]
                self.assertEqual(saved["status"], "running")
                self.assertEqual(saved["phase"], "cooldown")
                self.assertEqual(saved["cooldown_until"], 1600)
                observed.append(saved)
            with patch.object(core, "ensure_profile_publish_enabled"), patch.object(core, "digest_paths", return_value="digest"), patch.object(core, "expected_confirmation", return_value="发布 1 条"), patch.object(core, "expected_replacement_confirmation", return_value="替换 1 条"), patch.object(core, "replacement_targets_path", return_value=root / "targets.json"), patch.object(core, "publish_command", return_value=["offline"]), patch.object(core, "replacement_publish_command", return_value=["offline"]), patch.object(core, "run_external", side_effect=external):
                if replace:
                    core.step5_replace_upload(path, state, "替换 1 条")
                else:
                    core.step5_upload(path, state, "发布 1 条")
            self.assertEqual(len(observed), 1)
            final = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(final["steps"]["flow5"]["status"], "published")
            self.assertNotIn("phase", final["steps"]["flow5"])

    def test_upload_marks_running_before_child_and_persists_cooldown(self):
        self.check_upload_running()

    def test_replacement_marks_running_before_child_and_persists_cooldown(self):
        self.check_upload_running(replace=True)


if __name__ == "__main__":
    unittest.main()
