import csv
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import analyze_bililive_xml as legacy
import analyze_engagement as engagement
import annotate_chat_reads as reads
import workflow_app_core as app


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, list(reader)


class OptionalDanmakuTests(unittest.TestCase):
    def test_missing_empty_and_metadata_only_xml_write_empty_evidence(self):
        for kind, content in (("omitted", None), ("missing", None), ("empty", ""),
                              ("metadata", "<i><chatserver>test</chatserver></i>")):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                xml = root / "recording.xml"
                if content is not None:
                    xml.write_text(content, encoding="utf-8")
                output = root / "engagement"
                output.mkdir()
                # A rerun must replace old signals rather than leave false evidence.
                (output / "engagement-hotspots.csv").write_text("stale\n1\n")
                args = ["--output", str(output)]
                if kind != "omitted":
                    args += ["--xml", str(xml)]
                self.assertEqual(engagement.main(args), 0)
                for name in ("engagement-hotspots.csv", "superchats.csv",
                             "repeated-messages.csv", "keyword-context.csv",
                             "danmaku-windows.csv", "danmaku-bursts.csv",
                             *[f"danmaku-{name}-{window}s.csv"
                               for name in ("windows", "bursts") for window in (10, 30, 60)]):
                    fields, rows = read_csv(output / name)
                    self.assertTrue(fields, name)
                    self.assertEqual(rows, [], name)
                summary = json.loads((output / "engagement-summary.json").read_text())
                self.assertEqual(summary["danmaku_count"], 0)
                self.assertEqual(summary["superchat_count"], 0)
                self.assertEqual(summary["discovery_mode"], "transcript_audio_only")
                self.assertEqual(summary["input_status"], {
                    "omitted": "missing_xml", "missing": "missing_xml",
                    "empty": "empty_xml", "metadata": "available",
                }[kind])
                audit = json.loads((output / "timeline-audit.json").read_text())
                self.assertEqual(audit["source_xml"], "" if kind == "omitted" else str(xml.resolve()))
                if kind in ("missing", "omitted"):
                    self.assertFalse(xml.exists())

    def test_sc_only_preserves_original_text_alignment_and_response_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            xml = root / "sc.xml"
            xml.write_text('<i><sc ts="101" user="payer" price="30" time="60">  exact SC body  </sc></i>')
            output = root / "engagement"
            self.assertEqual(engagement.main([
                "--xml", str(xml), "--output", str(output), "--offset-seconds", "2.5",
            ]), 0)
            _, rows = read_csv(output / "superchats.csv")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["raw_text"], "  exact SC body  ")
            self.assertEqual(rows[0]["aligned_time_seconds"], "103.500")
            self.assertEqual(rows[0]["response_search_end_seconds"], "283.500")
            self.assertEqual(rows[0]["post_peak_start_seconds"], "")
            self.assertEqual(read_csv(output / "engagement-hotspots.csv")[1], [])
            summary = json.loads((output / "engagement-summary.json").read_text())
            self.assertEqual(summary["discovery_mode"], "transcript_audio_sc")
            self.assertEqual(summary["superchat_count"], 1)

    def test_corrupt_xml_still_reports_parse_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            xml = root / "broken.xml"
            xml.write_text("<i><d")
            with self.assertRaises(ET.ParseError):
                engagement.main(["--xml", str(xml), "--output", str(root / "output")])

    def test_legacy_analyzer_accepts_no_danmaku_and_preserves_sc(self):
        for content in (None, "", "<i/>", '<i><sc ts="5" price="30">hello</sc></i>'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                xml = root / "recording.xml"
                if content is not None:
                    xml.write_text(content)
                output = root / "engagement"
                self.assertEqual(legacy.main(["--xml", str(xml), "--output", str(output)]), 0)
                self.assertEqual(read_csv(output / "danmaku-bursts.csv")[1], [])
                self.assertEqual(len(read_csv(output / "superchats.csv")[1]), int(bool(content and "<sc" in content)))

    def test_annotation_accepts_missing_or_empty_xml_without_changing_text(self):
        for content in (None, "", "<i/>"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                xml = root / "recording.xml"
                if content is not None:
                    xml.write_text(content)
                transcript = root / "transcript.csv"
                transcript.write_text("start_seconds,end_seconds,text\n0,12,测试录播内容\n", encoding="utf-8-sig")
                self.assertEqual(reads.main([
                    "--xml", str(xml), "--transcript", str(transcript),
                    "--output", str(transcript), "--audit", str(root / "audit.csv"),
                ]), 0)
                self.assertEqual(read_csv(transcript)[1][0]["text"], "测试录播内容")
                self.assertEqual(read_csv(root / "audit.csv")[1], [])

    def test_flow1_then_selection_without_danmaku_in_all_modes(self):
        for mode in ("narrative", "mixed", "song"):
            for kind in ("omitted", "missing", "empty", "metadata"):
                with self.subTest(mode=mode, kind=kind), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source = root / "recording.mp4"
                    source.write_bytes(b"fake media")
                    xml = None if kind == "omitted" else root / "recording.xml"
                    if kind in ("empty", "metadata"):
                        xml.write_text("" if kind == "empty" else "<i/>")
                    state_path = app.init_project(root / "project", source, xml,
                                                 "sumire", mode, "test", "cpu", "int8")
                    _, state = app.load_project(state_path)
                    original_run_external = app.run_external
                    stages = []

                    def run_external(project_root, stage, command):
                        stages.append(stage)
                        if stage == "flow1-transcription":
                            transcript = project_root / "analysis" / "transcript"
                            name = "transcript.csv" if mode == "narrative" else "transcript_multilingual.csv"
                            (transcript / name).write_text(
                                "start_seconds,end_seconds,text\n0,12,测试录播内容\n", encoding="utf-8-sig")
                        else:
                            original_run_external(project_root, stage, command)

                    with mock.patch.object(app, "run_external", side_effect=run_external):
                        outputs = app.step1_transcribe(state_path, state)
                    evidence = state_path.parent / "analysis" / "engagement"
                    self.assertIn(evidence / "engagement-hotspots.csv", outputs)
                    self.assertEqual(read_csv(evidence / "engagement-hotspots.csv")[1], [])
                    self.assertEqual("flow1-read-message-annotation" in stages,
                                     kind == "metadata" and mode in ("narrative", "mixed"))
                    transcript = state_path.parent / "analysis" / "transcript" / (
                        "transcript.filtered.csv" if mode in ("narrative", "mixed") else "transcript_multilingual.csv")
                    context = root / "selection-context.csv"
                    app.build_compact_selection_context(transcript, evidence, context, mode=mode)
                    self.assertEqual(read_csv(context)[1][0]["text"], "测试录播内容")


if __name__ == "__main__":
    unittest.main()
