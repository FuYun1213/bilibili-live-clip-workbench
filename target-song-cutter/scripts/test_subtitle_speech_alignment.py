import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audit_media_subtitles as audit

from audit_media_subtitles import (
    cue_speech_metrics,
    cue_speech_warnings,
    speech_events,
    unanchored_cue_warning,
)
from transcribe_review_clips import merge_intervals, tighten_cue_to_speech


class SubtitleSpeechAlignmentTests(unittest.TestCase):
    def test_merge_intervals_closes_only_tiny_gaps(self):
        self.assertEqual(
            merge_intervals([(2.0, 2.5), (1.0, 1.4), (1.45, 1.8), (3.0, 2.0)]),
            [(1.0, 1.8), (2.0, 2.5)],
        )

    def test_early_and_late_cue_is_tightened_to_speech(self):
        cue = {"start_seconds": 1.0, "end_seconds": 2.5, "text": "测试"}
        adjusted, qa = tighten_cue_to_speech(cue, [(1.4, 2.1)])
        self.assertIsNotNone(adjusted)
        self.assertAlmostEqual(adjusted["start_seconds"], 1.34)
        self.assertAlmostEqual(adjusted["end_seconds"], 2.28)
        self.assertEqual(qa["status"], "clamped")
        self.assertEqual(qa["needs_review"], "yes")

    def test_silence_only_cue_is_rejected(self):
        cue = {"start_seconds": 1.0, "end_seconds": 1.7, "text": "幻觉字幕"}
        adjusted, qa = tighten_cue_to_speech(cue, [(2.0, 2.8)])
        self.assertIsNone(adjusted)
        self.assertEqual(qa["status"], "rejected_no_speech")
        self.assertEqual(qa["needs_review"], "yes")

    def test_speech_events_use_regions_without_text(self):
        self.assertEqual(
            speech_events(
                {
                    "regions": [
                        {"start_seconds": 1.25, "end_seconds": 2.0},
                        {"start_seconds": "bad", "end_seconds": 4},
                    ]
                }
            ),
            [(1.25, 2.0)],
        )

    def test_vad_disagreements_are_reported_as_nonfatal_warnings(self):
        warnings = cue_speech_warnings(
            4,
            cue_speech_metrics(1.0, 2.5, [(3.0, 3.4)]),
            min_speech_overlap=0.08,
            min_speech_ratio=0.10,
            max_silent_lead=0.12,
            max_silent_tail=0.35,
        )
        self.assertTrue(warnings)
        self.assertTrue(all(value.startswith("cue 4") for value in warnings))

    def test_review_context_without_local_asr_is_nonfatal(self):
        warning = unanchored_cue_warning(
            "review-context",
            cue_speech_metrics(1.0, 2.0, []),
            speech_supplied=True,
            min_speech_overlap=0.08,
        )
        self.assertIn("full-session review context", warning)

    def test_vad_confirmed_cue_without_local_asr_is_nonfatal(self):
        warning = unanchored_cue_warning(
            "",
            cue_speech_metrics(1.0, 2.0, [(1.2, 1.8)]),
            speech_supplied=True,
            min_speech_overlap=0.08,
        )
        self.assertIn("VAD-confirmed speech", warning)

    def test_silent_normal_cue_without_local_asr_remains_fatal(self):
        warning = unanchored_cue_warning(
            "",
            cue_speech_metrics(1.0, 2.0, []),
            speech_supplied=True,
            min_speech_overlap=0.08,
        )
        self.assertEqual(warning, "")

    def test_final_media_vad_can_replace_stale_timeline_anchors(self):
        speech = [(8.48, 10.85), (12.0, 23.9)]
        first = cue_speech_metrics(8.31, 8.62, speech)
        later = cue_speech_metrics(14.14, 15.46, speech)
        self.assertGreaterEqual(first["overlap"], 0.08)
        self.assertEqual(later["ratio"], 1.0)

    def test_explicit_human_review_allows_intentionally_empty_ass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "clip.mp4"
            media.write_bytes(b"fake")
            ass = root / "clip.ass"
            ass.write_text("[Script Info]\n[Events]\n", encoding="utf-8")
            asr = root / "transcript.json"
            asr.write_text("{}", encoding="utf-8")
            probe = mock.Mock(stdout="10.0\n")
            argv = [
                "audit_media_subtitles.py",
                "--media", str(media),
                "--ass", str(ass),
                "--asr-json", str(asr),
                "--human-reviewed",
            ]
            with (
                mock.patch.object(audit, "run_checked_with_transient_retries", return_value=probe),
                mock.patch.object(sys, "argv", argv),
            ):
                self.assertEqual(audit.main(), 0)

    def test_metrics_expose_early_caption_silence(self):
        metrics = cue_speech_metrics(1.0, 2.5, [(1.4, 2.1)])
        self.assertAlmostEqual(metrics["overlap"], 0.7)
        self.assertAlmostEqual(metrics["silent_lead"], 0.4)
        self.assertAlmostEqual(metrics["silent_tail"], 0.4)


if __name__ == "__main__":
    unittest.main()
