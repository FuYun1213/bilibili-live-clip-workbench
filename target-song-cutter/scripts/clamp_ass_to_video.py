#!/usr/bin/env python3
"""Clamp ASS dialogue ends to the paired video's final whole centisecond."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path


def duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def parse_ass_time(value: str) -> float:
    hour, minute, rest = value.split(":")
    second, centisecond = rest.split(".")
    return int(hour) * 3600 + int(minute) * 60 + int(second) + int(centisecond) / 100


def format_ass_time(value: float) -> str:
    centiseconds = max(0, int(round(value * 100)))
    hour, remainder = divmod(centiseconds, 360000)
    minute, remainder = divmod(remainder, 6000)
    second, centisecond = divmod(remainder, 100)
    return f"{hour}:{minute:02d}:{second:02d}.{centisecond:02d}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()

    changed_files = 0
    changed_events = 0
    for video in sorted(args.root.rglob("*.mp4")):
        ass = video.with_suffix(".ass")
        if not ass.exists():
            continue
        safe_end = math.floor(max(0.0, duration(video) - 0.01) * 100) / 100
        output: list[str] = []
        file_changed = False
        for line in ass.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("Dialogue:"):
                fields = line.split(",", 9)
                if len(fields) == 10 and parse_ass_time(fields[2]) > safe_end:
                    fields[2] = format_ass_time(safe_end)
                    line = ",".join(fields)
                    file_changed = True
                    changed_events += 1
            output.append(line)
        if file_changed:
            ass.write_text("\n".join(output) + "\n", encoding="utf-8-sig")
            changed_files += 1

    print(f"changed_files={changed_files} changed_events={changed_events}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
