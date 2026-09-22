import json
import unittest
from pathlib import Path
from unittest.mock import patch
import review_workspace as rw
import test_review_proxy

class StreamingCacheFreshnessTests(unittest.TestCase):
    setUp = test_review_proxy.ReviewProxyTests.setUp
    tearDown = test_review_proxy.ReviewProxyTests.tearDown
    def prepare(self):
        with patch.object(rw, '_media_duration_with_ffmpeg', return_value=200.0):
            return rw.prepare_streaming_review(self.video, Path('ffmpeg'))

    def change_plan(self):
        plan = self.run / 'edit-plan.csv'
        plan.write_text(plan.read_text(encoding='utf-8-sig').replace(',100,110,', ',99.65,110,'), encoding='utf-8-sig')

    def test_reexport_boundary_refreshes_mapping_without_overwriting_new_ass(self):
        self.prepare()
        proxy = rw.review_proxy_ass_path(self.video)
        proxy.write_text(proxy.read_text(encoding='utf-8-sig').replace('第一段','旧审核修改'), encoding='utf-8-sig')
        self.change_plan()
        exact = self.video.with_suffix('.ass')
        exact.write_text(exact.read_text(encoding='utf-8-sig').replace('第一段','新导出字幕'), encoding='utf-8-sig')
        before = exact.read_bytes()
        self.assertFalse(rw.has_streaming_review(self.video))
        fresh = self.prepare()
        self.assertEqual(fresh['regions'][0]['source_start'], 99.65)
        self.assertEqual(exact.read_bytes(), before)
        self.assertEqual(proxy.read_bytes(), before)
        backups = list((rw.review_proxy_directory(self.video) / 'stale-exports').glob('*/*.ass'))
        self.assertEqual(len(backups), 1)
        self.assertIn('旧审核修改', backups[0].read_text(encoding='utf-8-sig'))
        self.assertTrue(rw.has_streaming_review(self.video))

    def test_unchanged_export_preserves_working_subtitle_edits(self):
        initial = self.prepare()
        proxy = rw.review_proxy_ass_path(self.video)
        proxy.write_text('user-edited', encoding='utf-8')
        self.assertEqual(self.prepare(), initial)
        self.assertEqual(proxy.read_text(encoding='utf-8'), 'user-edited')

    def test_replaced_video_with_same_ranges_invalidates_cache(self):
        self.prepare()
        self.video.write_bytes(b're-exported-video-changed')
        self.assertFalse(rw.has_streaming_review(self.video))
        self.prepare()
        self.assertTrue(rw.has_streaming_review(self.video))

    def test_unapplied_draft_survives_reexport(self):
        self.prepare()
        rw.save_timeline_edit(self.video, {'segments':[{'source_start':101, 'source_end':109}]})
        paths = [rw.timeline_edit_path(self.video), rw.review_proxy_ass_path(self.video), rw.review_proxy_metadata_path(self.video), self.video.with_suffix('.ass')]
        saved = {p:p.read_bytes() for p in paths}
        self.change_plan()
        with self.assertRaisesRegex(ValueError, '未应用'):
            self.prepare()
        self.assertEqual(saved, {p:p.read_bytes() for p in paths})

    def test_applied_manual_cut_is_not_reset_to_original_plan(self):
        self.prepare()
        self.video.write_bytes(b'manually-rendered')
        rw.finish_review_timeline_metadata(self.video, None, [{'source_start':102, 'source_end':109}])
        self.assertTrue(rw.has_streaming_review(self.video))
        self.assertEqual(self.prepare()['regions'][0]['source_start'],102)

    def test_legacy_boundary_cache_is_refreshed(self):
        metadata = self.prepare()
        metadata.pop('export_inputs')
        rw._atomic_json(rw.review_proxy_metadata_path(self.video), metadata)
        self.change_plan()
        self.assertFalse(rw.has_streaming_review(self.video))
        self.assertEqual(self.prepare()['regions'][0]['source_start'],99.65)

if __name__ == '__main__':
    unittest.main()
