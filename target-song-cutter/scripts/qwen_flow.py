#!/usr/bin/env python3
"""Flow1 writer for Qwen ASR transcripts with speaker diarization."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from qwen_asr import (
    QwenAsrConfig,
    QwenAsrError,
    is_qwen_asr_model,
    refine_transcript,
    split_hotwords,
    transcribe_media,
)
from transcribe_review_clips import normalize_segments
from virtuareal_glossary import compact_paid_thanks, load_replacements, normalize_text
from windows_process import hidden_subprocess_kwargs


PLAN_COLUMNS = [
    "slice_id", "order", "start_seconds", "end_seconds", "title",
    "outline", "hook", "reason", "keep",
]


def ensure_ffmpeg() -> str | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        return str(Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve())
    except Exception:
        return None


def media_duration(path: Path) -> float:
    ffmpeg = ensure_ffmpeg()
    if not ffmpeg:
        raise QwenAsrError("找不到 FFmpeg")
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_subprocess_kwargs(),
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise QwenAsrError(f"无法读取媒体时长：{path.name}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def should_use_qwen(model: str) -> bool:
    return is_qwen_asr_model(model)


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name) or "").strip().casefold()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise QwenAsrError(f"{name} 必须是 true/false")


def _env_int(name: str) -> int | None:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise QwenAsrError(f"{name} 必须是整数") from exc


def _merge_speech_regions(records: list[dict]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for item in records:
        start = float(item["start_seconds"])
        end = float(item["end_seconds"])
        if not merged or start > merged[-1][1] + 0.35:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(round(start, 3), round(end, 3)) for start, end in merged]


def _config(model: str, speaker_count: int | None = None) -> QwenAsrConfig:
    resolved_speaker_count = (
        speaker_count
        if speaker_count is not None
        else _env_int("QWEN_ASR_SPEAKER_COUNT")
    )
    if resolved_speaker_count is not None and resolved_speaker_count <= 0:
        raise QwenAsrError("speaker_count 必须是正整数")
    return QwenAsrConfig(
        model=model,
        api_key_env=str(os.getenv("QWEN_API_KEY_ENV") or "DASHSCOPE_API_KEY"),
        base_url=str(os.getenv("QWEN_ASR_BASE_URL") or ""),
        upload_base_url=str(os.getenv("QWEN_UPLOAD_BASE_URL") or ""),
        poll_seconds=float(os.getenv("QWEN_ASR_POLL_SECONDS") or 3.0),
        timeout_seconds=float(os.getenv("QWEN_ASR_TIMEOUT_SECONDS") or 7200.0),
        diarization_enabled=_env_bool("QWEN_ASR_DIARIZATION", True),
        speaker_count=resolved_speaker_count,
    )


def run_qwen_flow1(args, *, multilingual: bool) -> int:
    source = args.input.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if not _env_bool("QWEN_ASR_ALLOW_UPLOAD", False):
        raise QwenAsrError(
            "千问 ASR 需要把提取后的单声道音频上传到阿里云百炼；"
            "确认当前素材允许云端处理后，请设置 "
            "QWEN_ASR_ALLOW_UPLOAD=true"
        )
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    ffmpeg = ensure_ffmpeg()
    if not ffmpeg:
        raise QwenAsrError("找不到 FFmpeg")
    duration = media_duration(source)
    replacements = load_replacements()

    def normalizer(value: str) -> str:
        return compact_paid_thanks(normalize_text(str(value), replacements)).strip()

    language_hints = (
        [item.strip() for item in str(args.languages).split(",") if item.strip()]
        if multilingual
        else [str(getattr(args, "language", "zh") or "zh").strip()]
    )
    hotwords = split_hotwords(str(getattr(args, "initial_prompt", "") or ""))
    config = _config(str(args.model), getattr(args, "speaker_count", None))
    from asr_hotword_guard import AUDIO_ONLY_POLICY, GUARD_POLICY, require_no_hotword_echoes
    # Invalidate any previous PASS before replacing this source transcript.
    (output / "transcript-completeness.json").write_text(
        json.dumps({"status": "IN_PROGRESS", "authoritative": False,
                    "hotword_guard_policy": GUARD_POLICY}), encoding="utf-8"
    )
    records, provider_report = transcribe_media(
        source,
        ffmpeg=ffmpeg,
        duration_seconds=duration,
        config=config,
        language_hints=language_hints,
        hotwords=hotwords,
        chunk_seconds=float(os.getenv("QWEN_ASR_CHUNK_SECONDS") or 6900.0),
        normalize=normalizer,
    )
    require_no_hotword_echoes(records, terms=hotwords)
    records = normalize_segments(records, duration)
    provider_report["context_policy"] = AUDIO_ONLY_POLICY
    provider_report["guard_terms"] = hotwords
    if not records:
        raise QwenAsrError("千问转写完成，但没有返回任何可用字幕句子")

    refine_enabled = _env_bool("QWEN_TRANSCRIPT_REFINE", True)
    refine_model = str(os.getenv("QWEN_TRANSCRIPT_REFINE_MODEL") or "qwen3.8-max")
    refinement_report: dict[str, Any] = {
        "enabled": refine_enabled,
        "model": refine_model,
        "status": "skipped",
    }
    if refine_enabled:
        try:
            records, details = refine_transcript(
                records,
                model=refine_model,
                api_key_env=config.api_key_env,
                base_url=str(os.getenv("QWEN_CHAT_BASE_URL") or ""),
            )
            refinement_report.update(details)
            refinement_report["status"] = "completed"
        except QwenAsrError as exc:
            refinement_report["status"] = "fallback-to-raw-asr"
            refinement_report["error"] = str(exc)
            print(f"Warning: {exc}; keeping raw Qwen ASR text", file=sys.stderr)

    require_no_hotword_echoes(records, terms=hotwords)
    csv_name = "transcript_multilingual.csv" if multilingual else "transcript.csv"
    json_name = "transcript_multilingual.json" if multilingual else "transcript.json"
    srt_name = "transcript_multilingual.srt" if multilingual else "transcript.srt"
    csv_fields = [
        "start_seconds", "end_seconds", "speaker_id", "speaker_key",
        "speaker_label", "language", "language_probability", "avg_logprob", "text",
    ]
    with (output / csv_name).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        for item in records:
            writer.writerow(
                {
                    **item,
                    "start_seconds": f"{float(item['start_seconds']):.3f}",
                    "end_seconds": f"{float(item['end_seconds']):.3f}",
                    "language_probability": f"{float(item.get('language_probability', 0.0)):.4f}",
                    "avg_logprob": f"{float(item.get('avg_logprob', 0.0)):.4f}",
                }
            )
    (output / json_name).write_text(
        json.dumps(
            {
                "schema_version": 3,
                "source": str(source),
                "scope": "full-session",
                "authoritative": True,
                "provider": provider_report,
                "hotword_guard_terms": hotwords,
                "refinement": refinement_report,
                "languages": language_hints,
                "source_signature": {
                    "size": source.stat().st_size,
                    "mtime_ns": source.stat().st_mtime_ns,
                    "duration": duration,
                },
                "segments": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with (output / srt_name).open("w", encoding="utf-8-sig", newline="\n") as handle:
        for index, item in enumerate(records, 1):
            speaker = str(item.get("speaker_label") or "").strip()
            language = str(item.get("language") or "").strip()
            labels = [value for value in (speaker, language if multilingual else "") if value]
            prefix = f"[{' / '.join(labels)}] " if labels else ""
            handle.write(
                f"{index}\n{srt_timestamp(float(item['start_seconds']))} --> "
                f"{srt_timestamp(float(item['end_seconds']))}\n"
                f"{prefix}{item['text']}\n\n"
            )

    speech_regions = _merge_speech_regions(records)
    completeness = {
        "schema_version": 3,
        "status": "PASS",
        "scope": "full-session-before-clipping",
        "authoritative": True,
        "provider": "qwen-audio",
        "context_policy": AUDIO_ONLY_POLICY,
        "hotword_guard_policy": GUARD_POLICY,
        "model": config.model,
        "diarization_enabled": config.diarization_enabled,
        "review_windows": [],
        "review_window_count": 0,
        "unresolved_windows": [],
        "unresolved_window_count": 0,
        "unresolved_seconds": 0.0,
        "recovered_segment_count": 0,
        "note": (
            "Qwen file transcription is authoritative. Speech activity is derived "
            "from sentence timestamps; no Whisper gap recovery ran."
        ),
    }
    for name in ("transcript-completeness.json", "transcript-gap-recovery.json"):
        (output / name).write_text(
            json.dumps(completeness, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (output / "speech-activity.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "scope": "full-session",
                "detector": "qwen-audio sentence timestamps",
                "regions": [
                    {"start_seconds": start, "end_seconds": end}
                    for start, end in speech_regions
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    unresolved_speakers = [
        item for item in records
        if config.diarization_enabled and item.get("speaker_id") in (None, "")
    ]
    with (output / "transcript-confidence-review.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        fields = (
            "start_seconds", "end_seconds", "text", "avg_logprob",
            "min_word_probability", "reason", "reviewed", "corrected_text",
        )
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in unresolved_speakers:
            writer.writerow(
                {
                    "start_seconds": f"{float(item['start_seconds']):.3f}",
                    "end_seconds": f"{float(item['end_seconds']):.3f}",
                    "text": item["text"],
                    "avg_logprob": "",
                    "min_word_probability": "",
                    "reason": "speaker-unresolved",
                    "reviewed": "no",
                    "corrected_text": "",
                }
            )
    (output / "qwen-asr-provenance.json").write_text(
        json.dumps(
            {"asr": provider_report, "refinement": refinement_report},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with (output / "edit-plan.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerow(PLAN_COLUMNS)
    speaker_count = len(
        {str(item.get("speaker_key")) for item in records if item.get("speaker_key")}
    )
    print(
        f"Done: wrote {len(records)} Qwen transcript segments; "
        f"speaker-labels={speaker_count}; refinement={refinement_report['status']}"
    )
    return 0
