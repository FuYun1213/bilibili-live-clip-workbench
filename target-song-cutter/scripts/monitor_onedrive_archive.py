#!/usr/bin/env python3
"""Monitor OneDrive archive hydration state until every manifest file is online-only."""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path


FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_UNPINNED = 0x00100000
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def attributes(path: Path) -> int:
    value = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    if value == INVALID_FILE_ATTRIBUTES:
        raise OSError(f"GetFileAttributesW failed: {path}")
    return int(value)


def allocated_size(path: Path) -> int:
    high = ctypes.c_ulong(0)
    low = ctypes.windll.kernel32.GetCompressedFileSizeW(str(path), ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.windll.kernel32.GetLastError() != 0:
        raise OSError(f"GetCompressedFileSizeW failed: {path}")
    return (int(high.value) << 32) | int(low)


def flag_text(value: int) -> str:
    labels: list[str] = []
    if value & FILE_ATTRIBUTE_OFFLINE:
        labels.append("offline")
    if value & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
        labels.append("recall-on-access")
    if value & FILE_ATTRIBUTE_PINNED:
        labels.append("pinned")
    if value & FILE_ATTRIBUTE_UNPINNED:
        labels.append("unpinned")
    return "+".join(labels) or "local"


def online_only(value: int) -> bool:
    return bool(value & (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))


def write_atomic(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def snapshot(rows: list[dict[str, str]], output_json: Path, output_csv: Path) -> dict[str, object]:
    item_rows: list[dict[str, object]] = []
    for row in rows:
        destination = Path(row["destination"])
        source = Path(row["source"])
        exists = destination.is_file()
        source_absent = not source.exists()
        if exists:
            value = attributes(destination)
            cloud_state = flag_text(value)
            is_online = online_only(value)
            local_bytes = allocated_size(destination)
            if not is_online:
                subprocess.run(
                    ["attrib", "+U", "-P", str(destination)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        else:
            cloud_state = "missing"
            is_online = False
            local_bytes = 0
        item_rows.append(
            {
                "source_id": row["source_id"],
                "creator": row["creator"],
                "kind": row["kind"],
                "destination": str(destination),
                "destination_exists": "yes" if exists else "no",
                "source_absent": "yes" if source_absent else "no",
                "cloud_state": cloud_state,
                "online_only_verified": "yes" if is_online else "no",
                "logical_bytes": int(row["bytes"]),
                "local_allocated_bytes": local_bytes,
            }
        )

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    csv_lines: list[str] = []
    if item_rows:
        import io

        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=list(item_rows[0].keys()))
        writer.writeheader()
        writer.writerows(item_rows)
        csv_lines.append(buffer.getvalue())
    write_atomic(output_csv, "\ufeff" + "".join(csv_lines))

    total = len(item_rows)
    online = sum(row["online_only_verified"] == "yes" for row in item_rows)
    sources_absent = sum(row["source_absent"] == "yes" for row in item_rows)
    destinations_present = sum(row["destination_exists"] == "yes" for row in item_rows)
    summary = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": total,
        "destinations_present": destinations_present,
        "sources_absent": sources_absent,
        "online_only_verified": online,
        "logical_gib": round(sum(int(row["logical_bytes"]) for row in item_rows) / 1024**3, 3),
        "local_allocated_gib": round(
            sum(int(row["local_allocated_bytes"]) for row in item_rows) / 1024**3, 3
        ),
        "complete": total > 0 and online == total and sources_absent == total and destinations_present == total,
    }
    write_atomic(output_json, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-status", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--timeout", type=int, default=43200)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    with args.archive_status.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    started = time.monotonic()
    while True:
        current = snapshot(rows, args.output_json, args.output_csv)
        if current["complete"]:
            return 0
        if args.once:
            return 2
        if time.monotonic() - started >= args.timeout:
            return 3
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
