import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import content_slicer as cutter
import media_packaging as media
import qwen_local_asr as adapter
import qwen_local_flow as flow
import qwen_local_worker as worker
import workflow_auto as auto


class EmptySpeechTests(unittest.TestCase):
    def payload(self, regions):
        class Model:
            vad_model = object()
            vad_kwargs = {}
            def generate(self, **kwargs):
                return [{"key": "short", "text": "", "timestamp": []}]
            def inference(self, *args, **kwargs):
                return [{"key": "short", "value": regions}]
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "short.wav"
            source.write_bytes(b"placeholder")
            return worker.run_request({"input": str(source), "cache_root": tmp,
                "model": "local:qwen3-asr-auto", "device": "cpu"},
                model_factory=lambda **kwargs: Model())

    def test_empty_vad_is_successful_and_reported(self):
        rows, report = adapter.parse_local_result(self.payload([]))
        self.assertEqual(rows, [])
        self.assertIs(report["no_speech"], True)

    def test_empty_asr_with_detected_speech_remains_failure(self):
        with self.assertRaisesRegex(worker.LocalQwenWorkerError, "VAD 未确认无语音"):
            self.payload([[100, 900]])

    def test_missing_sentence_timing_is_not_silently_accepted(self):
        with self.assertRaises(adapter.LocalQwenAsrError):
            adapter.parse_local_result({"result": {"text": "确实有人说话"}})

    def test_verified_empty_flow_writes_valid_empty_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/"short.wav"; source.write_bytes(b"placeholder")
            out=Path(tmp)/"out"
            args=SimpleNamespace(input=source, output=out, model="local:qwen3-asr-auto")
            with patch.object(flow, "media_duration", return_value=9.599), patch.object(
                flow, "transcribe_local_media", return_value=([], {"no_speech": True})):
                self.assertEqual(flow.run_qwen_local_flow1(args, multilingual=False), 0)
            self.assertEqual(json.loads((out/"transcript.json").read_text())["segments"], [])
            completeness=json.loads((out/"transcript-completeness.json").read_text())
            self.assertEqual(completeness["status"], "PASS")
            self.assertIs(completeness["no_speech"], True)
            self.assertTrue((out/"transcript.csv").exists())

    def test_empty_unverified_transcript_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/"short.wav"; source.touch()
            args=SimpleNamespace(input=source, output=Path(tmp)/"out", model="local:qwen3-asr-auto")
            with patch.object(flow, "media_duration", return_value=9), patch.object(
                flow, "transcribe_local_media", return_value=([], {})):
                with self.assertRaises(adapter.LocalQwenAsrError):
                    flow.run_qwen_local_flow1(args, multilingual=False)

    def test_empty_job_finishes_without_selection_and_retains_existing_edits(self):
        state={"steps": {"flow1": {"status": "completed"}}, "transcription_no_speech": True}
        job={"status": "failed", "next_retry_at": 100}
        with patch.object(auto, "_set_job_progress") as progress:
            self.assertTrue(auto.finalize_no_speech_job({}, {}, job, state))
            self.assertEqual(job["status"], "no_candidates")
            self.assertNotIn("next_retry_at", job)
            self.assertEqual(progress.call_args.args[3], 100)
            state["selection_file"]="existing.json"
            self.assertFalse(auto.finalize_no_speech_job({}, {}, job, state))


class VideoTailTests(unittest.TestCase):
    def test_matroska_video_end_is_not_audio_duration(self):
        data={"streams": [{"codec_type": "video", "width":1920,"height":1080,
            "avg_frame_rate":"60/1", "tags":{"DURATION":"01:28:18.620000000"}},
            {"codec_type":"audio"}], "format":{"duration":"5428.622"}}
        with patch.object(media.subprocess, "run", return_value=subprocess.CompletedProcess([],0,stdout=json.dumps(data))):
            metadata=media.probe_media(Path('ffmpeg.exe'),Path('source.mkv'))
        self.assertAlmostEqual(metadata["video_end_seconds"],5298.62)
        self.assertAlmostEqual(metadata["duration"],5428.622)

    def test_rejects_audio_only_tail_before_encoder(self):
        part=cutter.EditRange("009",1,5348.12,5363.47,"尾段","","","")
        with patch.object(media,"probe_media",return_value={"fps":60,"video_end_seconds":5298.62}):
            with self.assertRaisesRegex(ValueError,"没有视频帧"):
                cutter.build_slice_command('ffmpeg','source.mkv','source.mkv',[part],'out.mp4')

    def test_batch_checks_invalid_tail_before_exporting_good_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/"source.mkv"; source.touch()
            parts=[cutter.EditRange("001",1,1,2,"good","","",""),
                   cutter.EditRange("009",1,12,13,"bad","","","")]
            args=SimpleNamespace(audio=source,video=source,plan=Path(tmp)/"plan.csv",
                output=Path(tmp)/"out",timeline_tolerance=1,tail_padding=0,
                gift_acks=None,preset="veryfast",crf=20)
            with patch.object(cutter,"media_duration",return_value=15), patch.object(
                cutter,"load_plan",return_value=parts), patch.object(cutter,"ensure_ffmpeg",return_value="ffmpeg"), patch.object(
                media,"probe_media",return_value={"fps":60,"video_end_seconds":10}), patch.object(cutter,"run_command") as encode:
                with self.assertRaisesRegex(ValueError,"没有视频帧"):
                    cutter.export(args)
                encode.assert_not_called()

    def test_failed_external_process_exposes_diagnostics(self):
        result=subprocess.CompletedProcess([],22,stdout="Could not open encoder before EOF")
        log=io.StringIO()
        with patch.object(cutter.subprocess,"run",return_value=result), contextlib.redirect_stderr(log):
            with self.assertRaises(subprocess.CalledProcessError):
                cutter.run_command(['ffmpeg','fixture'])
        self.assertIn("Could not open encoder before EOF",log.getvalue())

if __name__ == '__main__':
    unittest.main()
