#!/usr/bin/env python3
"""Audit ASS against final media and clip-local ASR with common time-key variants."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

TAG_RE = re.compile(r"\{[^}]*\}")
TEXT_RE = re.compile(r"[^0-9A-Za-z\u3400-\u9fff]+")


@dataclass
class Event:
    start: float
    end: float
    text: str


def parse_time(value: str) -> float:
    hours, minutes, seconds = value.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def normalize(value: str) -> str:
    return TEXT_RE.sub("", TAG_RE.sub("", value)).lower()


def ass_events(path: Path) -> list[Event]:
    result = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) == 10 and normalize(parts[9]):
            result.append(Event(parse_time(parts[1]), parse_time(parts[2]), normalize(parts[9])))
    return sorted(result, key=lambda item: (item.start, item.end))


def recursive_asr_events(value: object) -> list[Event]:
    result: list[Event] = []
    if isinstance(value, dict):
        start = value.get("start", value.get("start_seconds"))
        end = value.get("end", value.get("end_seconds"))
        if start is not None and end is not None and "text" in value:
            try:
                text = normalize(str(value["text"]))
                if text:
                    result.append(Event(float(start), float(end), text))
            except (TypeError, ValueError):
                pass
        for child in value.values():
            result.extend(recursive_asr_events(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(recursive_asr_events(child))
    return result


def similar(cue: Event, candidates: list[Event]) -> bool:
    nearby = [item.text for item in candidates if item.end >= cue.start - 0.8 and item.start <= cue.end + 0.8]
    if not nearby:
        return False
    heard = "".join(nearby)
    if cue.text in heard or heard in cue.text:
        return True
    return difflib.SequenceMatcher(None, cue.text, heard).ratio() >= 0.38


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--ass", required=True, type=Path)
    parser.add_argument("--asr-json", required=True, type=Path)
    parser.add_argument("--min-tail", type=float, default=0.2)
    args = parser.parse_args()
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(args.media.resolve()),
    ], check=True, capture_output=True, text=True, encoding="utf-8")
    duration = float(probe.stdout.strip())
    cues = ass_events(args.ass.resolve())
    asr = sorted(recursive_asr_events(json.loads(args.asr_json.resolve().read_text(encoding="utf-8-sig"))), key=lambda item: item.start)
    failures: list[str] = []
    if not cues:
        failures.append("ASS has no dialogue events")
    if not asr:
        failures.append("ASR JSON has no timed text")
    for index, cue in enumerate(cues, 1):
        if cue.start < 0 or cue.end <= cue.start or cue.end > duration + 0.03:
            failures.append(f"cue {index} has invalid media timing")
        if asr and not similar(cue, asr):
            failures.append(f"cue {index} has no nearby audible ASR anchor")
    if cues and duration - cues[-1].end < args.min_tail:
        failures.append("last cue leaves too little media tail")
    report = {
        "duration": round(duration, 3), "cue_count": len(cues),
        "asr_event_count": len(asr), "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
