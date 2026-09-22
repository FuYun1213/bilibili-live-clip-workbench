from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from qwen_asr import (
    QwenAsrClient,
    QwenAsrConfig,
    QwenAsrError,
    parse_qwen_result,
    refine_transcript,
)
from qwen_flow import run_qwen_flow1
from transcribe_review_clips import write_ass


SAMPLE_RESULT = {
    "transcripts": [
        {
            "language": "zh",
            "sentences": [
                {
                    "sentence_id": 1,
                    "begin_time": 120,
                    "end_time": 1320,
                    "speaker_id": 0,
                    "text": "你好 world!",
                    "words": [
                        {"begin_time": 120, "end_time": 450, "text": "你好"},
                        {
                            "begin_time": 500,
                            "end_time": 1020,
                            "text": " world",
                            "punctuation": "!",
                        },
                    ],
                },
                {
                    "sentence_id": 2,
                    "begin_time": 1600,
                    "end_time": 2300,
                    "speaker_id": 1,
                    "text": "晚上好。",
                    "words": [],
                },
            ],
        }
    ]
}


class QwenResultTests(unittest.TestCase):
    def test_parse_preserves_speakers_offsets_and_english_spaces(self) -> None:
        rows = parse_qwen_result(
            SAMPLE_RESULT,
            offset_seconds=10.0,
            chunk_index=2,
            normalize=lambda value: value.strip(),
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["start_seconds"], 10.12)
        self.assertEqual(rows[0]["speaker_id"], 0)
        self.assertEqual(rows[0]["speaker_key"], "chunk-003:speaker-0")
        self.assertEqual(rows[0]["speaker_label"], "分段 3 · 说话人 1")
        self.assertEqual(rows[0]["words"][1]["word"], " world!")
        self.assertEqual(rows[1]["speaker_label"], "分段 3 · 说话人 2")


class QwenClientTests(unittest.TestCase):
    def test_upload_submit_poll_and_download(self) -> None:
        submitted: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/api/v1/uploads":
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "data": {
                            "policy": "policy",
                            "signature": "signature",
                            "upload_dir": "temporary/test",
                            "upload_host": "https://upload.example",
                            "oss_access_key_id": "access-key",
                            "x_oss_object_acl": "private",
                            "x_oss_forbid_overwrite": "true",
                            "max_file_size_mb": 10,
                        }
                    },
                )
            if request.method == "POST" and request.url.host == "upload.example":
                return httpx.Response(200, request=request)
            if (
                request.method == "POST"
                and request.url.path == "/api/v1/services/audio/asr/transcription"
            ):
                submitted.update(json.loads(request.content.decode("utf-8")))
                submitted["resolve_header"] = request.headers.get(
                    "X-DashScope-OssResourceResolve"
                )
                return httpx.Response(
                    200, request=request, json={"output": {"task_id": "task-1"}}
                )
            if request.method == "GET" and request.url.path == "/api/v1/tasks/task-1":
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "output": {
                            "task_status": "SUCCEEDED",
                            "results": [
                                {
                                    "subtask_status": "SUCCEEDED",
                                    "transcription_url": "https://result.example/out.json",
                                }
                            ],
                        }
                    },
                )
            if request.method == "GET" and request.url.host == "result.example":
                return httpx.Response(200, request=request, json=SAMPLE_RESULT)
            return httpx.Response(404, request=request, json={"message": "unexpected"})

        transport = httpx.MockTransport(handler)
        http = httpx.Client(transport=transport)
        config = QwenAsrConfig(
            base_url="https://api.example/api/v1",
            upload_base_url="https://api.example/api/v1",
            poll_seconds=0.0,
            speaker_count=2,
        )
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "sample.flac"
            audio.write_bytes(b"fake flac")
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}):
                with QwenAsrClient(config, client=http, sleep=lambda _seconds: None) as client:
                    result, task_id = client.transcribe_local_file(
                        audio,
                        language_hints=["zh", "en"],
                        hotwords=["VirtuaReal"],
                    )

        self.assertEqual(task_id, "task-1")
        self.assertEqual(result, SAMPLE_RESULT)
        self.assertEqual(submitted["resolve_header"], "enable")
        self.assertTrue(submitted["parameters"]["diarization_enabled"])
        self.assertEqual(submitted["parameters"]["speaker_count"], 2)
        self.assertEqual(submitted["parameters"]["language_hints"], ["zh", "en"])
        self.assertNotIn("vocabulary", submitted["parameters"])
        self.assertTrue(submitted["input"]["file_urls"][0].startswith("oss://"))


class RefinementTests(unittest.TestCase):
    def test_refinement_accepts_conservative_edit_and_rejects_hallucination(self) -> None:
        records = [
            {"text": "今天天汽很好", "words": [{"word": "天气"}]},
            {"text": "早上好", "words": [{"word": "早上好"}]},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            content = json.dumps(
                {
                    "rows": [
                        {"id": 0, "text": "今天天气很好"},
                        {"id": 1, "text": "这里添加了完全无关的一大段虚构内容"},
                    ]
                },
                ensure_ascii=False,
            )
            return httpx.Response(
                200,
                request=request,
                json={"choices": [{"message": {"content": content}}]},
            )

        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}):
            revised, report = refine_transcript(
                records,
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )

        self.assertEqual(revised[0]["text"], "今天天气很好")
        self.assertEqual(revised[0]["raw_text"], "今天天汽很好")
        self.assertEqual(revised[0]["words"], [])
        self.assertEqual(revised[1]["text"], "早上好")
        self.assertEqual(report["accepted_changes"], 1)
        self.assertEqual(report["rejected_changes"], 1)


class WorkflowSafetyTests(unittest.TestCase):
    def test_flow_refuses_cloud_upload_without_explicit_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "recording.mp4"
            source.write_bytes(b"placeholder")
            args = SimpleNamespace(
                input=source,
                output=Path(temporary) / "out",
                model="qwen-audio-3.0-asr-flash-filetrans",
            )
            with patch.dict(os.environ, {"QWEN_ASR_ALLOW_UPLOAD": "false"}):
                with self.assertRaisesRegex(QwenAsrError, "QWEN_ASR_ALLOW_UPLOAD=true"):
                    run_qwen_flow1(args, multilingual=False)

    def test_ass_event_name_uses_speaker_label(self) -> None:
        template = """[Script Info]
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname
Style: Default,Arial

[Events]
"""
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "subtitle.ass"
            write_ass(
                destination,
                template,
                [
                    {
                        "start_seconds": 1.0,
                        "end_seconds": 2.0,
                        "text": "测试字幕",
                        "speaker_label": "说话人 2",
                    }
                ],
                "Default",
                "Fallback",
                0.0,
            )
            ass = destination.read_text(encoding="utf-8-sig")

        self.assertIn(",Default,说话人 2,0,0,0,,测试字幕", ass)


if __name__ == "__main__":
    unittest.main()
