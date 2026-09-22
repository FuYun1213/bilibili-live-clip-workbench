from __future__ import annotations

import csv
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workflow_app_core as app


class CoverRefreshTests(unittest.TestCase):
    def prepare(self, root, *, radio=False, revision=False):
        source = root / "source.mp4"
        source.write_bytes(b"source")
        state_path = app.init_project(
            root / "project", source, None,
            "sumire", "narrative", "large-v3-turbo", "cpu", "int8",
        )
        _, state = app.load_project(state_path)
        run = state_path.parent / "runs" / "test"
        clips = run / "export" / "clips"
        clips.mkdir(parents=True)
        state.update(active_run_dir=str(run), active_review_dir=str(clips))
        state["radio_layout"] = radio
        if radio:
            master = run / "master"
            master.mkdir()
            state["active_burn_source_dir"] = str(master)
        rows = []
        for name in ("001_review.mp4", "002_other.mp4"):
            video = clips / name
            video.write_bytes(b"review-video")
            video.with_suffix(".ass").write_text(
                "[Script Info]\n[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:00.00,0:00:01.00,Regular,,0,0,0,,正文\n",
                encoding="utf-8",
            )
            if radio:
                (master / name).write_bytes(b"unburned-master")
            video.with_name(f"{video.stem}-cover.jpg").write_bytes(b"old-cover")
            rows.append(dict(video=name, content_type="narrative", title="测试标题",
                             cover_text_primary="封面主行", cover_text_secondary="修改后的副行"))
        with (clips / "titles-and-covers.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        app.review_workspace.set_decision(clips, rows[0]["video"], "approved")
        app.review_workspace.set_decision(clips, rows[1]["video"], "approved" if revision else "skipped")
        if revision:
            state["revision_clip_names"] = [rows[0]["video"]]
        return state_path, state, clips

    def test_latest_cover_is_generated_before_burn_and_packaged_for_all_burn_routes(self):
        for radio, revision, approval in [(False, False, "human"), (True, False, "human"),
                                          (False, True, "human"), (False, False, "automated-qa")]:
            with self.subTest(radio=radio, revision=revision, approval=approval), tempfile.TemporaryDirectory() as temp:
                state_path, state, clips = self.prepare(Path(temp), radio=radio, revision=revision)
                stages = []
                generated = []
                new_cover = "封面主行｜修改后的副行".encode("utf-8")

                def option(command, name):
                    return Path(command[command.index(name) + 1])

                def external(_root, stage, command):
                    stages.append(stage)
                    if stage == "flow4-cover-render":
                        self.assertIn("--force", command)
                        self.assertEqual(option(command, "--clips-dir"), clips)
                        name = command[command.index("--only-video") + 1]
                        generated.append(name)
                        row = next(row for row in app.copy_rows(option(command, "--copy")) if row["video"] == name)
                        cover = (row["cover_text_primary"] + "｜" + row["cover_text_secondary"]).encode("utf-8")
                        (clips / f"{Path(name).stem}-cover.jpg").write_bytes(cover)
                    elif stage == "flow4-burn":
                        self.assertEqual((clips / "001_review-cover.jpg").read_bytes(), new_cover)
                        source = option(command, "--clips")
                        output = option(command, "--output")
                        shutil.copy2(source / "001_review.mp4", output / "001_review.mp4")
                    elif stage == "flow4-final-audit":
                        option(command, "--report").write_text("status\nPASS\n", encoding="utf-8")

                with patch.object(app, "run_external", side_effect=external):
                    delivery, _ = app.step4_burn(state_path, state, approval == "human", approval)
                self.assertEqual(generated, ["001_review.mp4"])
                self.assertLess(stages.index("flow4-cover-render"), stages.index("flow4-burn"))
                self.assertEqual((delivery / "001_review-cover.jpg").read_bytes(), new_cover)
                self.assertEqual((clips / "002_other-cover.jpg").read_bytes(), b"old-cover")

    def test_cover_failure_stops_before_burn_even_when_old_cover_exists(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp:
                state_path, state, clips = self.prepare(Path(temp))
                stages = []

                def external(_root, stage, _command):
                    stages.append(stage)
                    if stage == "flow4-cover-render":
                        if missing:
                            (clips / "001_review-cover.jpg").write_bytes(b"")
                        else:
                            raise app.WorkflowError("封面渲染失败")

                with patch.object(app, "run_external", side_effect=external), self.assertRaises(app.WorkflowError):
                    app.step4_burn(state_path, state, True)
                self.assertNotIn("flow4-burn", stages)


if __name__ == "__main__":
    unittest.main()
