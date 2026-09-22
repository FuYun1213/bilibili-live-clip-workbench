import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workflow_auto as auto
import workflow_app_core as core


class ReplacementWorkflowTests(unittest.TestCase):
    def test_queue_cli_exposes_replace_published_job(self):
        args = auto.build_parser().parse_args([
            "--config", "config.json", "replace-published-job",
            "--job-id", "revision",
        ])
        self.assertEqual(args.command, "replace-published-job")
        self.assertEqual(args.job_id, "revision")

    def test_redo_cli_exposes_targeted_auto_replace(self):
        args = auto.build_parser().parse_args([
            "--config", "config.json", "redo-job",
            "--job-id", "revision", "--from-flow", "flow4",
            "--auto-replace", "--clip-name", "001-topic.mp4",
        ])
        self.assertTrue(args.auto_replace)
        self.assertEqual(args.clip_name, ["001-topic.mp4"])

    def test_auto_replacement_revision_is_runnable_only_with_upload_authority(self):
        job = {
            "creator": "sumire",
            "status": "ready_to_publish",
            "auto_replace_revision": True,
        }
        self.assertTrue(auto.job_is_runnable(
            job, allow_burn=True, allow_upload=True, current_time=100
        ))
        self.assertFalse(auto.job_is_runnable(
            job, allow_burn=True, allow_upload=False, current_time=100
        ))

    def test_ready_auto_revision_routes_to_replace_instead_of_new_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = {
                "_state_file": Path(temporary) / "queue.json",
                "_output_root": Path(temporary),
            }
            state = auto.empty_state()
            job = {
                "id": "revision",
                "creator": "sumire",
                "status": "ready_to_publish",
                "auto_replace_revision": True,
            }
            state["jobs"][job["id"]] = job

            def replaced(_config, _state, target):
                target["status"] = "published"

            with (
                patch.object(auto, "defer_automatic_job_for_manual_work", return_value=False),
                patch.object(auto, "ensure_heavy_job_disk_space"),
                patch.object(auto, "replace_published_job", side_effect=replaced) as replace,
            ):
                auto.process_job(
                    config, state, job, allow_burn=True, allow_upload=True
                )
            replace.assert_called_once()
            self.assertEqual("published", job["status"])

    def test_previous_publish_receipts_are_materialized_in_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            delivery = Path(temporary)
            state = {
                "delivery_dir": str(delivery),
                "publication_history": [{
                    "publish_receipts": {
                        "items": {
                            "001": {
                                "title": "旧稿",
                                "bvid": "BV1234567890",
                            }
                        }
                    }
                }],
            }
            path = core.write_replacement_targets(state)
            self.assertTrue(path.is_file())
            self.assertIn("BV1234567890", path.read_text(encoding="utf-8"))

    def test_replace_job_requires_published_revision_flag(self):
        job = {
            "id": "revision",
            "status": "ready_to_publish",
            "project": "project",
        }
        with self.assertRaisesRegex(auto.AutomationError, "不是已发布"):
            auto.replace_published_job({}, auto.empty_state(), job)

    def test_replace_job_marks_published_only_after_upload_succeeds(self):
        job = {
            "id": "revision",
            "status": "ready_to_publish",
            "project": "project",
            "manual_republish_required": True,
        }
        state = auto.empty_state()
        project_state = {"delivery_dir": "delivery"}
        with (
            patch.object(
                auto.core,
                "load_project",
                side_effect=[
                    (Path("project/workflow-project.json"), project_state),
                    (Path("project/workflow-project.json"), project_state),
                ],
            ),
            patch.object(
                auto.core,
                "step5_replace_preview",
                return_value="替换 1 条",
            ) as preview,
            patch.object(auto.core, "step5_replace_upload") as upload,
            patch.object(auto, "_set_job_progress") as progress,
        ):
            auto.replace_published_job({}, state, job)
        preview.assert_called_once()
        upload.assert_called_once_with(
            Path("project/workflow-project.json"),
            project_state,
            "替换 1 条",
        )
        self.assertEqual(job["status"], "published")
        self.assertNotIn("manual_republish_required", job)
        self.assertIn("B站将重新审核", progress.call_args.args[-1])

    def test_replace_failure_keeps_manual_republish_guard(self):
        job = {
            "id": "revision",
            "status": "ready_to_publish",
            "project": "project",
            "manual_republish_required": True,
        }
        with (
            patch.object(
                auto.core,
                "load_project",
                side_effect=[
                    (Path("project/workflow-project.json"), {}),
                    (Path("project/workflow-project.json"), {}),
                ],
            ),
            patch.object(
                auto.core,
                "step5_replace_preview",
                return_value="替换 1 条",
            ),
            patch.object(
                auto.core,
                "step5_replace_upload",
                side_effect=core.WorkflowError("replace failed"),
            ),
            patch.object(auto, "_set_job_progress"),
        ):
            with self.assertRaisesRegex(core.WorkflowError, "replace failed"):
                auto.replace_published_job({}, auto.empty_state(), job)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["publish_failure_action"], "replace")
        self.assertTrue(job["manual_republish_required"])


if __name__ == "__main__":
    unittest.main()