#!/usr/bin/env python3
"""Read-only exact-title lookup for recent Bilibili submissions."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import biliup_publish as publish


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title")
    parser.add_argument("--hours", type=float, default=2.0)
    args = parser.parse_args()
    not_before = int(time.time() - args.hours * 3600)
    try:
        bvid = publish.find_recent_bvid_by_title(
            publish.default_collection_cookie_path(), args.title, not_before
        )
    except RuntimeError as exc:
        print(f"NOT_FOUND\t{exc}")
        return 2
    print(f"FOUND\t{bvid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
