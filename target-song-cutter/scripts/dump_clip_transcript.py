#!/usr/bin/env python3
"""Dump clip-local ASR segments for selected clip ids."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("clip_ids", nargs="+")
    args = parser.parse_args()

    wanted = tuple(args.clip_ids)
    for transcript_path in sorted(args.root.rglob("transcript.json")):
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        clip_name = Path(payload.get("source", transcript_path.parent.name)).stem
        if not clip_name.startswith(wanted):
            continue
        print(f"\n## {clip_name}")
        for segment in payload.get("segments") or []:
            start = float(segment.get("start_seconds", 0.0))
            end = float(segment.get("end_seconds", 0.0))
            text = " ".join(str(segment.get("text", "")).split())
            print(f"{start:8.2f}-{end:8.2f}  {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
