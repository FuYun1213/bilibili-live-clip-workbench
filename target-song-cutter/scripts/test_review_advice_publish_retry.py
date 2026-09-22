from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import workflow_app_core as core
import workflow_auto as auto
import review_workspace as rw
import workflow_app as ui

class ReviewAdviceAndRetryTests(unittest.TestCase):
    def test_suggestions_survive_decision_undo_and_do_not_approve(self):
        with tempfile.TemporaryDirectory() as t:
            review=Path(t); video=review/'001.mp4';video.write_bytes(b'video')
            rw.set_review_suggestions(review,video.name,'核对末尾是否完整')
            self.assertEqual(rw.load_decisions(review)['clips'][video.name]['status'],'pending')
            rw.set_decision(review,video.name,'approved','通过',record_undo=True)
            rw.set_review_suggestions(review,video.name,'检查 00:12 的人名')
            rw.undo_approval(review,video_name=video.name)
            row=rw.load_decisions(review)['clips'][video.name]
            self.assertEqual(row['status'],'pending')
            self.assertEqual(row['review_suggestions'],'检查 00:12 的人名')
            self.assertEqual(row['notes'],'')

    def test_upload_records_failure_and_second_attempt(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'workflow-project.json'
            state={'schema_version':core.SCHEMA_VERSION,'config':{'publish':{}},'delivery_dir':t,'publish_preview_digest':'digest'}
            with patch.object(core,'ensure_profile_publish_enabled'),patch.object(core,'expected_confirmation',return_value='发布 1 条'),patch.object(core,'digest_paths',return_value='digest'),patch.object(core,'publish_command',return_value=['native-upload']),patch.object(core,'run_external',side_effect=[RuntimeError('network unavailable'),None]):
                with self.assertRaises(RuntimeError): core.step5_upload(path,state,'发布 1 条')
                self.assertEqual(state['steps']['flow5']['status'],'failed')
                self.assertEqual(core.load_project(path)[1]['publish_attempts'][0]['status'],'failed')
                core.step5_upload(path,state,'发布 1 条')
            self.assertEqual([x['status'] for x in state['publish_attempts']],['failed','published'])
            self.assertEqual(state['steps']['flow5']['status'],'published')

    def test_retry_reuses_delivery_and_stays_failed_if_second_attempt_fails(self):
        for fails in (False,True):
            job={'id':'job','project':'project','status':'failed'}
            project={'delivery_dir':'same-delivery'}
            with patch.object(auto,'resumable_publish_failure',return_value=True),patch.object(core,'load_project',return_value=(Path('project.json'),project)),patch.object(core,'step5_preview') as preview,patch.object(core,'expected_confirmation',return_value='发布 1 条'),patch.object(core,'step5_upload',side_effect=RuntimeError('upload failed') if fails else None) as upload,patch.object(auto,'_set_job_progress'),patch.object(auto,'save_state'),patch.object(auto,'process_job') as earlier:
                if fails:
                    with self.assertRaises(RuntimeError): auto.retry_publish_job({}, {},job)
                else: auto.retry_publish_job({}, {},job)
                earlier.assert_not_called()
                preview.assert_called_once()
                upload.assert_called_once_with(Path('project.json'),project,'发布 1 条')
            self.assertEqual(job['status'],'failed' if fails else 'published')
            self.assertEqual(job['publish_retry_count'],1)

    def test_retry_button_rejects_unrelated_failed_tasks(self):
        bench=object.__new__(ui.Workbench)
        bench._selected_auto_jobs_or_warn=Mock(return_value=[{'status':'failed'}])
        bench._publish_auto_job=Mock()
        with patch.object(auto,'resumable_publish_failure',return_value=False),patch.object(ui.messagebox,'showwarning') as warning:
            bench.retry_selected_failed_publish()
        warning.assert_called_once()
        bench._publish_auto_job.assert_not_called()

if __name__=='__main__': unittest.main()
