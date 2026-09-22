#!/usr/bin/env python3
"""Batch multilingual/no-VAD transcription while loading Whisper only once."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path

from content_slicer import build_whisper_prompt, ensure_ffmpeg, media_duration, srt_timestamp


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument("--languages", default="zh,ja,en")
    parser.add_argument("--chunk-seconds", type=float, default=60.0)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--initial-prompt", default="")
    args = parser.parse_args()

    allowed = [item.strip() for item in args.languages.split(",") if item.strip()]
    sources = sorted(args.root.rglob("*.mp4"))
    if not sources:
        raise RuntimeError(f"No MP4 files under {args.root}")

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

    for source in sources:
        source_id = source.parents[1].name
        clip_id = source.stem.split("_", 1)[0]
        output = args.output / source_id / clip_id
        output.mkdir(parents=True, exist_ok=True)
        duration = media_duration(source)
        rows: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory(prefix="song-asr-", dir=output) as temporary:
            chunk_path = Path(temporary) / "chunk.wav"
            chunk_count = max(1, math.ceil(duration / args.chunk_seconds))
            for index in range(chunk_count):
                offset = index * args.chunk_seconds
                chunk_duration = min(args.chunk_seconds, duration - offset)
                subprocess.run(
                    [
                        ffmpeg, "-loglevel", "error", "-y", "-ss", f"{offset:.3f}",
                        "-t", f"{chunk_duration:.3f}", "-i", str(source), "-vn",
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(chunk_path),
                    ],
                    check=True,
                )

                def decode(language: str | None):
                    raw, info = model.transcribe(
                        str(chunk_path), language=language, beam_size=args.beam_size,
                        vad_filter=False, condition_on_previous_text=False,
                        initial_prompt=build_whisper_prompt(args.initial_prompt),
                    )
                    items = list(raw)
                    confidence = (
                        sum(float(item.avg_logprob) for item in items) / len(items)
                        if items else float("-inf")
                    )
                    return items, info, confidence

                items, info, _ = decode(None)
                detected = info.language
                if detected not in allowed:
                    alternatives = [decode(language) for language in allowed]
                    items, info, _ = max(alternatives, key=lambda value: value[2])
                    detected = info.language
                for item in items:
                    text = item.text.strip()
                    if text:
                        rows.append(
                            {
                                "start_seconds": offset + float(item.start),
                                "end_seconds": offset + float(item.end),
                                "language": detected,
                                "language_probability": float(info.language_probability),
                                "avg_logprob": float(item.avg_logprob),
                                "text": text,
                            }
                        )
                print(f"{source_id}/{clip_id}: chunk {index + 1}/{chunk_count} ({detected})", flush=True)

        with (output / "transcript_multilingual.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            fieldnames = ["start_seconds", "end_seconds", "language", "language_probability", "avg_logprob", "text"]
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    **row,
                    "start_seconds": f"{float(row['start_seconds']):.3f}",
                    "end_seconds": f"{float(row['end_seconds']):.3f}",
                    "language_probability": f"{float(row['language_probability']):.4f}",
                    "avg_logprob": f"{float(row['avg_logprob']):.4f}",
                })
        (output / "transcript_multilingual.json").write_text(
            json.dumps({"source": str(source), "languages": allowed, "segments": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with (output / "transcript_multilingual.srt").open("w", encoding="utf-8-sig", newline="\n") as handle:
            for index, row in enumerate(rows, 1):
                handle.write(
                    f"{index}\n{srt_timestamp(float(row['start_seconds']))} --> "
                    f"{srt_timestamp(float(row['end_seconds']))}\n"
                    f"[{row['language']}] {row['text']}\n\n"
                )
        print(f"DONE {source_id}/{clip_id}: {len(rows)} segments", flush=True)

    if dll_handle is not None:
        dll_handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
