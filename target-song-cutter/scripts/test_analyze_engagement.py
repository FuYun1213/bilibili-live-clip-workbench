import csv
import json
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_engagement import main


class AnalyzeEngagementTests(unittest.TestCase):
    def test_multiscale_outputs_alignment_and_sc_review_fields(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<i>
  <d p="1,1,25,16777215,0,0,0,0" user="u1">first</d>
  <d p="2,1,25,16777215,0,0,0,0" user="u2">same</d>
  <d p="3,1,25,16777215,0,0,0,0" user="u3">same</d>
  <d p="100,1,25,16777215,0,0,0,0" user="u1">wow</d>
  <d p="101,1,25,16777215,0,0,0,0" user="u2">wow</d>
  <d p="102,1,25,16777215,0,0,0,0" user="u3">turn</d>
  <d p="103,1,25,16777215,0,0,0,0" user="u4">turn</d>
  <d p="104,1,25,16777215,0,0,0,0" user="u5">payoff</d>
  <sc ts="101" user="payer" price="30" time="60">  exact SC body  </sc>
</i>
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "recording.xml"
            output = root / "engagement"
            source.write_text(xml, encoding="utf-8")

            result = main(
                [
                    "--input", str(source), "--output", str(output),
                    "--windows", "10,30,60", "--offset-seconds", "2.5",
                    "--alignment-anchor", "shared spoken SC",
                ]
            )
            self.assertEqual(result, 0)
            for name in (
                "danmaku-windows-10s.csv",
                "danmaku-windows-30s.csv",
                "danmaku-windows-60s.csv",
                "engagement-hotspots.csv",
                "engagement-summary.json",
                "timeline-audit.json",
                "superchats.csv",
            ):
                self.assertTrue((output / name).is_file(), name)

            with (output / "danmaku-windows-10s.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertGreater(len(rows), 10)
            self.assertTrue(any(int(row["count"]) == 0 for row in rows))
            self.assertTrue(any("burst" in row["signal_labels"] for row in rows))
            self.assertTrue(any("\u00d7" in row["top_messages"] for row in rows if row["top_messages"]))
            self.assertIn("spam_risk", rows[0])
            self.assertIn("message_diversity_ratio", rows[0])
            self.assertTrue(any(float(row["user_coverage_ratio"]) > 0 for row in rows))

            with (output / "superchats.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                sc = next(csv.DictReader(handle))
            self.assertEqual(sc["source_time_seconds"], "101.000")
            self.assertEqual(sc["aligned_time_seconds"], "103.500")
            self.assertEqual(sc["raw_text"], "  exact SC body  ")
            self.assertEqual(sc["editorial_role"], "unreviewed")
            self.assertEqual(sc["response_search_start_seconds"], "103.500")
            self.assertEqual(sc["response_search_end_seconds"], "283.500")
            self.assertIn("post_peak_lag_seconds", sc)

            audit = json.loads((output / "timeline-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["offset_seconds"], 2.5)
            self.assertEqual(audit["alignment_anchor"], "shared spoken SC")

    def test_standard_bilibili_sender_hash_drives_unique_user_count(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<i>
  <d p="1,1,25,16777215,0,0,hash-a,0">same</d>
  <d p="2,1,25,16777215,0,0,hash-b,0">same</d>
  <d p="3,1,25,16777215,0,0,hash-c,0">same</d>
  <d p="4,1,25,16777215,0,0,hash-d,0">turn</d>
</i>
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "recording.xml"
            output = root / "engagement"
            source.write_text(xml, encoding="utf-8")

            self.assertEqual(
                main(["--input", str(source), "--output", str(output), "--windows", "10"]),
                0,
            )
            with (output / "danmaku-windows-10s.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["unique_users"], "4")
            self.assertEqual(row["user_coverage_ratio"], "1.000")
            self.assertIn("crowd-repeat", row["signal_labels"])
            self.assertNotIn("spam-risk", row["signal_labels"])


if __name__ == "__main__":
    unittest.main()
