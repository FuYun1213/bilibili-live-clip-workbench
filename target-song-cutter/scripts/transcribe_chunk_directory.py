#!/usr/bin/env python3
"""Resume-friendly faster-whisper transcription for numbered audio chunks."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from pathlib import Path

from general_hotwords import build_whisper_prompt


def srt_clock(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def chunk_index(path: Path) -> int:
    match = re.search(r"(\d+)$", path.stem)
    if not match:
        raise ValueError(f"Chunk filename has no numeric suffix: {path.name}")
    return int(match.group(1))


def write_outputs(output: Path, rows: list[dict], completed: set[str]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda row: (row["start_seconds"], row["end_seconds"]))
    with (output / "transcript.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("start_seconds", "end_seconds", "language", "text"),
        )
        writer.writeheader()
        writer.writerows(rows)
    (output / "transcript.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (output / "transcript.srt").open("w", encoding="utf-8-sig", newline="\n") as handle:
        for index, row in enumerate(rows, 1):
            handle.write(
                f"{index}\n{srt_clock(row['start_seconds'])} --> "
                f"{srt_clock(row['end_seconds'])}\n{row['text']}\n\n"
            )
    (output / "checkpoint.json").write_text(
        json.dumps(
            {"completed_windows": sorted(completed), "rows": rows},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--source-chunk-seconds", type=float, default=600.0)
    parser.add_argument("--window-seconds", type=float, default=90.0)
    parser.add_argument("--initial-prompt")
    args = parser.parse_args()

    source = args.input_dir.resolve()
    chunks = sorted(source.glob("chunk-*.wav"), key=chunk_index)
    if not chunks:
        raise FileNotFoundError(f"No chunk-*.wav files in {source}")

    output = args.output.resolve()
    checkpoint = output / "checkpoint.json"
    if checkpoint.is_file():
        saved = json.loads(checkpoint.read_text(encoding="utf-8-sig"))
        completed = set(saved.get("completed_windows", []))
        rows = list(saved.get("rows", []))
    else:
        completed, rows = set(), []

    dll_handle = None
    if args.device == "cuda" and os.name == "nt":
        import torch

        torch_lib = Path(torch.__file__).resolve().parent / "lib"
        if torch_lib.is_dir():
            os.environ["PATH"] = str(torch_lib) + os.pathsep + os.environ.get("PATH", "")
            dll_handle = os.add_dll_directory(str(torch_lib))

    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio

    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    sample_rate = 16_000
    window_samples = round(args.window_seconds * sample_rate)
    total_windows = sum(
        math.ceil(len(decode_audio(str(path), sampling_rate=sample_rate)) / window_samples)
        for path in chunks
    )
    processed = len(completed)

    for path in chunks:
        base_offset = chunk_index(path) * args.source_chunk_seconds
        audio = decode_audio(str(path), sampling_rate=sample_rate)
        for window_index, sample_start in enumerate(range(0, len(audio), window_samples)):
            key = f"{path.name}:{window_index}"
            if key in completed:
                continue
            sample_end = min(len(audio), sample_start + window_samples)
            local_offset = sample_start / sample_rate
            segments, info = model.transcribe(
                audio[sample_start:sample_end],
                language=None,
                beam_size=args.beam_size,
                vad_filter=True,
                condition_on_previous_text=False,
                initial_prompt=build_whisper_prompt(args.initial_prompt),
            )
            for segment in segments:
                text = segment.text.strip()
                if text:
                    rows.append(
                        {
                            "start_seconds": round(base_offset + local_offset + segment.start, 3),
                            "end_seconds": round(base_offset + local_offset + segment.end, 3),
                            "language": info.language,
                            "text": text,
                        }
                    )
            completed.add(key)
            processed += 1
            write_outputs(output, rows, completed)
            print(
                f"window {processed}/{total_windows}: {key}, language={info.language}, "
                f"rows={len(rows)}",
                flush=True,
            )

    print(f"Done: wrote {len(rows)} transcript rows to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
