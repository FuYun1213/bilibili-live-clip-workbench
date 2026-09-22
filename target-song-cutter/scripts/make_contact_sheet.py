#!/usr/bin/env python3
"""Create a compact JPEG contact sheet for local visual QA."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pattern", default="*.jpg")
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--width", type=int, default=480)
    args = parser.parse_args()
    files = sorted(args.input_dir.resolve().glob(args.pattern))
    if not files:
        raise SystemExit("no images found")
    height = round(args.width * 9 / 16)
    rows = math.ceil(len(files) / args.columns)
    sheet = Image.new("RGB", (args.columns * args.width, rows * (height + 28)), "#202020")
    draw = ImageDraw.Draw(sheet)
    for index, path in enumerate(files):
        image = Image.open(path).convert("RGB")
        image.thumbnail((args.width, height), Image.Resampling.LANCZOS)
        x = (index % args.columns) * args.width
        y = (index // args.columns) * (height + 28)
        sheet.paste(image, (x + (args.width - image.width) // 2, y))
        draw.text((x + 8, y + height + 5), f"{index + 1:02d}  {path.stem[:22]}", fill="white")
    args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.output.resolve(), "JPEG", quality=78, optimize=True)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
