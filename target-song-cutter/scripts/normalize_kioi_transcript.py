#!/usr/bin/env python3
"""Normalize recurring Kioi-community names in an ASR transcript CSV."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from virtuareal_glossary import DEFAULT_GLOSSARY, load_replacements, normalize_text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--glossary", type=Path, default=DEFAULT_GLOSSARY)
    args = parser.parse_args()
    replacements = load_replacements(args.glossary)

    with args.input.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        rows = list(reader)
        columns = reader.fieldnames
    if not columns or "text" not in columns:
        raise ValueError("Transcript CSV must contain a text column")
    changed = 0
    for row in rows:
        text = normalize_text(row["text"], replacements)
        if text != row["text"]:
            changed += 1
            row["text"] = text

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Normalized {changed} transcript rows: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
