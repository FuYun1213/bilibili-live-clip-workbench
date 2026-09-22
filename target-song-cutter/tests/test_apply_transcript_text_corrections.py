import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "apply_transcript_text_corrections.py"


class TranscriptCorrectionTests(unittest.TestCase):
    def test_rewrites_only_text_and_preserves_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "transcript.csv"
            output = root / "corrected.csv"
            corrections = root / "corrections.json"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["start_seconds", "end_seconds", "text"])
                writer.writeheader()
                writer.writerow(
                    {"start_seconds": "1.25", "end_seconds": "2.50", "text": "错词和错词"}
                )
            corrections.write_text(
                json.dumps({"authoritative_replacements": [{"old": "错词", "new": "正词"}]}),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--input",
                    str(source),
                    "--corrections",
                    str(corrections),
                    "--output",
                    str(output),
                ],
                check=True,
            )

            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(
                row,
                {"start_seconds": "1.25", "end_seconds": "2.50", "text": "正词和正词"},
            )

    def test_fails_when_source_phrase_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "transcript.csv"
            output = root / "corrected.csv"
            corrections = root / "corrections.json"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["start_seconds", "end_seconds", "text"])
                writer.writeheader()
                writer.writerow({"start_seconds": "1", "end_seconds": "2", "text": "原文"})
            corrections.write_text(
                json.dumps({"authoritative_replacements": [{"old": "不存在", "new": "新词"}]}),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--input",
                    str(source),
                    "--corrections",
                    str(corrections),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
