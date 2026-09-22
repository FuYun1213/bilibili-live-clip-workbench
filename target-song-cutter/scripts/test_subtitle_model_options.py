from pathlib import Path
from unittest.mock import Mock, patch
import json
import tempfile
import unittest

from PIL import Image
import cover_emotes
import cover_layout
import local_publish
import workflow_app
import workflow_app_core as core
import workflow_auto as auto


class SubtitleModelOptionsTests(unittest.TestCase):
    def test_three_phrases_survive_selection_and_public_json_round_trip(self):
        subtitle = "柚雨舍不得删收藏夹｜视频还要传给孙子孙女｜连孙辈看什么都安排好了"
        raw = [{"标题": "柚雨的收藏夹还要留给孙子孙女", "副标题": subtitle,
                "时间戳": [{"开始": "00:01:00", "结束": "00:01:40"}]}]
        selected = core.normalize_selection(raw, "kioi", "narrative")
        self.assertEqual(selected[0]["cover_text_secondary"], "视频还要传给孙子孙女｜连孙辈看什么都安排好了")
        public = core.canonical_selection(selected)
        self.assertEqual(public[0]["副标题"], subtitle)
        self.assertEqual(core.normalize_selection(public, "kioi", "narrative")[0]["subtitle"], subtitle)

    def test_three_phrases_validate_each_sentence_instead_of_total_lower_length(self):
        self.assertEqual(
            cover_emotes.validate_cover_lines("上区有具体内容", "二" * 22 + "｜" + "三" * 22)[1],
            "二" * 22 + "｜" + "三" * 22,
        )
        for bad in ("二" * 23 + "｜第三句", "第二句｜", "第二句｜第三句｜第四句"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cover_emotes.validate_cover_lines("上区有具体内容", bad)
        for bad in ("第一句｜第二句｜第三句｜第四句", "第一句｜｜第三句"):
            with self.assertRaises(core.WorkflowError):
                core.split_cover_copy(bad, "narrative")
        self.assertEqual(core.split_cover_copy("第一句｜第二句", "narrative"), ("第一句", "第二句"))
        self.assertEqual(core.split_cover_copy("完整歌名", "song"), ("完整歌名", ""))
        with self.assertRaises(core.WorkflowError):
            core.split_cover_copy("歌名｜第二句｜第三句", "song")

    def test_three_phrase_render_preserves_all_text_and_non_overlapping_safe_boxes(self):
        for source_region in ("full", "radio-left"):
            with self.subTest(source_region=source_region):
                primary = "柚雨舍不得删收藏夹"
                lower = ["视频还要传给孙子孙女", "连孙辈看什么都安排好了"]
                canvas = Image.new("RGBA", cover_layout.CANVAS, (30, 35, 45, 255))
                layout = cover_layout.render_cover_regions(
                    canvas, primary, "｜".join(lower), {}, source_region=source_region,
                    has_emote=False, fit_font=local_publish.fit_autoclip_font,
                    draw_text=local_publish.draw_autoclip_text,
                )
                regions = layout["regions"]
                self.assertEqual(set(regions), {"primary", "secondary", "tertiary"})
                for key, expected in zip(("primary", "secondary", "tertiary"), [primary, *lower]):
                    self.assertEqual("".join(regions[key]["lines"]), expected)
                    for left, top, right, bottom in regions[key]["boxes"]:
                        self.assertGreaterEqual(left, cover_layout.SAFE_4_3_LEFT)
                        self.assertLessEqual(right, cover_layout.SAFE_4_3_RIGHT)
                        self.assertGreaterEqual(top, 0)
                        self.assertLessEqual(bottom, 1080)
                self.assertLess(
                    max(box[3] for box in regions["secondary"]["boxes"]),
                    min(box[1] for box in regions["tertiary"]["boxes"]),
                )

    def test_model_switch_persists_each_preset_without_changing_other_settings(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(core, "WORKSPACE_ROOT", Path(temp)):
            path = Path(temp) / "workflow-auto.json"
            baseline = auto.default_config(Path(temp))
            baseline["custom_keep"] = "retain"
            baseline["session_quiet_seconds"] = 777
            auto.save_config(path, baseline)
            for label, pair in auto.CODEX_MODEL_PRESETS.items():
                bench = object.__new__(workflow_app.Workbench)
                bench.vars = {"selection_model": Mock(get=Mock(return_value=label)), "status": Mock()}
                bench.append_log = Mock()
                bench._selection_model_changed()
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual((stored["codex_model"], stored["codex_reasoning_effort"]), pair)
                self.assertEqual(stored["custom_keep"], "retain")
                self.assertEqual(stored["session_quiet_seconds"], 777)
                self.assertEqual(stored["asr"], baseline["asr"])
                self.assertIn(label, bench.flow2_prompt_description())
                command = auto.codex_selection_command(
                    ["codex"], "prompt", Path("schema.json"), Path("result.json"),
                    model=stored["codex_model"], reasoning_effort=stored["codex_reasoning_effort"],
                )
                self.assertEqual(command[command.index("--model") + 1], pair[0])
                self.assertEqual(command[command.index("--config") + 1], f'model_reasoning_effort="{pair[1]}"')

    def test_model_save_failure_restores_previous_selection(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(core, "WORKSPACE_ROOT", Path(temp)):
            path = Path(temp) / "workflow-auto.json"
            auto.save_config(path, auto.default_config(Path(temp)))
            before = path.read_bytes()
            bench = object.__new__(workflow_app.Workbench)
            bench.vars = {"selection_model": Mock(get=Mock(return_value="GPT 5.6 / xhigh")), "status": Mock()}
            bench.append_log = Mock()
            with patch.object(auto, "save_config", side_effect=OSError("read only")), patch.object(workflow_app.messagebox, "showerror"):
                bench._selection_model_changed()
            self.assertEqual(path.read_bytes(), before)
            bench.vars["selection_model"].set.assert_called_once_with("GPT 6 / xhigh（默认）")


if __name__ == "__main__":
    unittest.main()
