from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workflow_auto as auto
import workflow_app_core as core


class PublishRetryQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        self.review = self.project / 'review'
        self.delivery = self.project / 'delivery'
        self.review.mkdir(parents=True)
        self.delivery.mkdir()
        video = self.review / '001-topic.mp4'
        video.write_bytes(b'video')
        core.review_workspace.set_decision(self.review, video.name, 'approved')
        (self.delivery / 'titles-and-covers.csv').write_text(
            'clip_id,video,title\n001,001-topic.mp4,topic\n', encoding='utf-8-sig'
        )
        self.project_state = {
            'schema_version': core.SCHEMA_VERSION,
            'config': {'publish': {}},
            'steps': {'flow4': {'status': 'completed'}},
            'approvals': {},
            'active_review_dir': str(self.review),
            'delivery_dir': str(self.delivery),
        }
        self.project_path = self.project / core.PROJECT_FILENAME
        core.atomic_json(self.project_path, self.project_state)
        self.config = {'_output_root': self.root, '_state_file': self.root / 'queue.json'}
        self.job = {
            'id': 'upload', 'creator': 'chilly', 'status': 'failed',
            'project': str(self.project), 'attempts': 1,
            'detail': 'flow5-publish-execute failed',
        }
        self.state = auto.empty_state()
        self.state['jobs'][self.job['id']] = self.job
        auto.save_state(self.config, self.state)

    def test_publish_failure_cannot_be_requested_as_production_retry(self):
        with self.assertRaisesRegex(auto.AutomationError, '重新发布'):
            auto.request_failed_job_retry(self.config, self.job['id'])
        self.assertFalse(auto.retry_request_pending(self.config, self.job['id']))

    def test_stale_generic_retry_request_keeps_publish_failure_visible(self):
        request = auto.retry_request_path(self.config, self.job['id'])
        core.atomic_json(request, {'job_id': self.job['id'], 'requested_action': 'requeue_failed_job'})
        self.assertEqual(auto.apply_retry_requests(self.config, self.state), [])
        self.assertEqual(self.job['status'], 'failed')
        self.assertEqual(self.job['attempts'], 1)
        self.assertFalse(request.exists())

    def test_safe_queue_never_runs_failed_upload_as_media_production(self):
        self.assertFalse(auto.job_is_runnable(
            self.job, allow_burn=True, allow_upload=False, current_time=100
        ))
        with patch.object(auto, 'materialize_session') as media:
            auto.process_job(self.config, self.state, self.job, allow_burn=True, allow_upload=False)
        media.assert_not_called()
        self.assertEqual(self.job['status'], 'failed')

    def test_upload_authorized_queue_retries_only_existing_delivery(self):
        self.assertTrue(auto.job_is_runnable(
            self.job, allow_burn=True, allow_upload=True, current_time=100
        ))
        with (
            patch.object(core, 'step5_preview') as preview,
            patch.object(core, 'expected_confirmation', return_value='confirmed'),
            patch.object(core, 'step5_upload') as upload,
            patch.object(auto, 'materialize_session') as media,
        ):
            auto.process_job(self.config, self.state, self.job, allow_burn=True, allow_upload=True)
        preview.assert_called_once()
        upload.assert_called_once()
        media.assert_not_called()
        self.assertEqual(self.job['status'], 'published')
        self.assertEqual(auto.load_state(self.config)['jobs']['upload']['status'], 'published')

    def test_interrupted_publish_retry_recovers_as_upload_failure(self):
        def upload(_path, _project, _confirmation):
            recovered = auto.load_state(self.config, recover_running=True)['jobs']['upload']
            self.assertEqual(recovered['status'], 'failed')
            self.assertEqual(auto.failed_job_publish_action(recovered), 'publish')

        with (
            patch.object(core, 'step5_preview'),
            patch.object(core, 'expected_confirmation', return_value='confirmed'),
            patch.object(core, 'step5_upload', side_effect=upload),
        ):
            auto.retry_publish_job(self.config, self.state, self.job)
        self.assertNotIn('resume_status', self.job)

    def test_resume_paused_publish_failure_keeps_publish_recovery_path(self):
        auto.pause_queue_job(self.config, self.state, self.job)
        auto.resume_queue_job(self.config, self.state, self.job)
        self.assertEqual(self.job['status'], 'failed')
        self.assertEqual(auto.failed_job_publish_action(self.job), 'publish')

    def test_replacement_failure_is_saved_and_retried_without_new_upload_or_burn(self):
        self.job.update(status='ready_to_publish', manual_republish_required=True)
        self.job.pop('detail')
        with (
            patch.object(core, 'step5_replace_preview', return_value='replace confirmed'),
            patch.object(core, 'step5_replace_upload', side_effect=[core.WorkflowError('network failed'), None]) as replacement,
            patch.object(core, 'step5_upload') as new_upload,
            patch.object(auto, 'materialize_session') as media,
        ):
            with self.assertRaisesRegex(core.WorkflowError, 'network failed'):
                auto.replace_published_job(self.config, self.state, self.job)
            saved = auto.load_state(self.config)['jobs']['upload']
            self.assertEqual(saved['status'], 'failed')
            self.assertEqual(saved['publish_failure_action'], 'replace')
            self.assertTrue(saved['manual_republish_required'])
            self.assertTrue(auto.resumable_replacement_failure(saved))
            self.assertFalse(auto.resumable_publish_failure(saved))
            self.assertFalse(auto.job_is_runnable(
                saved, allow_burn=True, allow_upload=True, current_time=100
            ))
            auto.run_selected_job(self.config, self.state, self.job, allow_burn=True, allow_upload=True)
        self.assertEqual(replacement.call_count, 2)
        new_upload.assert_not_called()
        media.assert_not_called()
        self.assertEqual(self.job['status'], 'published')
        self.assertNotIn('manual_republish_required', self.job)
        self.assertNotIn('publish_failure_action', self.job)

    def test_selected_mixed_batch_keeps_replacement_route_and_continues_after_failure(self):
        revision = dict(self.job, id='revision', status='ready_to_publish', manual_republish_required=True)
        self.state['jobs']['revision'] = revision
        calls = []

        def replace(_config, _state, job):
            calls.append(('replace', job['id']))
            self.assertTrue(job['manual_republish_required'])
            raise core.WorkflowError('replacement failed')

        def publish(_config, _state, job):
            calls.append(('publish', job['id']))
            job['status'] = 'published'

        with (
            patch.object(auto, 'replace_published_job', side_effect=replace),
            patch.object(auto, 'retry_publish_job', side_effect=publish),
            patch.object(auto, 'process_job') as media,
        ):
            report = auto.run_selected_jobs(
                self.config, self.state, ['revision', 'upload', 'upload'],
                allow_burn=True, allow_upload=True,
            )
        self.assertEqual(calls, [('replace', 'revision'), ('publish', 'upload')])
        self.assertEqual(report['completed'], ['upload'])
        self.assertEqual(len(report['failures']), 1)
        media.assert_not_called()

    def test_reviewed_revision_routes_to_original_after_media_preparation(self):
        self.project_state.update(
            selection_file='existing-selection.json',
            steps={f'flow{i}': {'status': 'completed'} for i in range(1, 5)},
        )
        for automatic in (True, False):
            with self.subTest(automatic=automatic):
                self.job.update(status='awaiting_delivery_review', mode='talk')
                self.job.pop('auto_replace_revision', None)
                self.job.pop('manual_republish_required', None)
                self.job['auto_replace_revision' if automatic else 'manual_republish_required'] = True

                def replaced(_config, _state, target):
                    self.assertEqual(target['status'], 'ready_to_publish')
                    target['status'] = 'published'

                with (
                    patch.object(auto, 'prepare_job_campaign_scope', return_value=True),
                    patch.object(auto, 'ensure_heavy_job_disk_space'),
                    patch.object(auto, 'materialize_session', return_value=(self.root / 'source.mp4', None)),
                    patch.object(auto, '_project_state', return_value=self.project_state),
                    patch.object(auto, '_identity_mismatches', return_value=[]),
                    patch.object(auto, 'sync_job_campaign_to_project'),
                    patch.object(auto, 'mark_flow0_complete', return_value='existing material'),
                    patch.object(auto, 'finalize_empty_completed_review', return_value=False),
                    patch.object(auto, 'unattended_review_ready'),
                    patch.object(core, 'run_stage'),
                    patch.object(auto, 'replace_published_job', side_effect=replaced) as replacement,
                    patch.object(core, 'step5_upload') as new_upload,
                ):
                    auto.process_job(self.config, self.state, self.job, allow_burn=True, allow_upload=True)
                self.assertEqual(replacement.call_count, int(automatic))
                self.assertEqual(self.job['status'], 'published' if automatic else 'ready_to_publish')
                new_upload.assert_not_called()

    def test_core_replacement_failure_records_attempt_without_losing_original_history(self):
        self.project_state.update(
            replacement_preview_digest='digest',
            publication_history=[{'bvids': ['BV-original']}],
        )
        with (
            patch.object(core, 'ensure_profile_publish_enabled'),
            patch.object(core, 'expected_replacement_confirmation', return_value='confirmed'),
            patch.object(core, 'digest_paths', return_value='digest'),
            patch.object(core, 'replacement_publish_command', return_value=['stub']),
            patch.object(core, 'run_external', side_effect=core.WorkflowError('network failed')),
        ):
            with self.assertRaisesRegex(core.WorkflowError, 'network failed'):
                core.step5_replace_upload(self.project_path, self.project_state, 'confirmed')
        saved = core.load_project(self.project_path)[1]
        self.assertEqual(saved['replacement_attempts'][-1]['status'], 'failed')
        self.assertEqual(saved['steps']['flow5']['status'], 'failed')
        self.assertEqual(saved['publication_history'][0]['bvids'], ['BV-original'])


if __name__ == '__main__':
    unittest.main()
