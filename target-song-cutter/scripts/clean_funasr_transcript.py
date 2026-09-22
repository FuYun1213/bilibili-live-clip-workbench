#!/usr/bin/env python3
"""Merge token-like FunASR rows into readable timestamped subtitle sentences."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from virtuareal_glossary import DEFAULT_GLOSSARY, load_replacements, normalize_text


CONTENT = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")
SENTENCE_END = re.compile(r"[。！？!?…][”’」』）】]*$")
NOISE_TOKENS = {"the", "a", "an"}


def clock(milliseconds: int, srt: bool = False) -> str:
    hours, rest = divmod(max(0, milliseconds), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1_000)
    if srt:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def join_text(left: str, right: str) -> str:
    if not left:
        return right
    spacer = (
        " "
        if left[-1:].isascii()
        and left[-1:].isalnum()
        and right[:1].isascii()
        and right[:1].isalnum()
        else ""
    )
    return left + spacer + right


def clean_rows(
    rows: list[dict[str, str]], replacements: dict[str, str]
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    group: dict[str, object] | None = None

    def flush() -> None:
        nonlocal group
        if not group:
            return
        text = re.sub(r"\s+", " ", str(group["text"])).strip()
        text = re.sub(r"([，。！？!?、；：…])\1+", r"\1", text)
        if CONTENT.search(text):
            group["text"] = text
            result.append(group)
        group = None

    for row in rows:
        text = normalize_text(row.get("text", "").strip(), replacements)
        plain = re.sub(r"[^A-Za-z0-9\u3400-\u9fff]+", "", text).casefold()
        if not CONTENT.search(text) or plain in NOISE_TOKENS:
            continue
        start = int(float(row["start_ms"]))
        end = int(float(row["end_ms"]))
        speaker = row.get("speaker", "")
        if group and (
            start - int(group["end_ms"]) > 1_600
            or (speaker and group["speaker"] and speaker != group["speaker"])
        ):
            flush()
        if group is None:
            group = {
                "start_ms": start,
                "end_ms": end,
                "speaker": speaker,
                "text": text,
            }
        else:
            group["end_ms"] = max(int(group["end_ms"]), end)
            group["text"] = join_text(str(group["text"]), text)
        duration = int(group["end_ms"]) - int(group["start_ms"])
        visual_chars = len(re.sub(r"\s+", "", str(group["text"])))
        if (
            duration >= 6_500
            or visual_chars >= 32
            or (
                duration >= 1_800
                and visual_chars >= 8
                and SENTENCE_END.search(str(group["text"]))
            )
        ):
            flush()
    flush()
    return result


def rows_from_raw_chunks(
    path: Path, replacements: dict[str, str], chunk_seconds: int
) -> list[dict[str, object]]:
    chunks = json.loads(path.read_text(encoding="utf-8"))
    result: list[dict[str, object]] = []
    for chunk_index, raw in enumerate(chunks):
        tokens = str(raw.get("raw_text") or "").split()
        timestamps = raw.get("timestamp") or []
        count = min(len(tokens), len(timestamps))
        group: dict[str, object] | None = None
        offset = chunk_index * chunk_seconds * 1_000

        def flush() -> None:
            nonlocal group
            if not group:
                return
            text = normalize_text(str(group["text"]), replacements).strip()
            if CONTENT.search(text):
                group["text"] = text
                result.append(group)
            group = None

        for index in range(count):
            token = tokens[index].strip()
            plain = re.sub(r"[^A-Za-z0-9\u3400-\u9fff]+", "", token).casefold()
            if not plain or plain in NOISE_TOKENS:
                continue
            start = offset + int(timestamps[index][0])
            end = offset + int(timestamps[index][1])
            if group and start - int(group["end_ms"]) > 1_200:
                flush()
            if group is None:
                group = {
                    "start_ms": start,
                    "end_ms": end,
                    "speaker": "",
                    "text": token,
                }
            else:
                group["end_ms"] = end
                group["text"] = join_text(str(group["text"]), token)
            duration = int(group["end_ms"]) - int(group["start_ms"])
            visual_chars = len(re.sub(r"\s+", "", str(group["text"])))
            if duration >= 6_000 or visual_chars >= 30:
                flush()
        flush()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--input", type=Path)
    inputs.add_argument("--raw-json", type=Path)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--glossary", type=Path, default=DEFAULT_GLOSSARY)
    parser.add_argument("--chunk-seconds", type=int, default=600)
    args = parser.parse_args()

    replacements = load_replacements(args.glossary)
    if args.raw_json:
        cleaned = rows_from_raw_chunks(
            args.raw_json, replacements, args.chunk_seconds
        )
        source_count = sum(
            len(item.get("timestamp") or [])
            for item in json.loads(args.raw_json.read_text(encoding="utf-8"))
        )
    else:
        with args.input.open("r", encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        cleaned = clean_rows(rows, replacements)
        source_count = len(rows)
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)

    csv_path = args.output_prefix.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["start_ms", "end_ms", "speaker", "text"]
        )
        writer.writeheader()
        writer.writerows(cleaned)

    txt_path = args.output_prefix.with_suffix(".txt")
    with txt_path.open("w", encoding="utf-8-sig", newline="\n") as handle:
        for row in cleaned:
            handle.write(
                f"[{clock(int(row['start_ms']))}-{clock(int(row['end_ms']))}] "
                f"{row['text']}\n"
            )

    srt_path = args.output_prefix.with_suffix(".srt")
    with srt_path.open("w", encoding="utf-8-sig", newline="\n") as handle:
        for index, row in enumerate(cleaned, 1):
            handle.write(
                f"{index}\n{clock(int(row['start_ms']), True)} --> "
                f"{clock(int(row['end_ms']), True)}\n{row['text']}\n\n"
            )
    print(f"Cleaned {source_count} source units into {len(cleaned)} subtitles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
