#!/usr/bin/env python3
"""Apply audited text replacements to an authoritative transcript CSV.

Only the ``text`` column is changed.  Timestamps and every other column are
preserved, and the command fails when a requested source phrase is absent so a
stale correction list cannot silently pass.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--corrections", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    config = json.loads(args.corrections.read_text(encoding="utf-8-sig"))
    replacements = list(config.get("authoritative_replacements", []))
    if not replacements:
        raise ValueError("corrections file has no authoritative_replacements")

    with args.input.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "text" not in reader.fieldnames:
            raise ValueError(f"{args.input}: missing text column")
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    seen = [0] * len(replacements)
    for row in rows:
        text = row.get("text", "")
        for index, item in enumerate(replacements):
            old = str(item["old"])
            new = str(item["new"])
            count = text.count(old)
            if count:
                seen[index] += count
                text = text.replace(old, new)
        row["text"] = text

    missing = [str(replacements[i]["old"]) for i, count in enumerate(seen) if count == 0]
    if missing:
        raise ValueError(f"{args.input}: correction targets not found: {missing}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(
        f"CORRECTED {args.input.name} replacements={sum(seen)} "
        f"rules={len(replacements)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
