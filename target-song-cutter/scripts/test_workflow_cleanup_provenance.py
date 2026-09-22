"""Read-only cleanup-plan regression tests; deletion is never invoked unmocked."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import workflow_auto as app


class ReviewPackageCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace_patch = patch.object(app.core, "WORKSPACE_ROOT", self.root)
        self.workspace_patch.start()
        self.addCleanup(self.workspace_patch.stop)
        self.watch = self.root / "recordings"
        self.output = self.root / "projects"
        self.watch.mkdir()
        self.output.mkdir()
        self.batch = "review-batch-20260906"
        self.batch_root = self.root / "work-manifests" / self.batch
        self.origin_id = "20260906-180029-1727076670-narrative-8558c2079838"
        self.origin = self.output / self.origin_id
        self.origin.mkdir()
        self.recording = self.watch / "original.flv"
        self.recording.write_bytes(b"source recording")
        (self.origin / "shared-clip.mp4").write_bytes(b"other selected clip")
        self.project = self.batch_root / "packages-resume" / f"122-{self.origin_id}"
        self.project.mkdir(parents=True)
        (self.project / "generated.mp4").write_bytes(b"generated clip")
        self.sibling = self.project.parent / f"125-{self.origin_id}"
        self.sibling.mkdir()
        (self.sibling / "keep.mp4").write_bytes(b"another package")
        self.config = {
            "_watch_root": self.watch, "_output_root": self.output,
            "_state_file": self.output / "workflow-auto-state.json",
        }
        self.job = {
            "id": f"{self.batch}-clip-122", "project": str(self.project),
            "status": "published", "segments": [],
            "origin_job_id": self.origin_id, "origin_clip_index": 122,
            "queue_import": {"batch": self.batch},
        }
        self.state = app.empty_state()
        self.state["jobs"] = {
            self.origin_id: {"id": self.origin_id, "project": str(self.origin)},
            self.job["id"]: self.job,
        }
        app.save_state(self.config, self.state)
        inventory = [{} for _ in range(123)]
        inventory[122] = {"index": 122, "project": str(self.origin)}
        self.write("inventory.json", inventory)
        self.write("resume-release.json", [{"index": 122, "project": str(self.project)}])
        self.write("monitor-sync/result.json", {
            "registered_publication_jobs": [self.job["id"]],
            "updated_original_jobs": [self.origin_id],
        })
        self.package_state = {
            "origin_project": str(self.origin), "origin_indices": [122],
            # Producer deep-copies these; they must never become targets.
            "config": {"source": str(self.recording)},
            "active_run_dir": str(self.origin),
            "steps": {"flow0": {"outputs": {"source": str(self.recording)}}},
        }
        self.save_package_state()

    def write(self, name, value):
        path = self.batch_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def save_package_state(self):
        (self.project / app.core.PROJECT_FILENAME).write_text(
            json.dumps(self.package_state), encoding="utf-8"
        )

    def assertRejected(self, job=None, pattern="拒绝清理|越出"):
        with self.assertRaisesRegex(app.AutomationError, pattern):
            app.cleanup_job_plan(self.config, job or self.job)

    def test_registered_single_clip_package_targets_only_its_directory(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        plan = app.cleanup_job_plan(self.config, self.job)
        self.assertEqual([x["path"] for x in plan["targets"]], [str(self.project)])
        self.assertEqual(plan["existing_count"], 1)
        self.assertEqual(plan["provenance"]["scope"], "review_package")
        self.assertEqual(plan["provenance"]["origin_project"], str(self.origin))
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_identity_and_arbitrary_path_tampering_are_rejected(self):
        cases = [
            {"origin_clip_index": True}, {"origin_clip_index": -1},
            {"origin_clip_index": "122"}, {"origin_clip_index": 125},
            {"origin_job_id": "../other"}, {"id": "another-job"},
            {"queue_import": {"batch": "../../recordings"}},
            {"queue_import": {"batch": str(self.root)}},
            {"project": str(self.root)}, {"project": str(self.batch_root)},
            {"project": str(self.project.parent)}, {"project": str(self.sibling)},
            # A forged import cannot bypass provenance by pointing inside output_root.
            {"project": str(self.origin)},
            {"segments": [{"path": str(self.recording)}]},
        ]
        for change in cases:
            with self.subTest(change=change):
                job = copy.deepcopy(self.job)
                job.update(change)
                self.assertRejected(job)

    def test_missing_registration_rejected_even_with_consistent_project_json(self):
        self.write("monitor-sync/result.json", {"registered_publication_jobs": []})
        self.assertRejected(pattern="没有对应的队列注册记录")

    def test_inconsistent_inventory_or_original_queue_job_is_rejected(self):
        data = [{} for _ in range(123)]
        data[122] = {"index": 122, "project": str(self.root / "elsewhere")}
        self.write("inventory.json", data)
        self.assertRejected(pattern="原始项目与清单不一致")
        data[122]["project"] = str(self.origin)
        self.write("inventory.json", data)
        self.state["jobs"][self.origin_id]["project"] = str(self.sibling)
        app.save_state(self.config, self.state)
        self.assertRejected(pattern="匹配的原始自动任务")

    def test_release_requires_one_exact_owner(self):
        for rows in (
            [], [{"index": 122, "project": str(self.sibling)}],
            [{"index": 122, "project": str(self.project)}] * 2,
            [{"index": 122, "project": str(self.project)},
             {"index": 125, "project": str(self.project)}],
        ):
            with self.subTest(rows=rows):
                self.write("resume-release.json", rows)
                self.assertRejected(pattern="独占的单片目录")

    def test_shared_packages_and_mismatched_package_state_are_rejected(self):
        for changes in (
            {"origin_indices": [122, 125]}, {"origin_indices": [125]},
            {"origin_project": str(self.root)},
        ):
            with self.subTest(changes=changes):
                self.package_state = {"origin_indices": [122], "origin_project": str(self.origin)}
                self.package_state.update(changes)
                self.save_package_state()
                self.assertRejected(pattern="来源或单片归属不一致")

    def test_missing_package_can_be_retried_without_expanding_targets(self):
        # Point at a legitimately registered package that has not been created.
        index = 123
        project = self.project.parent / f"{index}-{self.origin_id}"
        job = copy.deepcopy(self.job)
        job.update(id=f"{self.batch}-clip-{index}", project=str(project),
                   origin_clip_index=index, status="cleanup_failed", cleanup={"reason": "published"})
        inventory = [{} for _ in range(index + 1)]
        inventory[index] = {"index": index, "project": str(self.origin)}
        self.write("inventory.json", inventory)
        self.write("resume-release.json", [{"index": index, "project": str(project)}])
        self.write("monitor-sync/result.json", {
            "registered_publication_jobs": [job["id"]], "updated_original_jobs": [self.origin_id],
        })
        plan = app.cleanup_job_plan(self.config, job)
        self.assertEqual(plan["existing_count"], 0)
        self.assertEqual([x["path"] for x in plan["targets"]], [str(project)])
        self.assertTrue(self.recording.exists())

    def test_mixed_plan_deduplicates_without_adding_original_sources(self):
        ordinary = {"id": "ordinary", "project": str(self.output / "ordinary"), "segments": []}
        plan = app.cleanup_jobs_plan(self.config, [self.job, ordinary, self.job])
        self.assertEqual(plan["job_count"], 3)
        self.assertEqual(plan["review_package_count"], 2)
        self.assertEqual(len(plan["targets"]), 2)
        self.assertNotIn(str(self.origin), [x["path"] for x in plan["targets"]])
        self.assertNotIn(str(self.recording), [x["path"] for x in plan["targets"]])

    def test_normal_auto_safety_boundary_is_not_relaxed(self):
        for path in (self.output, self.root, self.watch, self.batch_root, self.project):
            with self.subTest(path=path):
                self.assertRejected({"id": "ordinary", "project": str(path)}, "安全子路径")
        config = {"_output_root": self.watch, "_watch_root": self.watch}
        with self.assertRaisesRegex(app.AutomationError, "监控录播目录.*重叠"):
            app.cleanup_job_plan(config, {"project": str(self.watch / "nested")})

    def test_unsafe_batch_is_rejected_before_any_cleanup_call(self):
        bad = copy.deepcopy(self.job)
        bad.update(id="unsafe", project=str(self.root), queue_import={})
        self.state["jobs"][bad["id"]] = bad
        jobs = [self.job, bad]
        with patch.object(app, "cleanup_job_files") as cleanup:
            with self.assertRaises(app.AutomationError):
                app.cleanup_selected_jobs(
                    self.config, self.state, [j["id"] for j in jobs],
                    confirmation=app.cleanup_jobs_confirmation(jobs),
                )
        cleanup.assert_not_called()

    def create_directory_link(self, source, link):
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(source), str(link))
        else:
            link.symlink_to(source, target_is_directory=True)

    def test_internal_directory_junction_is_rejected_before_size_traversal(self):
        self.create_directory_link(self.origin, self.project / "source-link")
        with patch.object(app, "_tree_size") as size:
            self.assertRejected(pattern="符号链接或目录联接")
        size.assert_not_called()
        self.assertTrue(self.recording.exists())

    def test_alias_to_exact_package_is_rejected_even_after_resolve(self):
        alias = self.root / "alias"
        self.create_directory_link(self.project, alias)
        job = {**self.job, "project": str(alias)}
        self.assertRejected(job, "符号链接或目录联接")

    def test_manifest_path_junction_is_rejected(self):
        # A fixed evidence filename is not trusted through a linked parent.
        other = self.root / "other-batch"
        other.mkdir()
        (other / "result.json").write_text("{}", encoding="utf-8")
        alias = self.batch_root / "evidence-alias"
        self.create_directory_link(other, alias)
        with self.assertRaisesRegex(app.AutomationError, "符号链接或目录联接"):
            app._cleanup_require_unlinked(alias / "result.json", self.root)

    def test_execution_revalidates_after_plan_without_deleting_anything(self):
        def change_registration(*_args):
            self.write("monitor-sync/result.json", {"registered_publication_jobs": []})
        with patch.object(app, "save_state", side_effect=change_registration), \
                patch.object(app.shutil, "rmtree") as remove:
            with self.assertRaisesRegex(app.AutomationError, "目标删除失败"):
                app.cleanup_job_files(
                    self.config, self.state, self.job, reason="published",
                    confirmation=app.cleanup_confirmation(self.job["id"]),
                )
        remove.assert_not_called()
        self.assertEqual(self.job["status"], "cleanup_failed")
        self.assertIn("队列注册记录", self.job["cleanup"]["failed"][0]["error"])
        self.assertTrue((self.project / "generated.mp4").exists())

    def test_ui_explains_package_source_preservation_and_cancel_runs_nothing(self):
        import workflow_app
        bench = object.__new__(workflow_app.Workbench)
        bench.profiles = {}
        bench._job_display_name = lambda *_args: "已发布片段"
        bench._run_selected_auto_command = Mock()
        with patch.object(app, "load_config", return_value=self.config), \
                patch.object(workflow_app.messagebox, "askokcancel", return_value=False) as prompt:
            self.assertFalse(bench._start_cleanup_jobs([self.job]))
        self.assertIn("独立审核发布包", prompt.call_args.args[1])
        self.assertIn("保留其原录播、原项目和审核批次记录", prompt.call_args.args[1])
        bench._run_selected_auto_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
