#!/usr/bin/env python3
"""Host-side adapter for the isolated, fully local Qwen3-ASR worker."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

from qwen_local_worker import DEFAULT_FALLBACK_MODEL, resolve_model_id
from asr_hotword_guard import require_no_hotword_echoes
from windows_process import hidden_subprocess_kwargs


DEFAULT_LOCAL_MODEL = "local:qwen3-asr-auto"
LOCAL_MODEL_CHOICES = (
    DEFAULT_LOCAL_MODEL,
    "local:qwen3-asr-1.7b",
    "local:qwen3-asr-0.6b",
)
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_ROOT = WORKSPACE_ROOT / "models" / "qwen3-asr"
DEFAULT_WORKER_PYTHON = (
    Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    / "target-song-cutter" / "qwen-asr-runtime" / "Scripts" / "python.exe"
)
WORKER_SCRIPT = Path(__file__).resolve().with_name("qwen_local_worker.py")


class LocalQwenAsrError(RuntimeError):
    pass


def is_local_qwen_model(model: str) -> bool:
    return str(model or "").strip().casefold().startswith("local:qwen3-asr-")


def _worker_python() -> Path:
    configured = str(os.getenv("QWEN_LOCAL_PYTHON") or "").strip()
    candidate = Path(configured).expanduser() if configured else DEFAULT_WORKER_PYTHON
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise LocalQwenAsrError(
            f"找不到本地 Qwen 隔离运行时：{candidate}；请先执行本地模型安装"
        )
    return candidate


def _speaker_value(value: Any) -> int | str | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def parse_local_result(
    payload: dict[str, Any],
    *,
    normalize: Callable[[str], str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if payload.get("error"):
        raise LocalQwenAsrError(str(payload["error"]))
    raw = payload.get("result")
    if not isinstance(raw, dict):
        raise LocalQwenAsrError("本地 Qwen 结果缺少 result")
    sentence_info = raw.get("sentence_info")
    if not isinstance(sentence_info, list):
        raise LocalQwenAsrError(
            "本地 Qwen 没有返回 VAD 句级时间；无法生成可靠字幕"
        )
    # Check raw output before normalization can disguise a copied word list.
    guard_rows = []
    for item in sentence_info:
        if not isinstance(item, dict):
            continue
        guard_row = dict(item)
        try:
            guard_row["start_seconds"] = float(item["start"]) / 1000.0
            guard_row["end_seconds"] = float(item["end"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            pass
        guard_rows.append(guard_row)
    require_no_hotword_echoes(guard_rows, terms=payload.get("guard_terms", []))
    normalizer = normalize or (lambda value: value.strip())
    language = str(raw.get("language") or "auto").strip()
    # FunASR concatenates the language label across VAD chunks.
    repeated = re.fullmatch(r"([A-Za-z]+?)\1+", language)
    if repeated:
        language = repeated.group(1)
    records: list[dict[str, Any]] = []
    for sentence_id, item in enumerate(sentence_info):
        if not isinstance(item, dict):
            continue
        try:
            start = float(item["start"]) / 1000.0
            end = float(item["end"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        text = normalizer(
            str(item.get("sentence") or item.get("text") or "").strip()
        )
        if not text or end <= start:
            continue
        speaker_id = _speaker_value(item.get("spk"))
        speaker_key = f"session:speaker-{speaker_id}" if speaker_id is not None else ""
        if isinstance(speaker_id, int):
            speaker_label = f"说话人 {speaker_id + 1}"
        elif speaker_id is not None:
            speaker_label = f"说话人 {speaker_id}"
        else:
            speaker_label = ""
        records.append(
            {
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "text": text,
                "avg_logprob": 0.0,
                "no_speech_prob": 0.0,
                "words": [],
                "language": language,
                "language_probability": 0.0,
                "speaker_id": speaker_id,
                "speaker_key": speaker_key,
                "speaker_label": speaker_label,
                "speaker_scope": "full-session",
                "provider": "qwen3-asr-local",
                "sentence_id": sentence_id,
            }
        )
    records.sort(
        key=lambda item: (float(item["start_seconds"]), float(item["end_seconds"]))
    )
    report = {key: value for key, value in payload.items() if key != "result"}
    report["segment_count"] = len(records)
    report["no_speech"] = (
        raw.get("no_speech") is True and not sentence_info
        and raw.get("text") == "" and raw.get("timestamp") == []
    )
    return records, report


def transcribe_local_media(
    source: Path,
    *,
    model: str = DEFAULT_LOCAL_MODEL,
    device: str = "cuda",
    language: str | None = "zh",
    speaker_count: int | None = None,
    context_terms: Iterable[str] = (),
    normalize: Callable[[str], str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    requested = resolve_model_id(model)
    fallback = (
        DEFAULT_FALLBACK_MODEL
        if requested != DEFAULT_FALLBACK_MODEL
        and str(os.getenv("QWEN_LOCAL_DISABLE_06B_FALLBACK") or "").casefold()
        not in {"1", "true", "yes", "on"}
        else ""
    )
    if speaker_count is None:
        raw_speaker_count = str(os.getenv("QWEN_LOCAL_SPEAKER_COUNT") or "").strip()
        speaker_count = int(raw_speaker_count) if raw_speaker_count else None
    if speaker_count is not None and int(speaker_count) <= 0:
        raise LocalQwenAsrError("speaker_count 必须是正整数")
    cache_root = Path(
        str(os.getenv("QWEN_LOCAL_MODEL_ROOT") or DEFAULT_CACHE_ROOT)
    ).expanduser().resolve()
    request_payload: dict[str, Any] = {
        "input": str(source),
        "model": model,
        "fallback_model": fallback,
        "device": "cuda:0" if str(device).startswith("cuda") else "cpu",
        "language": language or "",
        "context": "",
        "guard_terms": [str(value).strip() for value in context_terms if str(value).strip()],
        "cache_root": str(cache_root),
        "local_files_only": True,
        "vad_model": str(os.getenv("QWEN_LOCAL_VAD_MODEL") or "fsmn-vad"),
        "speaker_model": str(os.getenv("QWEN_LOCAL_SPEAKER_MODEL") or "cam++"),
        "diarization": str(os.getenv("QWEN_LOCAL_DIARIZATION") or "true").casefold()
        not in {"0", "false", "no", "off"},
        "speaker_count": int(speaker_count) if speaker_count is not None else None,
        "max_segment_milliseconds": int(
            os.getenv("QWEN_LOCAL_MAX_SEGMENT_MS") or 30_000
        ),
        "batch_size_seconds": int(os.getenv("QWEN_LOCAL_BATCH_SECONDS") or 30),
        "max_inference_batch_size": int(
            os.getenv("QWEN_LOCAL_INFERENCE_BATCH") or 1
        ),
        "max_new_tokens": int(os.getenv("QWEN_LOCAL_MAX_NEW_TOKENS") or 256),
    }
    with tempfile.TemporaryDirectory(prefix="qwen-local-flow-") as temporary:
        root = Path(temporary)
        request_path = root / "request.json"
        result_path = root / "result.json"
        request_path.write_text(
            json.dumps(request_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        command = [
            str(_worker_python()),
            "-X",
            "utf8",
            str(WORKER_SCRIPT),
            "--request",
            str(request_path),
            "--output",
            str(result_path),
        ]
        completed = subprocess.run(
            command,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        if not result_path.is_file():
            raise LocalQwenAsrError(
                f"本地 Qwen 子进程失败（退出码 {completed.returncode}），且未生成诊断结果"
            )
        payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
        if completed.returncode:
            raise LocalQwenAsrError(
                str(payload.get("error") or f"本地 Qwen 退出码 {completed.returncode}")
            )
    return parse_local_result(payload, normalize=normalize)
