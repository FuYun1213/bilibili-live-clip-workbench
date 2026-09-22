#!/usr/bin/env python3
"""Locate a locally re-encoded proxy inside its source by audio correlation."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import numpy as np
from scipy.signal import correlate


def pcm(ffmpeg: Path, media: Path, start: float, duration: float, rate: int) -> np.ndarray:
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(media),
        "-vn", "-ac", "1", "-ar", str(rate), "-f", "f32le", "pipe:1",
    ]
    raw = subprocess.run(command, check=True, stdout=subprocess.PIPE).stdout
    samples = np.frombuffer(raw, dtype="<f4").astype(np.float64)
    if samples.size == 0:
        raise RuntimeError(f"No audio decoded from {media}")
    samples -= samples.mean()
    deviation = samples.std()
    if deviation < 1e-7:
        raise RuntimeError(f"Audio is effectively silent: {media}")
    return samples / deviation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--proxy", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--approx-start", type=float, required=True)
    parser.add_argument("--radius", type=float, default=20.0)
    parser.add_argument("--proxy-sample-start", type=float, default=8.0)
    parser.add_argument("--sample-duration", type=float, default=24.0)
    parser.add_argument("--rate", type=int, default=2000)
    args = parser.parse_args()

    window_start = max(0.0, args.approx_start - args.radius)
    source_duration = args.radius * 2 + args.proxy_sample_start + args.sample_duration
    needle = pcm(args.ffmpeg, args.proxy, args.proxy_sample_start, args.sample_duration, args.rate)
    haystack = pcm(args.ffmpeg, args.source, window_start, source_duration, args.rate)
    if haystack.size < needle.size:
        raise RuntimeError("Source search window is shorter than proxy sample")

    values = correlate(haystack, needle, mode="valid", method="fft")
    energy = np.convolve(haystack * haystack, np.ones(needle.size), mode="valid")
    normalized = values / np.sqrt(np.maximum(energy * np.dot(needle, needle), 1e-12))
    best = int(np.argmax(normalized))
    proxy_start = window_start + best / args.rate - args.proxy_sample_start
    print(f"proxy_start={proxy_start:.6f} correlation={normalized[best]:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
