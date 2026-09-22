"""Offline process-level coverage for publication locks and persistent backoff."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import biliup_publish as publish
import publish_diagnostics as diagnostics


SCRIPTS = Path(__file__).resolve().parent


class PublishConcurrencyTests(unittest.TestCase):
    def child(self, source, *args):
        return subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", source, str(SCRIPTS), *map(str, args)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )

    def finish(self, processes):
        results = []
        try:
            for process in processes:
                out, err = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, out + err)
                results.append(out)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        return results

    def test_wait_reloads_extension_without_overwriting_newer_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "throttle.json"
            clock, waits = [1000], []
            publish.record_flow5_upload_rate_limit(path, clip_id="first", now=lambda: clock[0])

            def sleep(seconds):
                waits.append(seconds)
                if len(waits) == 1:
                    publish.record_flow5_upload_rate_limit(path, clip_id="later", code="137022", now=lambda: 1500)
                clock[0] += seconds

            with contextlib.redirect_stdout(io.StringIO()):
                state = publish.wait_for_flow5_upload_slot(path, sleep=sleep, now=lambda: clock[0])
            self.assertEqual(waits, [600, 1100])
            self.assertEqual(clock[0], 2700)
            self.assertEqual(state["last_rate_limited_clip_id"], "later")
            self.assertEqual(state["consecutive_rate_limits"], 2)
            self.assertEqual(state["cooldown_until"], 0)

    def test_early_sleep_return_cannot_skip_remaining_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "throttle.json"
            clock, waits = [1000], []
            publish.record_flow5_upload_rate_limit(path, clip_id="first", now=lambda: clock[0])

            def sleep(seconds):
                waits.append(seconds)
                clock[0] += min(seconds, 200)

            with contextlib.redirect_stdout(io.StringIO()):
                publish.wait_for_flow5_upload_slot(path, sleep=sleep, now=lambda: clock[0])
            self.assertEqual(waits, [600, 400, 200])
            self.assertEqual(clock[0], 1600)

    def test_backoff_persists_across_tasks_caps_and_resets_after_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "throttle.json"
            clock = 1000
            for index, expected in enumerate([600, 1200, 2400, 3600, 3600]):
                state = publish.record_flow5_upload_rate_limit(path, clip_id=str(index), now=lambda: clock)
                self.assertEqual(state["rate_limit_backoff_seconds"], expected)
                self.assertEqual(state["consecutive_rate_limits"], index + 1)
                clock = state["cooldown_until"] + 1
            publish.record_flow5_upload_success(path, clip_id="ok", bvid="BV1G2b26bE4P", now=lambda: clock)
            state = publish.record_flow5_upload_rate_limit(path, clip_id="new", now=lambda: clock + 1)
            self.assertEqual(state["rate_limit_backoff_seconds"], 600)
            self.assertEqual(state["consecutive_rate_limits"], 1)

    def test_late_success_preserves_cooldown_and_records_accepted_bv(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "throttle.json"
            rejected = publish.record_flow5_upload_rate_limit(path, clip_id="limited", now=lambda: 1000)
            state = publish.record_flow5_upload_success(path, clip_id="inflight", bvid="BV1G2b26bE4P", now=lambda: 1005)
            self.assertEqual(state["cooldown_until"], rejected["cooldown_until"])
            self.assertEqual(state["completed_in_window"], 1)
            self.assertEqual(state["last_bvid"], "BV1G2b26bE4P")

    def test_multiprocess_success_updates_are_not_lost(self):
        source = '''
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import biliup_publish as p
path=Path(sys.argv[2]); ident=sys.argv[3]
original=p._load_flow5_throttle
def delayed(path):
    value=original(path);time.sleep(0.01);return value
p._load_flow5_throttle=delayed
for number in range(4):
    p.record_flow5_upload_success(path,clip_id=ident,bvid='BV'+ident,now=lambda:1000)
'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "throttle.json"
            self.finish([self.child(source, path, index) for index in range(4)])
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["completed_in_window"], 16)
            self.assertEqual(state["cooldown_until"], 1600)

    def test_two_publishers_recheck_receipt_inside_lock_and_upload_once(self):
        source = '''
import sys,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
import biliup_publish as p
root=Path(sys.argv[2])
item={'clip_id':'003','title':'same','video':str(root/'003.mp4'),'collection':'test','section_id':1}
p.find_existing_bvid_by_title=lambda *args: None
p.ensure_collection_membership=lambda *args: None
p.build_upload_command=lambda *args: ['offline']
def run(*args,**kwargs):
    with (root/'calls.txt').open('a') as output: output.write('UPLOAD\\n')
    time.sleep(0.4)
    return SimpleNamespace(returncode=0,stdout='BV1G2b26bE4P',stderr='')
p.execute_uploads('offline',root/'cookies.json',[item],3,'',0,collection_cookie_path=root/'cookies.json',run=run)
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "003.mp4").write_bytes(b"offline")
            outputs = self.finish([self.child(source, root) for _ in range(2)])
            self.assertEqual((root / "calls.txt").read_text().splitlines(), ["UPLOAD"])
            events = [diagnostics.parse_progress_line(line) for out in outputs for line in out.splitlines()]
            self.assertTrue(any(event and event["phase"] == "waiting_lock" for event in events))
            receipt = json.loads((root / "publish-receipts.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["items"]["003"]["bvid"], "BV1G2b26bE4P")

    def test_running_diagnostics_keeps_waiting_lock_detail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "logs").mkdir()
            detail = "另一项任务正在投稿；当前任务等待共享发布锁。"
            log = root / "logs" / "current-flow5-publish-execute.log"
            log.write_text(diagnostics.PROGRESS_PREFIX + json.dumps({
                "phase": "waiting_lock", "detail": detail,
            }), encoding="utf-8")
            result = diagnostics.describe_publish_job({"project": str(root), "status": "running"})
            self.assertEqual(result["phase"], "waiting_lock")
            self.assertEqual(result["detail"], detail)

    def test_os_releases_lock_when_owner_process_is_terminated(self):
        owner = '''
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from process_file_lock import exclusive_file_lock
with exclusive_file_lock(Path(sys.argv[2])):
    print('LOCKED',flush=True)
    time.sleep(30)
'''
        challenger = owner.replace("time.sleep(30)", "pass")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "owner.lock"
            process = self.child(owner, path)
            try:
                self.assertEqual(process.stdout.readline().strip(), "LOCKED")
            finally:
                process.terminate()
                process.communicate(timeout=5)
            self.finish([self.child(challenger, path)])


if __name__ == "__main__":
    unittest.main()
