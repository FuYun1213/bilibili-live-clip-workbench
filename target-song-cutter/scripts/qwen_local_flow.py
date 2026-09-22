#!/usr/bin/env python3
"""Flow 1 output writer for fully local Qwen3-ASR + VAD + CAM++."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from qwen_asr import split_hotwords
from qwen_flow import PLAN_COLUMNS, _merge_speech_regions, media_duration, srt_timestamp
from qwen_local_asr import LocalQwenAsrError, transcribe_local_media
from asr_hotword_guard import find_hotword_echoes
from transcribe_review_clips import normalize_segments
from virtuareal_glossary import compact_paid_thanks, load_replacements, normalize_text


def run_qwen_local_flow1(args, *, multilingual: bool) -> int:
    source = args.input.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    duration = media_duration(source)
    replacements = load_replacements()

    def normalizer(value: str) -> str:
        return compact_paid_thanks(normalize_text(str(value), replacements)).strip()

    context_terms = split_hotwords(str(getattr(args, "initial_prompt", "") or ""))
    # A failed retry must not leave yesterday's PASS usable downstream.
    (output / "transcript-completeness.json").write_text(
        json.dumps({"status": "IN_PROGRESS", "authoritative": False,
                    "scope": "full-session-before-clipping"}), encoding="utf-8"
    )
    language = None if multilingual else str(getattr(args, "language", "zh") or "zh")
    try:
        records, provider_report = transcribe_local_media(
            source,
            model=str(args.model),
            device=str(getattr(args, "device", "cuda") or "cuda"),
            language=language,
            speaker_count=getattr(args, "speaker_count", None),
            context_terms=context_terms,
            normalize=normalizer,
        )
    except Exception as exc:
        failure = {"status": "FAIL", "authoritative": False,
                   "scope": "full-session-before-clipping", "error": str(exc)}
        for filename in ("transcript-completeness.json", "transcript-hotword-guard.json"):
            (output / filename).write_text(
                json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        raise
    records = normalize_segments(records, duration)
    echoes = find_hotword_echoes(records, terms=context_terms)
    guard_report = {"schema_version": 1, "status": "FAIL" if echoes else "PASS",
                    "context_policy": provider_report.get("context_policy"),
                    "checked_segment_count": len(records), "findings": echoes}
    (output / "transcript-hotword-guard.json").write_text(
        json.dumps(guard_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if echoes:
        (output / "transcript-completeness.json").write_text(
            json.dumps({"status": "FAIL", "authoritative": False,
                        "hotword_guard": guard_report}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise LocalQwenAsrError("本地 Qwen 热词复读检查失败；不得继续生成字幕")
    if not records and provider_report.get("no_speech") is not True:
        raise LocalQwenAsrError("本地 Qwen 转写完成，但没有返回可用的句级字幕")

    csv_name = "transcript_multilingual.csv" if multilingual else "transcript.csv"
    json_name = "transcript_multilingual.json" if multilingual else "transcript.json"
    srt_name = "transcript_multilingual.srt" if multilingual else "transcript.srt"
    csv_fields = [
        "start_seconds",
        "end_seconds",
        "speaker_id",
        "speaker_key",
        "speaker_label",
        "language",
        "language_probability",
        "avg_logprob",
        "text",
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
                    "language_probability": "0.0000",
                    "avg_logprob": "0.0000",
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
                "refinement": {
                    "enabled": False,
                    "status": "not-needed-local-qwen-output",
                },
                "languages": [language] if language else ["auto"],
                "source_signature": {
                    "size": source.stat().st_size,
                    "mtime_ns": source.stat().st_mtime_ns,
                    "duration": duration,
                },
                "hotword_guard_terms": context_terms,
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
            detected_language = str(item.get("language") or "").strip()
            labels = [
                value
                for value in (
                    speaker,
                    detected_language if multilingual else "",
                )
                if value
            ]
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
        "provider": "qwen3-asr-local",
        "model": provider_report.get("model"),
        "no_speech": provider_report.get("no_speech") is True,
        "requested_model": provider_report.get("requested_model"),
        "hotword_guard": guard_report,
        "diarization_enabled": provider_report.get("diarization_enabled", True),
        "review_windows": [],
        "review_window_count": 0,
        "unresolved_windows": [],
        "unresolved_window_count": 0,
        "unresolved_seconds": 0.0,
        "recovered_segment_count": 0,
        "note": (
            "Fully local Qwen3-ASR transcript. Sentence timing comes from local "
            "FSMN-VAD and anonymous speakers from local CAM++. No audio was uploaded."
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
                "detector": "local fsmn-vad sentence regions",
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
    with (output / "transcript-confidence-review.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        fields = (
            "start_seconds",
            "end_seconds",
            "text",
            "avg_logprob",
            "min_word_probability",
            "reason",
            "reviewed",
            "corrected_text",
        )
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in records:
            if provider_report.get("diarization_enabled", True) and item.get(
                "speaker_id"
            ) in (None, ""):
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
    (output / "qwen-local-asr-provenance.json").write_text(
        json.dumps(provider_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (output / "edit-plan.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        csv.writer(handle).writerow(PLAN_COLUMNS)
    speaker_count = len(
        {str(item.get("speaker_key")) for item in records if item.get("speaker_key")}
    )
    print(
        f"Done: wrote {len(records)} fully local Qwen transcript segments; "
        f"speaker-labels={speaker_count}; model={provider_report.get('model')}; "
        f"fallback={provider_report.get('fallback_used', False)}"
    )
    return 0
