#!/usr/bin/env python3
"""Move manifest-listed source/XML pairs into OneDrive and request online-only state."""

from __future__ import annotations

import argparse
import csv
import ctypes
import shutil
import subprocess
from pathlib import Path


FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_UNPINNED = 0x00100000
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def windows_attributes(path: Path) -> int:
    value = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    if value == INVALID_FILE_ATTRIBUTES:
        raise OSError(f"GetFileAttributesW failed: {path}")
    return int(value)


def state(path: Path) -> str:
    value = windows_attributes(path)
    flags: list[str] = []
    if value & FILE_ATTRIBUTE_OFFLINE:
        flags.append("offline")
    if value & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
        flags.append("recall-on-access")
    if value & FILE_ATTRIBUTE_PINNED:
        flags.append("pinned")
    if value & FILE_ATTRIBUTE_UNPINNED:
        flags.append("unpinned")
    return "+".join(flags) or "local"


def is_online_only(path: Path) -> bool:
    value = windows_attributes(path)
    return bool(value & (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--allowed-source-root", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    with args.manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        manifest_rows = list(csv.DictReader(handle))
    if not manifest_rows:
        raise SystemExit("empty manifest")

    planned: list[dict[str, object]] = []
    seen_sources: set[Path] = set()
    seen_destinations: set[Path] = set()
    for row in manifest_rows:
        if row.get("status") != "closed":
            raise SystemExit(f"source is not closed: {row.get('source_id')}")
        creator = (row.get("creator") or "unknown").strip()
        for kind, column in (("video", "source_video"), ("xml", "xml")):
            source = Path(row[column]).resolve()
            if not within(source, args.allowed_source_root):
                raise SystemExit(f"source outside allowed root: {source}")
            destination = (args.archive_root / args.batch / creator / source.name).resolve()
            if source in seen_sources or destination in seen_destinations:
                raise SystemExit(f"duplicate source or destination: {source}")
            seen_sources.add(source)
            seen_destinations.add(destination)
            if not source.is_file():
                raise SystemExit(f"source missing: {source}")
            if destination.exists():
                raise SystemExit(f"destination already exists: {destination}")
            planned.append(
                {
                    "source_id": row["source_id"],
                    "creator": creator,
                    "kind": kind,
                    "source": source,
                    "destination": destination,
                    "bytes": source.stat().st_size,
                }
            )

    output_rows: list[dict[str, object]] = []
    for item in planned:
        source = Path(item["source"])
        destination = Path(item["destination"])
        if args.execute:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            if source.exists() or not destination.is_file():
                raise SystemExit(f"move verification failed: {source}")
            if destination.stat().st_size != item["bytes"]:
                raise SystemExit(f"size verification failed: {destination}")
            subprocess.run(["attrib", "+U", "-P", str(destination)], check=True)
            current_state = state(destination)
            online_only = is_online_only(destination)
        else:
            current_state = "planned"
            online_only = False
        output_rows.append(
            {
                **{key: item[key] for key in ("source_id", "creator", "kind", "bytes")},
                "source": str(source),
                "destination": str(destination),
                "move_verified": "yes" if args.execute else "planned",
                "cloud_state": current_state,
                "online_only_verified": "yes" if online_only else "no",
            }
        )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0].keys()))
        writer.writeheader()
        writer.writerows(output_rows)

    total_gib = sum(int(row["bytes"]) for row in output_rows) / (1024**3)
    online_count = sum(row["online_only_verified"] == "yes" for row in output_rows)
    mode = "executed" if args.execute else "planned"
    print(f"{mode} files={len(output_rows)} total_gib={total_gib:.2f} online_only={online_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
