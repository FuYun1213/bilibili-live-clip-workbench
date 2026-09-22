"""Real canvas regressions for the subtitle track beneath the waveform."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import test_workbench_review_layout as layout_fixture
import test_timeline_editor as timeline_fixture
import workflow_app


class WorkbenchSubtitleTrackTests(unittest.TestCase):
    tk_scaling = 4.0 / 3.0
    sizes = ("1280x760", "1440x900", "1720x980")
    setUp = layout_fixture.WorkbenchReviewLayoutTests.setUp
    tearDown = layout_fixture.WorkbenchReviewLayoutTests.tearDown
    open_review = layout_fixture.WorkbenchReviewLayoutTests.open_review
    assert_visible_inside_window = layout_fixture.WorkbenchReviewLayoutTests.assert_visible_inside_window
    assert_fully_visible = layout_fixture.WorkbenchReviewLayoutTests.assert_fully_visible

    def load_track(self):
        bench = self.bench
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bench.current_review_ass = directory / "001.ass"
        bench.current_review_ass.write_text(timeline_fixture.ASS, encoding="utf-8-sig")
        bench.current_review_video = directory / "001.mp4"
        bench.current_review_video.write_bytes(b"fixture")
        bench.current_review_dir = directory
        bench.review_dialogue_rows = workflow_app.review_workspace.read_dialogues(bench.current_review_ass)
        bench.review_waveform_data = {"duration": 10.0, "peaks": [0.4] * 100}
        bench.timeline_state = {"segments": [{"source_start": 0.0, "source_end": 10.0}]}
        bench.timeline_zoom = 40.0
        self.enterContext(patch.object(bench, "ensure_timeline_state"))
        self.enterContext(patch.object(bench, "seek_timeline_seconds"))
        self.enterContext(patch.object(bench, "_ensure_current_approved_revision", return_value=True))
        self.enterContext(patch.object(workflow_app.review_workspace, "sync_review_proxy_ass_to_original"))
        bench.filter_review_subtitles()
        return bench.review_dialogue_rows[0]["line_number"]

    def test_subtitle_blocks_handles_and_text_fit_the_actual_canvas(self):
        line = self.load_track()
        bench = self.bench
        for size in self.sizes:
            with self.subTest(size=size):
                self.open_review(size)
                bench.review_subtitle_tree.selection_set(str(line))
                bench.review_subtitle_selected()
                bench.update()
                canvas = bench.review_waveform
                self.assert_fully_visible(canvas)
                blocks = canvas.find_withtag("subtitle-cue")
                handles = canvas.find_withtag("subtitle-handle")
                self.assertTrue(blocks)
                self.assertTrue(handles)
                for item in (*blocks, *handles):
                    box = canvas.bbox(item)
                    self.assertGreaterEqual(box[1], 0)
                    self.assertLessEqual(box[3], canvas.winfo_height(), (size, box, canvas.winfo_height()))

    def test_click_visible_track_loads_editable_text_and_saves_it(self):
        line = self.load_track()
        bench = self.bench
        self.open_review("1280x760")
        canvas = bench.review_waveform
        event = SimpleNamespace(x=64 + 1.5 * bench.timeline_zoom, y=109)
        self.assertLess(event.y, canvas.winfo_height())
        bench.review_editor_notebook.select(bench.review_feedback_tab)
        bench.update()
        bench.timeline_mouse_down(event)
        bench.timeline_mouse_up(event)
        self.assertEqual(bench.review_editor_notebook.select(), str(bench.review_subtitle_tab))
        bench.update()
        self.assert_fully_visible(bench.review_text_editor)
        self.assertEqual(bench._review_edit_line, line)
        self.assertEqual(bench.review_subtitle_tree.selection(), (str(line),))
        self.assertIn("hello world", bench.review_text_editor.get("1.0", "end"))
        bench.review_text_editor.delete("1.0", "end")
        bench.review_text_editor.insert("1.0", "这条字幕可以直接修改")
        self.assertTrue(bench._flush_pending_subtitle_autosave())
        rows = workflow_app.review_workspace.read_dialogues(bench.current_review_ass)
        self.assertEqual(rows[0]["text"], "这条字幕可以直接修改")

    def test_dragging_visible_track_persists_new_timing(self):
        self.load_track()
        bench = self.bench
        self.open_review("1280x760")
        zoom = bench.timeline_zoom
        bench.timeline_mouse_down(SimpleNamespace(x=64 + 1.5 * zoom, y=109))
        bench.timeline_mouse_drag(SimpleNamespace(x=64 + 3.5 * zoom, y=109))
        bench.timeline_mouse_up(SimpleNamespace(x=64 + 3.5 * zoom, y=109))
        rows = workflow_app.review_workspace.read_dialogues(bench.current_review_ass)
        start = workflow_app.review_workspace.parse_ass_time(rows[0]["start"])
        end = workflow_app.review_workspace.parse_ass_time(rows[0]["end"])
        # Canvas coordinates round to screen pixels before ASS centiseconds.
        self.assertAlmostEqual(start, 3.0, delta=1.0 / zoom + 0.01)
        self.assertAlmostEqual(end - start, 1.0, places=2)


class WorkbenchSubtitleTrackHighDpiTests(WorkbenchSubtitleTrackTests):
    tk_scaling = 2.0


if __name__ == "__main__":
    unittest.main()
