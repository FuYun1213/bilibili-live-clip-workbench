#!/usr/bin/env python3
"""Transcribe audio, validate editorial plans, and render non-contiguous video slices."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from bilibili_gifts import DEFAULT_CATALOG, filter_transcript, transcript_times
from general_hotwords import build_whisper_prompt
from transcribe_review_clips import (
    anchor_rows,
    customize_ass_template,
    detect_speech_intervals,
    find_uncovered_speech_windows,
    normalize_segments,
    recover_transcript_gaps,
    whisper_segment_record,
    write_ass,
)
from virtuareal_glossary import compact_paid_thanks, load_replacements, normalize_text
from windows_process import hidden_subprocess_kwargs, is_transient_start_failure, unsigned_returncode


PLAN_COLUMNS = [
    "slice_id", "order", "start_seconds", "end_seconds", "title",
    "outline", "hook", "reason", "keep",
]
DEFAULT_SUBTITLE_DELAY = 0.1
DEFAULT_TAIL_PADDING = 5.0


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class EditRange:
    slice_id: str
    order: int
    start: float
    end: float
    title: str
    outline: str
    hook: str
    reason: str


def ensure_ffmpeg() -> str | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return str(Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve())
    except Exception:
        return None


def _local_model_ready(
    path: Path, required_files: tuple[tuple[str, int], ...]
) -> bool:
    return path.is_dir() and all(
        (path / name).is_file() and (path / name).stat().st_size >= minimum_bytes
        for name, minimum_bytes in required_files
    )


def doctor() -> int:
    """Check both the default local Qwen path and the selectable Whisper fallback."""
    from qwen_local_asr import DEFAULT_CACHE_ROOT, DEFAULT_WORKER_PYTHON

    issues: list[str] = []
    ffmpeg = ensure_ffmpeg()
    print(f"ffmpeg={ffmpeg or 'missing'}")
    if not ffmpeg:
        issues.append("ffmpeg")

    worker = DEFAULT_WORKER_PYTHON.resolve()
    print(f"qwen_runtime={worker} exists={worker.is_file()}")
    if worker.is_file():
        check = subprocess.run(
            [
                str(worker),
                "-I",
                "-X",
                "utf8",
                "-c",
                "from qwen_asr import Qwen3ASRModel; "
                "import funasr, torch, transformers; "
                "print(transformers.__version__, torch.cuda.is_available(), Qwen3ASRModel.__name__)",
            ],
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
        summary = (check.stdout or check.stderr).strip().splitlines()
        print(
            f"qwen_imports={'ok' if check.returncode == 0 else 'failed'}"
            f" detail={summary[-1] if summary else '-'}"
        )
        if check.returncode:
            issues.append("qwen-imports")
    else:
        issues.append("qwen-runtime")

    required = {
        "Qwen3-ASR-0.6B": (
            ("config.json", 1),
            ("model.safetensors", 1_800_000_000),
        ),
        "fsmn-vad": (("model.pt", 1_000_000),),
        "cam++": (("campplus_cn_common.bin", 25_000_000),),
    }
    optional_17b = _local_model_ready(
        DEFAULT_CACHE_ROOT / "Qwen3-ASR-1.7B",
        (
            ("config.json", 1),
            ("model-00001-of-00002.safetensors", 4_000_000_000),
            ("model-00002-of-00002.safetensors", 450_000_000),
        ),
    )
    print(f"local_model=Qwen3-ASR-1.7B ready={optional_17b} optional=true")
    for directory, files in required.items():
        ready = _local_model_ready(DEFAULT_CACHE_ROOT / directory, files)
        print(f"local_model={directory} ready={ready}")
        if not ready:
            issues.append(directory)

    whisper = importlib.util.find_spec("faster_whisper") is not None
    print(f"whisper_fallback={'ready' if whisper else 'missing'}")
    return 1 if issues else 0

def run_command(args: Sequence[str], retries: int = 3) -> None:
    print("+", subprocess.list2cmdline([str(value) for value in args]), flush=True)
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    for attempt in range(1, max(1, retries) + 1):
        result = subprocess.run(
            [str(value) for value in args],
            check=False,
            env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            **hidden_subprocess_kwargs(),
        )
        if result.returncode == 0:
            return
        if is_transient_start_failure(result.returncode) and attempt < retries:
            time.sleep(0.25 * attempt)
            continue
        diagnostic = result.stdout or ""
        if diagnostic:
            print(diagnostic[-12000:], file=sys.stderr, flush=True)
        raise subprocess.CalledProcessError(result.returncode, list(args), output=diagnostic)


def media_duration(path: Path, retries: int = 3) -> float:
    ffmpeg = ensure_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is unavailable")
    for attempt in range(1, max(1, retries) + 1):
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
        match = re.search(
            r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr
        )
        if match:
            hours, minutes, seconds = match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if is_transient_start_failure(result.returncode) and attempt < retries:
            time.sleep(0.25 * attempt)
            continue
        if is_transient_start_failure(result.returncode):
            code = unsigned_returncode(result.returncode)
            raise RuntimeError(
                f"FFmpeg 连续 {retries} 次启动失败 (0x{code:08X})：{path}"
            )
        raise RuntimeError(f"Could not determine media duration for {path}")
    raise RuntimeError(f"Could not determine media duration for {path}")


def srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def validate_completeness_thresholds(args) -> None:
    pause = float(args.completeness_pause_tolerance)
    review = float(args.completeness_review_gap)
    blocking = float(args.completeness_blocking_gap)
    if pause < 0 or review <= 0 or blocking < review:
        raise ValueError(
            "Completeness thresholds must satisfy pause >= 0 and blocking >= review > 0"
        )


def resumable_failed_transcript(output: Path, source: Path) -> dict | None:
    """Reuse a same-source Flow1 transcript when only its completeness gate failed."""
    transcript_path = output / "transcript.json"
    completeness_path = output / "transcript-completeness.json"
    if not transcript_path.is_file() or not completeness_path.is_file():
        return None
    try:
        payload = json.loads(transcript_path.read_text(encoding="utf-8-sig"))
        completeness = json.loads(
            completeness_path.read_text(encoding="utf-8-sig")
        )
        recorded_source = Path(str(payload.get("source", ""))).resolve()
        if (
            recorded_source != source.resolve()
            or completeness.get("status") != "FAIL"
            or not completeness.get("unresolved_windows")
            or not isinstance(payload.get("segments"), list)
            or not payload["segments"]
            or source.stat().st_mtime_ns > transcript_path.stat().st_mtime_ns
        ):
            return None
        from asr_hotword_guard import require_clean_artifact
        require_clean_artifact(transcript_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload

def transcribe(args) -> int:
    """Create the one authoritative full-session transcript used by every later flow."""
    source = args.input.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    validate_completeness_thresholds(args)
    from qwen_local_asr import is_local_qwen_model
    from qwen_local_flow import run_qwen_local_flow1

    if is_local_qwen_model(args.model):
        return run_qwen_local_flow1(args, multilingual=False)
    from qwen_flow import run_qwen_flow1, should_use_qwen

    if should_use_qwen(args.model):
        return run_qwen_flow1(args, multilingual=False)

    dll_handle = None
    if args.device == "cuda" and os.name == "nt":
        try:
            import torch
        except ImportError:
            torch = None
        if torch is not None:
            torch_lib = Path(torch.__file__).resolve().parent / "lib"
            if torch_lib.is_dir():
                os.environ["PATH"] = str(torch_lib) + os.pathsep + os.environ.get("PATH", "")
                dll_handle = os.add_dll_directory(str(torch_lib))
    from faster_whisper import WhisperModel

    model = WhisperModel(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
        download_root=str(args.model_cache.resolve()) if args.model_cache else None,
    )
    replacements = load_replacements()
    prompt = build_whisper_prompt(args.initial_prompt)
    primary_vad = {
        "threshold": 0.35,
        "min_speech_duration_ms": 120,
        # Wider boundaries avoid fragmenting one utterance at short pauses.
        "min_silence_duration_ms": 2000,
        "speech_pad_ms": 400,
    }
    resumable = resumable_failed_transcript(output, source)
    if resumable is not None:
        records = [dict(item) for item in resumable["segments"]]
        detected_language = str(resumable.get("language") or args.language)
        detected_language_probability = float(
            resumable.get("language_probability", 0.0) or 0.0
        )
        print(
            f"Resuming {len(records)} existing authoritative segments; "
            "retrying unresolved Flow1 gaps only.",
            flush=True,
        )
    else:
        raw_segments, info = model.transcribe(
            str(source),
            language=args.language,

            beam_size=args.beam_size,
            vad_filter=True,
            vad_parameters=primary_vad,
            condition_on_previous_text=False,
            initial_prompt=prompt,
            word_timestamps=True,
            hallucination_silence_threshold=1.0,
        )
        records = []
        for item in list(raw_segments):
            record = whisper_segment_record(item, replacements)
            if record is not None:
                records.append(record)
        detected_language = info.language
        detected_language_probability = info.language_probability

    duration = media_duration(source)
    audit_vad = {
        "threshold": 0.30,
        "min_speech_duration_ms": 120,
        "min_silence_duration_ms": 650,
        "speech_pad_ms": 0,
    }
    recovery_speech = detect_speech_intervals(
        source,
        threshold=audit_vad["threshold"],
        min_speech_ms=audit_vad["min_speech_duration_ms"],
        min_silence_ms=audit_vad["min_silence_duration_ms"],
    )
    recovered, gap_report = recover_transcript_gaps(
        model,
        source,
        records,
        recovery_speech,
        duration,
        # A narrative stream can contain watched clips or quoted English/Japanese.
        # Let each uncovered window detect its own language instead of forcing zh.
        language=None,
        beam_size=args.beam_size,
        prompt=prompt,
        replacements=replacements,
        hallucination_silence_threshold=1.0,
        padding=0.60,
        minimum_gap=args.completeness_review_gap,
        coverage_merge_gap=args.completeness_pause_tolerance,
    )
    records = normalize_segments([*records, *recovered], duration)
    review_windows = find_uncovered_speech_windows(
        recovery_speech,
        records,
        duration,
        min_region=args.completeness_review_gap,
        merge_gap=0.40,
        padding=0.0,
        coverage_merge_gap=args.completeness_pause_tolerance,
    )
    unresolved = [
        item for item in review_windows
        if item["core_end"] - item["core_start"] >= args.completeness_blocking_gap
    ]
    gap_report.update(
        {
            "schema_version": 2,
            "status": "PASS" if not unresolved else "FAIL",
            "scope": "full-session-before-clipping",
            "authoritative": True,
            "batched_inference": False,
            "primary_vad": primary_vad,
            "audit_vad": audit_vad,
            "pause_tolerance_seconds": args.completeness_pause_tolerance,
            "minimum_review_gap_seconds": args.completeness_review_gap,
            "minimum_blocking_gap_seconds": args.completeness_blocking_gap,
            "review_windows": review_windows,
            "review_window_count": len(review_windows),
            "unresolved_windows": unresolved,
            "unresolved_window_count": len(unresolved),
            "unresolved_seconds": round(
                sum(item["core_end"] - item["core_start"] for item in unresolved), 3
            ),
        }
    )

    segments = [
        TranscriptSegment(
            float(item["start_seconds"]),
            float(item["end_seconds"]),
            str(item["text"]),
        )
        for item in records
    ]
    (output / "transcript-gap-recovery.json").write_text(
        json.dumps(gap_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "transcript-completeness.json").write_text(
        json.dumps(gap_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "speech-activity.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "scope": "full-session",
                "detector": "faster-whisper Silero VAD",
                "settings": audit_vad,
                "regions": [
                    {"start_seconds": start, "end_seconds": end}
                    for start, end in recovery_speech
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    with (output / "transcript.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["start_seconds", "end_seconds", "text"])
        for item in segments:
            writer.writerow([f"{item.start:.3f}", f"{item.end:.3f}", item.text])
    with (output / "transcript.json").open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema_version": 2,
                "source": str(source),
                "scope": "full-session",
                "authoritative": True,
                "language": detected_language,
                "language_probability": detected_language_probability,
                "source_signature": {
                    "size": source.stat().st_size,
                    "mtime_ns": source.stat().st_mtime_ns,
                    "duration": duration,
                },
                "segments": records,
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
    with (output / "transcript.srt").open("w", encoding="utf-8-sig", newline="\n") as handle:
        for index, item in enumerate(segments, 1):
            handle.write(
                f"{index}\n{srt_timestamp(item.start)} --> {srt_timestamp(item.end)}\n"
                f"{item.text}\n\n"
            )

    confidence_rows = []
    for item in records:
        probabilities = [
            float(word["probability"])
            for word in item.get("words", [])
            if word.get("probability") is not None
        ]
        minimum = min(probabilities) if probabilities else None
        reasons = []
        if float(item.get("avg_logprob", 0.0)) < -0.85:
            reasons.append("low-segment-confidence")
        if minimum is not None and minimum < 0.35:
            reasons.append("low-word-confidence")
        if reasons:
            confidence_rows.append(
                {
                    "start_seconds": f"{float(item['start_seconds']):.3f}",
                    "end_seconds": f"{float(item['end_seconds']):.3f}",
                    "text": item["text"],
                    "avg_logprob": f"{float(item.get('avg_logprob', 0.0)):.4f}",
                    "min_word_probability": "" if minimum is None else f"{minimum:.4f}",
                    "reason": ";".join(reasons),
                    "reviewed": "no",
                    "corrected_text": "",
                }
            )
    with (output / "transcript-confidence-review.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        fields = (
            "start_seconds", "end_seconds", "text", "avg_logprob",
            "min_word_probability", "reason", "reviewed", "corrected_text",
        )
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(confidence_rows)

    with (output / "edit-plan.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerow(PLAN_COLUMNS)
    print(
        f"Done: wrote {len(segments)} authoritative transcript segments; "
        f"gap-recovered={gap_report['recovered_segment_count']}; "
        f"review-gaps={len(review_windows)}; blocking-gaps={len(unresolved)}; "
        f"confidence-review={len(confidence_rows)}"
    )
    if dll_handle is not None:
        dll_handle.close()
    if unresolved:
        details = ", ".join(
            f"{item['core_start']:.3f}-{item['core_end']:.3f}s"
            for item in unresolved[:10]
        )
        raise RuntimeError(
            "Flow1 full-session transcript completeness gate failed; "
            f"uncovered speech remains at {details}. Later clipping is blocked."
        )
    return 0

def transcribe_multilingual(args) -> int:
    """Create one multilingual full-session transcript with overlap-safe chunk boundaries."""
    source = args.input.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    validate_completeness_thresholds(args)
    allowed_languages = [item.strip() for item in args.languages.split(",") if item.strip()]
    if not allowed_languages:
        raise ValueError("--languages must contain at least one language code")
    from qwen_local_asr import is_local_qwen_model
    from qwen_local_flow import run_qwen_local_flow1

    if is_local_qwen_model(args.model):
        return run_qwen_local_flow1(args, multilingual=True)
    from qwen_flow import run_qwen_flow1, should_use_qwen

    if should_use_qwen(args.model):
        return run_qwen_flow1(args, multilingual=True)
    if args.chunk_overlap_seconds < 0 or args.chunk_overlap_seconds * 2 >= args.chunk_seconds:
        raise ValueError("--chunk-overlap-seconds must be non-negative and under half a chunk")

    dll_handle = None
    if args.device == "cuda" and os.name == "nt":
        try:
            import torch
        except ImportError:
            torch = None
        if torch is not None:
            torch_lib = Path(torch.__file__).resolve().parent / "lib"
            if torch_lib.is_dir():
                os.environ["PATH"] = str(torch_lib) + os.pathsep + os.environ.get("PATH", "")
                dll_handle = os.add_dll_directory(str(torch_lib))
    from faster_whisper import WhisperModel

    model = WhisperModel(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
        download_root=str(args.model_cache.resolve()) if args.model_cache else None,
    )
    ffmpeg = ensure_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is unavailable")
    duration = media_duration(source)
    replacements = load_replacements()
    prompt = build_whisper_prompt(args.initial_prompt)
    records: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="multilingual-asr-", dir=output) as temporary:
        chunk_path = Path(temporary) / "chunk.wav"
        chunk_count = max(1, int((duration + args.chunk_seconds - 1) // args.chunk_seconds))
        for chunk_index in range(chunk_count):
            core_start = chunk_index * args.chunk_seconds
            core_end = min(duration, (chunk_index + 1) * args.chunk_seconds)
            window_start = max(
                0.0,
                core_start - (args.chunk_overlap_seconds if chunk_index else 0.0),
            )
            window_end = min(
                duration,
                core_end
                + (
                    args.chunk_overlap_seconds
                    if chunk_index + 1 < chunk_count
                    else 0.0
                ),
            )
            chunk_duration = window_end - window_start
            subprocess.run(
                [
                    ffmpeg, "-loglevel", "error", "-y",
                    "-ss", f"{window_start:.3f}", "-t", f"{chunk_duration:.3f}",
                    "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(chunk_path),
                ],
                check=True,
            )

            def decode(language):
                raw, info = model.transcribe(
                    str(chunk_path),
                    language=language,
                    beam_size=args.beam_size,
                    vad_filter=False,
                    condition_on_previous_text=False,
                    initial_prompt=prompt,
                    word_timestamps=True,
                    hallucination_silence_threshold=1.0,
                )
                items = list(raw)
                confidence = (
                    sum(float(item.avg_logprob) for item in items) / len(items)
                    if items
                    else float("-inf")
                )
                return items, info, confidence

            items, info, _ = decode(None)
            detected = info.language
            if detected not in allowed_languages:
                alternatives = [decode(language) for language in allowed_languages]
                items, info, _ = max(alternatives, key=lambda value: value[2])
                detected = info.language
            keep_end = core_end if chunk_index + 1 == chunk_count else core_end - 0.001
            for item in items:
                record = whisper_segment_record(
                    item,
                    replacements,
                    offset=window_start,
                    core_start=core_start,
                    core_end=keep_end,
                )
                if record is None:
                    continue
                record["language"] = detected
                record["language_probability"] = float(info.language_probability)
                records.append(record)
            print(
                f"Transcribed chunk {chunk_index + 1}/{chunk_count} "
                f"({core_start / 60:.1f} min, language={detected})",
                flush=True,
            )

    records = normalize_segments(records, duration)
    audit_vad = {
        "threshold": 0.30,
        "min_speech_duration_ms": 120,
        "min_silence_duration_ms": 650,
        "speech_pad_ms": 0,
    }
    recovery_speech = detect_speech_intervals(
        source,
        threshold=audit_vad["threshold"],
        min_speech_ms=audit_vad["min_speech_duration_ms"],
        min_silence_ms=audit_vad["min_silence_duration_ms"],
    )
    recovered, gap_report = recover_transcript_gaps(
        model,
        source,
        records,
        recovery_speech,
        duration,
        language=None,
        beam_size=args.beam_size,
        prompt=prompt,
        replacements=replacements,
        hallucination_silence_threshold=1.0,
        padding=args.chunk_overlap_seconds,
        minimum_gap=args.completeness_review_gap,
        coverage_merge_gap=args.completeness_pause_tolerance,
    )
    for item in recovered:
        item["language"] = "auto"
        item["language_probability"] = 0.0
    records = normalize_segments([*records, *recovered], duration)
    review_windows = find_uncovered_speech_windows(
        recovery_speech,
        records,
        duration,
        min_region=args.completeness_review_gap,
        merge_gap=0.40,
        padding=0.0,
        coverage_merge_gap=args.completeness_pause_tolerance,
    )
    unresolved = [
        item for item in review_windows
        if item["core_end"] - item["core_start"] >= args.completeness_blocking_gap
    ]
    gap_report.update(
        {
            "schema_version": 2,
            "status": "PASS" if not unresolved else "FAIL",
            "scope": "full-session-before-clipping",
            "authoritative": True,
            "batched_inference": False,
            "chunk_seconds": args.chunk_seconds,
            "chunk_overlap_seconds": args.chunk_overlap_seconds,
            "audit_vad": audit_vad,
            "pause_tolerance_seconds": args.completeness_pause_tolerance,
            "minimum_review_gap_seconds": args.completeness_review_gap,
            "minimum_blocking_gap_seconds": args.completeness_blocking_gap,
            "review_windows": review_windows,
            "review_window_count": len(review_windows),
            "unresolved_windows": unresolved,
            "unresolved_window_count": len(unresolved),
            "unresolved_seconds": round(
                sum(item["core_end"] - item["core_start"] for item in unresolved), 3
            ),
        }
    )

    with (output / "transcript_multilingual.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "start_seconds", "end_seconds", "language",
                "language_probability", "avg_logprob", "text",
            ],
        )
        writer.writeheader()
        for row in records:
            writer.writerow(
                {
                    "start_seconds": f"{float(row['start_seconds']):.3f}",
                    "end_seconds": f"{float(row['end_seconds']):.3f}",
                    "language": row.get("language", "auto"),
                    "language_probability": f"{float(row.get('language_probability', 0.0)):.4f}",
                    "avg_logprob": f"{float(row.get('avg_logprob', 0.0)):.4f}",
                    "text": row["text"],
                }
            )
    (output / "transcript_multilingual.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "source": str(source),
                "scope": "full-session",
                "authoritative": True,
                "languages": allowed_languages,
                "chunk_seconds": args.chunk_seconds,
                "chunk_overlap_seconds": args.chunk_overlap_seconds,
                "segments": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with (output / "transcript_multilingual.srt").open(
        "w", encoding="utf-8-sig", newline="\n"
    ) as handle:
        for index, row in enumerate(records, 1):
            handle.write(
                f"{index}\n{srt_timestamp(float(row['start_seconds']))} --> "
                f"{srt_timestamp(float(row['end_seconds']))}\n"
                f"[{row.get('language', 'auto')}] {row['text']}\n\n"
            )
    (output / "transcript-completeness.json").write_text(
        json.dumps(gap_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "transcript-gap-recovery.json").write_text(
        json.dumps(gap_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "speech-activity.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "scope": "full-session",
                "detector": "faster-whisper Silero VAD",
                "settings": audit_vad,
                "regions": [
                    {"start_seconds": start, "end_seconds": end}
                    for start, end in recovery_speech
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    confidence_rows = []
    for item in records:
        probabilities = [
            float(word["probability"])
            for word in item.get("words", [])
            if word.get("probability") is not None
        ]
        minimum = min(probabilities) if probabilities else None
        reasons = []
        if float(item.get("avg_logprob", 0.0)) < -0.85:
            reasons.append("low-segment-confidence")
        if minimum is not None and minimum < 0.35:
            reasons.append("low-word-confidence")
        if reasons:
            confidence_rows.append(
                {
                    "start_seconds": f"{float(item['start_seconds']):.3f}",
                    "end_seconds": f"{float(item['end_seconds']):.3f}",
                    "text": item["text"],
                    "avg_logprob": f"{float(item.get('avg_logprob', 0.0)):.4f}",
                    "min_word_probability": "" if minimum is None else f"{minimum:.4f}",
                    "reason": ";".join(reasons),
                    "reviewed": "no",
                    "corrected_text": "",
                }
            )
    with (output / "transcript-confidence-review.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        fields = (
            "start_seconds", "end_seconds", "text", "avg_logprob",
            "min_word_probability", "reason", "reviewed", "corrected_text",
        )
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(confidence_rows)
    print(
        f"Done: wrote {len(records)} authoritative multilingual segments; "
        f"gap-recovered={gap_report['recovered_segment_count']}; "
        f"review-gaps={len(review_windows)}; blocking-gaps={len(unresolved)}; "
        f"confidence-review={len(confidence_rows)}"
    )
    if dll_handle is not None:
        dll_handle.close()
    if unresolved:
        details = ", ".join(
            f"{item['core_start']:.3f}-{item['core_end']:.3f}s"
            for item in unresolved[:10]
        )
        raise RuntimeError(
            "Flow1 full-session multilingual transcript completeness gate failed; "
            f"uncovered speech remains at {details}. Later clipping is blocked."
        )
    return 0

def load_plan(path: Path, duration: float | None = None) -> list[EditRange]:
    if not path.is_file():
        raise FileNotFoundError(path)
    ranges: list[EditRange] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(PLAN_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Edit plan is missing columns: {', '.join(sorted(missing))}")
        for row_number, row in enumerate(reader, 2):
            if row["keep"].strip().lower() not in {"1", "true", "yes", "y", "keep"}:
                continue
            try:
                item = EditRange(
                    slice_id=row["slice_id"].strip(),
                    order=int(row["order"]),
                    start=float(row["start_seconds"]),
                    end=float(row["end_seconds"]),
                    title=row["title"].strip(),
                    outline=row["outline"].strip(),
                    hook=row["hook"].strip(),
                    reason=row["reason"].strip(),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid value on edit-plan row {row_number}: {exc}") from exc
            if not item.slice_id:
                raise ValueError(f"Missing slice_id on edit-plan row {row_number}")
            if item.start < 0 or item.end <= item.start:
                raise ValueError(f"Invalid time range on edit-plan row {row_number}")
            if duration is not None and item.end > duration + 0.05:
                raise ValueError(f"Range exceeds source duration on edit-plan row {row_number}")
            ranges.append(item)
    if not ranges:
        raise ValueError("Edit plan has no rows marked keep")
    seen: set[tuple[str, int]] = set()
    for item in ranges:
        key = (item.slice_id, item.order)
        if key in seen:
            raise ValueError(f"Duplicate order {item.order} in slice {item.slice_id}")
        seen.add(key)
    return sorted(ranges, key=lambda item: (item.slice_id, item.order))


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned[:80] or "slice"


def group_plan(ranges: Sequence[EditRange]) -> dict[str, list[EditRange]]:
    grouped: dict[str, list[EditRange]] = {}
    for item in ranges:
        grouped.setdefault(item.slice_id, []).append(item)
    return grouped


def apply_tail_padding(
    ranges: Sequence[EditRange], padding: float = DEFAULT_TAIL_PADDING,
    duration: float | None = None,
) -> list[EditRange]:
    """Add padding only after the final retained part of each finished slice."""
    if padding < 0:
        raise ValueError("Tail padding cannot be negative")
    padded: list[EditRange] = []
    for parts in group_plan(ranges).values():
        for index, item in enumerate(parts):
            if index != len(parts) - 1 or padding == 0:
                padded.append(item)
                continue
            padded_end = item.end + padding
            if duration is not None:
                padded_end = min(duration, padded_end)
            padded.append(replace(item, end=padded_end))
    return sorted(padded, key=lambda item: (item.slice_id, item.order))


def load_excluded_ranges(path: Path) -> list[tuple[float, float, str]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    excluded = []
    for row in rows:
        start, end = transcript_times(row)
        excluded.append((start, end, row.get("text", "")))
    return excluded


def reject_gift_overlaps(ranges: Sequence[EditRange], gift_acks: Path | None) -> None:
    if not gift_acks:
        return
    excluded = load_excluded_ranges(gift_acks)
    overlaps = []
    for item in ranges:
        for start, end, text in excluded:
            if end > item.start and start < item.end:
                overlaps.append(
                    f"slice {item.slice_id}/{item.order} overlaps {start:.3f}-{end:.3f}: {text}"
                )
    if overlaps:
        details = "\n".join(overlaps[:10])
        raise ValueError(
            "Edit plan still contains Bilibili gift acknowledgements. "
            "Split or shorten the retained ranges around these rows:\n" + details
        )


def build_slice_command(ffmpeg, video, audio, parts, destination, preset="veryfast", crf=19):
    """Trim in editorial order and encode audio once, avoiding per-part AAC delay."""
    from media_packaging import probe_media
    metadata = probe_media(ffmpeg, video)
    fps = metadata["fps"]
    video_end = metadata.get("video_end_seconds")
    if video_end is not None:
        for part in parts:
            if float(part.start) >= video_end:
                raise ValueError(
                    f"切片 {part.title or part.slice_id} 的 {part.start:.3f}–{part.end:.3f} 秒"
                    f"没有视频帧：视频在 {video_end:.3f} 秒结束，后续可能仅有音频；请移除该候选或调整范围"
                )
    command = [str(ffmpeg), "-y"]
    filters, streams = [], []
    shared = Path(video).resolve() == Path(audio).resolve()
    for index, part in enumerate(parts):
        duration = float(part.end) - float(part.start)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Invalid retained source range")
        input_index = index if shared else index * 2
        command += ["-ss", f"{part.start:.6f}", "-t", f"{duration:.6f}", "-i", str(video)]
        if not shared:
            command += ["-ss", f"{part.start:.6f}", "-t", f"{duration:.6f}", "-i", str(audio)]
        audio_index = input_index if shared else input_index + 1
        filters += [f"[{input_index}:v]trim=duration={duration:.6f},setpts=PTS-STARTPTS[v{index}]",
                    f"[{audio_index}:a]atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[a{index}]"]
        streams.append(f"[v{index}][a{index}]")
    filters.append("".join(streams) + f"concat=n={len(parts)}:v=1:a=1[outv][outa]")
    command += ["-filter_complex_threads", "1", "-filter_complex", ";".join(filters),
                "-map", "[outv]", "-map", "[outa]", "-r", f"{fps:.9f}", "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(destination)]
    return command


def export(args) -> int:
    audio = args.audio.resolve()
    video = args.video.resolve()
    plan = args.plan.resolve()
    for path in (audio, video):
        if not path.is_file():
            raise FileNotFoundError(path)
    audio_duration = media_duration(audio)
    video_duration = media_duration(video)
    if abs(audio_duration - video_duration) > args.timeline_tolerance:
        raise ValueError(
            f"Audio/video durations differ by {abs(audio_duration-video_duration):.3f}s, "
            f"over the {args.timeline_tolerance:.3f}s tolerance"
        )
    source_duration = min(audio_duration, video_duration)
    source_ranges = load_plan(plan, source_duration)
    source_by_slice = group_plan(source_ranges)
    ranges = apply_tail_padding(source_ranges, args.tail_padding, source_duration)
    reject_gift_overlaps(ranges, args.gift_acks.resolve() if args.gift_acks else None)
    output = args.output.resolve()
    clips = output / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    ffmpeg = ensure_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is unavailable")

    # Validate every selected range before encoding any clip in the batch.
    render_jobs = []
    for slice_index, (slice_id, parts) in enumerate(group_plan(ranges).items(), 1):
        title = parts[0].title or slice_id
        final_path = clips / f"{slice_index:03d}_{safe_name(title)}.mp4"
        command = build_slice_command(ffmpeg, video, audio, parts, final_path, args.preset, args.crf)
        render_jobs.append((slice_id, parts, title, final_path, command))
    manifest_rows = []
    for slice_id, parts, title, final_path, command in render_jobs:
        run_command(command)
        manifest_rows.append([
            final_path.name, slice_id, title, parts[0].outline, parts[0].hook,
            sum(item.end - item.start for item in parts), len(parts),
            parts[-1].end - source_by_slice[slice_id][-1].end,
        ])
    with (output / "slices.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "file", "slice_id", "title", "outline", "hook", "duration_seconds",
            "part_count", "tail_padding_seconds",
        ])
        writer.writerows(manifest_rows)
    print(f"Done: exported {len(manifest_rows)} edited slice(s) to {clips}")
    return 0


def validate(args) -> int:
    duration = media_duration(args.audio.resolve()) if args.audio else None
    ranges = load_plan(args.plan.resolve(), duration)
    padded = apply_tail_padding(ranges, args.tail_padding, duration)
    reject_gift_overlaps(padded, args.gift_acks.resolve() if args.gift_acks else None)
    print(
        f"OK: {len(ranges)} kept range(s) across {len(group_plan(ranges))} slice(s); "
        f"tail padding={args.tail_padding:.3f}s; rendered duration="
        f"{sum(item.end - item.start for item in padded):.3f}s"
    )
    return 0


def slice_subtitles(args) -> int:
    transcript = args.transcript.resolve()
    if not transcript.is_file():
        raise FileNotFoundError(transcript)
    ranges = load_plan(args.plan.resolve())
    with transcript.open("r", encoding="utf-8-sig", newline="") as handle:
        transcript_rows = list(csv.DictReader(handle))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    written = 0
    for slice_index, (slice_id, parts) in enumerate(group_plan(ranges).items(), 1):
        title = parts[0].title or slice_id
        destination = output / f"{slice_index:03d}_{safe_name(title)}.srt"
        subtitle_index = 1
        output_offset = 0.0
        with destination.open("w", encoding="utf-8-sig", newline="\n") as subtitle:
            for part in parts:
                for row in transcript_rows:
                    if row.get("start_seconds") not in (None, ""):
                        start = float(row["start_seconds"])
                        end = float(row["end_seconds"])
                    else:
                        start = float(row["start_ms"]) / 1000.0
                        end = float(row["end_ms"]) / 1000.0
                    if end <= part.start or start >= part.end:
                        continue
                    detected_start = output_offset + max(start, part.start) - part.start
                    local_start = max(output_offset, detected_start + args.subtitle_delay)
                    local_end = min(
                        output_offset + (part.end - part.start),
                        output_offset + min(end, part.end) - part.start + args.subtitle_delay,
                    )
                    text = row["text"].strip()
                    if not text or local_end <= local_start:
                        continue
                    language = row.get("language", "").strip()
                    prefix = f"[{language}] " if args.language_tags and language else ""
                    subtitle.write(
                        f"{subtitle_index}\n{srt_timestamp(local_start)} --> "
                        f"{srt_timestamp(local_end)}\n{prefix}{text}\n\n"
                    )
                    subtitle_index += 1
                output_offset += part.end - part.start
                if part is parts[-1]:
                    output_offset += args.tail_padding
        written += 1
    print(f"Done: wrote {written} sliced subtitle file(s) to {output}")
    return 0


def _canonical_bounds(value: dict) -> tuple[float, float]:
    return (
        float(value.get("start_seconds", value.get("start", 0.0))),
        float(value.get("end_seconds", value.get("end", 0.0))),
    )


def load_authoritative_segments(transcript_json: Path, transcript_csv: Path) -> list[dict]:
    """Load Flow1 text, retaining word timing only when it still matches canonical CSV text."""
    payload = json.loads(transcript_json.read_text(encoding="utf-8-sig"))
    rich = payload.get("segments", [])
    if not isinstance(rich, list):
        raise ValueError(f"Invalid authoritative transcript JSON: {transcript_json}")
    with transcript_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    from asr_hotword_guard import require_clean_artifact, stored_guard_terms
    require_clean_artifact(transcript_csv, terms=stored_guard_terms(transcript_json))

    segments: list[dict] = []
    for row in rows:
        start, end = transcript_times(row)
        text = str(row.get("text") or "").strip()
        if not text or end <= start:
            continue
        candidates = []
        for value in rich:
            if not isinstance(value, dict):
                continue
            rich_start, rich_end = _canonical_bounds(value)
            distance = abs(rich_start - start) + abs(rich_end - end)
            if distance <= 0.20:
                candidates.append((distance, value))
        matched = min(candidates, key=lambda item: item[0])[1] if candidates else {}
        words = [dict(word) for word in matched.get("words", []) if isinstance(word, dict)]
        recognized = "".join(str(word.get("word") or "") for word in words).strip()
        if recognized and re.sub(r"\s+", "", recognized) != re.sub(r"\s+", "", text):
            # CSV may have been corrected by a person. Never let stale ASR words
            # overwrite that authoritative correction. A partial cut now requires
            # reviewed source segmentation instead of proportional timing.
            words = []
        segments.append(
            {
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "text": text,
                "avg_logprob": float(matched.get("avg_logprob", 0.0)),
                "no_speech_prob": float(matched.get("no_speech_prob", 0.0)),
                "words": words,
                "speaker_id": matched.get("speaker_id", row.get("speaker_id", "")),
                "speaker_key": matched.get("speaker_key", row.get("speaker_key", "")),
                "speaker_label": matched.get("speaker_label", row.get("speaker_label", "")),
                "speaker_scope": matched.get("speaker_scope", ""),
                "render_policy": row.get("render_policy") or matched.get("render_policy", ""),
                "subtitle_prefix": matched.get("subtitle_prefix", ""),
            }
        )
    return segments


def _map_intervals_to_parts(
    intervals: list[tuple[float, float]], parts: Sequence[EditRange]
) -> list[tuple[float, float]]:
    mapped: list[tuple[float, float]] = []
    output_offset = 0.0
    for part in parts:
        for start, end in intervals:
            left = max(start, part.start)
            right = min(end, part.end)
            if right > left:
                mapped.append(
                    (
                        round(output_offset + left - part.start, 3),
                        round(output_offset + right - part.start, 3),
                    )
                )
        output_offset += part.end - part.start
    return mapped


def _map_segments_to_parts(
    segments: list[dict], parts: Sequence[EditRange]
) -> list[dict]:
    mapped: list[dict] = []
    output_offset = 0.0
    for part in parts:
        for segment in segments:
            # Preserve reviewed native captions without duplicating them.
            if segment.get("render_policy") == "native-captions":
                continue
            if not re.search(r"\w", str(segment.get("text") or "")):
                continue
            start, end = _canonical_bounds(segment)
            if end <= part.start or start >= part.end:
                continue
            source_words = [
                word
                for word in segment.get("words", [])
                if str(word.get("word") or "").strip()
            ]
            words = []
            for word in source_words:
                word_start, word_end = _canonical_bounds(word)
                midpoint = (word_start + word_end) / 2.0
                if not (part.start - 0.02 <= midpoint <= part.end + 0.02):
                    continue
                words.append(
                    {
                        **word,
                        "start_seconds": round(
                            output_offset + max(part.start, word_start) - part.start, 3
                        ),
                        "end_seconds": round(
                            output_offset + min(part.end, word_end) - part.start, 3
                        ),
                    }
                )
            words = [
                word
                for word in words
                if float(word["end_seconds"]) >= float(word["start_seconds"])
            ]
            if source_words:
                if not words:
                    continue
                local_text = "".join(str(word.get("word") or "") for word in words).strip()
                prefix = str(segment.get("subtitle_prefix") or "")
                if prefix and not local_text.startswith(prefix):
                    local_text = prefix + local_text
                local_start = float(words[0]["start_seconds"])
                local_end = float(words[-1]["end_seconds"])
            else:
                local_text = str(segment.get("text") or "").strip()
                # Text without word timing cannot be apportioned across a cut.
                # Ignore a sub-frame edge overlap, but never squeeze a whole
                # source paragraph into the small piece retained by the edit.
                overlap = min(end, part.end) - max(start, part.start)
                if not local_text or overlap <= 0.020001:
                    continue
                if max(part.start - start, end - part.end) > 0.060001:
                    raise ValueError(
                        f"Flow1 row {start:.3f}-{end:.3f}s is partially cut by "
                        f"slice {part.slice_id} range {part.start:.3f}-{part.end:.3f}s "
                        "without matching word timing. Split/align the source "
                        "transcript row before remapping; whole-row text would "
                        "include speech outside the clip."
                    )
                local_start = output_offset + max(start, part.start) - part.start
                local_end = output_offset + min(end, part.end) - part.start
            if not local_text or local_end <= local_start:
                continue
            mapped.append(
                {
                    "start_seconds": round(local_start, 3),
                    "end_seconds": round(local_end, 3),
                    "text": local_text,
                    "avg_logprob": float(segment.get("avg_logprob", 0.0)),
                    "no_speech_prob": float(segment.get("no_speech_prob", 0.0)),
                    "words": words,
                    "source_start_seconds": round(start, 3),
                    "source_end_seconds": round(end, 3),
                    "speaker_id": segment.get("speaker_id", ""),
                    "speaker_key": segment.get("speaker_key", ""),
                    "speaker_label": segment.get("speaker_label", ""),
                    "speaker_scope": segment.get("speaker_scope", ""),
                }
            )
        output_offset += part.end - part.start
    return sorted(
        mapped,
        key=lambda value: (
            float(value["start_seconds"]), float(value["end_seconds"])
        ),
    )


def slice_authoritative(args) -> int:
    """Render clip subtitles strictly by remapping Flow1; never run clip-local ASR."""
    for path in (
        args.transcript.resolve(),
        args.transcript_json.resolve(),
        args.completeness_report.resolve(),
        args.speech_json.resolve(),
        args.plan.resolve(),
        args.manifest.resolve(),
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    completeness = json.loads(
        args.completeness_report.resolve().read_text(encoding="utf-8-sig")
    )
    if completeness.get("status") != "PASS" or not completeness.get("authoritative"):
        raise ValueError(
            "Flow1 authoritative transcript has not passed completeness. "
            "Redo Flow1 before preparing clips."
        )
    segments = load_authoritative_segments(
        args.transcript_json.resolve(), args.transcript.resolve()
    )
    if not segments:
        raise ValueError("Authoritative Flow1 transcript has no retained subtitle rows")
    speech_payload = json.loads(args.speech_json.resolve().read_text(encoding="utf-8-sig"))
    source_speech = [
        _canonical_bounds(value)
        for value in speech_payload.get("regions", [])
        if isinstance(value, dict)
    ]
    ranges = apply_tail_padding(
        load_plan(args.plan.resolve()), args.tail_padding
    )
    grouped = group_plan(ranges)
    with args.manifest.resolve().open("r", encoding="utf-8-sig", newline="") as handle:
        manifest = list(csv.DictReader(handle))
    if not manifest:
        raise ValueError("Slice manifest is empty")
    content_types: dict[str, str] = {}
    if args.content_types_csv:
        with args.content_types_csv.resolve().open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            content_types = {
                Path(row.get("video", "")).name: (
                    row.get("content_type") or "narrative"
                ).strip().lower()
                for row in csv.DictReader(handle)
                if Path(row.get("video", "")).name
            }

    prepared = []
    for row in manifest:
        video_name = Path(row.get("file", "")).name
        slice_id = str(row.get("slice_id") or "").strip()
        if not video_name or slice_id not in grouped:
            raise ValueError(f"Manifest row does not match edit plan: {row}")
        clip_segments = _map_segments_to_parts(segments, grouped[slice_id])
        clip_speech = _map_intervals_to_parts(source_speech, grouped[slice_id])
        if not clip_segments:
            raise ValueError(
                f"Clip {video_name} has no Flow1 authoritative transcript rows; "
                "adjust the edit range or redo Flow1"
            )
        prepared.append((row, video_name, clip_segments, clip_speech))

    output = args.output.resolve()
    ass_output = args.ass_output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    ass_output.mkdir(parents=True, exist_ok=True)
    template = customize_ass_template(
        args.ass_template.resolve().read_text(encoding="utf-8-sig"),
        args.ass_style,
        font=args.ass_font,
        font_size=args.ass_font_size,
        color=args.ass_color,
        primary_color=getattr(args, "ass_primary_color", ""),
        outline_color=getattr(args, "ass_outline_color", ""),
        outline_width=getattr(args, "ass_outline_width", None),
    )
    qa_rows: list[dict] = []
    vad_rows: list[dict] = []
    for row, video_name, clip_segments, clip_speech in prepared:
        stem = Path(video_name).stem
        clip_output = output / stem
        clip_output.mkdir(parents=True, exist_ok=True)
        (clip_output / "transcript.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "source": str(args.transcript_json.resolve()),
                    "origin": "flow1-authoritative-remap",
                    "clip": video_name,
                    "segments": clip_segments,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        with (clip_output / "transcript.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=(
                    "start_seconds", "end_seconds", "speaker_id", "speaker_key",
                    "speaker_label", "text", "avg_logprob",
                ),
            )
            writer.writeheader()
            for segment in clip_segments:
                writer.writerow({key: segment[key] for key in writer.fieldnames})
        (clip_output / "speech-activity.json").write_text(
            json.dumps(
                {
                    "source": str(args.speech_json.resolve()),
                    "origin": "flow1-authoritative-remap",
                    "regions": [
                        {"start_seconds": start, "end_seconds": end}
                        for start, end in clip_speech
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (clip_output / "gap-recovery.json").write_text(
            json.dumps(
                {
                    "enabled": False,
                    "strategy": "forbidden-after-Flow1",
                    "recovered_segment_count": 0,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        use_speech = (
            clip_speech
            if content_types.get(video_name, "narrative") != "song"
            else None
        )
        ass_rows = write_ass(
            ass_output / f"{stem}.ass",
            template,
            clip_segments,
            args.ass_style,
            args.ass_name,
            args.subtitle_delay,
            speech_intervals=use_speech,
            vad_lead=args.subtitle_vad_lead,
            vad_tail=args.subtitle_vad_tail,
            vad_min_overlap=args.subtitle_vad_min_overlap,
        )
        vad_rows.extend({"clip": video_name, **value} for value in ass_rows)
        qa_rows.extend(
            {"clip": video_name, **value, "verified": "no", "notes": ""}
            for value in anchor_rows(clip_segments)
        )
        print(
            f"{video_name}: remapped {len(clip_segments)} authoritative Flow1 segments; "
            "clip-local ASR disabled"
        )

    with (output / "anchor-qa.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        fields = (
            "clip", "anchor", "start_seconds", "end_seconds", "text",
            "verified", "notes",
        )
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(qa_rows)
    with (output / "subtitle-vad-qa.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        fields = (
            "clip", "text", "original_start", "original_end", "adjusted_start",
            "adjusted_end", "speech_overlap", "status", "needs_review",
            "reviewed", "decision", "notes",
        )
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(vad_rows)
    (output / "provenance.json").write_text(
        json.dumps(
            {
                "origin": "flow1-authoritative-remap",
                "clip_local_transcription": False,
                "transcript": str(args.transcript.resolve()),
                "transcript_json": str(args.transcript_json.resolve()),
                "completeness_report": str(args.completeness_report.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Done: remapped authoritative subtitles for {len(manifest)} clip(s)")
    return 0

def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check the transcription and rendering environment")

    transcription = sub.add_parser("transcribe", help="Transcribe the source recording audio timeline")
    transcription.add_argument("--input", type=Path, required=True)
    transcription.add_argument("--output", type=Path, required=True)
    transcription.add_argument("--model", default="local:qwen3-asr-auto")
    transcription.add_argument("--model-cache", type=Path)
    transcription.add_argument("--language", default="zh")
    transcription.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    transcription.add_argument("--compute-type", default="float16")
    transcription.add_argument(
        "--speaker-count",
        type=int,
        help="Known number of speakers in the full session; constrains diarization.",
    )
    transcription.add_argument("--beam-size", type=int, default=5)
    transcription.add_argument(
        "--completeness-pause-tolerance",
        type=float,
        default=1.75,
        help="Treat shorter word-timestamp holes as normal intra-utterance pauses.",
    )
    transcription.add_argument(
        "--completeness-review-gap",
        type=float,
        default=2.0,
        help="Record remaining VAD-positive gaps at or above this duration for review.",
    )
    transcription.add_argument(
        "--completeness-blocking-gap",
        type=float,
        default=8.0,
        help="Fail Flow1 only when an unrecovered continuous gap reaches this duration.",
    )
    transcription.add_argument(
        "--initial-prompt",
        help="Optional transcription context, useful for canonical names and specialist vocabulary",
    )

    multilingual = sub.add_parser(
        "transcribe-multilingual",
        help="Transcribe chunk-by-chunk with automatic language detection",
    )
    multilingual.add_argument("--input", type=Path, required=True)
    multilingual.add_argument("--output", type=Path, required=True)
    multilingual.add_argument("--model", default="local:qwen3-asr-auto")
    multilingual.add_argument("--model-cache", type=Path)
    multilingual.add_argument("--languages", default="zh,ja,en")
    multilingual.add_argument("--chunk-seconds", type=float, default=60.0)
    multilingual.add_argument("--chunk-overlap-seconds", type=float, default=2.0)
    multilingual.add_argument("--completeness-pause-tolerance", type=float, default=1.75)
    multilingual.add_argument("--completeness-review-gap", type=float, default=2.0)
    multilingual.add_argument("--completeness-blocking-gap", type=float, default=8.0)
    multilingual.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    multilingual.add_argument("--compute-type", default="float16")
    multilingual.add_argument(
        "--speaker-count",
        type=int,
        help="Known number of speakers in the full session; constrains diarization.",
    )
    multilingual.add_argument("--beam-size", type=int, default=5)
    multilingual.add_argument(
        "--initial-prompt",
        help="Optional extra context appended after the shared general hotwords",
    )

    check = sub.add_parser("validate-plan", help="Validate a human- or agent-authored edit plan")
    check.add_argument("--plan", type=Path, required=True)
    check.add_argument("--audio", type=Path)
    check.add_argument("--gift-acks", type=Path, help="Removed gift rows; overlap makes validation fail")
    check.add_argument("--tail-padding", type=float, default=DEFAULT_TAIL_PADDING)

    gift_filter = sub.add_parser(
        "filter-gifts",
        help="Remove Bilibili gift acknowledgement rows before editorial analysis",
    )
    gift_filter.add_argument("--transcript", type=Path, required=True)
    gift_filter.add_argument("--output", type=Path, required=True)
    gift_filter.add_argument("--removed-output", type=Path, required=True)
    gift_filter.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    gift_filter.add_argument("--block-gap", type=float, default=12.0)

    subtitle = sub.add_parser(
        "slice-subtitles",
        help="Apply an edit plan to a timestamped transcript",
    )
    subtitle.add_argument("--transcript", type=Path, required=True)
    subtitle.add_argument("--plan", type=Path, required=True)
    subtitle.add_argument("--output", type=Path, required=True)
    subtitle.add_argument("--language-tags", action=argparse.BooleanOptionalAction, default=True)
    subtitle.add_argument("--subtitle-delay", type=float, default=DEFAULT_SUBTITLE_DELAY)
    subtitle.add_argument("--tail-padding", type=float, default=DEFAULT_TAIL_PADDING)

    authoritative = sub.add_parser(
        "slice-authoritative",
        help="Remap the Flow1 authoritative transcript without running clip-local ASR",
    )
    authoritative.add_argument("--transcript", type=Path, required=True)
    authoritative.add_argument("--transcript-json", type=Path, required=True)
    authoritative.add_argument("--completeness-report", type=Path, required=True)
    authoritative.add_argument("--speech-json", type=Path, required=True)
    authoritative.add_argument("--plan", type=Path, required=True)
    authoritative.add_argument("--manifest", type=Path, required=True)
    authoritative.add_argument("--output", type=Path, required=True)
    authoritative.add_argument("--ass-output", type=Path, required=True)
    authoritative.add_argument("--ass-template", type=Path, required=True)
    authoritative.add_argument("--ass-style", required=True)
    authoritative.add_argument("--ass-name", default="")
    authoritative.add_argument("--ass-font", default="")
    authoritative.add_argument("--ass-font-size", type=int)
    authoritative.add_argument("--ass-color", default="")
    authoritative.add_argument("--ass-primary-color", default="")
    authoritative.add_argument("--ass-outline-color", default="")
    authoritative.add_argument("--ass-outline-width", type=float)
    authoritative.add_argument("--content-types-csv", type=Path)
    authoritative.add_argument("--subtitle-delay", type=float, default=DEFAULT_SUBTITLE_DELAY)
    authoritative.add_argument("--subtitle-vad-lead", type=float, default=0.06)
    authoritative.add_argument("--subtitle-vad-tail", type=float, default=0.18)
    authoritative.add_argument("--subtitle-vad-min-overlap", type=float, default=0.08)
    authoritative.add_argument("--tail-padding", type=float, default=DEFAULT_TAIL_PADDING)

    render = sub.add_parser("export", help="Render non-contiguous slices using audio timestamps")
    render.add_argument("--audio", type=Path, required=True)
    render.add_argument("--video", type=Path, required=True)
    render.add_argument("--plan", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--timeline-tolerance", type=float, default=1.0)
    render.add_argument("--gift-acks", type=Path, help="Removed gift rows; overlap makes export fail")
    render.add_argument("--tail-padding", type=float, default=DEFAULT_TAIL_PADDING)
    render.add_argument("--preset", default="veryfast")
    render.add_argument("--crf", type=int, default=20)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "doctor":
            return doctor()
        if args.command == "transcribe":
            return transcribe(args)
        if args.command == "transcribe-multilingual":
            return transcribe_multilingual(args)
        if args.command == "validate-plan":
            return validate(args)
        if args.command == "filter-gifts":
            return filter_transcript(args)
        if args.command == "slice-subtitles":
            return slice_subtitles(args)
        if args.command == "slice-authoritative":
            return slice_authoritative(args)
        return export(args)
    except subprocess.CalledProcessError as exc:
        print(f"External command failed with exit code {exc.returncode}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
