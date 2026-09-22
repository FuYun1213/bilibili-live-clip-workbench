import csv
import tempfile
import unittest
from pathlib import Path

from annotate_chat_reads import main


class ChatAnnotationCliTests(unittest.TestCase):
    def test_in_place_transcript_annotation_is_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "transcript.filtered.csv"
            with transcript.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=("start_seconds", "end_seconds", "text")
                )
                writer.writeheader()
                writer.writerow({
                    "start_seconds": "100.000",
                    "end_seconds": "103.000",
                    "text": "今天晚上吃什么火锅",
                })
            xml = root / "record.xml"
            xml.write_text(
                '<i><d p="99,1,25,16777215,0,0,user,0">今天晚上吃什么火锅</d></i>',
                encoding="utf-8",
            )
            audit = root / "read-message-annotations.csv"

            self.assertEqual(main([
                "--transcript", str(transcript),
                "--xml", str(xml),
                "--output", str(transcript),
                "--audit", str(audit),
            ]), 0)
            with transcript.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["text"], "【读弹幕】今天晚上吃什么火锅")
            self.assertTrue(audit.is_file())
            self.assertFalse(transcript.with_name(transcript.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
