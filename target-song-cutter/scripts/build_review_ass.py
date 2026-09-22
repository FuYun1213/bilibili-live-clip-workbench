#!/usr/bin/env python3
"""Build granular editable ASS from a reviewed source or clip-local transcript."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path


def ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, rest = divmod(centiseconds, 360000)
    minutes, rest = divmod(rest, 6000)
    whole, fraction = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{fraction:02d}"


def clean_text(value: str) -> str:
    value = value.strip().replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", value)


def split_text(text: str, limit: int) -> list[str]:
    text = clean_text(text)
    if not text:
        return []
    phrases = [piece.strip() for piece in re.split(r"(?<=[，。！？；])", text) if piece.strip()]
    output: list[str] = []
    for phrase in phrases or [text]:
        while len(phrase) > limit:
            cut = max(phrase.rfind(mark, 0, limit + 1) for mark in (" ", "，", "、", "；"))
            if cut < max(4, limit // 2):
                cut = limit
            else:
                cut += 1
            output.append(phrase[:cut].strip())
            phrase = phrase[cut:].strip()
        if phrase:
            output.append(phrase)
    return output


def read_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    parsed = []
    for row in rows:
        start = row.get("start_seconds", row.get("start", ""))
        end = row.get("end_seconds", row.get("end", ""))
        if start in (None, "") or end in (None, ""):
            continue
        parsed.append({"start": float(start), "end": float(end), "text": clean_text(row.get("text", ""))})
    return parsed


def read_plan(path: Path, slice_id: str) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("slice_id", "").strip() == slice_id]
    return sorted(({
        "order": int(row["order"]),
        "start": float(row["start_seconds"]),
        "end": float(row["end_seconds"]),
    } for row in rows), key=lambda row: row["order"])


def mapped_rows(rows: list[dict], parts: list[dict] | None) -> list[dict]:
    if not parts:
        return rows
    output: list[dict] = []
    offset = 0.0
    for part in parts:
        for row in rows:
            # Never carry a transcript cue that began before a cut into the new
            # part; this is the stale-first-subtitle failure the review gate bans.
            if row["start"] < part["start"] - 0.08 or row["start"] >= part["end"]:
                continue
            start = offset + row["start"] - part["start"]
            end = offset + min(row["end"], part["end"]) - part["start"]
            if end > start and row["text"]:
                output.append({"start": start, "end": end, "text": row["text"]})
        offset += part["end"] - part["start"]
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--style", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--slice-id")
    parser.add_argument("--delay", type=float, default=0.10)
    parser.add_argument("--max-chars", type=int, default=18)
    parser.add_argument("--max-duration", type=float, default=5.2)
    parser.add_argument("--girimi-pattern", default="")
    args = parser.parse_args()

    parts = None
    if args.plan:
        if not args.slice_id:
            raise SystemExit("--slice-id is required with --plan")
        parts = read_plan(args.plan.resolve(), args.slice_id)
        if not parts:
            raise SystemExit(f"slice not found in plan: {args.slice_id}")
    rows = mapped_rows(read_rows(args.transcript.resolve()), parts)
    template = args.template.resolve().read_text(encoding="utf-8-sig").rstrip() + "\n"
    pattern = re.compile(args.girimi_pattern) if args.girimi_pattern else None
    events: list[str] = []
    for row in rows:
        chunks = split_text(row["text"], args.max_chars)
        if not chunks:
            continue
        span = max(row["end"] - row["start"], 0.1)
        weights = [max(len(chunk), 1) for chunk in chunks]
        total = sum(weights)
        cursor = row["start"]
        for index, (chunk, weight) in enumerate(zip(chunks, weights)):
            natural_end = row["end"] if index == len(chunks) - 1 else cursor + span * weight / total
            end = min(natural_end, cursor + args.max_duration)
            if end - cursor < 0.45:
                end = min(row["end"], cursor + 0.45)
            style, name = args.style, args.name
            if pattern and pattern.search(chunk):
                style, name = "Girimi", "雾深Girimi"
            safe = chunk.replace("{", "（").replace("}", "）")
            events.append(
                f"Dialogue: 0,{ass_time(cursor + args.delay)},{ass_time(end + args.delay)},"
                f"{style},{name},0,0,0,,{safe}"
            )
            cursor = natural_end
    args.output.resolve().write_text(template + "\n".join(events) + "\n", encoding="utf-8-sig")
    print(f"events={len(events)} output={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
