#!/usr/bin/env python3
"""Search recent Bilibili creator clips and save auditable cover benchmarks."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_NAV = "https://api.bilibili.com/x/web-interface/nav"
API_SEARCH = "https://api.bilibili.com/x/web-interface/wbi/search/type"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://search.bilibili.com/",
}
MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)
CREATORS = {
    "sumire": {"keyword": "枝堇Sumire", "title_pattern": r"枝堇"},
    "kioi": {"keyword": "柚雨Kioi", "title_pattern": r"柚雨"},
    "viridis": {"keyword": "小松绿Viridis", "title_pattern": r"小松绿|松绿"},
    "yuchu": {"keyword": "羽啾chu2u", "title_pattern": r"羽啾"},
    "chilly": {"keyword": "昼夜_Chilly", "title_pattern": r"昼夜[_ ]?Chilly"},
}
FIELDS = (
    "creator", "rank", "search_order", "bvid", "title", "author", "play", "views_per_day",
    "pubdate_utc", "duration", "content_type_hint", "pic", "tags",
    "description", "search_keyword", "fetched_at_utc",
)


def request_bytes(url: str, retries: int = 3) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except Exception as exc:  # network errors should remain visible after retries
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def request_json(url: str) -> dict[str, Any]:
    return json.loads(request_bytes(url).decode("utf-8"))


def wbi_mixin_key() -> str:
    response = request_json(API_NAV)
    data = response.get("data") or {}
    wbi = data.get("wbi_img") or {}
    keys = []
    for field in ("img_url", "sub_url"):
        path = urllib.parse.urlparse(str(wbi.get(field, ""))).path
        keys.append(Path(path).stem)
    raw = "".join(keys)
    if len(raw) < 64:
        raise RuntimeError("Bilibili WBI keys are missing")
    return "".join(raw[index] for index in MIXIN_KEY_ENC_TAB)[:32]


def signed_query(params: dict[str, Any], mixin_key: str) -> str:
    sanitized = {
        key: re.sub(r"[!'()*]", "", str(value))
        for key, value in params.items()
    }
    query = urllib.parse.urlencode(sorted(sanitized.items()))
    signature = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return f"{query}&w_rid={signature}"


def clean_text(value: Any) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()


def content_type_hint(title: str) -> str:
    if re.search(r"直播回放|录播", title, re.IGNORECASE):
        return "replay"
    if re.search(r"歌切|翻唱|演唱|清唱|唱《|【[^】]*歌】|\bcover\b", title, re.IGNORECASE):
        return "song"
    return "narrative"


def search_page(
    keyword: str,
    page: int,
    page_size: int,
    mixin_key: str,
    order: str,
) -> list[dict[str, Any]]:
    params = {
        "keyword": keyword,
        "order": order,
        "page": page,
        "page_size": page_size,
        "search_type": "video",
        "wts": int(time.time()),
    }
    response = request_json(f"{API_SEARCH}?{signed_query(params, mixin_key)}")
    if int(response.get("code", -1)) != 0:
        raise RuntimeError(f"Bilibili search failed: {response.get('code')} {response.get('message', '')}")
    return list((response.get("data") or {}).get("result") or [])


def normalize_row(
    creator: str,
    config: dict[str, str],
    raw: dict[str, Any],
    fetched_at: datetime,
    order: str = "totalrank",
) -> dict[str, Any] | None:
    title = clean_text(raw.get("title"))
    if not re.search(config["title_pattern"], title, re.IGNORECASE):
        return None
    pubdate = datetime.fromtimestamp(int(raw.get("pubdate") or 0), tz=timezone.utc)
    age_days = max((fetched_at - pubdate).total_seconds() / 86400.0, 0.25)
    play = int(raw.get("play") or 0)
    pic = str(raw.get("pic") or "")
    if pic.startswith("//"):
        pic = "https:" + pic
    return {
        "creator": creator,
        "rank": 0,
        "search_order": order,
        "bvid": str(raw.get("bvid") or ""),
        "title": title,
        "author": clean_text(raw.get("author")),
        "play": play,
        "views_per_day": round(play / age_days, 2),
        "pubdate_utc": pubdate.isoformat(timespec="seconds"),
        "duration": clean_text(raw.get("duration")),
        "content_type_hint": content_type_hint(title),
        "pic": pic,
        "tags": clean_text(raw.get("tag")),
        "description": clean_text(raw.get("description")),
        "search_keyword": config["keyword"],
        "fetched_at_utc": fetched_at.isoformat(timespec="seconds"),
    }


def collect(
    creators: list[str],
    pages: int,
    page_size: int,
    top: int,
    *,
    order: str,
    recent_days: int,
) -> list[dict[str, Any]]:
    mixin_key = wbi_mixin_key()
    fetched_at = datetime.now(timezone.utc)
    output: list[dict[str, Any]] = []
    for creator in creators:
        config = CREATORS[creator]
        raw_rows: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            raw_rows.extend(
                search_page(config["keyword"], page, page_size, mixin_key, order)
            )
        unique: dict[str, dict[str, Any]] = {}
        for raw in raw_rows:
            row = normalize_row(creator, config, raw, fetched_at, order)
            if row and recent_days:
                published = datetime.fromisoformat(str(row["pubdate_utc"]))
                age_days = (fetched_at - published).total_seconds() / 86400.0
                if age_days > recent_days:
                    row = None
            if row and row["bvid"]:
                previous = unique.get(row["bvid"])
                if previous is None or row["play"] > previous["play"]:
                    unique[row["bvid"]] = row
        ranked = list(unique.values())
        if order == "click":
            ranked.sort(key=lambda row: row["play"], reverse=True)
        ranked = ranked[:top]
        for rank, row in enumerate(ranked, 1):
            row["rank"] = rank
            output.append(row)
        print(f"{creator}\t{len(ranked)}")
    return output


def download_covers(rows: list[dict[str, Any]], output: Path, cover_count: int) -> None:
    if cover_count <= 0:
        return
    for row in rows:
        if int(row["rank"]) > cover_count or not row["pic"]:
            continue
        folder = output / "covers" / str(row["creator"])
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{int(row['rank']):02d}_{row['bvid']}.jpg"
        destination.write_bytes(request_bytes(str(row["pic"])))


def write_outputs(rows: list[dict[str, Any]], output: Path, args: argparse.Namespace) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "bilibili-search-results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (output / "bilibili-search-results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    metadata = {
        "schema_version": 1,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "creators": args.creator,
        "pages": args.pages,
        "page_size": args.page_size,
        "top": args.top,
        "cover_count": args.cover_count,
        "order": args.order,
        "recent_days": args.recent_days,
        "warning": "Views and ranking are time-sensitive. Treat title and cover patterns as evidence, not templates to copy.",
    }
    (output / "benchmark-metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    download_covers(rows, output, args.cover_count)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--creator", action="append", choices=sorted(CREATORS), help="Repeat to select creators; defaults to all five")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--cover-count", type=int, default=12)
    parser.add_argument(
        "--order",
        choices=("totalrank", "click", "pubdate"),
        default="totalrank",
        help="Bilibili search order; totalrank is the 综合 tab",
    )
    parser.add_argument(
        "--recent-days",
        type=int,
        default=60,
        help="Keep clips published within this many days; 0 disables the filter",
    )
    args = parser.parse_args()
    if (
        args.pages < 1
        or not 1 <= args.page_size <= 50
        or args.top < 1
        or args.cover_count < 0
        or args.recent_days < 0
    ):
        parser.error(
            "pages/top must be positive, page-size must be 1-50, and "
            "cover-count/recent-days cannot be negative"
        )
    args.creator = args.creator or list(CREATORS)
    rows = collect(
        args.creator,
        args.pages,
        args.page_size,
        args.top,
        order=args.order,
        recent_days=args.recent_days,
    )
    write_outputs(rows, args.output.resolve(), args)
    print(f"total\t{len(rows)}\t{args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
