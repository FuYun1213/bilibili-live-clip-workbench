#!/usr/bin/env python3

from __future__ import annotations

import csv
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import workflow_app_core as app


def write_flow1_artifacts(project: Path) -> None:
    root = project / "analysis" / "transcript"
    root.mkdir(parents=True, exist_ok=True)
    fields = ["start_seconds", "end_seconds", "text"]
    for name in ("transcript.csv", "transcript.filtered.csv"):
        with (root / name).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow({"start_seconds": 0, "end_seconds": 500, "text": "字" * 500})
    app.atomic_json(
        root / "transcript.json",
        {
            "schema_version": 2,
            "authoritative": True,
            "segments": [{
                "start_seconds": 0,
                "end_seconds": 500,
                "text": "字" * 500,
                "words": [{"start_seconds": i, "end_seconds": i + 1, "word": "字"} for i in range(500)],
            }],
        },
    )
    app.atomic_json(
        root / "transcript-completeness.json",
        {"status": "PASS", "authoritative": True},
    )
    app.atomic_json(
        root / "speech-activity.json",
        {"regions": [{"start_seconds": 0, "end_seconds": 500}]},
    )

class WorkflowAppIntegrationTests(unittest.TestCase):
    def test_narrative_selection_to_audited_delivery_with_fake_media_tools(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            state_path = app.init_project(
                root / "project", source, None,
                "sumire", "narrative", "large-v3-turbo", "cpu", "int8",
            )
            _, state = app.load_project(state_path)
            write_flow1_artifacts(state_path.parent)
            selection_source = root / "selection.json"
            app.atomic_json(
                selection_source,
                [
                    {
                        "标题": "枝堇认真解释人设后被自己的原话当场拆穿",
                        "副标题": "认真解释｜当场拆穿",
                        "时间戳": [{"开始秒": 10, "结束秒": 40}],
                    }
                ],
            )
            app.import_selection(state_path, state, selection_source)
            _, state = app.load_project(state_path)

            export_command = []
            audit_command = []

            def option(command, name):
                return Path(command[command.index(name) + 1])

            def fake_external(_project_root, stage, command):
                if stage == "flow3-export":
                    export_command.extend(command)
                    output = option(command, "--output")
                    clips = output / "clips"
                    clips.mkdir(parents=True)
                    video = clips / "001_测试.mp4"
                    video.write_bytes(b"review-video")
                    with (output / "slices.csv").open(
                        "w", encoding="utf-8-sig", newline=""
                    ) as handle:
                        writer = csv.DictWriter(handle, fieldnames=["file", "slice_id"])
                        writer.writeheader()
                        writer.writerow({"file": video.name, "slice_id": "001"})
                elif stage == "flow3-authoritative-subtitles":
                    asr_root = option(command, "--output")
                    ass_root = option(command, "--ass-output")
                    clips = ass_root
                    for video in clips.glob("*.mp4"):
                        item = asr_root / video.stem
                        item.mkdir(parents=True)
                        app.atomic_json(item / "transcript.json", {"segments": []})
                        app.atomic_json(item / "speech-activity.json", {"regions": []})
                        with (item / "transcript.csv").open(
                            "w", encoding="utf-8-sig", newline=""
                        ) as handle:
                            writer = csv.DictWriter(
                                handle,
                                fieldnames=["start_seconds", "end_seconds", "text", "avg_logprob"],
                            )
                            writer.writeheader()
                            writer.writerow(
                                {"start_seconds": "0", "end_seconds": "1", "text": "测试", "avg_logprob": "0"}
                            )
                        (ass_root / f"{video.stem}.ass").write_text(
                            "[Script Info]\n[V4+ Styles]\n[Events]\n"
                            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                            "Dialogue: 0,0:00:00.00,0:00:01.00,Regular,,0,0,0,,谢谢老板的刚镚，正文保留\n",
                            encoding="utf-8",
                        )
                elif stage.startswith("flow4-audit-"):
                    audit_command.extend(command)
                elif stage in {"flow3-cover-render", "flow4-cover-render"}:
                    clips = option(command, "--clips-dir")
                    for video in clips.glob("*.mp4"):
                        video.with_name(f"{video.stem}-cover.jpg").write_bytes(b"cover")
                elif stage == "flow4-burn":
                    clips = option(command, "--clips")
                    output = option(command, "--output")
                    output.mkdir(parents=True, exist_ok=True)
                    for video in clips.glob("*.mp4"):
                        shutil.copy2(video, output / video.name)
                elif stage == "flow4-final-audit":
                    option(command, "--report").write_text("status\nPASS\n", encoding="utf-8")

            with mock.patch.object(app, "run_external", side_effect=fake_external):
                run_root, _ = app.step3_prepare(state_path, state)
                self.assertTrue((run_root / "export" / "clips" / "001_测试.ass").is_file())
                self.assertEqual(option(export_command, "--audio"), source.resolve())
                self.assertEqual(option(export_command, "--video"), source.resolve())
                review_dir = Path(app.load_project(state_path)[1]["active_review_dir"])
                for video in review_dir.glob("*.mp4"):
                    app.review_workspace.set_decision(review_dir, video.name, "approved")
                    backup = app.review_workspace.timeline_backup_video_path(video)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_bytes(b"pre-edit-video")
                _, state = app.load_project(state_path)
                delivery, _ = app.step4_burn(state_path, state, True)

            self.assertTrue((delivery / "001_测试.mp4").is_file())
            self.assertTrue((delivery / "001_测试.ass").is_file())
            self.assertIn("--timeline-edited", audit_command)
            self.assertIn("--human-reviewed", audit_command)
            self.assertIn(
                "（谢谢SC），正文保留",
                (delivery / "001_测试.ass").read_text(encoding="utf-8-sig"),
            )
            self.assertTrue((delivery / "001_测试-cover.jpg").is_file())
            self.assertTrue((delivery / "titles-and-covers.csv").is_file())
            _, final_state = app.load_project(state_path)
            self.assertEqual(Path(final_state["delivery_dir"]), delivery)

    def test_radio_review_burns_ass_and_keeps_unburned_layout_master(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "主播电台回.mp4"
            source.write_bytes(b"vertical-radio-source")
            state_path = app.init_project(
                root / "project", source, None,
                "sumire", "narrative", "large-v3-turbo", "cpu", "int8",
            )
            _, state = app.load_project(state_path)
            write_flow1_artifacts(state_path.parent)
            selection_source = root / "selection.json"
            app.atomic_json(
                selection_source,
                [
                    {
                        "标题": "枝堇认真解释电台回里发生的完整故事",
                        "副标题": "电台回｜完整故事",
                        "时间戳": [{"开始秒": 10, "结束秒": 40}],
                    }
                ],
            )
            app.import_selection(state_path, state, selection_source)
            _, state = app.load_project(state_path)
            stages: list[str] = []

            def option(command, name):
                return Path(command[command.index(name) + 1])

            def fake_external(_project_root, stage, command):
                stages.append(stage)
                if stage == "flow3-export":
                    output = option(command, "--output")
                    clips = output / "clips"
                    clips.mkdir(parents=True)
                    video = clips / "001_电台测试.mp4"
                    video.write_bytes(b"exact-vertical-clip")
                    with (output / "slices.csv").open(
                        "w", encoding="utf-8-sig", newline=""
                    ) as handle:
                        writer = csv.DictWriter(handle, fieldnames=["file", "slice_id"])
                        writer.writeheader()
                        writer.writerow({"file": video.name, "slice_id": "001"})
                elif stage == "flow3-authoritative-subtitles":
                    ass_root = option(command, "--ass-output")
                    clips = ass_root
                    for video in clips.glob("*.mp4"):
                        (ass_root / f"{video.stem}.ass").write_text(
                            "[Script Info]\n[V4+ Styles]\n[Events]\n"
                            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,枝堇,0,0,0,,测试字幕\n",
                            encoding="utf-8",
                        )
                elif stage == "flow3-radio-layout":
                    input_dir = option(command, "--input-dir")
                    output_dir = option(command, "--output-dir")
                    output_dir.mkdir(parents=True)
                    for video in input_dir.glob("*.mp4"):
                        shutil.copy2(video, output_dir / video.name)
                        shutil.copy2(
                            video.with_suffix(".ass"), output_dir / video.with_suffix(".ass").name
                        )
                elif stage == "flow3-radio-review-burn":
                    input_dir = option(command, "--clips")
                    output_dir = option(command, "--output")
                    output_dir.mkdir(parents=True)
                    for video in input_dir.glob("*.mp4"):
                        shutil.copy2(video, output_dir / video.name)
                elif stage == "flow3-cover-render":
                    clips = option(command, "--clips-dir")
                    for video in clips.glob("*.mp4"):
                        video.with_name(f"{video.stem}-cover.jpg").write_bytes(b"cover")

            with mock.patch.object(app, "run_external", side_effect=fake_external):
                run_root, _ = app.step3_prepare(state_path, state)

            _, saved = app.load_project(state_path)
            layout = run_root / "export" / "radio-layout"
            layout_master = run_root / "export" / "radio-layout-master"
            self.assertTrue(saved["radio_layout"])
            self.assertEqual(Path(saved["active_review_dir"]), layout)
            self.assertEqual(Path(saved["active_burn_source_dir"]), layout_master)
            self.assertIn("flow3-radio-review-burn", stages)
            self.assertNotIn("flow3-review-context", stages)
            review_video = next(layout.glob("*.mp4"))
            self.assertEqual(app.review_workspace.review_media_path(review_video), review_video)


if __name__ == "__main__":
    unittest.main()
