"""Offline regression coverage for server 601/137022 and cross-task retry cooldown."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import biliup_publish as publish
import workflow_app_core as core


class Flow5RateLimitTests(unittest.TestCase):
    def make_item(self, root: Path, clip_id: str = "003") -> dict:
        root.mkdir(parents=True, exist_ok=True)
        video = root / f"{clip_id}.mp4"
        video.write_bytes(b"offline fixture")
        return {
            "clip_id": clip_id, "video": str(video), "title": f"测试稿件 {clip_id}",
            "collection": "测试合集", "section_id": 123,
        }

    @contextlib.contextmanager
    def offline_uploads(self, *, existing=None):
        with patch.object(
            publish, "find_existing_bvid_by_title", side_effect=existing,
            return_value=None,
        ), patch.object(
            publish, "build_upload_command", return_value=["offline-biliup"],
        ), patch.object(publish, "ensure_collection_membership") as collection:
            yield collection

    def execute(self, root, items, runner, clock, waits, throttle):
        def sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds
        return publish.execute_uploads(
            "offline-biliup", root / "unused-cookies.json", items,
            limit=3, line="auto", cooldown=0,
            collection_cookie_path=root / "unused-web-cookies.json",
            run=runner, flow5_throttle_path=throttle,
            sleep=sleep, now=lambda: clock[0],
        )

    def test_rate_limit_classifies_actual_biliup_formats_only(self):
        for text in (
            "upload rate limit (code: 601): 您上传视频过快，请您稍作休息后再继续",
            '{"code":601,"message":"slow down"}',
            "您上传视频过快，请您稍作休息后再继续",
            'ResponseData { code: 137022, data: None, message: "投稿过于频繁，请稍后再试" }',
            '投稿过于频繁，请稍后再试',
        ):
            with self.subTest(text=text):
                self.assertTrue(publish.is_upload_rate_limited(text))
        self.assertFalse(publish.is_upload_rate_limited("process exited 1"))
        self.assertFalse(publish.is_upload_rate_limited('{"code":6010}'))
        self.assertFalse(publish.is_upload_rate_limited('{"code":1370220}'))
        self.assertEqual(publish.upload_rate_limit_code('ResponseData { code: 137022 }'), "137022")

    def test_rate_limit_waits_ten_minutes_before_retrying_same_clip(self):
        self.check_rate_limit_wait("601")

    def test_submission_137022_waits_ten_minutes_before_retrying_same_clip(self):
        self.check_rate_limit_wait("137022")

    def check_rate_limit_wait(self, code):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = self.make_item(root / "delivery")
            clock, waits, calls = [1000], [], []
            throttle = root / "shared-throttle.json"
            publish.record_flow5_upload_success(
                throttle, clip_id="old", bvid="BV1old", now=lambda: 990,
            )
            def run(*args, **kwargs):
                calls.append(clock[0])
                if len(calls) == 1:
                    return SimpleNamespace(returncode=1, stdout="", stderr=f"ResponseData {{ code: {code} }}")
                return SimpleNamespace(returncode=0, stdout="BV1G2b26bE4P", stderr="")
            with self.offline_uploads(), contextlib.redirect_stdout(io.StringIO()):
                result = self.execute(root, [item], run, clock, waits, throttle)
            self.assertEqual(result, 0)
            self.assertEqual(len(calls), 2)
            self.assertGreaterEqual(calls[1] - calls[0], 600)
            self.assertEqual(waits, [2, 600])
            saved = json.loads(throttle.read_text(encoding="utf-8"))
            self.assertEqual(saved["completed_in_window"], 1)
            self.assertEqual(saved["cooldown_until"], 0)
            receipt = json.loads((root / "delivery" / "publish-receipts.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["items"]["003"]["bvid"], "BV1G2b26bE4P")

    def test_exhausted_rate_limit_persists_cooldown_for_next_task(self):
        self.check_exhausted_rate_limit("601")

    def test_exhausted_137022_persists_cooldown_for_next_task(self):
        self.check_exhausted_rate_limit("137022")

    def check_exhausted_rate_limit(self, code):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_item(root / "first")
            next_item = self.make_item(root / "next", "004")
            clock, waits, calls = [1000], [], []
            throttle = root / "shared-throttle.json"
            def fail(*args, **kwargs):
                calls.append(clock[0])
                return SimpleNamespace(returncode=1, stdout="", stderr=f"ResponseData {{ code: {code} }}")
            messages = io.StringIO()
            with self.offline_uploads() as membership, contextlib.redirect_stdout(messages), contextlib.redirect_stderr(messages):
                result = self.execute(root, [first], fail, clock, waits, throttle)
                membership.assert_not_called()
            self.assertEqual(result, 1)
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(b - a >= 600 for a, b in zip(calls, calls[1:])))
            self.assertIn(f"Flow 5 投稿受限（B站 {code}）", messages.getvalue())
            self.assertNotIn(10, waits)
            self.assertFalse((root / "first" / "publish-receipts.json").exists())
            saved = json.loads(throttle.read_text(encoding="utf-8"))
            self.assertEqual(saved["completed_in_window"], 0)
            self.assertEqual(saved["last_rate_limit_code"], code)
            cooldown_until = saved["cooldown_until"]
            self.assertEqual(cooldown_until, clock[0] + 2400)
            def succeed(*args, **kwargs):
                calls.append(clock[0])
                return SimpleNamespace(returncode=0, stdout="BV1G2b26bE4P", stderr="")
            with self.offline_uploads(), contextlib.redirect_stdout(io.StringIO()):
                result = self.execute(root, [next_item], succeed, clock, waits, throttle)
            self.assertEqual(result, 0)
            self.assertGreaterEqual(calls[-1], cooldown_until)

    def test_remote_recovery_does_not_retry_an_accepted_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = self.make_item(root / "delivery")
            runner = Mock(return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="upload rate limit (code: 601)",
            ))
            clock, waits = [1000], []
            throttle = root / "shared-throttle.json"
            with self.offline_uploads(existing=[None, "BV1G2b26bE4P"]), contextlib.redirect_stdout(io.StringIO()):
                result = self.execute(root, [item], runner, clock, waits, throttle)
            self.assertEqual(result, 0)
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(waits, [2])
            self.assertEqual(json.loads(throttle.read_text(encoding="utf-8"))["cooldown_until"], 0)

    def test_rate_limit_is_persisted_if_receipt_recovery_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = self.make_item(root / "delivery")
            runner = Mock(return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="ResponseData { code: 137022 }",
            ))
            clock, waits = [1000], []
            throttle = root / "shared-throttle.json"
            with self.offline_uploads(existing=[None, RuntimeError("archive manager unavailable")]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "archive manager unavailable"):
                    self.execute(root, [item], runner, clock, waits, throttle)
            self.assertEqual(runner.call_count, 1)
            self.assertFalse((root / "delivery" / "publish-receipts.json").exists())
            saved = json.loads(throttle.read_text(encoding="utf-8"))
            self.assertEqual(saved["cooldown_reason"], "rate_limit_137022")
            self.assertEqual(saved["cooldown_until"], clock[0] + 600)

    def test_generic_process_failure_keeps_existing_short_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = self.make_item(root / "delivery")
            runner = Mock(side_effect=[
                SimpleNamespace(returncode=1, stdout="", stderr="temporary process error"),
                SimpleNamespace(returncode=0, stdout="BV1G2b26bE4P", stderr=""),
            ])
            clock, waits = [1000], []
            with self.offline_uploads(), contextlib.redirect_stdout(io.StringIO()):
                result = self.execute(root, [item], runner, clock, waits, root / "throttle.json")
            self.assertEqual(result, 0)
            self.assertEqual(waits, [2, 10])

    def test_failure_reason_reaches_workflow_error_and_log(self):
        self.check_failure_reason("601")

    def test_137022_reason_reaches_workflow_error_and_log(self):
        self.check_failure_reason("137022")

    def check_failure_reason(self, code):
        reason = f"Flow 5 投稿受限（B站 {code}）：{publish.FLOW5_RATE_LIMIT_MESSAGES[code]}；本次自动重试已用完。"
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            with self.assertRaises(core.WorkflowError) as caught:
                core.run_external(
                    root, "flow5-publish-execute",
                    [sys.executable, "-c", f"import sys; print({reason!r}); sys.exit(1)"],
                )
            self.assertIn(reason, str(caught.exception))
            log = next((root / "logs").glob("*-flow5-publish-execute.log"))
            self.assertIn(reason, log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
