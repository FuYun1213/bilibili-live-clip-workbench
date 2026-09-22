#!/usr/bin/env python3
"""Import an emotion-folder emote library into the cover asset catalog."""

from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
from pathlib import Path

from PIL import Image


SKILL_ROOT = Path(__file__).resolve().parents[1]
CATALOG_FIELDS = (
    "label",
    "category",
    "source",
    "url",
    "local_path",
    "sha256",
    "overlay_style",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def overlay_style(path: Path) -> str:
    """Transparent art is a sticker; opaque/semi-opaque art is a rounded tile."""
    with Image.open(path) as source:
        alpha = source.convert("RGBA").getchannel("A")
        minimum, _maximum = alpha.getextrema()
    return "sticker" if minimum <= 32 else "tile"


def source_metadata(path: Path) -> dict[str, dict[str, str]]:
    catalog = path / "catalog.csv"
    if not catalog.is_file():
        return {}
    with catalog.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            Path(str(row.get("local_path") or "")).name: dict(row)
            for row in csv.DictReader(handle)
        }


def organize(source: Path, destination: Path) -> list[dict[str, str]]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    metadata = source_metadata(source)
    rows: list[dict[str, str]] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in {".png", ".webp", ".jpg", ".jpeg"}:
            continue
        relative = path.relative_to(source)
        if len(relative.parts) < 2:
            continue
        category = relative.parts[0]
        target = destination / category / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        old = metadata.get(path.name, {})
        label = str(old.get("label") or path.stem.split("_", 2)[-1]).strip()
        rows.append({
            "label": label,
            "category": category,
            "source": str(old.get("source") or "local"),
            "url": str(old.get("url") or ""),
            "local_path": target.relative_to(SKILL_ROOT).as_posix(),
            "sha256": sha256(target),
            "overlay_style": overlay_style(target),
        })
    destination.mkdir(parents=True, exist_ok=True)
    catalog = destination / "catalog.csv"
    with catalog.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    rows = organize(args.source, args.destination)
    print(f"organized={len(rows)} catalog={args.destination / 'catalog.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
