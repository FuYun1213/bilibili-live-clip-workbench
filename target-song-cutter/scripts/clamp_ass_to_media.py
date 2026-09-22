#!/usr/bin/env python3
"""Clamp ASS cues to a final media duration while preserving a small clean tail."""

from __future__ import annotations

import argparse
import math
import subprocess
from pathlib import Path

from windows_process import hidden_subprocess_kwargs, run_checked_with_transient_retries


def parse_time(value: str) -> float:
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, rest = divmod(centiseconds, 360000)
    minutes, rest = divmod(rest, 6000)
    whole, fraction = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{fraction:02d}"


def ass_time_from_centiseconds(centiseconds: int) -> str:
    centiseconds = max(0, int(centiseconds))
    hours, rest = divmod(centiseconds, 360000)
    minutes, rest = divmod(rest, 6000)
    whole, fraction = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{fraction:02d}"


def clamp_ass_lines(
    lines: list[str], *, duration: float, tail: float
) -> tuple[list[str], int]:
    """Clamp using ASS centiseconds so rounding cannot create zero-length cues."""
    limit_cs = max(0, int(math.floor((duration - tail) * 100 + 1e-7)))
    output: list[str] = []
    changed = 0
    for line in lines:
        if not line.startswith("Dialogue:"):
            output.append(line)
            continue
        parts = line.split(",", 9)
        start_cs = int(round(parse_time(parts[1]) * 100))
        end_cs = int(round(parse_time(parts[2]) * 100))
        clamped_end_cs = min(end_cs, limit_cs)
        if clamped_end_cs <= start_cs:
            changed += 1
            continue
        if clamped_end_cs != end_cs:
            changed += 1
            parts[2] = ass_time_from_centiseconds(clamped_end_cs)
            line = ",".join(parts)
        output.append(line)
    return output, changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--ass", required=True, type=Path)
    parser.add_argument("--tail", type=float, default=0.25)
    args = parser.parse_args()
    probe = run_checked_with_transient_retries([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(args.media.resolve()),
    ], capture_output=True, text=True, encoding="utf-8", **hidden_subprocess_kwargs())
    lines = args.ass.resolve().read_text(encoding="utf-8-sig").splitlines()
    output, changed = clamp_ass_lines(
        lines,
        duration=float(probe.stdout.strip()),
        tail=args.tail,
    )
    args.ass.resolve().write_text("\n".join(output) + "\n", encoding="utf-8-sig")
    print(f"changed={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
