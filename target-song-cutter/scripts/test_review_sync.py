import json
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import review_workspace as review
import workflow_app
from test_review_speaker_roles import ASS


class ReviewSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'clip.mp4'
        self.video.write_bytes(b'exact-video')
        self.source = self.root / 'full.mkv'
        self.source.write_bytes(b'full-source')
        self.exact_ass = self.video.with_suffix('.ass')
        self.exact_ass.write_text(ASS, encoding='utf-8-sig')
        self.ass = review.review_proxy_ass_path(self.video)
        self.ass.parent.mkdir(parents=True)
        self.ass.write_bytes(self.exact_ass.read_bytes())
        self.regions = [
            {'source_start': 100.0, 'source_end': 110.0, 'exact_start': 0.0, 'exact_end': 10.0, 'proxy_start': 0.0, 'proxy_end': 10.0},
            {'source_start': 200.0, 'source_end': 210.0, 'exact_start': 10.0, 'exact_end': 20.0, 'proxy_start': 10.0, 'proxy_end': 20.0},
        ]
        review._atomic_json(review.review_proxy_metadata_path(self.video), {
            'streaming': True, 'regions': self.regions, 'source': str(self.source),
            'source_duration': 300.0, 'initial_segments': self.regions,
        })
        self.segments = [{'source_start': 103.0, 'source_end': 110.0}, {'source_start': 200.0, 'source_end': 210.0}]

    def test_render_source_does_not_follow_short_preview_and_missing_source_fails(self):
        self.assertEqual(review.review_media_path(self.video), self.video)
        self.assertEqual(review.timeline_render_source(self.video), self.source)
        self.source.unlink()
        with self.assertRaises(FileNotFoundError):
            review.timeline_render_source(self.video)

    def test_pending_ripple_draft_cannot_replace_unedited_master_ass(self):
        before = self.exact_ass.read_bytes()
        review.delete_ass_interval(self.ass, 0.0, 3.0)
        review.save_timeline_edit(self.video, {'segments': self.segments})
        self.assertFalse(review.sync_review_proxy_ass_to_original(self.video))
        self.assertEqual(self.exact_ass.read_bytes(), before)

    def test_applied_cut_preserves_new_source_mapping_when_reopened(self):
        review.delete_ass_interval(self.ass, 0.0, 3.0)
        expected_ass = self.ass.read_bytes()
        review.finish_review_timeline_metadata(self.video, self.ass, self.segments)
        self.assertEqual(self.exact_ass.read_bytes(), expected_ass)
        self.assertEqual(self.ass.read_bytes(), expected_ass)
        self.assertEqual(review.review_media_to_source_seconds(self.video, self.video, 2.0), 105.0)
        self.assertEqual(review.review_media_to_source_seconds(self.video, self.video, 8.0), 201.0)
        self.assertEqual(review.source_to_review_media_seconds(self.video, self.video, 205.0), 12.0)
        reopened = review.prepare_streaming_review(self.video, Path('ffmpeg'))
        self.assertEqual(reopened['exact_duration'], 17.0)
        self.assertEqual(reopened['initial_segments'][0]['source_start'], 103.0)
        self.assertEqual(review.timeline_duration(reopened['initial_segments']), 17.0)

    def test_apply_and_finish_map_short_clip_and_keep_draft_subtitles(self):
        review.delete_ass_interval(self.ass, 0.0, 3.0)
        expected_ass = self.ass.read_bytes()
        state = {'status': 'pending', 'segments': self.segments}
        review.save_timeline_edit(self.video, state)
        bench = object.__new__(workflow_app.Workbench)
        bench.current_review_video = self.video
        bench.current_review_media = self.video  # The preview now uses the exact MP4.
        bench.current_review_ass = self.ass
        bench.timeline_state = state
        bench.timeline_state_by_video = {str(self.video.resolve()): state}
        bench.timeline_rendering = False
        bench._timeline_approval_request = None
        bench._flush_pending_subtitle_autosave = Mock(return_value=True)
        bench._ensure_current_approved_revision = Mock(return_value=True)
        bench.timeline_segments = lambda: self.segments
        bench.stop_embedded_preview = Mock()
        bench.refresh_timeline_apply_button = Mock()
        bench.vars = {'review_line_status': Mock(), 'review_notes': Mock()}
        bench.vars['review_notes'].get.return_value = ''
        bench.output_queue = queue.Queue()
        bench.after = Mock()
        bench.report_callback_exception = Mock()
        bench.timeline_undo_by_video = {}
        bench.timeline_redo_by_video = {}
        bench.review_waveform_cache = {}
        bench.review_global_mode = False
        bench.load_review_directory = Mock()
        rendered_sources = []
        def render(source, destination, segments, _ffmpeg):
            rendered_sources.append(source)
            self.assertEqual(segments, [{"source_start": 3.0, "source_end": 10.0}, {"source_start": 10.0, "source_end": 20.0}])
            destination.write_bytes(b'edited-video')
        with patch.object(review, 'render_timeline_edit', side_effect=render), patch.object(
            workflow_app.threading, 'Thread', side_effect=lambda *, target, **_kwargs: SimpleNamespace(start=target)
        ):
            self.assertTrue(bench.apply_timeline_edit(confirm=False))
        self.assertEqual(rendered_sources, [self.video])
        _kind, payload = bench.output_queue.get_nowait()
        bench.finish_timeline_render(payload)
        self.assertEqual(self.video.read_bytes(), b'edited-video')
        self.assertEqual(self.exact_ass.read_bytes(), expected_ass)
        self.assertFalse(review.timeline_edit_path(self.video).exists())
        self.assertEqual(review.review_media_to_source_seconds(self.video, self.video, 8.0), 201.0)
        self.assertTrue(review.has_streaming_review(self.video))

    def test_extended_or_missing_range_uses_full_source_clock(self):
        for segments in ([{"source_start": 95.0, "source_end": 110.0}],
                         [{"source_start": 105.0, "source_end": 205.0}]):
            with self.subTest(segments=segments):
                source, mapped = review.timeline_render_plan(self.video, segments)
                self.assertEqual(source, self.source)
                self.assertEqual(mapped, segments)

    def test_commit_rolls_back_video_subtitles_and_mapping_on_failure(self):
        review.save_timeline_edit(self.video, {"segments": self.segments})
        temporary = self.root / 'edited.tmp.mp4'
        temporary.write_bytes(b'edited-video')
        paths = [self.video, self.exact_ass, self.ass,
                 review.review_proxy_metadata_path(self.video), review.timeline_edit_path(self.video)]
        before = {path: path.read_bytes() for path in paths}
        original_finish = review.finish_review_timeline_metadata
        def fail_after_metadata(*args):
            original_finish(*args)
            raise OSError('simulated metadata write failure')
        with patch.object(review, 'finish_review_timeline_metadata', side_effect=fail_after_metadata):
            with self.assertRaisesRegex(OSError, 'simulated'):
                review.commit_timeline_render(self.video, temporary, self.ass, self.segments)
        self.assertEqual(temporary.read_bytes(), b'edited-video')
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_finish_failure_unlocks_apply_and_keeps_draft(self):
        review.save_timeline_edit(self.video, {"segments": self.segments})
        key = str(self.video.resolve())
        state = {"status": "applying", "segments": self.segments}
        bench = object.__new__(workflow_app.Workbench)
        bench.timeline_rendering = True
        bench.timeline_rendering_video_key = key
        bench.timeline_state_by_video = {key: state}
        bench._timeline_approval_request = {"key": key}
        bench._finish_timeline_render_result = Mock(side_effect=PermissionError('video in use'))
        bench.report_callback_exception = Mock()
        bench.refresh_timeline_apply_button = Mock()
        bench.vars = {"review_line_status": Mock()}
        bench.finish_timeline_render({"video": str(self.video)})
        self.assertFalse(bench.timeline_rendering)
        self.assertEqual(state["status"], "pending")
        self.assertIsNone(bench._timeline_approval_request)
        self.assertTrue(review.timeline_edit_path(self.video).is_file())
        bench.report_callback_exception.assert_called_once()
        bench.refresh_timeline_apply_button.assert_called_once()

    def test_callback_error_does_not_stop_queue_or_next_render_completion(self):
        bench = object.__new__(workflow_app.Workbench)
        bench.output_queue = queue.Queue()
        bench.output_queue.put(("waveform", {"error": "broken callback"}))
        bench.output_queue.put(("timeline_render", {"video": str(self.video)}))
        bench.finish_review_waveform = Mock(side_effect=ValueError('broken waveform'))
        bench.finish_timeline_render = Mock()
        bench.report_callback_exception = Mock()
        bench.after = Mock()
        bench._drain_queue()
        bench.finish_timeline_render.assert_called_once_with({"video": str(self.video)})
        bench.report_callback_exception.assert_called_once()
        bench.after.assert_called_once_with(120, bench._drain_queue)


if __name__ == '__main__':
    unittest.main()
