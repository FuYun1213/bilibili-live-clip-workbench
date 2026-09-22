from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qwen_local_asr import is_local_qwen_model, parse_local_result
from qwen_local_worker import _honor_short_speaker_hint, run_request


class LocalResultTests(unittest.TestCase):
    def test_local_model_selector_is_explicit(self) -> None:
        self.assertTrue(is_local_qwen_model("local:qwen3-asr-auto"))
        self.assertTrue(is_local_qwen_model("LOCAL:QWEN3-ASR-0.6B"))
        self.assertFalse(is_local_qwen_model("qwen-audio-3.0-asr-flash-filetrans"))
        self.assertFalse(is_local_qwen_model("large-v3-turbo"))

    def test_local_files_only_rejects_missing_models(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "audio.wav"
            source.write_bytes(b"placeholder")
            with self.assertRaisesRegex(Exception, "本地模型未安装完整"):
                run_request(
                    {
                        "input": str(source),
                        "model": "local:qwen3-asr-0.6b",
                        "fallback_model": "",
                        "cache_root": str(Path(temporary) / "models"),
                        "device": "cuda:0",
                        "diarization": True,
                        "local_files_only": True,
                    },
                    model_factory=lambda **_kwargs: self.fail(
                        "model factory must not run for a missing offline model"
                    ),
                )

    def test_parse_local_vad_sentences_and_speakers(self) -> None:
        payload = {
            "provider": "qwen3-asr-local",
            "model": "Qwen/Qwen3-ASR-1.7B",
            "fully_local": True,
            "audio_uploaded": False,
            "result": {
                "language": "Chinese",
                "sentence_info": [
                    {"start": 120, "end": 1120, "spk": 0, "sentence": "你好。"},
                    {"start": 1500, "end": 2600, "spk": 1, "text": "晚上好。"},
                ],
            },
        }
        rows, report = parse_local_result(payload)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["start_seconds"], 0.12)
        self.assertEqual(rows[0]["speaker_key"], "session:speaker-0")
        self.assertEqual(rows[0]["speaker_label"], "说话人 1")
        self.assertEqual(rows[1]["speaker_label"], "说话人 2")
        self.assertEqual(rows[1]["text"], "晚上好。")
        self.assertTrue(report["fully_local"])
        self.assertFalse(report["audio_uploaded"])


class WorkerFallbackTests(unittest.TestCase):
    def test_known_speaker_count_bypasses_short_embedding_cutoff(self) -> None:
        calls = []

        class Features:
            shape = (4, 2)

            def detach(self):
                return self

            def cpu(self):
                return self

            def numpy(self):
                return [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [0.1, 0.9]]

        class Backend:
            def spectral_cluster(self, values, count):
                calls.append((values, count))
                return [0, 0, 1, 1]

            def forward(self, _features, **_params):
                return [0, 0, 0, 0]

        class Model:
            cb_model = Backend()

        model = Model()
        _honor_short_speaker_hint(model, 2)
        self.assertEqual(model.cb_model.forward(Features(), oracle_num=2), [0, 0, 1, 1])
        self.assertEqual(calls[0][1], 2)

    def test_cuda_oom_retries_with_06b(self) -> None:
        created_models: list[str] = []

        class FakeModel:
            def __init__(self, model: str) -> None:
                self.model = model

            def generate(self, **_kwargs):
                if "1.7B" in self.model:
                    raise RuntimeError("CUDA out of memory")
                return [
                    {
                        "language": "Chinese",
                        "sentence_info": [
                            {"start": 0, "end": 800, "spk": 0, "sentence": "测试。"}
                        ],
                    }
                ]

        def factory(**kwargs):
            created_models.append(str(kwargs["model"]))
            return FakeModel(str(kwargs["model"]))

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "audio.wav"
            source.write_bytes(b"placeholder")
            payload = run_request(
                {
                    "input": str(source),
                    "model": "local:qwen3-asr-1.7b",
                    "fallback_model": "Qwen/Qwen3-ASR-0.6B",
                    "cache_root": str(Path(temporary) / "models"),
                    "device": "cuda:0",
                    "diarization": True,
                },
                model_factory=factory,
            )

        self.assertEqual(len(created_models), 2)
        self.assertIn("1.7B", created_models[0])
        self.assertIn("0.6B", created_models[1])
        self.assertTrue(payload["fallback_used"])
        self.assertEqual(payload["model"], "Qwen/Qwen3-ASR-0.6B")
        self.assertFalse(payload["audio_uploaded"])


if __name__ == "__main__":
    unittest.main()
