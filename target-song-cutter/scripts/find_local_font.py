#!/usr/bin/env python3
"""Find a font by its internal OpenType names, regardless of file extension."""

from __future__ import annotations

import argparse
import re
import struct
from pathlib import Path


def decoded_names(data: bytes, sfnt_offset: int = 0) -> set[str]:
    if len(data) < sfnt_offset + 12:
        return set()
    signature, table_count = struct.unpack_from(">4sH", data, sfnt_offset)
    if signature not in {b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"}:
        return set()
    name_offset = None
    for index in range(table_count):
        record_offset = sfnt_offset + 12 + index * 16
        if record_offset + 16 > len(data):
            return set()
        tag, _, offset, _ = struct.unpack_from(">4sIII", data, record_offset)
        if tag == b"name":
            name_offset = offset
            break
    if name_offset is None or name_offset + 6 > len(data):
        return set()
    _, count, storage_offset = struct.unpack_from(">HHH", data, name_offset)
    names: set[str] = set()
    for index in range(count):
        record_offset = name_offset + 6 + index * 12
        if record_offset + 12 > len(data):
            break
        platform, _, _, name_id, length, offset = struct.unpack_from(">HHHHHH", data, record_offset)
        if name_id not in {1, 2, 4, 6, 16, 17}:
            continue
        start = name_offset + storage_offset + offset
        raw = data[start : start + length]
        try:
            names.add(raw.decode("utf-16-be" if platform in {0, 3} else "mac_roman"))
        except UnicodeDecodeError:
            continue
    return names


def font_names(path: Path) -> set[str]:
    with path.open("rb") as handle:
        signature = handle.read(4)
    if signature not in {b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1", b"ttcf"}:
        return set()
    data = path.read_bytes()
    if data[:4] == b"ttcf" and len(data) >= 16:
        font_count = struct.unpack_from(">I", data, 8)[0]
        names: set[str] = set()
        for index in range(min(font_count, 64)):
            names |= decoded_names(data, struct.unpack_from(">I", data, 12 + index * 4)[0])
        return names
    return decoded_names(data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pattern")
    parser.add_argument("roots", nargs="+", type=Path)
    args = parser.parse_args()
    pattern = re.compile(args.pattern, re.I)

    scanned = 0
    fonts = 0
    for root in args.roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            try:
                if not path.is_file() or not (20_000 <= path.stat().st_size <= 60_000_000):
                    continue
                scanned += 1
                names = font_names(path)
                if not names:
                    continue
                fonts += 1
                if any(pattern.search(name) for name in names):
                    print(path)
                    for name in sorted(names):
                        print(f"  {name}")
            except (OSError, KeyError, ValueError, struct.error):
                continue
    print(f"Scanned {scanned} candidate files; parsed {fonts} fonts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
