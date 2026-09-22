#!/usr/bin/env python3
"""Transcribe a long livestream with FunASR and write timestamped review files."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from pathlib import Path

from general_hotwords import build_funasr_hotwords
from virtuareal_glossary import DEFAULT_GLOSSARY, load_hotword_string


def srt_time(milliseconds: int) -> str:
    hours, rest = divmod(max(0, milliseconds), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def clock(milliseconds: int) -> str:
    hours, rest = divmod(max(0, milliseconds), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds = rest // 1_000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def clean_text(text: str) -> str:
    return re.sub(r"<\|[^|]+\|>", "", text or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument(
        "--model",
        default="paraformer-zh",
        help="FunASR model name or an already-downloaded local model directory.",
    )
    parser.add_argument(
        "--vad-model",
        default="fsmn-vad",
        help="VAD model name or an already-downloaded local model directory.",
    )
    parser.add_argument(
        "--punc-model",
        default="ct-punc",
        help=(
            "Punctuation model name or an already-downloaded local model directory. "
            "Use 'none' to skip punctuation for transcript comparison."
        ),
    )
    parser.add_argument(
        "--speaker-diarization",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--hotword",
        help="Space-separated recording-specific override. General hotwords are always retained.",
    )
    parser.add_argument("--glossary", type=Path, default=DEFAULT_GLOSSARY)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    hotword = build_funasr_hotwords(args.hotword or load_hotword_string(args.glossary))

    from funasr import AutoModel

    model_options = {
        "model": args.model,
        "vad_model": args.vad_model,
        "device": args.device,
        "disable_update": True,
    }
    if str(args.punc_model or "").strip().casefold() not in {"", "none", "off"}:
        model_options["punc_model"] = args.punc_model
    if args.speaker_diarization:
        model_options["spk_model"] = "cam++"
    model = AutoModel(**model_options)
    result = model.generate(
        input=str(args.input.resolve()),
        batch_size_s=300,
        hotword=hotword,
        sentence_timestamp=True,
        return_raw_text=True,
    )
    raw = result[0] if isinstance(result, list) else result
    (args.output / "funasr-raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    sentence_info = raw.get("sentence_info") or []
    rows: list[dict[str, object]] = []
    for item in sentence_info:
        text = clean_text(str(item.get("text", "")))
        if not text:
            continue
        start = int(item.get("start", 0))
        end = int(item.get("end", start))
        rows.append(
            {
                "start_ms": start,
                "end_ms": end,
                "speaker": item.get("spk", ""),
                "text": text,
            }
        )

    if not rows:
        timestamps = raw.get("timestamp") or []
        text = clean_text(str(raw.get("text", "")))
        if timestamps and text:
            start = int(timestamps[0][0])
            end = int(timestamps[-1][1])
            rows.append({"start_ms": start, "end_ms": end, "speaker": "", "text": text})
    if not rows:
        raise RuntimeError("FunASR returned no timestamped sentence data")

    with (args.output / "funasr-transcript.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["start_ms", "end_ms", "speaker", "text"]
        )
        writer.writeheader()
        writer.writerows(rows)

    with (args.output / "funasr-transcript.txt").open(
        "w", encoding="utf-8-sig", newline="\n"
    ) as handle:
        for row in rows:
            speaker = f"[SPK{row['speaker']}]" if row["speaker"] != "" else ""
            handle.write(
                f"[{clock(int(row['start_ms']))}-{clock(int(row['end_ms']))}]"
                f"{speaker} {row['text']}\n"
            )

    with (args.output / "funasr-transcript.srt").open(
        "w", encoding="utf-8-sig", newline="\n"
    ) as handle:
        for index, row in enumerate(rows, 1):
            handle.write(
                f"{index}\n{srt_time(int(row['start_ms']))} --> "
                f"{srt_time(int(row['end_ms']))}\n{row['text']}\n\n"
            )
    print(f"FunASR wrote {len(rows)} timestamped sentences to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
