import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from review_workspace import (
    delete_ass_interval,
    edited_to_source,
    force_single_line_ass,
    load_decisions,
    read_dialogues,
    render_timeline_edit,
    save_timeline_edit,
    selected_video_names,
    set_decision,
    source_to_edited,
    split_timeline_segment,
    subtitle_drag_times,
    timeline_duration,
    update_dialogue,
)


ASS = """[Script Info]
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Main,Arial,70

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Main,主播,0,0,0,,hello world\\N下一行
"""


class TimelineEditorTests(unittest.TestCase):
    def test_decisions_require_approval_and_skip_without_deleting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("001.mp4", "002.mp4"):
                (root / name).write_bytes(b"video")
            state = load_decisions(root)
            self.assertEqual(set(state["clips"]), {"001.mp4", "002.mp4"})
            with self.assertRaisesRegex(ValueError, "待审核"):
                selected_video_names(root, require_human_decisions=True)
            set_decision(root, "001.mp4", "approved")
            set_decision(root, "002.mp4", "skipped", "故事不完整")
            selected, state = selected_video_names(
                root, require_human_decisions=True
            )
            self.assertEqual(selected, ["001.mp4"])
            self.assertTrue((root / "002.mp4").is_file())
            self.assertEqual(state["clips"]["002.mp4"]["notes"], "故事不完整")

    def test_single_line_editor_removes_explicit_breaks(self):
        with tempfile.TemporaryDirectory() as temporary:
            ass = Path(temporary) / "001.ass"
            ass.write_text(ASS, encoding="utf-8-sig")
            self.assertEqual(force_single_line_ass(ass), 2)
            value = ass.read_text(encoding="utf-8-sig")
            self.assertIn("WrapStyle: 2", value)
            self.assertIn("hello world 下一行", value)
            self.assertNotIn(r"\N", value)
            row = read_dialogues(ass)[0]
            update_dialogue(
                ass,
                row["line_number"],
                start="0:00:01.20",
                end="0:00:02.40",
                text="NFY cannot break",
            )
            updated = read_dialogues(ass)[0]
            self.assertEqual(updated["start"], "0:00:01.20")
            self.assertEqual(updated["end"], "0:00:02.40")
            self.assertEqual(updated["text"], "NFY cannot break")


    def test_timeline_mapping_and_split(self):
        segments = [
            {"source_start": 0.0, "source_end": 5.0},
            {"source_start": 10.0, "source_end": 15.0},
        ]
        self.assertEqual(10.0, timeline_duration(segments))
        self.assertEqual((10.0, 1), edited_to_source(segments, 5.0))
        self.assertEqual((12.0, 1), edited_to_source(segments, 7.0))
        self.assertEqual((7.0, 1), source_to_edited(segments, 12.0))
        self.assertEqual((None, None), source_to_edited(segments, 7.0))
        split, selected = split_timeline_segment(segments, 12.0)
        self.assertEqual(2, selected)
        self.assertEqual(
            [
                {"source_start": 0.0, "source_end": 5.0},
                {"source_start": 10.0, "source_end": 12.0},
                {"source_start": 12.0, "source_end": 15.0},
            ],
            split,
        )

    def test_subtitle_blocks_resize_and_move_within_timeline(self):
        self.assertEqual(
            (4.0, 6.0),
            subtitle_drag_times(1.0, 3.0, 2.0, 5.0, "move", 10.0),
        )
        self.assertEqual(
            (0.0, 2.0),
            subtitle_drag_times(1.0, 3.0, 2.0, -5.0, "move", 10.0),
        )
        self.assertEqual(
            (8.0, 10.0),
            subtitle_drag_times(1.0, 3.0, 2.0, 20.0, "move", 10.0),
        )
        self.assertEqual(
            (2.75, 3.0),
            subtitle_drag_times(1.0, 3.0, 1.0, 2.75, "start", 10.0),
        )
        self.assertEqual(
            (1.0, 4.25),
            subtitle_drag_times(1.0, 3.0, 3.0, 4.25, "end", 10.0),
        )
    def test_ripple_delete_moves_and_clips_subtitles(self):
        with tempfile.TemporaryDirectory() as temporary:
            ass = Path(temporary) / "001.ass"
            ass.write_text(
                ASS.replace(
                    "Dialogue: 0,0:00:01.00,0:00:02.00,Main,主播,0,0,0,,hello world\\N下一行",
                    "\n".join(
                        [
                            "Dialogue: 0,0:00:01.00,0:00:02.00,Main,主播,0,0,0,,before",
                            "Dialogue: 0,0:00:02.50,0:00:04.00,Main,主播,0,0,0,,left edge",
                            "Dialogue: 0,0:00:04.00,0:00:06.00,Main,主播,0,0,0,,inside",
                            "Dialogue: 0,0:00:06.00,0:00:08.00,Main,主播,0,0,0,,right edge",
                            "Dialogue: 0,0:00:08.00,0:00:10.00,Main,主播,0,0,0,,after",
                        ]
                    ),
                ),
                encoding="utf-8-sig",
            )
            result = delete_ass_interval(ass, 3.0, 7.0)
            rows = read_dialogues(ass)
            self.assertEqual({"removed": 1, "shifted": 1, "clipped": 2}, result)
            self.assertEqual(
                [
                    ("0:00:01.00", "0:00:02.00", "before"),
                    ("0:00:02.50", "0:00:03.00", "left edge"),
                    ("0:00:03.00", "0:00:04.00", "right edge"),
                    ("0:00:04.00", "0:00:06.00", "after"),
                ],
                [(row["start"], row["end"], row["text"]) for row in rows],
            )

    def test_render_builds_concat_filter_for_retained_segments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            destination = root / "edited.mp4"
            source.write_bytes(b"source")

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"edited")
                graph = command[command.index("-filter_complex") + 1]
                self.assertIn("concat=n=2:v=1:a=1[joinedv][joineda]", graph)
                self.assertIn(
                    "[joinedv]settb=AVTB,setpts=PTS-STARTPTS[outv]", graph
                )
                self.assertIn("[joineda]aresample=async=1:first_pts=0", graph)
                self.assertIn("[0:v]trim=start=0:duration=2.000000", graph)
                self.assertIn("[1:v]trim=start=0:duration=2.000000", graph)
                self.assertIn("[0:a]atrim=start=0:duration=2.000000", graph)
                self.assertIn("[1:a]atrim=start=0:duration=2.000000", graph)
                self.assertEqual(2, command.count("-i"))
                self.assertEqual(["0.000000", "4.000000"], [
                    command[index + 1]
                    for index, value in enumerate(command)
                    if value == "-ss"
                ])
                self.assertEqual(["2.000000", "2.000000"], [
                    command[index + 1]
                    for index, value in enumerate(command)
                    if value == "-t"
                ])
                return mock.Mock(returncode=0, stderr=b"")

            with mock.patch(
                "review_workspace.subprocess.run", side_effect=fake_run
            ):
                render_timeline_edit(
                    source,
                    destination,
                    [
                        {"source_start": 0.0, "source_end": 2.0},
                        {"source_start": 4.0, "source_end": 6.0},
                    ],
                    Path("ffmpeg.exe"),
                )
            self.assertEqual(b"edited", destination.read_bytes())

    def test_pending_timeline_edit_blocks_approved_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "001.mp4"
            video.write_bytes(b"video")
            load_decisions(root)
            set_decision(root, video.name, "approved")
            save_timeline_edit(
                video,
                {
                    "segments": [
                        {"source_start": 0.0, "source_end": 1.0}
                    ]
                },
            )
            with self.assertRaisesRegex(ValueError, "未应用的时间轴剪辑"):
                selected_video_names(root, require_human_decisions=True)


if __name__ == "__main__":
    unittest.main()
