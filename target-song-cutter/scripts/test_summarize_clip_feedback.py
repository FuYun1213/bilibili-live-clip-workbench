import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from summarize_clip_feedback import main


class SummarizeClipFeedbackTests(unittest.TestCase):
    def test_repeated_preferences_adjust_rank_but_missing_scale_does_not(self) -> None:
        fields = [
            "source_id", "clip_id", "title", "user_score", "score_scale",
            "user_verdict", "user_notes", "editorial_tags", "topic_key",
            "hook_type", "title_style", "duration_seconds", "danmaku_signal", "sc_signal",
        ]
        rows = [
            {
                "source_id": "v1", "clip_id": "001", "title": "A", "user_score": "5",
                "score_scale": "1-5", "editorial_tags": "self-own|commentary",
                "hook_type": "cold-open", "title_style": "narrative-reversal",
                "danmaku_signal": "strong-confirm", "sc_signal": "trigger:10.0",
            },
            {
                "source_id": "v2", "clip_id": "002", "title": "B", "user_score": "4",
                "score_scale": "1-5", "editorial_tags": "self-own",
                "hook_type": "cold-open", "title_style": "narrative-reversal",
                "danmaku_signal": "confirm", "sc_signal": "trigger:20.0",
            },
            {
                "source_id": "v3", "clip_id": "003", "title": "C", "user_score": "5",
                "score_scale": "1-5", "editorial_tags": "self-own",
                "hook_type": "cold-open", "title_style": "narrative-reversal",
                "danmaku_signal": "strong-confirm", "sc_signal": "trigger:30.0",
            },
            {
                "source_id": "v4", "clip_id": "004", "title": "D", "user_score": "10",
                "score_scale": "", "editorial_tags": "self-own",
                "hook_type": "cold-open", "title_style": "narrative-reversal",
                "danmaku_signal": "strong-confirm", "sc_signal": "trigger:40.0",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip-evaluation.csv"
            output = root / "feedback"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)

            self.assertEqual(main(["--input", str(source), "--output", str(output)]), 0)
            profile = json.loads((output / "feedback-profile.json").read_text(encoding="utf-8"))
            self.assertEqual(profile["rated_clip_count"], 4)
            self.assertEqual(profile["normalized_clip_count"], 3)
            self_own = next(
                item for item in profile["dimensions"]["editorial_tag"]
                if item["value"] == "self-own"
            )
            self.assertEqual(self_own["rated_count"], 3)
            self.assertEqual(self_own["preference_adjustment"], 1)
            sc_trigger = next(
                item for item in profile["dimensions"]["sc_role"]
                if item["value"] == "trigger"
            )
            self.assertEqual(sc_trigger["preference_adjustment"], 1)

            with (output / "feedback-ledger.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                ledger = list(csv.DictReader(handle))
            self.assertEqual(ledger[-1]["normalization_status"], "scale-not-explicit")
            self.assertEqual(ledger[-1]["normalized_score"], "")


if __name__ == "__main__":
    unittest.main()
