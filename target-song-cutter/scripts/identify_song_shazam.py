#!/usr/bin/env python3
"""Identify a song candidate from several clean mid-song fingerprints."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from shazamio import Shazam


async def identify(candidate: Path, ffmpeg: Path, offsets: list[float]) -> list[dict[str, object]]:
    shazam = Shazam()
    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="song-id-") as temp_dir:
        temp_root = Path(temp_dir)
        for index, offset in enumerate(offsets, start=1):
            sample = temp_root / f"sample-{index}.wav"
            subprocess.run(
                [
                    str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{offset:.3f}", "-t", "15", "-i", str(candidate),
                    "-vn", "-ac", "1", "-ar", "44100", str(sample),
                ],
                check=True,
            )
            try:
                payload = await shazam.recognize_song(str(sample))
            except Exception as exc:  # network failures should not discard other offsets
                results.append({"offset": offset, "error": str(exc)})
                continue
            track = (payload or {}).get("track") or {}
            results.append(
                {
                    "offset": offset,
                    "title": track.get("title"),
                    "artist": track.get("subtitle"),
                    "isrc": track.get("isrc"),
                    "url": track.get("url"),
                }
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--offsets", default="30,60,120,180")
    args = parser.parse_args()
    offsets = [float(item) for item in args.offsets.split(",") if item.strip()]
    print(json.dumps(asyncio.run(identify(args.input, args.ffmpeg, offsets)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
