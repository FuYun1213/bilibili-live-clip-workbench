import copy
import json
import math
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import authoritative_alignment as a
import workflow_app_core as core
import content_slicer as cs
from test_workflow_app_integration import write_flow1_artifacts


def segment(start=10, end=12, text="甲乙"):
    return dict(start_seconds=start, end_seconds=end, text=text, words=[])


def selection(*spans):
    return [dict(clip_id="001", content_type="narrative", timestamps=[
        dict(start_seconds=start,end_seconds=end) for start,end in spans])]


def units():
    return [dict(text="甲",start_time=0.1,end_time=0.8),dict(text="乙",start_time=1.1,end_time=1.8)]


class AlignmentTests(unittest.TestCase):
    def test_timing_restores_exact_canonical_text_and_nonspoken_label(self):
        words=a.restore_words(segment(text="【读SC】甲，乙！"),units())
        self.assertEqual("".join(w["word"] for w in words),"【读SC】甲，乙！")
        self.assertEqual(words[1]["start_seconds"],11.1)
        self.assertEqual(a.spoken_text("【读SC】甲，乙！"),"甲，乙！")

    def test_rejects_mismatched_missing_nonfinite_and_unordered_alignment(self):
        variants=[[],units()[:1], [dict(text="丙",start_time=0,end_time=1)],
                  [dict(text="甲",start_time=float("nan"),end_time=0.8),units()[1]],
                  [units()[0],dict(text="乙",start_time=0.5,end_time=1.8)],
                  [units()[0],dict(text="乙",start_time=1.1,end_time=4)]]
        for values in variants:
            with self.subTest(values=values),self.assertRaises(a.AlignmentError):
                a.restore_words(segment(),values)

    def test_internal_and_over_twenty_second_rows_are_all_checked(self):
        rows=[segment(100,130),segment(200,230),segment(300,330)]
        self.assertEqual(a.crossed_rows(selection((100,130),(210,220),(300,330)),rows),[1])
        self.assertEqual(a.crossed_rows(selection((110,320)),rows),[0,2])
        self.assertEqual(a.crossed_rows(selection((5931,5931.46)),[segment(5931.16,5931.63,"嗯。")]),[0])

    def test_single_interjection_uses_existing_measured_interval_without_interpolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/"source";src.write_bytes(b"source")
            row=segment(5931.16,5931.63,"嗯。")
            aligner=Mock(side_effect=AssertionError("single measured unit needs no subdivision"))
            fixed,audit=a.align_crossed_rows(src,[row],selection((5931,5931.46)),root/"cache",aligner=aligner)
            self.assertEqual(fixed[0]["words"],[dict(word="嗯。",start_seconds=5931.16,end_seconds=5931.63)])
            self.assertEqual(audit[0]["method"],"authoritative-single-unit-vad")
            mapped=cs._map_segments_to_parts(fixed,[cs.EditRange("001",1,5931,5931.46,"","","","")])
            self.assertEqual(mapped[0]["text"],"嗯。")
            self.assertEqual(row["words"],[])
            aligner.assert_not_called()
        # A paragraph or a long VAD row must still obtain acoustic word timing.
        for row in [segment(1,1.5,"甲乙"),segment(1,31,"嗯。"),segment(1,1.5,"abc"),segment(2,1,"嗯")]:
            self.assertIsNone(a.single_unit_vad_words(row))

    def test_only_conflicting_rows_are_aligned_and_cut_text_matches_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/"source";src.write_bytes(b"source")
            original=[segment()];spans=selection((11,12))
            aligner=Mock(return_value=[units()])
            fixed,audit=a.align_crossed_rows(src,original,spans,root/"cache",aligner=aligner)
            mapped=cs._map_segments_to_parts(fixed,[cs.EditRange("001",1,11,12,"","","","")])
            self.assertEqual(mapped[0]["text"],"乙")
            self.assertEqual(original[0]["words"],[])
            self.assertEqual(spans,selection((11,12)))
            self.assertEqual(len(audit),1)
            _,second=a.align_crossed_rows(src,original,spans,root/"cache",aligner=aligner)
            self.assertEqual(second[0]["cache"],"hit")
            aligner.assert_called_once()

    def test_cache_cannot_reuse_different_source_text_or_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/"source";src.write_bytes(b"a")
            base=a.row_cache_key(src,segment())
            self.assertNotEqual(base,a.row_cache_key(src,segment(text="甲丙")))
            self.assertNotEqual(base,a.row_cache_key(src,segment(start=9)))
            src.write_bytes(b"changed")
            self.assertNotEqual(base,a.row_cache_key(src,segment()))

    def test_corrupt_cache_is_recomputed_and_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/"source";src.write_bytes(b"source")
            aligner=Mock(return_value=[units()])
            a.align_crossed_rows(src,[segment()],selection((11,12)),root/"cache",aligner=aligner)
            path=next((root/"cache").glob("*.json"));saved=json.loads(path.read_text())
            saved["units"][0]["text"]="错";path.write_text(json.dumps(saved))
            a.align_crossed_rows(src,[segment()],selection((11,12)),root/"cache",aligner=aligner)
            self.assertEqual(aligner.call_count,2)

    @unittest.skipUnless(os.name == "nt", "Windows venv launcher regression")
    def test_alignment_timeout_stops_only_owned_worker_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"model.safetensors").write_bytes(b"model")
            worker=Mock(pid=4321)
            worker.wait.side_effect=[subprocess.TimeoutExpired("worker",180),0]
            with patch.object(a,"MODEL_PATH",root), \
                 patch("qwen_local_asr._worker_python",return_value=root/"python.exe"), \
                 patch.object(a.subprocess,"Popen",return_value=worker), \
                 patch.object(a.subprocess,"run") as stop:
                with self.assertRaisesRegex(a.AlignmentError,"超时"):
                    a.run_aligner(root/"source",[segment()],root)
            self.assertEqual(stop.call_args.args[0],["taskkill","/PID","4321","/T","/F"])
            self.assertEqual(worker.wait.call_count,2)

    def test_read_sc_prefix_survives_partial_cut_and_authority_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/"source";src.write_bytes(b"source")
            fixed,_=a.align_crossed_rows(src,[segment(text="【读SC】甲，乙！")],selection((11,12)),root/"cache",aligner=Mock(return_value=[units()]))
            core.atomic_json(root/"transcript.json",dict(segments=fixed))
            (root/"transcript.csv").write_text("start_seconds,end_seconds,text\n10,12,【读SC】甲，乙！\n",encoding="utf-8")
            loaded=cs.load_authoritative_segments(root/"transcript.json",root/"transcript.csv")
            mapped=cs._map_segments_to_parts(loaded,[cs.EditRange("001",1,11,12,"","","","")])
            self.assertEqual(mapped[0]["text"],"【读SC】乙！")


class TimingFailureQueueTests(unittest.TestCase):
    def test_timing_failure_has_actionable_status_without_blind_retry(self):
        import workflow_auto as auto
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            job=dict(id="job",creator="sumire",project=str(root/"project"),status="failed",
                     progress_percent=50,current_stage="执行失败",next_retry_at=123)
            state=dict(jobs={"job":job});config={"_output_root":root}
            with patch.object(auto,"defer_automatic_job_for_manual_work",return_value=False), \
                 patch.object(auto,"prepare_job_campaign_scope",return_value=True), \
                 patch.object(auto,"ensure_heavy_job_disk_space"), \
                 patch.object(auto,"save_state"), \
                 patch.object(auto,"materialize_session",side_effect=core.SubtitleTimingNeedsReview("缺少声学时间")):
                auto.process_job(config,state,job,allow_burn=False,allow_upload=False)
            self.assertEqual(job["status"],"awaiting_selection_review")
            self.assertEqual(job["current_stage"],"字幕时间待修复")
            self.assertNotIn("next_retry_at",job)
            self.assertIn("缺少声学时间",job["detail"])
            self.assertEqual(job["selection_gate_version"],auto.SELECTION_GATE_VERSION)
            self.assertTrue(job["subtitle_timing_blocked"])
            with patch.object(auto,"_project_state",return_value={"selection_file":"already-approved.json"}):
                for now in [0,9999999999]:
                    self.assertFalse(auto.job_is_runnable(job,allow_burn=True,allow_upload=True,current_time=now))
                # Normal selection-review jobs keep their established behavior.
                resumed=copy.deepcopy(job);resumed.pop("subtitle_timing_blocked")
                self.assertTrue(auto.job_is_runnable(resumed,allow_burn=False,allow_upload=False,current_time=0))


class ExecutablePlanTests(unittest.TestCase):
    def test_approval_and_export_share_plan_and_edits_invalidate_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/"source.mp4";source.write_bytes(b"source")
            path=core.init_project(root/"project",source,None,"sumire","narrative","model","cpu","int8")
            write_flow1_artifacts(path.parent)
            chosen=root/"selection.json"
            core.atomic_json(chosen,[{"标题":"枝堇解释人设后被原话当场拆穿","副标题":"认真解释｜当场拆穿","时间戳":[[10,40]]}])
            _,state=core.load_project(path)
            core.import_selection(path,state,chosen)
            first=core.prepare_executable_plan(path,state)
            self.assertEqual(state["approvals"]["editorial"]["executable_plan"],first["record"])
            with patch("authoritative_alignment.align_crossed_rows",side_effect=AssertionError("cache missed")):
                self.assertEqual(core.prepare_executable_plan(path,state),first)
            items=core.load_canonical_selection(state);items[0]["timestamps"][0]["start_seconds"]=11
            second=core.prepare_executable_plan(path,state,items)
            self.assertNotEqual(first["input_digest"],second["input_digest"])
            # Modifying the authoritative CSV invalidates the old approval, even
            # when the selection and source recording have not changed.
            csv_path=path.parent/"analysis/transcript/transcript.filtered.csv"
            csv_path.write_text(csv_path.read_text(encoding="utf-8-sig")+"\n",encoding="utf-8-sig")
            third=core.prepare_executable_plan(path,state)
            self.assertNotEqual(first["input_digest"],third["input_digest"])
            with patch.object(a,"MODEL_REVISION","new-pinned-revision"):
                self.assertNotEqual(core.prepare_executable_plan(path,state)["input_digest"],third["input_digest"])
            # Tampered/corrupt prepared items cannot silently replace the plan.
            record=Path(third["record"]);modified=copy.deepcopy(third)
            modified["items"][0]["timestamps"][0]["start_seconds"]=29
            core.atomic_json(record,modified)
            self.assertEqual(core.prepare_executable_plan(path,state)["items"],third["items"])

if __name__ == "__main__":
    unittest.main()
