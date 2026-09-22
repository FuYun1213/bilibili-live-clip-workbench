#!/usr/bin/env python3
"""Print compact first/last ASR evidence for every clip-local transcript."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()

    for transcript_path in sorted(args.root.rglob("transcript.json")):
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        segments = payload.get("segments") or []
        clip_name = Path(payload.get("source", transcript_path.parent.name)).stem
        if not segments:
            print(f"{clip_name}\tNO_SEGMENTS")
            continue
        first = segments[0]
        last = segments[-1]
        first_text = " ".join(str(first.get("text", "")).split())
        last_text = " ".join(str(last.get("text", "")).split())
        print(
            f"{clip_name}\t"
            f"FIRST {float(first.get('start_seconds', 0.0)):.2f}-{float(first.get('end_seconds', 0.0)):.2f}: {first_text}\t"
            f"LAST {float(last.get('start_seconds', 0.0)):.2f}-{float(last.get('end_seconds', 0.0)):.2f}: {last_text}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
