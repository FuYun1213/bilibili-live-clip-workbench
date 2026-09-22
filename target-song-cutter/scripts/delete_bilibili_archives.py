#!/usr/bin/env python3
"""Safely preview or delete exact Bilibili archives by BV id and title.

Deletion is fail-closed: every item must resolve through the authenticated
creator API, match the expected title, and appear in the acting account's
archive list before the first destructive request is sent.  After each delete,
the archive list is read back and must no longer contain the exact aid.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONFIRMATION = "DELETE_EXACT_VALIDATED_ARCHIVES"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_credentials(path: Path) -> tuple[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    cookies = payload.get("cookie_info", {}).get("cookies", [])
    values = {
        str(item["name"]): str(item["value"])
        for item in cookies
        if item.get("name") and item.get("value")
    }
    if not values.get("SESSDATA") or not values.get("bili_jct"):
        raise ValueError("web cookie file is incomplete")
    return "; ".join(f"{name}={value}" for name, value in values.items()), values["bili_jct"]


def request_json(
    url: str,
    cookie_header: str,
    form: dict[str, Any] | None = None,
    allow_api_error: bool = False,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "Cookie": cookie_header,
        "Origin": "https://member.bilibili.com",
        "Referer": "https://member.bilibili.com/platform/upload-manager/article",
        "User-Agent": "Mozilla/5.0",
    }
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Bilibili request failed: {exc}") from exc
    if result.get("code") != 0 and not allow_api_error:
        raise RuntimeError(
            f"Bilibili API rejected request: code={result.get('code')} "
            f"message={result.get('message', '')}"
        )
    return result


def archive_info(bvid: str, cookie_header: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"bvid": bvid})
    result = request_json(
        f"https://member.bilibili.com/x/vupre/web/archive/view?{query}",
        cookie_header,
    )
    archive = result.get("data", {}).get("archive") or {}
    if not archive.get("aid") or not archive.get("title"):
        raise RuntimeError(f"Authenticated archive metadata incomplete for {bvid}")
    return {
        "aid": int(archive["aid"]),
        "bvid": str(archive.get("bvid") or bvid),
        "title": str(archive["title"]),
        "is_owner": int((archive.get("attrs") or {}).get("is_owner") or 0),
    }


def recent_archives(cookie_header: str, max_pages: int = 3, request_delay: float = 1.0) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    # The creator endpoint currently caps responses at ten rows even when a
    # larger page size is requested, so paginate explicitly.
    for page in range(1, max_pages + 1):
        query = urllib.parse.urlencode(
            {
                "status": "pubed,pubing,not_pubed",
                "pn": page,
                "ps": 10,
                "coop": 1,
                "interactive": 1,
            }
        )
        data = request_json(
            f"https://member.bilibili.com/x/web/archives?{query}",
            cookie_header,
        ).get("data", {})
        page_rows: list[dict[str, Any]] = []
        for key in ("arc_audits", "archives"):
            for item in data.get(key) or []:
                archive = item.get("Archive", item)
                aid = int(archive.get("aid") or 0)
                if aid and aid not in seen:
                    seen.add(aid)
                    page_rows.append(archive)
        rows.extend(page_rows)
        if page < max_pages:
            time.sleep(request_delay)
        if len(page_rows) < 10:
            break
    return rows


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest.get("items") or []
    if not items:
        raise ValueError("Deletion manifest is empty")
    cookie_header, csrf = load_credentials(args.credentials.resolve())
    account_rows = recent_archives(cookie_header)
    account_by_bvid = {str(row.get("bvid") or ""): row for row in account_rows}

    resolved: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in items:
        if item.get("state") == "deleted":
            continue
        bvid = str(item["bvid"])
        listed = account_by_bvid.get(bvid)
        if not listed:
            raise RuntimeError(f"Exact archive is not present in acting account list: {bvid}")
        title = str(listed.get("title") or "")
        if title != item["title"]:
            raise RuntimeError(
                f"Title mismatch for {bvid}: {title!r} != {item['title']!r}"
            )
        aid = int(listed.get("aid") or 0)
        if aid <= 0:
            raise RuntimeError(f"Authenticated archive list omitted aid for {bvid}")
        if item.get("aid") and int(item["aid"]) != aid:
            raise RuntimeError(f"Stored aid changed for {bvid}")
        item["aid"] = aid
        item["state"] = "validated"
        item["validated_at"] = now_iso()
        info = {"aid": aid, "bvid": bvid, "title": title, "is_owner": 1}
        resolved.append((item, info))

    manifest["updated_at"] = now_iso()
    atomic_json(manifest_path, manifest)
    preview = [
        {"bvid": item["bvid"], "aid": info["aid"], "title": info["title"]}
        for item, info in resolved
    ]
    if not args.execute:
        print(json.dumps({"mode": "dry-run", "count": len(preview), "items": preview}, ensure_ascii=False, indent=2))
        return 0
    if args.confirm != CONFIRMATION:
        raise SystemExit(f"execution blocked: pass --confirm {CONFIRMATION}")

    # All rows were validated before the first destructive request.
    for item, info in resolved:
        result = request_json(
            "https://member.bilibili.com/x/web/archive/delete",
            cookie_header,
            {"aid": info["aid"], "csrf": csrf},
        )
        removed = False
        for attempt in range(6):
            listed_aids = {
                int(row.get("aid") or 0)
                for row in recent_archives(cookie_header, max_pages=2, request_delay=1.0)
            }
            if info["aid"] not in listed_aids:
                removed = True
                break
            if attempt < 5:
                time.sleep(2)
        if not removed:
            raise RuntimeError(f"Deletion read-back failed for {item['bvid']}")
        item["state"] = "deleted"
        item["deleted_at"] = now_iso()
        item["api_result"] = {"code": result.get("code"), "message": result.get("message", "")}
        manifest["updated_at"] = now_iso()
        atomic_json(manifest_path, manifest)
        print(f"DELETED {item['bvid']} {item['title']}", flush=True)
        time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
