#!/usr/bin/env python3
"""Audit ASS bounds against clip-local ASR and independent speech activity."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from windows_process import hidden_subprocess_kwargs, run_checked_with_transient_retries


def parse_time(value: str) -> float:
    hours, minutes, seconds = value.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def ass_events(path: Path) -> list[tuple[float, float, str]]:
    events = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            if len(parts) == 10 and re.sub(r"\{[^}]*\}", "", parts[9]).strip():
                events.append(
                    (parse_time(parts[1]), parse_time(parts[2]), parts[8].strip())
                )
    return sorted(events)


def asr_events(value: object) -> list[tuple[float, float]]:
    events: list[tuple[float, float]] = []
    if isinstance(value, dict):
        start = value.get("start", value.get("start_seconds"))
        end = value.get("end", value.get("end_seconds"))
        if start is not None and end is not None and str(value.get("text", "")).strip():
            try:
                events.append((float(start), float(end)))
            except (TypeError, ValueError):
                pass
        for child in value.values():
            events.extend(asr_events(child))
    elif isinstance(value, list):
        for child in value:
            events.extend(asr_events(child))
    return events


def speech_events(value: object) -> list[tuple[float, float]]:
    if not isinstance(value, dict):
        return []
    regions = value.get("regions", [])
    events: list[tuple[float, float]] = []
    if not isinstance(regions, list):
        return events
    for region in regions:
        if not isinstance(region, dict):
            continue
        start = region.get("start_seconds", region.get("start"))
        end = region.get("end_seconds", region.get("end"))
        try:
            start_value, end_value = float(start), float(end)
        except (TypeError, ValueError):
            continue
        if end_value > start_value:
            events.append((start_value, end_value))
    return sorted(events)


def cue_speech_metrics(
    start: float, end: float, speech: list[tuple[float, float]]
) -> dict[str, float]:
    overlapping = [(left, right) for left, right in speech if right > start and left < end]
    overlap = sum(max(0.0, min(end, right) - max(start, left)) for left, right in overlapping)
    duration = max(end - start, 0.001)
    if not overlapping:
        return {
            "overlap": 0.0,
            "ratio": 0.0,
            "silent_lead": duration,
            "silent_tail": duration,
        }
    first = min(left for left, _ in overlapping)
    last = max(right for _, right in overlapping)
    return {
        "overlap": overlap,
        "ratio": overlap / duration,
        "silent_lead": max(0.0, first - start),
        "silent_tail": max(0.0, end - last),
    }


def cue_speech_warnings(
    index: int,
    metrics: dict[str, float],
    *,
    min_speech_overlap: float,
    min_speech_ratio: float,
    max_silent_lead: float,
    max_silent_tail: float,
) -> list[str]:
    """Report VAD disagreements without treating probabilistic timing as fatal."""
    warnings: list[str] = []
    if metrics["overlap"] < min_speech_overlap:
        warnings.append(f"cue {index} has no independent audible speech overlap")
    if metrics["ratio"] < min_speech_ratio:
        warnings.append(f"cue {index} is mostly outside detected speech")
    if metrics["silent_lead"] > max_silent_lead:
        warnings.append(
            f"cue {index} starts {metrics['silent_lead']:.3f}s before detected speech"
        )
    if metrics["silent_tail"] > max_silent_tail:
        warnings.append(
            f"cue {index} ends {metrics['silent_tail']:.3f}s after detected speech"
        )
    return warnings


def unanchored_cue_warning(
    effect: str,
    metrics: dict[str, float],
    *,
    speech_supplied: bool,
    min_speech_overlap: float,
) -> str:
    if effect == "review-context":
        return "is full-session review context without a clip-local ASR anchor"
    if speech_supplied and metrics["overlap"] >= min_speech_overlap:
        return "has VAD-confirmed speech but no clip-local ASR anchor"
    return ""


def final_media_vad_events(media: Path) -> list[tuple[float, float]]:
    """Rebuild only the lightweight speech axis after a user timeline edit.

    Flow 1 remains the sole transcription authority. A timeline edit changes
    the clip-local clock, however, so the old sliced ASR/VAD timestamps can no
    longer audit the edited MP4. Re-running VAD on the final media validates
    audible alignment without performing a second Whisper transcription.
    """
    from transcribe_review_clips import detect_speech_intervals

    return detect_speech_intervals(
        media.resolve(),
        threshold=0.30,
        min_speech_ms=120,
        min_silence_ms=650,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--ass", required=True, type=Path)
    parser.add_argument("--asr-json", required=True, type=Path)
    parser.add_argument("--speech-json", type=Path)
    parser.add_argument("--timeline-edited", action="store_true")
    parser.add_argument("--human-reviewed", action="store_true")
    parser.add_argument("--min-tail", type=float, default=0.2)
    parser.add_argument("--anchor-slop", type=float, default=0.25)
    parser.add_argument("--min-speech-overlap", type=float, default=0.08)
    # Short Chinese cues can legitimately span two speech bursts with a pause
    # between them.  Keep overlap/lead/tail guards strict, but avoid rejecting
    # a cue solely because its union speech ratio is just below 12%.
    parser.add_argument("--min-speech-ratio", type=float, default=0.10)
    parser.add_argument("--max-silent-lead", type=float, default=0.12)
    parser.add_argument("--max-silent-tail", type=float, default=0.35)
    args = parser.parse_args()
    probe = run_checked_with_transient_retries([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(args.media.resolve()),
    ], capture_output=True, text=True, encoding="utf-8", **hidden_subprocess_kwargs())
    duration = float(probe.stdout.strip())
    cues = ass_events(args.ass.resolve())
    if args.human_reviewed:
        speech = []
        anchors = []
        anchor_source = "explicit-human-review"
    elif args.timeline_edited:
        speech = sorted(final_media_vad_events(args.media))
        anchors = list(speech)
        anchor_source = "final-edited-media-vad"
    else:
        anchors = sorted(
            asr_events(json.loads(args.asr_json.resolve().read_text(encoding="utf-8-sig")))
        )
        speech = []
        if args.speech_json:
            speech = speech_events(
                json.loads(args.speech_json.resolve().read_text(encoding="utf-8-sig"))
            )
        anchor_source = "flow1-clip-local-asr"
    from asr_hotword_guard import audit_artifact
    text_guard = audit_artifact(args.ass.resolve())
    failures = ["ASS contains a hotword-list echo"] if text_guard["findings"] else []
    warnings = []
    for index, (start, end, effect) in enumerate(cues, 1):
        if start < 0 or end <= start or end > duration + 0.03:
            failures.append(f"cue {index} has invalid media timing")
        if args.human_reviewed:
            continue
        metrics = cue_speech_metrics(start, end, speech)
        has_anchor = any(
            anchor_end >= start - args.anchor_slop
            and anchor_start <= end + args.anchor_slop
            for anchor_start, anchor_end in anchors
        )
        if not has_anchor:
            anchor_warning = unanchored_cue_warning(
                effect,
                metrics,
                speech_supplied=bool(args.speech_json or args.timeline_edited),
                min_speech_overlap=args.min_speech_overlap,
            )
            if anchor_warning:
                warnings.append(f"cue {index} {anchor_warning}")
            else:
                failures.append(
                    f"cue {index} has no nearby clip-local speech anchor"
                )
        if args.speech_json or args.timeline_edited:
            warnings.extend(
                cue_speech_warnings(
                    index,
                    metrics,
                    min_speech_overlap=args.min_speech_overlap,
                    min_speech_ratio=args.min_speech_ratio,
                    max_silent_lead=args.max_silent_lead,
                    max_silent_tail=args.max_silent_tail,
                )
            )
    if args.human_reviewed:
        warnings.append(
            "speech-anchor gate skipped after explicit human review; "
            "media bounds remain enforced"
        )
        if not cues:
            warnings.append("human reviewer intentionally kept no dialogue cues")
    else:
        if not cues:
            failures.append("ASS has no dialogue cues")
        if not anchors:
            failures.append("ASR JSON has no speech anchors")
        if (args.speech_json or args.timeline_edited) and not speech:
            failures.append("speech activity JSON has no detected speech regions")
    if cues and duration - cues[-1][1] < args.min_tail:
        failures.append("last cue leaves too little media tail")
    report = {
        "duration": round(duration, 3),
        "cue_count": len(cues),
        "asr_anchor_count": len(anchors),
        "speech_region_count": len(speech),
        "anchor_source": anchor_source,
        "warnings": warnings,
        "failures": failures,
        "hotword_guard": text_guard,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
