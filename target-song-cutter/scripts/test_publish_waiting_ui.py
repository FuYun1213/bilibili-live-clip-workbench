"""Offline UI regressions for shared publishing waits; no real submissions."""
from __future__ import annotations

from collections import deque
import copy
import json
from pathlib import Path
import tempfile
import unittest

import publish_diagnostics as diagnostics
import workflow_app as ui
from workflow_workbench_ui import build_execution_snapshot


class PublishWaitingUiTests(unittest.TestCase):
    def snapshot(self, phase, code, detail):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project = Path(temporary.name)
        logs = project / 'logs'
        logs.mkdir()
        event = {'phase': phase, 'code': code, 'detail': detail, 'cooldown_until': 0}
        (logs / '20260907-160000-flow5-publish-execute.log').write_text(
            diagnostics.PROGRESS_PREFIX + json.dumps(event, ensure_ascii=False), encoding='utf-8')
        job = {'id': 'chilly', 'project': str(project), 'status': 'running',
               'title': '昼夜测试任务', 'current_stage': '失败投稿再次投递',
               'detail': '复用已有成片', 'progress_percent': 100}
        before = copy.deepcopy(job)
        enriched = ui.auto_jobs_with_progress([job])[0]
        self.assertEqual(job, before)
        self.assertEqual(enriched['status'], 'running')
        self.assertLess(enriched['progress_percent'], 100)
        return enriched

    def test_shared_lock_wait_visible_in_task_table_and_execution_panel(self):
        job = self.snapshot('waiting_lock', '', '当前任务等待共享发布锁，结束后按队列继续。')
        self.assertEqual(job['publish_phase'], 'waiting_lock')
        self.assertEqual(job['current_stage'], '等待前一条投稿完成')
        self.assertIn('共享发布锁', job['detail'])
        active = {'manual:flow5': {'id': 1, 'flow_key': 'manual:flow5',
            'command': ['python', 'workflow_auto.py', 'run-job', '--job-id', 'chilly']}}
        execution = build_execution_snapshot(active, deque(), [job])
        self.assertEqual(len(execution['running']), 1)
        self.assertEqual(execution['running'][0]['detail'], '等待前一条投稿完成')

    def test_local_batch_spacing_does_not_claim_platform_rejection(self):
        job = self.snapshot('cooldown', '', 'Flow 5 已连续投稿 5 条，等待本地冷却。')
        self.assertEqual(job['current_stage'], '投稿间隔冷却，等待继续')
        self.assertEqual(job['publish_error_code'], '')
        self.assertNotIn('B站限制', job['current_stage'])

    def test_platform_cooldown_retains_real_error_code(self):
        job = self.snapshot('cooldown', '137022', 'B站限制投稿频率，等待本地冷却后重试。')
        self.assertEqual(job['current_stage'], 'B站限制投稿频率，正在冷却')
        self.assertEqual(job['publish_error_code'], '137022')
        self.assertIn('137022', str({'code': job['publish_error_code']}))


if __name__ == '__main__':
    unittest.main()
