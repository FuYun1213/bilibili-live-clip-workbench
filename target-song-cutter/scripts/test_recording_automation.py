import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import recording_automation as automation


class RecordingAutomationTests(unittest.TestCase):
    def make_config(self, root: Path) -> dict:
        incoming = root / "incoming"
        incoming.mkdir()
        path = root / "config.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "watch_roots": ["incoming"],
                    "output_root": "runs",
                    "state_file": "runs/state.json",
                    "stable_seconds": 0,
                    "initial_scan": "ignore-existing",
                    "creator_default": "sumire",
                    "creator_rules": [],
                    "cookie_file": str(root / "private" / "cookies.json"),
                    "publish": {"enabled": False, "tid": 0, "copyright": 1},
                }
            ),
            encoding="utf-8",
        )
        return automation.load_config(path)

    def test_init_ignores_existing_and_scan_registers_new(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.make_config(root)
            old = root / "incoming" / "old.mp4"
            old.write_bytes(b"old")
            state = automation.initialize(config)
            self.assertIn(str(old.resolve()), state["known"])

            new = root / "incoming" / "new.mp4"
            new.write_bytes(b"new")
            os.utime(new, (time.time() - 5, time.time() - 5))
            registered = automation.scan_state(config, state)
            self.assertEqual(len(registered), 1)
            self.assertEqual(state["jobs"][registered[0]]["status"], "discovered")

    def test_content_type_inference_routes_song_and_narrative(self):
        self.assertEqual(
            automation.infer_content_type({"title": "翻唱《夏天的风》"}),
            "song",
        )
        self.assertEqual(
            automation.infer_content_type({"title": "主播点评直播间趣事"}),
            "narrative",
        )

    def test_approval_digest_invalidates_after_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.make_config(root)
            plan = root / "job" / "analysis" / "transcript" / "edit-plan.csv"
            plan.parent.mkdir(parents=True)
            plan.write_text(
                "slice_id,order,start_seconds,end_seconds,title,outline,hook,reason,keep\n"
                "a,1,0,5,t,o,h,r,1\n",
                encoding="utf-8",
            )
            job = {"work_dir": str(root / "job"), "approvals": {}}
            digest = automation.approval_digest(config, job, "editorial")
            job["approvals"]["editorial"] = {"digest": digest}
            self.assertTrue(automation.approval_valid(config, job, "editorial"))
            plan.write_text(plan.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            self.assertFalse(automation.approval_valid(config, job, "editorial"))


    def test_creator_subtitle_styles_match_dedicated_templates(self):
        self.assertEqual(automation.subtitle_style("kioi"), "Kioi")
        self.assertEqual(automation.subtitle_style("sumire"), "Sumire")
        self.assertEqual(automation.subtitle_style("viridis"), "Viridis")
        self.assertEqual(automation.subtitle_style("yuchu"), "Yuchu")
        self.assertEqual(automation.subtitle_style("unknown"), "Regular")

    def test_subtitle_vad_review_blocks_unheard_flagged_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "subtitle-vad-qa.csv"
            path.write_text(
                "clip,text,needs_review,reviewed,decision\n"
                "a.mp4,幻觉字幕,yes,no,\n",
                encoding="utf-8-sig",
            )
            with self.assertRaises(automation.AutomationError):
                automation.validate_subtitle_vad_qa(path)
            path.write_text(
                "clip,text,needs_review,reviewed,decision\n"
                "a.mp4,幻觉字幕,yes,yes,drop\n",
                encoding="utf-8-sig",
            )
            automation.validate_subtitle_vad_qa(path)


if __name__ == "__main__":
    unittest.main()
