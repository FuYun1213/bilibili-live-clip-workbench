"""Publishing UI regression tests: no production state or network calls."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import workflow_app as ui


class PublishRoutingTests(unittest.TestCase):
    def bench(self):
        bench = object.__new__(ui.Workbench)
        bench.profiles = {'creator': {'display_name': '测试主播'}}
        bench.vars = {'status': Mock(), 'auto_status': Mock()}
        bench._review_batch_for_job = Mock(return_value=(Path('review'), ['clip.mp4'], ['测试切片'], 0))
        bench.auto_command = lambda action, extra=None, **kwargs: ['python', 'workflow_auto.py', action, *(extra or [])]
        bench._run_selected_auto_command = Mock()
        return bench

    def test_click_failed_upload_routes_to_publish_not_production_queue(self):
        for action in ('publish', 'replace'):
            with self.subTest(action=action):
                bench = self.bench()
                bench.auto_job_tree = Mock(identify_row=Mock(return_value='row'))
                bench.auto_job_row_to_job = {'row': 'job'}
                bench._publish_auto_job = Mock()
                job = {'id': 'job', 'status': 'failed'}
                with patch.object(ui.auto, 'load_config', return_value={}), patch.object(ui.auto, 'load_state', return_value={'jobs': {'job': job}}), patch.object(ui.auto, 'failed_job_publish_action', return_value=action), patch.object(ui.auto, 'request_failed_job_retry') as requeue:
                    bench.retry_failed_auto_job_on_click(SimpleNamespace(y=1))
                bench._publish_auto_job.assert_called_once_with(job)
                requeue.assert_not_called()

    def test_mixed_batch_queues_each_job_in_selected_order(self):
        bench = self.bench()
        jobs = [
            {'id': 'retry', 'creator': 'creator', 'title': '上传续传', 'project': 'one', 'status': 'failed'},
            {'id': 'replacement', 'creator': 'creator', 'title': '换源修订', 'project': 'two', 'status': 'ready_to_publish', 'manual_republish_required': True},
            {'id': 'new', 'creator': 'creator', 'title': '首次发布', 'project': 'three', 'status': 'awaiting_delivery_review'},
        ]
        with patch.object(ui.auto, 'failed_job_publish_action', return_value='publish'), patch.object(ui.core, 'load_project', return_value=(Path('project.json'), {})), patch.object(ui.auto, 'collaboration_review_summary', return_value={'required': False}), patch.object(ui.messagebox, 'askokcancel', return_value=True) as confirm:
            bench._publish_auto_jobs(jobs)
        calls = bench._run_selected_auto_command.call_args_list
        self.assertEqual([call.args[0][4] for call in calls], ['retry', 'replacement', 'new'])
        self.assertEqual([call.args[0][2] for call in calls], ['run-job', 'replace-published-job', 'run-job'])
        self.assertIn('替换原 BV', confirm.call_args.args[1])
        self.assertNotIn('--allow-upload', calls[1].args[0])
        bench._after_selected_upload = Mock(return_value=False)
        for call in calls:
            call.kwargs['after'](False)
        self.assertEqual([call.args[0] for call in bench._after_selected_upload.call_args_list], ['retry', 'replacement', 'new'])

    def test_cancelled_batch_launches_nothing(self):
        bench = self.bench()
        jobs = [{'id': str(index), 'status': 'ready_to_publish', 'project': 'p'} for index in range(2)]
        with patch.object(ui.core, 'load_project', return_value=(Path('p.json'), {})), patch.object(ui.auto, 'collaboration_review_summary', return_value={'required': False}), patch.object(ui.messagebox, 'askokcancel', return_value=False):
            bench._publish_auto_jobs(jobs)
        bench._run_selected_auto_command.assert_not_called()

    def test_failed_replacement_is_allowed_to_resume(self):
        bench = self.bench()
        job = {'id': 'job', 'status': 'failed', 'manual_republish_required': True}
        with patch.object(ui.auto, 'resumable_replacement_failure', return_value=True), patch.object(ui.messagebox, 'askokcancel', return_value=True):
            bench.replace_selected_published_job(job)
        command = bench._run_selected_auto_command.call_args.args[0]
        self.assertEqual(command[2], 'replace-published-job')

    def test_ready_revision_starts_without_undefined_status(self):
        bench = self.bench()
        bench._run_ready_approved_replacement('job', Path('review'), 'clip.mp4', resume_monitor=False)
        self.assertEqual(bench._run_selected_auto_command.call_args.args[0][2], 'replace-published-job')


if __name__ == '__main__':
    unittest.main()
