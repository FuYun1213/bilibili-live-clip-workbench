#!/usr/bin/env python3
"""Qwen file-transcription client and transcript normalization helpers.

The public workflow keeps credentials in environment variables. Local media is
converted to mono FLAC chunks before it is uploaded to DashScope temporary
storage; this both keeps uploads small and satisfies speaker-diarization's
single-channel requirement.
"""

from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from windows_process import hidden_subprocess_kwargs


DEFAULT_ASR_MODEL = "qwen-audio-3.0-asr-flash-filetrans"
DEFAULT_ASR_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_UPLOAD_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_CHAT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
TERMINAL_TASK_STATES = {"SUCCEEDED", "FAILED", "UNKNOWN", "CANCELED"}


class QwenAsrError(RuntimeError):
    """A safe-to-display Qwen ASR integration error."""


@dataclass(frozen=True)
class QwenAsrConfig:
    model: str = DEFAULT_ASR_MODEL
    api_key_env: str = "DASHSCOPE_API_KEY"
    base_url: str = ""
    upload_base_url: str = ""
    poll_seconds: float = 3.0
    timeout_seconds: float = 7200.0
    diarization_enabled: bool = True
    speaker_count: int | None = None


def is_qwen_asr_model(model: str) -> bool:
    value = str(model or "").strip().casefold()
    return value.startswith("qwen-audio-") or value.startswith("qwen3-asr-")


def _base_url(value: str, fallback: str) -> str:
    return str(value or fallback).rstrip("/")


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return f"HTTP {response.status_code}"
    candidates: list[str] = []
    if isinstance(payload, dict):
        for container in (payload, payload.get("output"), payload.get("error")):
            if not isinstance(container, dict):
                continue
            for key in ("code", "message", "request_id"):
                value = str(container.get(key) or "").strip()
                if value:
                    candidates.append(f"{key}={value[:300]}")
    return "; ".join(candidates) or f"HTTP {response.status_code}"


class QwenAsrClient:
    def __init__(
        self,
        config: QwenAsrConfig,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.api_key = str(os.getenv(config.api_key_env) or "").strip()
        if not self.api_key:
            raise QwenAsrError(
                f"未配置 {config.api_key_env}；请把阿里云百炼 API Key 写入该环境变量"
            )
        self.base_url = _base_url(
            config.base_url or os.getenv("QWEN_ASR_BASE_URL", ""),
            DEFAULT_ASR_BASE_URL,
        )
        self.upload_base_url = _base_url(
            config.upload_base_url or os.getenv("QWEN_UPLOAD_BASE_URL", ""),
            DEFAULT_UPLOAD_BASE_URL,
        )
        self._owned_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=900.0, pool=30.0),
            follow_redirects=True,
        )
        self.sleep = sleep

    def close(self) -> None:
        if self._owned_client:
            self.client.close()

    def __enter__(self) -> "QwenAsrClient":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _request(
        self,
        method: str,
        url: str,
        *,
        attempts: int = 4,
        **kwargs,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.client.request(method, url, **kwargs)
                if response.status_code < 400:
                    return response
                if response.status_code not in {408, 409, 429} and response.status_code < 500:
                    raise QwenAsrError(
                        f"千问接口请求失败：{_error_detail(response)}"
                    )
                last_error = QwenAsrError(_error_detail(response))
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
            if attempt < attempts:
                self.sleep(min(8.0, 0.75 * (2 ** (attempt - 1))))
        raise QwenAsrError(f"千问接口暂时不可用：{last_error}") from last_error

    def upload_local_file(self, path: Path) -> str:
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        response = self._request(
            "GET",
            f"{self.upload_base_url}/uploads",
            headers={**self.auth_headers, "Content-Type": "application/json"},
            params={"action": "getPolicy", "model": self.config.model},
        )
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise QwenAsrError("千问临时文件上传凭证响应缺少 data")
        required = {
            "policy", "signature", "upload_dir", "upload_host",
            "oss_access_key_id", "x_oss_object_acl", "x_oss_forbid_overwrite",
        }
        missing = sorted(key for key in required if not data.get(key))
        if missing:
            raise QwenAsrError("千问临时文件上传凭证缺少字段：" + ", ".join(missing))
        try:
            max_size_mb = float(data.get("max_file_size_mb") or 1024)
        except (TypeError, ValueError):
            max_size_mb = 1024.0
        if path.stat().st_size > max_size_mb * 1024 * 1024:
            raise QwenAsrError(
                f"待上传音频为 {path.stat().st_size / 1024**2:.1f} MB，"
                f"超过临时存储上限 {max_size_mb:.0f} MB"
            )
        filename = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name).strip("._") or "audio.flac"
        key = f"{str(data['upload_dir']).rstrip('/')}/{filename}"
        form = {
            "OSSAccessKeyId": str(data["oss_access_key_id"]),
            "Signature": str(data["signature"]),
            "policy": str(data["policy"]),
            "x-oss-object-acl": str(data["x_oss_object_acl"]),
            "x-oss-forbid-overwrite": str(data["x_oss_forbid_overwrite"]),
            "key": key,
            "success_action_status": "200",
        }
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as handle:
            self._request(
                "POST",
                str(data["upload_host"]),
                data=form,
                files={"file": (path.name, handle, content_type)},
                attempts=3,
            )
        return f"oss://{key}"

    def submit(
        self,
        file_url: str,
        *,
        language_hints: Iterable[str] = (),
        hotwords: Iterable[str] = (),
    ) -> str:
        parameters: dict[str, Any] = {
            "channel_id": [0],
            "diarization_enabled": bool(self.config.diarization_enabled),
        }
        hints = [str(value).strip() for value in language_hints if str(value).strip()]
        if hints:
            parameters["language_hints"] = hints[:4]
        # Do not introduce vocabulary bias through a cloud fallback either.
        # Spelling corrections run after audio decoding.
        del hotwords
        if self.config.speaker_count is not None:
            if not 2 <= int(self.config.speaker_count) <= 100:
                raise QwenAsrError("speaker_count 必须是 2–100 的整数")
            parameters["speaker_count"] = int(self.config.speaker_count)
        headers = {
            **self.auth_headers,
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        if file_url.startswith("oss://"):
            headers["X-DashScope-OssResourceResolve"] = "enable"
        response = self._request(
            "POST",
            f"{self.base_url}/services/audio/asr/transcription",
            headers=headers,
            json={
                "model": self.config.model,
                "input": {"file_urls": [file_url]},
                "parameters": parameters,
            },
        )
        payload = response.json()
        output = payload.get("output") if isinstance(payload, dict) else None
        task_id = str(output.get("task_id") if isinstance(output, dict) else "").strip()
        if not task_id:
            raise QwenAsrError("千问转写任务提交成功但未返回 task_id")
        return task_id

    def wait(self, task_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + max(30.0, float(self.config.timeout_seconds))
        last_status = "PENDING"
        while time.monotonic() < deadline:
            response = self._request(
                "GET",
                f"{self.base_url}/tasks/{task_id}",
                headers=self.auth_headers,
            )
            payload = response.json()
            output = payload.get("output") if isinstance(payload, dict) else None
            if not isinstance(output, dict):
                raise QwenAsrError("千问任务查询响应缺少 output")
            last_status = str(output.get("task_status") or "UNKNOWN").upper()
            if last_status in TERMINAL_TASK_STATES:
                if last_status != "SUCCEEDED":
                    message = str(output.get("message") or output.get("code") or last_status)
                    raise QwenAsrError(f"千问转写任务失败：{message[:300]}")
                results = output.get("results")
                if not isinstance(results, list) or not results:
                    raise QwenAsrError("千问转写任务完成但没有 results")
                successful = [
                    item for item in results
                    if isinstance(item, dict)
                    and str(item.get("subtask_status") or "").upper() == "SUCCEEDED"
                    and item.get("transcription_url")
                ]
                if not successful:
                    failed = next((item for item in results if isinstance(item, dict)), {})
                    detail = str(failed.get("message") or failed.get("code") or "无成功子任务")
                    raise QwenAsrError(f"千问转写子任务失败：{detail[:300]}")
                result_response = self._request(
                    "GET", str(successful[0]["transcription_url"]), attempts=3
                )
                result_payload = result_response.json()
                if not isinstance(result_payload, dict):
                    raise QwenAsrError("千问转写结果不是 JSON 对象")
                return result_payload
            self.sleep(max(0.25, float(self.config.poll_seconds)))
        raise QwenAsrError(
            f"千问转写任务等待超时（最后状态：{last_status}，task_id={task_id}）"
        )

    def transcribe_local_file(
        self,
        path: Path,
        *,
        language_hints: Iterable[str] = (),
        hotwords: Iterable[str] = (),
    ) -> tuple[dict[str, Any], str]:
        file_url = self.upload_local_file(path)
        task_id = self.submit(
            file_url, language_hints=language_hints, hotwords=hotwords
        )
        return self.wait(task_id), task_id


def split_hotwords(value: str | Iterable[str]) -> list[str]:
    values = [value] if isinstance(value, str) else list(value)
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for item in re.split(r"[,，、;；\n]+", str(raw)):
            term = item.strip()
            folded = term.casefold()
            if not term or folded in seen:
                continue
            seen.add(folded)
            result.append(term)
    return result


def _speaker_value(value: Any) -> int | str | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def parse_qwen_result(
    payload: dict[str, Any],
    *,
    offset_seconds: float = 0.0,
    chunk_index: int = 0,
    normalize: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    """Convert DashScope's sentence/word response into the workflow schema."""
    normalize_text = normalize or (lambda value: value.strip())
    records: list[dict[str, Any]] = []
    scope = f"chunk-{chunk_index + 1:03d}"
    transcripts = payload.get("transcripts")
    if not isinstance(transcripts, list):
        raise QwenAsrError("千问转写结果缺少 transcripts")
    for transcript in transcripts:
        if not isinstance(transcript, dict):
            continue
        sentences = transcript.get("sentences")
        if not isinstance(sentences, list):
            continue
        for sentence in sentences:
            if not isinstance(sentence, dict):
                continue
            try:
                start = offset_seconds + float(sentence["begin_time"]) / 1000.0
                end = offset_seconds + float(sentence["end_time"]) / 1000.0
            except (KeyError, TypeError, ValueError):
                continue
            text = normalize_text(str(sentence.get("text") or "").strip())
            if not text or end <= start:
                continue
            words: list[dict[str, Any]] = []
            for raw_word in sentence.get("words") or []:
                if not isinstance(raw_word, dict):
                    continue
                try:
                    word_start = offset_seconds + float(raw_word["begin_time"]) / 1000.0
                    word_end = offset_seconds + float(raw_word["end_time"]) / 1000.0
                except (KeyError, TypeError, ValueError):
                    continue
                raw_text = str(raw_word.get("text") or "")
                leading_space = " " if raw_text[:1].isspace() and words else ""
                word_text = raw_text + str(
                    raw_word.get("punctuation") or ""
                )
                word_text = leading_space + normalize_text(word_text).strip()
                if not word_text or word_end < word_start:
                    continue
                words.append(
                    {
                        "start_seconds": round(word_start, 3),
                        "end_seconds": round(word_end, 3),
                        "word": word_text,
                        "probability": None,
                    }
                )
            speaker_id = _speaker_value(sentence.get("speaker_id"))
            speaker_key = (
                f"{scope}:speaker-{speaker_id}" if speaker_id is not None else ""
            )
            anonymous_label = (
                f"说话人 {int(speaker_id) + 1}"
                if isinstance(speaker_id, int)
                else (f"说话人 {speaker_id}" if speaker_id is not None else "")
            )
            speaker_label = (
                f"分段 {chunk_index + 1} · {anonymous_label}"
                if chunk_index and anonymous_label
                else anonymous_label
            )
            language = str(
                sentence.get("language") or transcript.get("language") or "auto"
            ).strip()
            records.append(
                {
                    "start_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
                    "text": text,
                    "avg_logprob": 0.0,
                    "no_speech_prob": 0.0,
                    "words": words,
                    "language": language,
                    "language_probability": 0.0,
                    "speaker_id": speaker_id,
                    "speaker_key": speaker_key,
                    "speaker_label": speaker_label,
                    "speaker_scope": scope,
                    "provider": "qwen-audio",
                    "sentence_id": sentence.get("sentence_id"),
                }
            )
    return sorted(
        records,
        key=lambda item: (float(item["start_seconds"]), float(item["end_seconds"])),
    )


def _run_ffmpeg(command: list[str]) -> None:
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_subprocess_kwargs(),
    )
    if completed.returncode:
        detail = completed.stderr.strip().splitlines()[-1:] or ["unknown FFmpeg error"]
        raise QwenAsrError("千问上传音频提取失败：" + detail[0][:500])


def transcribe_media(
    source: Path,
    *,
    ffmpeg: str,
    duration_seconds: float,
    config: QwenAsrConfig,
    language_hints: Iterable[str] = (),
    hotwords: Iterable[str] = (),
    chunk_seconds: float = 6900.0,
    normalize: Callable[[str], str] | None = None,
    client_factory: Callable[[QwenAsrConfig], QwenAsrClient] = QwenAsrClient,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Transcribe local media in diarization-safe, upload-sized chunks."""
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if duration_seconds <= 0:
        raise QwenAsrError("媒体时长必须大于 0")
    chunk_seconds = min(7100.0, max(60.0, float(chunk_seconds)))
    chunk_count = max(1, math.ceil(duration_seconds / chunk_seconds))
    records: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="qwen-asr-") as temporary:
        temporary_root = Path(temporary)
        with client_factory(config) as client:
            for chunk_index in range(chunk_count):
                start = chunk_index * chunk_seconds
                end = min(duration_seconds, start + chunk_seconds)
                chunk_path = temporary_root / f"chunk-{chunk_index + 1:03d}.flac"
                _run_ffmpeg(
                    [
                        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                        "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1",
                        "-ar", "16000", "-c:a", "flac", str(chunk_path),
                    ]
                )
                print(
                    f"Qwen ASR {chunk_index + 1}/{chunk_count}: "
                    f"uploading {start / 60:.1f}-{end / 60:.1f} min",
                    flush=True,
                )
                result, task_id = client.transcribe_local_file(
                    chunk_path,
                    language_hints=language_hints,
                    hotwords=hotwords,
                )
                chunk_records = parse_qwen_result(
                    result,
                    offset_seconds=start,
                    chunk_index=chunk_index,
                    normalize=normalize,
                )
                records.extend(chunk_records)
                tasks.append(
                    {
                        "chunk": chunk_index + 1,
                        "start_seconds": round(start, 3),
                        "end_seconds": round(end, 3),
                        "task_id": task_id,
                        "segment_count": len(chunk_records),
                    }
                )
                print(
                    f"Qwen ASR {chunk_index + 1}/{chunk_count}: "
                    f"received {len(chunk_records)} sentence(s)",
                    flush=True,
                )
    return records, {
        "provider": "qwen-audio",
        "model": config.model,
        "diarization_enabled": config.diarization_enabled,
        "speaker_count_hint": config.speaker_count,
        "chunk_seconds": chunk_seconds,
        "chunk_count": chunk_count,
        "tasks": tasks,
    }


def _text_similarity(original: str, revised: str) -> float:
    def lexical(value: str) -> str:
        return "".join(char.casefold() for char in value if char.isalnum())

    left, right = lexical(original), lexical(revised)
    if not left or not right:
        return 1.0 if left == right else 0.0
    ratio = len(right) / len(left)
    if not 0.60 <= ratio <= 1.45:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def refine_transcript(
    records: list[dict[str, Any]],
    *,
    model: str = "qwen3.8-max",
    api_key_env: str = "DASHSCOPE_API_KEY",
    base_url: str = "",
    batch_size: int = 30,
    client: httpx.Client | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Conservatively clean ASR text with Qwen Max while preserving evidence."""
    api_key = str(os.getenv(api_key_env) or "").strip()
    if not api_key:
        raise QwenAsrError(f"未配置 {api_key_env}，无法运行 {model} 字幕校正")
    endpoint = _base_url(
        base_url or os.getenv("QWEN_CHAT_BASE_URL", ""), DEFAULT_CHAT_BASE_URL
    )
    owned = client is None
    http = client or httpx.Client(
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=30.0),
        follow_redirects=True,
    )
    revised = [dict(item) for item in records]
    accepted = 0
    rejected = 0
    try:
        for first in range(0, len(revised), max(1, int(batch_size))):
            batch = revised[first:first + max(1, int(batch_size))]
            source_rows = [
                {
                    "id": first + index,
                    "speaker": item.get("speaker_key") or item.get("speaker_label") or "unknown",
                    "text": str(item.get("text") or ""),
                }
                for index, item in enumerate(batch)
            ]
            response = http.post(
                f"{endpoint}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是中文直播字幕校对器。只修正明显的同音错字、专名、标点和断句；"
                                "不得添加、删去或概括任何事实，不得改变说话人。"
                                "返回 JSON 对象，唯一字段 rows 是数组，每项仅含 id 和 text。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps({"rows": source_rows}, ensure_ascii=False),
                        },
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
            if response.status_code >= 400:
                raise QwenAsrError(f"{model} 字幕校正失败：{_error_detail(response)}")
            payload = response.json()
            choices = payload.get("choices") if isinstance(payload, dict) else None
            content = ""
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    content = str(message.get("content") or "")
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
            try:
                result = json.loads(content)
            except json.JSONDecodeError as exc:
                raise QwenAsrError(f"{model} 未返回有效 JSON") from exc
            rows = result.get("rows") if isinstance(result, dict) else None
            by_id = {
                int(item["id"]): str(item.get("text") or "").strip()
                for item in (rows or [])
                if isinstance(item, dict) and str(item.get("id", "")).isdigit()
            }
            for index, item in enumerate(batch):
                global_index = first + index
                original = str(item.get("text") or "").strip()
                candidate = by_id.get(global_index, "")
                if not candidate or candidate == original:
                    continue
                if _text_similarity(original, candidate) < 0.62:
                    rejected += 1
                    continue
                revised[global_index]["raw_text"] = original
                revised[global_index]["text"] = candidate
                revised[global_index]["refined_by"] = model
                # Word text no longer exactly matches the revised sentence. The
                # downstream authoring path will use safe proportional timing.
                revised[global_index]["words"] = []
                accepted += 1
    finally:
        if owned:
            http.close()
    return revised, {
        "model": model,
        "accepted_changes": accepted,
        "rejected_changes": rejected,
        "segment_count": len(records),
    }
