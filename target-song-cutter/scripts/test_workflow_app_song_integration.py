#!/usr/bin/env python3

from __future__ import annotations

import csv
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import workflow_app_core as app


def write_multilingual_flow1_artifacts(project: Path) -> None:
    root = project / "analysis" / "transcript"
    root.mkdir(parents=True, exist_ok=True)
    with (root / "transcript_multilingual.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "start_seconds", "end_seconds", "language",
                "language_probability", "avg_logprob", "text",
            ],
        )
        writer.writeheader()
        writer.writerow({
            "start_seconds": 0,
            "end_seconds": 500,
            "language": "zh",
            "language_probability": 1,
            "avg_logprob": 0,
            "text": "夏天的风" * 125,
        })
    app.atomic_json(
        root / "transcript_multilingual.json",
        {
            "schema_version": 2,
            "authoritative": True,
            "segments": [{
                "start_seconds": 0,
                "end_seconds": 500,
                "text": "夏天的风" * 125,
                "words": [{"start_seconds": i, "end_seconds": i + 1, "word": "夏天的风"[i % 4]} for i in range(500)],
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

class WorkflowAppSongIntegrationTests(unittest.TestCase):
    def test_song_uses_auto_language_and_requires_reviewed_lyrics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "song-source.mp4"
            source.write_bytes(b"source")
            state_path = app.init_project(
                root / "project", source, None,
                "sumire", "song", "large-v3-turbo", "cpu", "int8",
            )
            _, state = app.load_project(state_path)
            write_multilingual_flow1_artifacts(state_path.parent)
            selection = root / "selection.json"
            app.atomic_json(
                selection,
                [{"标题": "枝堇完整演唱夏天的风", "副标题": "夏天的风", "时间戳": [[10, 40]]}],
            )
            app.import_selection(state_path, state, selection)
            _, state = app.load_project(state_path)
            clip_asr_command = []

            def option(command, name):
                return Path(command[command.index(name) + 1])

            def fake_external(_project_root, stage, command):
                if stage == "flow3-export":
                    output = option(command, "--output")
                    clips = output / "clips"
                    clips.mkdir(parents=True)
                    video = clips / "001_夏天的风.mp4"
                    video.write_bytes(b"review-song")
                    with (output / "slices.csv").open(
                        "w", encoding="utf-8-sig", newline=""
                    ) as handle:
                        writer = csv.DictWriter(handle, fieldnames=["file", "slice_id"])
                        writer.writeheader()
                        writer.writerow({"file": video.name, "slice_id": "001"})
                elif stage == "flow3-authoritative-subtitles":
                    clip_asr_command.extend(command)
                    asr_root = option(command, "--output")
                    ass_root = option(command, "--ass-output")
                    clips = ass_root
                    for video in clips.glob("*.mp4"):
                        item = asr_root / video.stem
                        item.mkdir(parents=True)
                        app.atomic_json(
                            item / "transcript.json",
                            {"segments": [{"start_seconds": 0, "end_seconds": 3, "text": "夏天的风" * 125, "words": []}]},
                        )
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
                                {"start_seconds": "0", "end_seconds": "3", "text": "夏天的风" * 125, "avg_logprob": "0"}
                            )
                        (ass_root / f"{video.stem}.ass").write_text("[Script Info]\n", encoding="utf-8")
                elif stage in {"flow3-cover-render", "flow4-cover-render"}:
                    clips = option(command, "--clips-dir")
                    for video in clips.glob("*.mp4"):
                        video.with_name(f"{video.stem}-cover.jpg").write_bytes(b"cover")
                elif stage == "flow4-song-retime":
                    source_lyrics = option(command, "--lyrics")
                    shutil.copy2(source_lyrics, option(command, "--output"))
                    option(command, "--report").write_text("clip,status\n001_夏天的风,PASS\n", encoding="utf-8")
                elif stage == "flow4-song-ass":
                    output = option(command, "--output-dir")
                    output.mkdir(parents=True, exist_ok=True)
                    (output / "001_夏天的风.ass").write_text("[Script Info]\n", encoding="utf-8")
                elif stage == "flow4-burn":
                    clips = option(command, "--clips")
                    output = option(command, "--output")
                    output.mkdir(parents=True, exist_ok=True)
                    for video in clips.glob("*.mp4"):
                        shutil.copy2(video, output / video.name)
                elif stage.startswith("flow4-song-gate-"):
                    app.atomic_json(option(command, "--output"), {"status": "PASS"})
                elif stage == "flow4-final-audit":
                    option(command, "--report").write_text("status\nPASS\n", encoding="utf-8")

            with mock.patch.object(app, "run_external", side_effect=fake_external):
                run_root, _ = app.step3_prepare(state_path, state)
                self.assertIn("slice-authoritative", clip_asr_command)
                self.assertNotIn("transcribe_review_clips.py", clip_asr_command)
                self.assertNotIn("--model", clip_asr_command)
                lyrics = run_root / "lyric-corrections.csv"
                with lyrics.open("r", encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                rows[0]["corrected_text"] = rows[0]["recognized_text"]
                rows[0]["reviewed"] = "yes"
                with lyrics.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
                review_dir = Path(app.load_project(state_path)[1]["active_review_dir"])
                for video in review_dir.glob("*.mp4"):
                    app.review_workspace.set_decision(review_dir, video.name, "approved")
                _, state = app.load_project(state_path)
                delivery, _ = app.step4_burn(state_path, state, True)

            self.assertTrue((delivery / "001_夏天的风.song-timing-gate.json").is_file())


if __name__ == "__main__":
    unittest.main()
