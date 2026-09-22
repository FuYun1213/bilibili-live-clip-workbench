#!/usr/bin/env python3
"""Remove reviewed ASS dialogue cues whose visible text exactly matches a deny list."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ass", required=True, type=Path)
    parser.add_argument("--text", action="append", required=True)
    args = parser.parse_args()
    path = args.ass.resolve()
    denied = {value.strip() for value in args.text}
    output, removed = [], 0
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            visible = re.sub(r"\{[^}]*\}", "", parts[9]).strip() if len(parts) == 10 else ""
            if visible in denied:
                removed += 1
                continue
        output.append(line)
    path.write_text("\n".join(output) + "\n", encoding="utf-8-sig")
    print(f"removed={removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
