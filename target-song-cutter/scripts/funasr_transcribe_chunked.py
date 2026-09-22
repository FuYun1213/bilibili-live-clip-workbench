#!/usr/bin/env python3
"""Recoverable FunASR transcription for long livestreams."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from pathlib import Path

from general_hotwords import build_funasr_hotwords
from virtuareal_glossary import DEFAULT_GLOSSARY, load_hotword_string


def clean(text: str) -> str:
    return re.sub(r"<\|[^|]+\|>", "", text or "").strip()


def clock(milliseconds: int, srt: bool = False) -> str:
    hours, rest = divmod(max(0, milliseconds), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1_000)
    return (
        f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
        if srt
        else f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    )


def write_outputs(output: Path, rows: list[dict], completed: int) -> None:
    with (output / "funasr-transcript.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["start_ms", "end_ms", "speaker", "text"]
        )
        writer.writeheader()
        writer.writerows(rows)
    with (output / "funasr-transcript.txt").open(
        "w", encoding="utf-8-sig", newline="\n"
    ) as handle:
        for row in rows:
            handle.write(
                f"[{clock(row['start_ms'])}-{clock(row['end_ms'])}] {row['text']}\n"
            )
    with (output / "funasr-transcript.srt").open(
        "w", encoding="utf-8-sig", newline="\n"
    ) as handle:
        for index, row in enumerate(rows, 1):
            handle.write(
                f"{index}\n{clock(row['start_ms'], True)} --> "
                f"{clock(row['end_ms'], True)}\n{row['text']}\n\n"
            )
    (output / "checkpoint.json").write_text(
        json.dumps(
            {"completed_chunks": completed, "sentence_count": len(rows)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--chunk-seconds", type=int, default=600)
    parser.add_argument("--rebuild-chunks", action="store_true")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--model", required=True)
    parser.add_argument("--vad-model", required=True)
    parser.add_argument("--punc-model")
    parser.add_argument(
        "--hotword",
        help="Space-separated recording-specific override. General hotwords are always retained.",
    )
    parser.add_argument("--glossary", type=Path, default=DEFAULT_GLOSSARY)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    hotword = build_funasr_hotwords(args.hotword or load_hotword_string(args.glossary))
    chunks = args.output / "audio-chunks"
    chunks.mkdir(exist_ok=True)
    checkpoint = args.output / "checkpoint.json"
    if (
        args.rebuild_chunks
        or not list(chunks.glob("chunk-*.wav"))
        or not checkpoint.is_file()
    ):
        subprocess.run(
            [
                str(args.ffmpeg),
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(args.input.resolve()),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-f",
                "segment",
                "-segment_time",
                str(args.chunk_seconds),
                "-reset_timestamps",
                "1",
                str(chunks / "chunk-%03d.wav"),
            ],
            check=True,
        )

    from funasr import AutoModel

    model_options = dict(
        model=args.model,
        vad_model=args.vad_model,
        device=args.device,
        disable_update=True,
    )
    if args.punc_model:
        model_options["punc_model"] = args.punc_model
    model = AutoModel(**model_options)
    rows: list[dict] = []
    raw_results: list[dict] = []
    for index, chunk in enumerate(sorted(chunks.glob("chunk-*.wav"))):
        result = model.generate(
            input=str(chunk.resolve()),
            batch_size_s=300,
            hotword=hotword,
            sentence_timestamp=True,
            return_raw_text=True,
        )
        raw = result[0] if isinstance(result, list) else result
        raw_results.append(raw)
        offset = index * args.chunk_seconds * 1_000
        sentence_info = raw.get("sentence_info") or []
        for item in sentence_info:
            text = clean(str(item.get("text", "")))
            if text:
                rows.append(
                    {
                        "start_ms": offset + int(item.get("start", 0)),
                        "end_ms": offset + int(item.get("end", 0)),
                        "speaker": "",
                        "text": text,
                    }
                )
        if not sentence_info:
            timestamps = raw.get("timestamp") or []
            raw_text = clean(str(raw.get("raw_text") or raw.get("text") or ""))
            tokens = raw_text.split()
            if len(tokens) != len(timestamps):
                tokens = list(raw_text.replace(" ", ""))
            if timestamps and tokens:
                count = min(len(tokens), len(timestamps))
                group_start = 0
                for token_index in range(count):
                    at_last = token_index == count - 1
                    duration = int(timestamps[token_index][1]) - int(timestamps[group_start][0])
                    next_gap = (
                        int(timestamps[token_index + 1][0]) - int(timestamps[token_index][1])
                        if not at_last
                        else 0
                    )
                    if at_last or duration >= 8_000 or next_gap >= 1_000 or token_index - group_start >= 24:
                        text = "".join(tokens[group_start : token_index + 1]).strip()
                        if text:
                            rows.append(
                                {
                                    "start_ms": offset + int(timestamps[group_start][0]),
                                    "end_ms": offset + int(timestamps[token_index][1]),
                                    "speaker": "",
                                    "text": text,
                                }
                            )
                        group_start = token_index + 1
        write_outputs(args.output, rows, index + 1)
        (args.output / "funasr-raw-chunks.json").write_text(
            json.dumps(raw_results, ensure_ascii=False), encoding="utf-8"
        )
        print(f"chunk {index + 1}: {chunk.name}, total sentences={len(rows)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
