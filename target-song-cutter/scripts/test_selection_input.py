from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workflow_app_core as core
import workflow_auto as auto


class SelectionInputTests(unittest.TestCase):
    def write_source(self, root):
        source = root / "transcript.csv"
        text = "这是完整的对话，正文末尾也必须保留。"
        fields = ["start_seconds", "end_seconds", "speaker_id", "speaker_key",
                  "speaker_label", "language", "avg_logprob", "raw_model_output", "text"]
        with source.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(dict(start_seconds=10, end_seconds=40, speaker_id=2,
                                 speaker_key="session:speaker-2", speaker_label="说话人 3",
                                 language="Chinese" * 2000, avg_logprob=0,
                                 raw_model_output="irrelevant" * 2000, text=text))
        return source, text

    def test_context_drops_bloated_metadata_without_truncating_dialogue_or_speakers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, text = self.write_source(root)
            target = root / "selection-context.csv"
            stats = core.build_compact_selection_context(source, root, target, mode="narrative", max_source_seconds=50)
            with target.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["text"], text)
            self.assertEqual(rows[0]["speaker_key"], "session:speaker-2")
            self.assertEqual(rows[0]["start_seconds"], "10")
            self.assertNotIn("language", rows[0])
            self.assertNotIn("raw_model_output", rows[0])
            self.assertLess(stats["selected_bytes"], 1000)
            self.assertIn("language", stats["omitted_fields"])

    def test_empty_clamped_context_does_not_copy_back_out_of_range_dialogue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _ = self.write_source(root)
            target = root / "selection-context.csv"
            stats = core.build_compact_selection_context(source, root, target, mode="narrative", max_source_seconds=5)
            self.assertEqual(stats["selected_rows"], 0)
            with target.open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

    def make_material(self, root, *, text="完整正文末尾"):
        (root / "selection-context.csv").write_text("start_seconds,end_seconds,text\n0,40," + text + "\n", encoding="utf-8-sig")
        core.atomic_json(root / "selection-context-stats.json", {"selected_rows": 1, "source_duration_seconds": 120})

    def test_inline_material_preserves_last_line_and_records_input_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_material(root, text="完整对白" * 3000 + "真实落点")
            value = auto.inline_selection_materials(root)
            material = json.loads(value[value.index("{"):])
            self.assertTrue(material["selection-context.csv"].splitlines()[-1].endswith("真实落点"))
            audit = json.loads((root / "selection-input-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["transport"], "inline-complete-materials")
            self.assertIn("selection-context.csv", audit["file_sha256"])

    def test_missing_or_oversized_material_is_input_failure_instead_of_no_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(auto.SelectionNeedsReview):
                auto.inline_selection_materials(root)
            self.make_material(root, text="x" * (512 * 1024))
            with self.assertRaisesRegex(auto.SelectionNeedsReview, "预算"):
                auto.inline_selection_materials(root)

    def test_model_invocation_receives_complete_material_on_stdin(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            directory = project / "selection"
            directory.mkdir()
            self.make_material(directory, text="最后一行的结论不能丢")
            prompt = directory / "selection-prompt.md"
            prompt.write_text("只分析本批材料", encoding="utf-8")
            output = directory / "selection.codex.json"
            state = {"config": {"mode": "narrative", "creator": "sumire"}}
            def run_external(_root, _stage, command, *, stdin_text):
                self.assertIn("最后一行的结论不能丢", stdin_text)
                self.assertIn("不要调用命令读取文件", stdin_text)
                self.assertEqual(command[-1], "-")
                core.atomic_json(output, {"选片": []})
            with patch.object(core, "build_selection_prompt", return_value=(prompt, None, None)), patch.object(
                auto, "recover_cached_selection", return_value=None
            ), patch.object(auto, "recover_previous_selection", return_value=None), patch.object(
                auto, "resolve_codex_command", return_value=["codex"]
            ), patch.object(core, "run_external", side_effect=run_external), patch.object(
                auto, "repair_selection_titles", side_effect=lambda p, _:p
            ), patch.object(auto, "repair_selection_boundaries", side_effect=lambda p, _:p), patch.object(
                auto, "filter_selection_to_source_duration", return_value=([], [], 120)
            ):
                self.assertEqual(auto.invoke_codex_selection({}, project / "workflow-project.json", state), output)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), [])

    def test_empty_summary_distinguishes_short_source_and_limited_candidate_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "selection"
            directory.mkdir()
            path = directory / "selection-context-stats.json"
            core.atomic_json(path, {"source_duration_seconds": 17.37, "selected_rows": 1})
            self.assertIn("17.37 秒", auto.empty_selection_detail(root, "narrative"))
            core.atomic_json(path, {"source_duration_seconds": 8000, "selected_rows": 900, "selected_unique_seconds": 3600})
            detail = auto.empty_selection_detail(root, "narrative")
            self.assertIn("900 行", detail)
            self.assertIn("不代表整场", detail)


if __name__ == "__main__":
    unittest.main()
