"""Offline table/card consistency during local publication queue-lock waits."""
from collections import deque
import copy
import unittest
from unittest.mock import Mock, patch

import workflow_app as ui


def publication(task_id, job_id, *, output='', replacement=False):
    return {
        'id': task_id, 'flow_key': 'manual:publish', 'output_tail': output,
        'command': ['python', 'workflow_auto.py',
                    'replace-published-job' if replacement else 'run-job',
                    '--job-id', job_id,
                    *([] if replacement else ['--allow-burn', '--allow-upload'])],
    }


class PublishPrelockTableTests(unittest.TestCase):
    def setUp(self):
        self.bench = object.__new__(ui.Workbench)
        self.bench.refresh_creator_profiles = Mock()
        self.bench.profiles = {}
        self.bench.monitor_process = None
        self.bench.vars = {'auto_status': Mock()}
        self.bench.active_commands = {}
        self.bench.pending_commands = deque()
        self.bench._render_auto_job_rows = Mock()
        self.bench.append_log = Mock()
        self.bench.auto_job_row_to_job = {'a': 'a', 'b': 'b'}
        self.bench.auto_job_tree = Mock(
            selection=Mock(return_value=('a', 'b')),
            get_children=Mock(return_value=('a', 'b')),
        )
        self.state = {'jobs': {
            job_id: {'id': job_id, 'title': job_id, 'status': 'failed',
                     'publish_failure_action': 'publish', 'progress_percent': 95,
                     'current_stage': '再次投递失败', 'detail': '上次 B站 137022 投稿被限流'}
            for job_id in ('a', 'b')
        }}
        patches = (
            patch.object(ui.auto, 'load_config', return_value={}),
            patch.object(ui.auto, 'load_state', return_value=self.state),
            patch.object(ui.auto, 'manual_work_active', return_value=True),
            patch.object(ui.auto, 'retry_request_pending', return_value=False),
            patch.object(ui.auto, 'save_state', side_effect=AssertionError('display must not write state')),
            patch.object(ui.messagebox, 'showerror', side_effect=AssertionError),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def refresh(self):
        before = copy.deepcopy(self.state)
        selection = self.bench.selected_auto_job_ids()
        self.bench.refresh_auto_status(silent=False)
        self.bench._render_auto_job_rows.assert_called()
        self.assertEqual(self.state, before)
        self.assertEqual(self.bench.selected_auto_job_ids(), selection)
        self.assertEqual([job['status'] for job in self.bench.selected_auto_jobs()], ['failed', 'failed'])
        return {job_id: (values, tag) for job_id, values, tag in self.bench._auto_job_display_rows}

    def test_active_and_pending_prelock_rows_match_cards_without_changing_business_status(self):
        self.bench.active_commands = {'manual:publish': publication(1, 'a', output=(
            '手动操作已进入优先队列；若自动监控正在收尾当前安全阶段，这里会等待它让位。\n'
        ))}
        self.bench.pending_commands.append(publication(2, 'b', replacement=True))
        rows = self.refresh()
        execution = self.bench._execution_snapshot
        for job_id, section in (('a', 'running'), ('b', 'pending')):
            values, tag = rows[job_id]
            self.assertEqual(values[3], execution[section][0]['detail'])
            self.assertEqual(values[5], ui.JOB_STATUS_LABELS['failed'])
            self.assertEqual(tag, 'failed')
            self.assertNotIn('137022', values[-1])
        self.assertIn('状态锁', rows['a'][0][-1])
        self.assertIn('已排队', rows['b'][0][-1])
        self.assertTrue(all(ui.auto.failed_job_publish_action(job) == 'publish' for job in self.state['jobs'].values()))

    def test_new_attempt_failure_is_not_hidden_by_active_command(self):
        self.state['jobs']['a'].update(current_stage='B站限制投稿频率', detail='本次 B站 137022 投稿被限流')
        self.bench.active_commands = {'manual:publish': publication(1, 'a', output=(
            'FLOW5_PROGRESS {"phase":"failed","code":"137022"}\n'
        ))}
        rows = self.refresh()
        self.assertEqual(rows['a'][0][3], 'B站限制投稿频率')
        self.assertEqual(rows['a'][0][-1], '本次 B站 137022 投稿被限流')
        self.assertEqual(rows['a'][0][3], self.bench._execution_snapshot['running'][0]['detail'])

    def test_pending_batch_uses_each_job_card_rule_and_removed_request_restores_history(self):
        request = publication(1, 'a')
        request['command'].extend(['--job-id', 'b'])
        self.bench.pending_commands.append(request)
        rows = self.refresh()
        self.assertEqual(rows['a'][0][3], '等待再次投递')
        self.assertEqual(rows['b'][0][3], '等待再次投递')
        self.assertEqual(self.bench._execution_snapshot['pending'][0]['detail'], '等待再次投递')
        self.bench.pending_commands.clear()
        rows = self.refresh()
        self.assertEqual(rows['a'][0][3], '再次投递失败')
        self.assertEqual(rows['a'][0][-1], '上次 B站 137022 投稿被限流')

    def test_media_only_pending_command_retains_its_actual_failure(self):
        request = publication(1, 'a')
        request['command'].remove('--allow-upload')
        self.bench.pending_commands.append(request)
        rows = self.refresh()
        self.assertEqual(rows['a'][0][3], '再次投递失败')
        self.assertEqual(rows['a'][0][-1], '上次 B站 137022 投稿被限流')
        self.assertEqual(self.bench._execution_snapshot['pending'][0]['detail'], '再次投递失败')


if __name__ == '__main__':
    unittest.main()
