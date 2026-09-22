#!/usr/bin/env python3
"""Refresh Bilibili gift names and remove gift acknowledgements from transcripts."""

from __future__ import annotations

import argparse
import csv
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from virtuareal_glossary import contains_paid_message


DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / "references" / "bilibili-live-gifts.json"
GIFT_ENDPOINT = "https://api.live.bilibili.com/xlive/web-room/v1/giftPanel/roomGiftList"

THANKS_RE = re.compile(r"(?:谢谢|感谢|多谢|感恩|thank\s*(?:you|s)?)", re.I)
PERSONAL_GRATITUDE_RE = re.compile(
    r"(?:谢谢|感谢)(?:你|大家)(?:愿意|陪|支持|理解|相信|喜欢|听|来看|一直|这么|那么)",
    re.I,
)
PAID_MESSAGE_RE = re.compile(
    r"(?:Super\s*Chat|(?<![A-Za-z0-9])SC(?![A-Za-z0-9])|醒目留言|钢镚)",
    re.I,
)
THANKS_ONLY_RE = re.compile(
    r"^(?:嗯|啊|好|然后|还有|以及|也)?\s*(?:谢谢|感谢|多谢|感恩)"
    r"(?:[\w\-\u3400-\u9fff·]{1,24})?(?:宝宝|老师|老板|哥|姐)?[呀啊哦呢～~！!。,.，]*$",
    re.I,
)
COUNT_RE = re.compile(
    r"(?:\d+|[一二三四五六七八九十百千万两]+)"
    r"(?:个|份|组|发|颗|只|张|艘|座|次|套|枚|盏|朵|箱|抽)?"
)
GIFT_CONTEXT_RE = re.compile(
    r"(?:礼物|盲盒|灯牌|打\s*call|SC|醒目留言|舰长|提督|总督|上舰|续舰|大航海)",
    re.I,
)


@dataclass(frozen=True)
class TimedText:
    start: float
    end: float
    text: str
    row: dict[str, str]


@dataclass(frozen=True)
class GiftMatch:
    explicit: bool
    thanks_only: bool
    paid_message: bool
    gifts: tuple[str, ...]
    reason: str


def normalize(value: str) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:～~]+", "", value).casefold()


def load_gift_names(path: Path = DEFAULT_CATALOG) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    names = [*data.get("names", []), *data.get("legacy_or_observed_names", [])]
    return {str(name).strip() for name in names if str(name).strip()}


def _gift_objects(value) -> Iterable[dict]:
    if isinstance(value, dict):
        name = value.get("gift_name") or value.get("name")
        if name and any(key in value for key in ("id", "gift_id", "price", "coin_type")):
            yield value
        for child in value.values():
            yield from _gift_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _gift_objects(child)


def fetch_gift_names(room_id: int, timeout: float = 20.0) -> set[str]:
    query = urllib.parse.urlencode({"platform": "pc", "room_id": room_id})
    request = urllib.request.Request(
        f"{GIFT_ENDPOINT}?{query}",
        headers={"User-Agent": "Mozilla/5.0 target-song-cutter/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if payload.get("code") != 0:
        raise RuntimeError(f"Bilibili gift API returned {payload.get('code')}: {payload.get('message')}")
    return {
        str(item.get("gift_name") or item.get("name")).strip()
        for item in _gift_objects(payload.get("data"))
        if item.get("gift_name") or item.get("name")
    }


def write_catalog(path: Path, room_id: int, names: Sequence[str], existing: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": {
            "provider": "Bilibili Live",
            "room_id": room_id,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "endpoint": f"{GIFT_ENDPOINT}?platform=pc&room_id={room_id}",
        },
        "names": sorted(set(names)),
        "legacy_or_observed_names": sorted(existing - set(names)),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def detect_gift_ack(text: str, gift_names: set[str]) -> GiftMatch:
    compact = normalize(text)
    matches = tuple(sorted(
        (name for name in gift_names if normalize(name) in compact),
        key=len,
        reverse=True,
    ))
    has_thanks = bool(THANKS_RE.search(text))
    has_count = bool(COUNT_RE.search(text))
    has_context = bool(GIFT_CONTEXT_RE.search(text))
    paid_message = contains_paid_message(text)
    thanks_only = bool(THANKS_ONLY_RE.match(text.strip())) and not bool(
        PERSONAL_GRATITUDE_RE.search(text)
    )
    # Paid messages require editorial review: retain a relevant SC/钢镚 body but
    # start the edit after its thank-you preamble.  Do not discard the whole row
    # automatically, because ASR often places preamble and body in one segment.
    explicit = (
        has_thanks
        and bool(matches or has_context or (has_count and "的" in text) or thanks_only)
        and not paid_message
    )
    reasons = []
    if matches:
        reasons.append("gift=" + "|".join(matches[:3]))
    if has_count:
        reasons.append("count")
    if has_context:
        reasons.append("gift-context")
    if thanks_only:
        reasons.append("thanks-only")
    if paid_message:
        reasons.append("paid-message-review")
    return GiftMatch(
        explicit,
        thanks_only,
        paid_message,
        matches,
        ";".join(reasons) or "none",
    )


def transcript_times(row: dict[str, str]) -> tuple[float, float]:
    if row.get("start_seconds") not in (None, ""):
        return float(row["start_seconds"]), float(row["end_seconds"])
    return float(row["start_ms"]) / 1000.0, float(row["end_ms"]) / 1000.0


def mark_gift_blocks(
    rows: Sequence[dict[str, str]], gift_names: set[str], block_gap: float = 12.0
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    timed = [TimedText(*transcript_times(row), row.get("text", "").strip(), row) for row in rows]
    matches = [detect_gift_ack(item.text, gift_names) for item in timed]
    removed = {index for index, match in enumerate(matches) if match.explicit}

    # Extend an explicit gift-reading run to short adjacent "谢谢宝宝/老师" rows.
    changed = True
    while changed:
        changed = False
        for index, (item, match) in enumerate(zip(timed, matches)):
            if index in removed or not match.thanks_only:
                continue
            neighbours = []
            if index > 0 and index - 1 in removed:
                neighbours.append(item.start - timed[index - 1].end)
            if index + 1 < len(timed) and index + 1 in removed:
                neighbours.append(timed[index + 1].start - item.end)
            if neighbours and min(neighbours) <= block_gap:
                removed.add(index)
                changed = True

    kept_rows: list[dict[str, str]] = []
    removed_rows: list[dict[str, str]] = []
    for index, item in enumerate(timed):
        if index not in removed:
            kept_rows.append(item.row)
            continue
        match = matches[index]
        removed_rows.append({
            "start_seconds": f"{item.start:.3f}",
            "end_seconds": f"{item.end:.3f}",
            "text": item.text,
            "matched_gifts": "|".join(match.gifts),
            "reason": match.reason if match.explicit else "adjacent-thanks",
        })
    return kept_rows, removed_rows


def write_rows(path: Path, rows: Sequence[dict[str, str]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def filter_transcript(args) -> int:
    with args.transcript.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    gift_names = load_gift_names(args.catalog)
    kept, removed = mark_gift_blocks(rows, gift_names, args.block_gap)
    write_rows(args.output, kept, fieldnames)
    write_rows(
        args.removed_output,
        removed,
        ("start_seconds", "end_seconds", "text", "matched_gifts", "reason"),
    )
    print(f"Done: kept {len(kept)} rows; removed {len(removed)} gift acknowledgement rows")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    update = sub.add_parser("update", help="Refresh gift names from a Bilibili live room")
    update.add_argument("--room-id", type=int, required=True)
    update.add_argument("--output", type=Path, default=DEFAULT_CATALOG)
    update.add_argument("--timeout", type=float, default=20.0)
    clean = sub.add_parser("filter", help="Remove gift acknowledgement rows from a transcript CSV")
    clean.add_argument("--transcript", type=Path, required=True)
    clean.add_argument("--output", type=Path, required=True)
    clean.add_argument("--removed-output", type=Path, required=True)
    clean.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    clean.add_argument("--block-gap", type=float, default=12.0)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "filter":
        return filter_transcript(args)
    existing = load_gift_names(args.output) if args.output.is_file() else set()
    names = fetch_gift_names(args.room_id, args.timeout)
    write_catalog(args.output, args.room_id, sorted(names), existing)
    print(f"Done: wrote {len(names)} current gift names to {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
