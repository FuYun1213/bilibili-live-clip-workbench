#!/usr/bin/env python3
"""Print Bilibili XML danmaku events inside a source-time window."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--contains", default="")
    args = parser.parse_args()

    keywords = [item for item in args.contains.split(",") if item]
    root = ET.parse(args.input).getroot()
    for event in root.iter("d"):
        timestamp = float(event.attrib["p"].split(",", 1)[0])
        text = event.text or ""
        if args.start <= timestamp <= args.end and (
            not keywords or any(keyword in text for keyword in keywords)
        ):
            print(f"{timestamp:.3f}\t{text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
