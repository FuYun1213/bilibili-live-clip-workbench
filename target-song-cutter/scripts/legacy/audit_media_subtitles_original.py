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
    result: list[Event] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        text = normalize(parts[9])
        if text:
            result.append(Event(parse_time(parts[1]), parse_time(parts[2]), text))
    return sorted(result, key=lambda item: (item.start, item.end))


def media_duration(path: Path, ffprobe: str) -> float:
    process = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return float(process.stdout.strip())


def recursive_asr_events(value: object) -> list[Event]:
    result: list[Event] = []
    if isinstance(value, dict):
        if all(key in value for key in ("start", "end", "text")):
            try:
                text = normalize(str(value["text"]))
                if text:
                    result.append(Event(float(value["start"]), float(value["end"]), text))
            except (TypeError, ValueError):
                pass
        for child in value.values():
            result.extend(recursive_asr_events(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(recursive_asr_events(child))
    return result


def similar(cue: Event, candidates: list[Event]) -> bool:
    nearby = [item.text for item in candidates if item.end >= cue.start - 0.55 and item.start <= cue.end + 0.55]
    if not nearby:
        return False
    heard = "".join(nearby)
    if cue.text in heard or heard in cue.text:
        return True
    return difflib.SequenceMatcher(None, cue.text, heard).ratio() >= 0.42


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit subtitle events against the final media timeline and optional clip-local ASR.")
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--ass", required=True, type=Path)
    parser.add_argument("--asr-json", type=Path)
    parser.add_argument("--require-asr", action="store_true")
    parser.add_argument("--min-tail", type=float, default=0.2)
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()

    failures: list[str] = []
    duration = media_duration(args.media, args.ffprobe)
    cues = ass_events(args.ass)
    if not cues:
        failures.append("ASS has no non-empty dialogue events")
    for index, cue in enumerate(cues, 1):
        if cue.start < 0 or cue.end <= cue.start:
            failures.append(f"cue {index} has invalid timing")
        if cue.end > duration + 0.03:
            failures.append(f"cue {index} ends after media duration")
    if cues and duration - cues[-1].end < args.min_tail:
        failures.append(f"last cue leaves less than {args.min_tail:.2f}s media tail")

    asr: list[Event] = []
    if args.asr_json:
        asr = sorted(recursive_asr_events(json.loads(args.asr_json.read_text(encoding="utf-8-sig"))), key=lambda item: item.start)
        if not asr:
            failures.append("ASR JSON contains no usable timed text")
        else:
            for index, cue in enumerate(cues, 1):
                if not similar(cue, asr):
                    failures.append(f"cue {index} has no matching audible ASR anchor: {cue.text}")
    elif args.require_asr:
        failures.append("clip-local ASR JSON is required")

    report = {
        "media": str(args.media),
        "ass": str(args.ass),
        "duration": round(duration, 3),
        "cue_count": len(cues),
        "asr_event_count": len(asr),
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
