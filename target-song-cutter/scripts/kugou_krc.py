#!/usr/bin/env python3
"""Fetch and decode public KuGou KRC word-timed lyrics.

The script never downloads song audio.  It searches KuGou's public catalogue,
selects a lyric candidate, decodes the KRC payload, and writes an inspection
JSON plus a CSV whose ``timed_units`` column can be consumed by the native ASS
renderer.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import re
import urllib.parse
import urllib.request
import zlib
from pathlib import Path


KRC_KEY = bytes(
    [0x40, 0x47, 0x61, 0x77, 0x5E, 0x32, 0x74, 0x47,
     0x51, 0x36, 0x31, 0x2D, 0xCE, 0xD2, 0x6E, 0x69]
)
LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
UNIT_RE = re.compile(r"<(\d+),(\d+),\d+>([^<]*)")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def get_json(url: str, params: dict[str, object]) -> dict:
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value not in (None, "")})
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": USER_AGENT, "Referer": "https://kugou.com/"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def search_song(keyword: str) -> list[dict]:
    payload = get_json(
        "http://mobilecdn.kugou.com/api/v3/search/song",
        {"format": "json", "keyword": keyword, "page": 1, "pagesize": 30, "showtype": 1},
    )
    return payload.get("data", {}).get("info", [])


def choose_song(rows: list[dict], title: str, singer: str | None, duration: float | None) -> dict:
    if not rows:
        raise RuntimeError("KuGou song search returned no candidates")
    target_title = title.casefold().replace(" ", "")
    target_singer = (singer or "").casefold().replace(" ", "")

    def score(row: dict) -> tuple[float, float]:
        name = str(row.get("songname_original") or row.get("songname") or "").casefold().replace(" ", "")
        artist = str(row.get("singername") or "").casefold().replace(" ", "")
        title_score = 3.0 if name == target_title else (1.5 if target_title in name or name in target_title else 0.0)
        singer_score = 2.0 if target_singer and target_singer in artist else 0.0
        row_duration = float(row.get("duration") or 0)
        duration_error = abs(row_duration - duration) if duration else 0.0
        return title_score + singer_score - min(duration_error / 60.0, 2.0), -duration_error

    return max(rows, key=score)


def fetch_krc(song: dict, keyword: str) -> tuple[dict, str]:
    duration_ms = round(float(song.get("duration") or 0) * 1000)
    lyric_search = get_json(
        "https://lyrics.kugou.com/search",
        {
            "ver": 1,
            "man": "no",
            "client": "pc",
            "keyword": keyword,
            "duration": duration_ms,
            "hash": song.get("hash"),
            "album_audio_id": song.get("album_audio_id") or song.get("audio_id"),
            "lrctxt": 1,
        },
    )
    candidates = lyric_search.get("candidates") or []
    if not candidates:
        raise RuntimeError("KuGou lyric search returned no candidates")
    candidate = max(candidates, key=lambda item: float(item.get("score") or 0))
    payload = get_json(
        "https://lyrics.kugou.com/download",
        {
            "ver": 1,
            "client": "pc",
            "id": candidate["id"],
            "accesskey": candidate["accesskey"],
            "fmt": "krc",
            "charset": "utf8",
        },
    )
    encoded = payload.get("content")
    if not encoded:
        raise RuntimeError("KuGou lyric download returned no content")
    raw = base64.b64decode(encoded)
    if raw.startswith(b"krc1"):
        encrypted = raw[4:]
        inflated = bytes(value ^ KRC_KEY[index % len(KRC_KEY)] for index, value in enumerate(encrypted))
        text = zlib.decompress(inflated).decode("utf-8", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    return candidate, text


def parse_krc(text: str) -> list[dict]:
    rows: list[dict] = []
    for raw_line in text.splitlines():
        line = LINE_RE.match(raw_line.strip())
        if not line:
            continue
        start_ms, duration_ms, body = int(line.group(1)), int(line.group(2)), line.group(3)
        units: list[dict] = []
        plain: list[str] = []
        for unit in UNIT_RE.finditer(body):
            offset_ms, unit_duration_ms, unit_text = int(unit.group(1)), int(unit.group(2)), unit.group(3)
            if not unit_text:
                continue
            plain.append(unit_text)
            units.append(
                {
                    "start_seconds": (start_ms + offset_ms) / 1000.0,
                    "end_seconds": (start_ms + offset_ms + unit_duration_ms) / 1000.0,
                    "text": unit_text,
                }
            )
        lyric = "".join(plain).strip()
        if lyric and units:
            rows.append(
                {
                    "start_seconds": start_ms / 1000.0,
                    "end_seconds": (start_ms + duration_ms) / 1000.0,
                    "text": lyric,
                    "timed_units": units,
                }
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--singer")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--hash", help="Force a specific KuGou song hash after catalogue search.")
    parser.add_argument("--clip", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    keyword = " ".join(part for part in (args.title, args.singer) if part)
    search_rows = search_song(keyword)
    if args.hash:
        song = next((row for row in search_rows if str(row.get("hash", "")).casefold() == args.hash.casefold()), None)
        if song is None:
            raise RuntimeError(f"Requested KuGou hash was not present in search results: {args.hash}")
    else:
        song = choose_song(search_rows, args.title, args.singer, args.duration)
    lyric_candidate, krc_text = fetch_krc(song, keyword)
    rows = parse_krc(krc_text)
    if not rows:
        raise RuntimeError("Decoded KRC contained no timed lyric rows")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w\-\u3040-\u30ff\u3400-\u9fff]+", "_", args.title).strip("_")
    inspection = args.output_dir / f"{stem}.json"
    inspection.write_text(
        json.dumps({"song": song, "lyric_candidate": lyric_candidate, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )
    destination = args.output_dir / f"{stem}.csv"
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["clip", "start_seconds", "end_seconds", "text", "corrected_text", "timed_units", "reviewed"])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "clip": args.clip,
                    "start_seconds": f"{row['start_seconds']:.3f}",
                    "end_seconds": f"{row['end_seconds']:.3f}",
                    "text": row["text"],
                    "corrected_text": row["text"],
                    "timed_units": json.dumps(row["timed_units"], ensure_ascii=False, separators=(",", ":")),
                    "reviewed": "yes",
                }
            )
    print(json.dumps({"clip": args.clip, "song": song.get("filename") or song.get("songname"), "rows": len(rows), "csv": str(destination)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
