from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import workflow_app_core as app


class DeliveryCoverRepairTests(unittest.TestCase):
    def prepare(self, root):
        source = root / "source.mp4"
        source.write_bytes(b"source")
        state_path = app.init_project(root / "project", source, None, "sumire", "narrative", "large-v3-turbo", "cpu", "int8")
        _, state = app.load_project(state_path)
        review = state_path.parent / "review"
        delivery = state_path.parent / "deliveries" / "test" / "files"
        review.mkdir()
        delivery.mkdir(parents=True)
        rows = []
        for name in ("001_test.mp4", "002_test.mp4", "003_rejected.mp4"):
            (review / name).write_bytes(b"review-" + name.encode())
            app.review_workspace.set_decision(review, name, "skipped" if "rejected" in name else "approved")
            rows.append(dict(video=name, title="原投稿标题", cover_mode="quote-impact", cover_text_primary="最新主文案", cover_text_secondary="最新副文案", cover_time_seconds="3.5", reference_image="manual/reference.png", guest_character_images="manual/guest.png"))
            if "rejected" not in name:
                (delivery / name).write_bytes(b"burned-" + name.encode())
                (delivery / f"{Path(name).stem}-cover.jpg").write_bytes(b"old-cover")
        self.write_rows(review / "titles-and-covers.csv", rows)
        old_rows = [dict(row, cover_text_primary="旧主文案") for row in rows[:2]]
        self.write_rows(delivery / "titles-and-covers.csv", old_rows)
        (delivery / "publish-receipts.json").write_text('{"items": {}}', encoding="utf-8")
        state.update(active_review_dir=str(review), delivery_dir=str(delivery), publish_preview_digest="old-publish", replacement_preview_digest="old-replace")
        state["approvals"]["delivery"] = {"source": "human", "digest": "reviewed"}
        state["publication_history"] = [{"publish_receipts": {"items": {"001": {"bvid": "BV-test"}}}}]
        app.save_project(state_path, state)
        return state_path, state, review, delivery

    def write_rows(self, path, rows):
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def option(self, command, name):
        return Path(command[command.index(name) + 1])

    def render(self, _root, stage, command, color="blue"):
        if stage == "flow5-cover-repair":
            self.assertIn("--force", command)
            name = command[command.index("--only-video") + 1]
            Image.new("RGB", (1920, 1080), color).save(self.option(command, "--clips-dir") / f"{Path(name).stem}-cover.jpg", "JPEG")

    def test_preview_uses_latest_review_copy_and_source_only_for_delivery_clips(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path, state, review, delivery = self.prepare(Path(temp))
            approvals = copy.deepcopy(state["approvals"])
            snapshots = {p: p.read_bytes() for p in delivery.iterdir() if p.suffix != ".jpg"}
            calls = []
            def external(root, stage, command):
                calls.append(stage)
                self.assertNotIn("--execute", command)
                if stage == "flow5-cover-repair":
                    name = command[command.index("--only-video") + 1]
                    self.assertEqual((self.option(command, "--clips-dir") / name).read_bytes(), (review / name).read_bytes())
                    rows = app.copy_rows(self.option(command, "--copy"))
                    self.assertEqual([r["video"] for r in rows], ["001_test.mp4", "002_test.mp4"])
                    self.assertEqual(rows[0]["cover_text_primary"], "最新主文案")
                    self.assertEqual(rows[0]["cover_time_seconds"], "3.5")
                    self.assertEqual(Path(rows[0]["reference_image"]), review / "manual/reference.png")
                    self.assertEqual(Path(rows[0]["guest_character_images"]), review / "manual/guest.png")
                    self.render(root, stage, command)
                else:
                    self.assertNotIn("publish_preview_digest", state)
                    self.assertNotIn("replacement_preview_digest", state)
                    for cover in delivery.glob("*-cover.jpg"):
                        with Image.open(cover) as image:
                            image.load()
            with patch.object(app, "run_external", side_effect=external):
                self.assertEqual(app.step5_preview(state_path, state), "发布 2 条")
            self.assertEqual(calls, ["flow5-cover-repair", "flow5-cover-repair", "flow5-publish-preview"])
            self.assertEqual(state["approvals"], approvals)
            for path, original in snapshots.items():
                self.assertEqual(path.read_bytes(), original)
            self.assertEqual(state["publish_preview_digest"], app.digest_paths([delivery], extra=state["config"]["publish"]))
            self.assertEqual(state["delivery_digest"], state["publish_preview_digest"])
            self.assertEqual(list(delivery.parent.glob(".cover-repair-*")), [])

    def test_replace_preview_repairs_before_binding_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path, state, _, delivery = self.prepare(Path(temp))
            calls = []
            def external(root, stage, command):
                calls.append(stage)
                self.render(root, stage, command)
                self.assertNotIn("--execute", command)
            with patch.object(app, "run_external", side_effect=external):
                self.assertEqual(app.step5_replace_preview(state_path, state), "替换 2 条")
            self.assertEqual(calls, ["flow5-cover-repair", "flow5-cover-repair", "flow5-replace-preview"])
            self.assertNotIn("publish_preview_digest", state)
            expected = app.digest_paths([delivery, delivery / "replacement-targets.json"], extra={"publish": state["config"]["publish"], "operation": "replace"})
            self.assertEqual(state["replacement_preview_digest"], expected)

    def test_missing_review_falls_back_without_copying_same_volume_videos(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path, state, _, delivery = self.prepare(Path(temp))
            state["active_review_dir"] = str(Path(temp) / "missing")
            def external(root, stage, command):
                if stage == "flow5-cover-repair":
                    name = command[command.index("--only-video") + 1]
                    self.assertEqual((self.option(command, "--clips-dir") / name).read_bytes(), (delivery / name).read_bytes())
                    self.assertEqual(app.copy_rows(self.option(command, "--copy"))[0]["cover_text_primary"], "旧主文案")
                self.render(root, stage, command)
            with patch.object(app, "run_external", side_effect=external), patch.object(app.shutil, "copy2", side_effect=AssertionError("expected hard link")):
                app.step5_preview(state_path, state)

    def test_missing_review_video_keeps_latest_copy_with_delivery_video(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path, state, review, delivery = self.prepare(Path(temp))
            (review / "001_test.mp4").unlink()
            def external(root, stage, command):
                if stage == "flow5-cover-repair":
                    name = command[command.index("--only-video") + 1]
                    rows = app.copy_rows(self.option(command, "--copy"))
                    self.assertEqual(rows[0]["cover_text_primary"], "最新主文案")
                    if name.startswith("001"):
                        self.assertEqual((self.option(command, "--clips-dir") / name).read_bytes(), (delivery / name).read_bytes())
                self.render(root, stage, command)
            with patch.object(app, "run_external", side_effect=external):
                app.step5_preview(state_path, state)

    def test_hard_link_failure_copies_source_without_modifying_original_videos(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path, state, review, delivery = self.prepare(Path(temp))
            original_copy = app.shutil.copy2
            with patch.object(app, "run_external", side_effect=self.render), patch.object(app.os, "link", side_effect=OSError("cross-device link")), patch.object(app.shutil, "copy2", wraps=original_copy) as copier:
                app.step5_preview(state_path, state)
            self.assertEqual(copier.call_count, 2)
            self.assertEqual((review / "001_test.mp4").read_bytes(), b"review-001_test.mp4")
            self.assertEqual((delivery / "001_test.mp4").read_bytes(), b"burned-001_test.mp4")

    def test_failed_or_invalid_render_preserves_all_covers_and_clears_old_tokens(self):
        for failure in ("process", "invalid", "missing", "small", "truncated"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                state_path, state, _, delivery = self.prepare(Path(temp))
                def external(root, stage, command):
                    self.assertEqual(stage, "flow5-cover-repair")
                    name = command[command.index("--only-video") + 1]
                    output = self.option(command, "--clips-dir") / f"{Path(name).stem}-cover.jpg"
                    if name.startswith("001"):
                        self.render(root, stage, command)
                    elif failure == "process":
                        raise app.WorkflowError("render failed")
                    elif failure == "invalid":
                        output.write_bytes(b"not-a-jpeg")
                    elif failure == "small":
                        Image.new("RGB", (1, 1)).save(output, "JPEG")
                    elif failure == "truncated":
                        self.render(root, stage, command)
                        output.write_bytes(output.read_bytes()[:1000])
                with patch.object(app, "run_external", side_effect=external), self.assertRaises(app.WorkflowError):
                    app.step5_preview(state_path, state)
                for cover in delivery.glob("*-cover.jpg"):
                    self.assertEqual(cover.read_bytes(), b"old-cover")
                saved = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertNotIn("publish_preview_digest", saved)
                self.assertNotIn("replacement_preview_digest", saved)
                with patch.object(app, "run_external") as external, self.assertRaises(app.WorkflowError):
                    app.step5_upload(state_path, saved, "发布 2 条")
                external.assert_not_called()

    def test_review_status_and_pending_timeline_edits_still_block(self):
        for status in ("pending", "revise", "skipped", "timeline"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                state_path, state, review, delivery = self.prepare(Path(temp))
                if status == "timeline":
                    app.review_workspace.timeline_edit_path(review / "001_test.mp4").parent.mkdir()
                    app.review_workspace.timeline_edit_path(review / "001_test.mp4").write_text('{"status": "pending"}', encoding="utf-8")
                else:
                    app.review_workspace.set_decision(review, "001_test.mp4", status)
                with patch.object(app, "run_external") as external, self.assertRaises(app.WorkflowError):
                    app.step5_preview(state_path, state)
                external.assert_not_called()
                self.assertNotIn("publish_preview_digest", state)
                self.assertEqual((delivery / "001_test-cover.jpg").read_bytes(), b"old-cover")

    def test_automated_approval_accepts_pending_review(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path, state, review, _ = self.prepare(Path(temp))
            state["approvals"]["delivery"]["source"] = "automated-qa"
            app.review_workspace.set_decision(review, "001_test.mp4", "pending")
            with patch.object(app, "run_external", side_effect=self.render):
                app.step5_preview(state_path, state)

    def test_each_preview_force_refreshes_profile_and_renderer_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path, state, _, delivery = self.prepare(Path(temp))
            digests = []
            for color in ("blue", "red"):
                with patch.object(app, "run_external", side_effect=lambda root, stage, command: self.render(root, stage, command, color)):
                    app.step5_preview(state_path, state)
                digests.append(state["publish_preview_digest"])
            self.assertNotEqual(*digests)
            self.assertEqual((delivery / "001_test.mp4").read_bytes(), b"burned-001_test.mp4")


if __name__ == "__main__":
    unittest.main()
