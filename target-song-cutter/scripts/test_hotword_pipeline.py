from __future__ import annotations
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from asr_hotword_guard import audit_artifact, find_hotword_echoes, is_hotword_echo
from test_asr_hotword_guard import TERMS, ECHO

def ass_text(lines):
    header = "[Script Info]\n[V4+ Styles]\nFormat: Name, Fontname\nStyle: Regular,Arial\n[Events]\n"
    return header + "\n".join(
        f"Dialogue: 0,0:00:{i+1:02d}.00,0:00:{i+2:02d}.00,Regular,,0,0,0,,{text}"
        for i, text in enumerate(lines)
    ) + "\n"

class ArtifactGuardTests(unittest.TestCase):
    def test_list_split_across_adjacent_ass_cues_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"subtitle.ass"
            path.write_text(ass_text(TERMS[3:]), encoding="utf-8")
            result = audit_artifact(path)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["findings"][0]["row_indices"], [0,1,2])

    def test_styled_fullwidth_echo_is_detected(self):
        text = r"{\c&HFFFFFF&}灰泽满Ｈａｚｅｌ\N灰泽满\NＨＡＺＥＬ\N芙娅之魂\N回火测试\N终焉地"
        self.assertTrue(is_hotword_echo(text, TERMS))

    def test_unrelated_proper_names_in_separate_scenes_are_not_a_list(self):
        rows = [{"start_seconds":i*30,"end_seconds":i*30+1,"text":word}
                for i, word in enumerate(TERMS[3:])]
        self.assertEqual(find_hotword_echoes(rows,terms=TERMS), [])

    def test_previous_pass_does_not_authorize_changed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"subtitle.ass"
            path.write_text(ass_text(["这是正常对白。"]),encoding="utf-8")
            before = audit_artifact(path)
            path.write_text(ass_text([ECHO]),encoding="utf-8")
            after = audit_artifact(path)
        self.assertEqual(before["status"],"PASS")
        self.assertEqual(after["status"],"FAIL")
        self.assertNotEqual(before["sha256"],after["sha256"])

    def test_saved_vocabulary_survives_profile_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/"workflow-project.json").write_text("{}",encoding="utf-8")
            directory = root/"analysis/transcript"; directory.mkdir(parents=True)
            old_terms = ["旧名甲甲","旧名乙乙","旧名丙丙"]
            source = directory/"transcript.json"
            source.write_text(json.dumps({"hotword_guard_terms":old_terms,"segments":[]}),encoding="utf-8")
            path = root/"subtitle.ass"
            path.write_text(ass_text(["、".join(old_terms)]),encoding="utf-8")
            with patch("asr_hotword_guard.vocabulary_groups",return_value=[]):
                result = audit_artifact(path)
                metadata = audit_artifact(source)
        self.assertEqual(result["status"],"FAIL")
        self.assertEqual(metadata["status"],"PASS")

    def test_burn_gate_rejects_echo_with_valid_styles(self):
        from burn_ass_subtitles import validate_ass_styles
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"subtitle.ass"
            path.write_text(ass_text([ECHO]),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"热词列表复读"):
                validate_ass_styles(path)
            path.write_text(ass_text(["这个回火测试我打不过。"]),encoding="utf-8")
            validate_ass_styles(path)

    def test_human_review_flag_cannot_bypass_fresh_text_audit(self):
        import audit_media_subtitles as audit
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); path = root/"subtitle.ass"
            path.write_text(ass_text([ECHO]),encoding="utf-8")
            argv = ["audit","--media",str(root/"clip.mp4"),"--ass",str(path),
                    "--asr-json",str(root/"unused.json"),"--human-reviewed"]
            out = io.StringIO()
            with patch.object(sys,"argv",argv), patch.object(
                audit,"run_checked_with_transient_retries",return_value=SimpleNamespace(stdout="10")
            ), redirect_stdout(out):
                code = audit.main()
        self.assertEqual(code,1)
        self.assertIn("hotword-list echo",out.getvalue())

    def test_delivery_gate_rejects_legacy_echo(self):
        import audit_final_delivery as audit
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/"clip.mp4").write_bytes(b"fixture")
            (root/"clip.ass").write_text(ass_text([ECHO]),encoding="utf-8")
            with patch.object(sys,"argv",["audit",str(root)]), patch.object(
                audit,"ffprobe_duration",return_value=10
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(audit.main(),1)

    def test_review_extension_cannot_reload_polluted_source(self):
        from review_workspace import _context_transcript_path
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);directory=root/"analysis/transcript";directory.mkdir(parents=True)
            (directory/"transcript.csv").write_text(
                "start_seconds,end_seconds,text\n0,1,"+ECHO+"\n",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"热词列表复读"):
                _context_transcript_path(root)

    def test_failed_cache_recovery_discards_polluted_source(self):
        from content_slicer import resumable_failed_transcript
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/"source.wav";source.write_bytes(b"fixture")
            (root/"transcript.json").write_text(json.dumps({
                "source":str(source),"segments":[{"start_seconds":0,"end_seconds":1,"text":ECHO}]
            }),encoding="utf-8")
            (root/"transcript-completeness.json").write_text(json.dumps({
                "status":"FAIL","unresolved_windows":[{"core_start":2,"core_end":5}]
            }),encoding="utf-8")
            self.assertIsNone(resumable_failed_transcript(root,source))

class PromptIsolationTests(unittest.TestCase):
    def test_raw_split_list_is_rejected_before_spelling_correction(self):
        from qwen_local_asr import parse_local_result
        payload={"guard_terms":TERMS,"result":{"sentence_info":[
            {"start":index*1000,"end":(index+1)*1000,"sentence":term}
            for index,term in enumerate(TERMS[3:])]}}
        with self.assertRaisesRegex(ValueError,"热词列表复读"):
            parse_local_result(payload,normalize=lambda _: "嗯。")

    def test_host_request_separates_guard_metadata_from_model_context(self):
        import qwen_local_asr as host
        requests=[]
        def run(command, **kwargs):
            request=json.loads(Path(command[command.index("--request")+1]).read_text("utf-8"))
            requests.append(request)
            Path(command[command.index("--output")+1]).write_text(json.dumps({
                "guard_terms":request["guard_terms"],"result":{"sentence_info":[
                    {"start":0,"end":1000,"sentence":"正常说话。"}]}
            }),encoding="utf-8")
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/"source.wav";source.write_bytes(b"fixture")
            with patch.object(host,"_worker_python",return_value=Path(sys.executable)), patch.object(
                host.subprocess,"run",side_effect=run):
                rows,_=host.transcribe_local_media(source,context_terms=TERMS)
        self.assertEqual(requests[0]["context"],"")
        self.assertEqual(requests[0]["guard_terms"],TERMS)
        self.assertEqual(rows[0]["text"],"正常说话。")

    def test_fallback_model_also_receives_no_vocabulary(self):
        from qwen_local_worker import run_request
        calls=[]
        class Model:
            def generate(self,**kwargs):
                calls.append(kwargs)
                if len(calls)==1:raise RuntimeError("CUDA out of memory")
                return [{"sentence_info":[{"start":0,"end":800,"sentence":"嗯。"}]}]
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/"source.wav";source.write_bytes(b"fixture")
            result=run_request({"input":str(source),"model":"local:qwen3-asr-1.7b",
                "fallback_model":"Qwen/Qwen3-ASR-0.6B","cache_root":str(Path(tmp)/"models"),
                "context":ECHO,"guard_terms":TERMS},model_factory=lambda **_:Model())
        self.assertTrue(result["fallback_used"])
        self.assertEqual([call["context"] for call in calls],["",""])
        self.assertFalse(any("guard_terms" in call for call in calls))

if __name__ == "__main__":
    unittest.main()
