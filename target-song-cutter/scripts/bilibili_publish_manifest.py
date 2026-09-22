#!/usr/bin/env python3
"""Dry-run or explicitly execute a resumable Bilibili publishing manifest."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFIRMATION = "I_HAVE_REVIEWED_THIS_BATCH"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def preflight(item: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if item.get("state") == "success" and item.get("bvid"):
        return errors
    if item.get("state") != "validated":
        errors.append("item is not validated")
    if not Path(item.get("video", "")).is_file():
        errors.append("video missing")
    if not Path(item.get("cover", "")).is_file():
        errors.append("cover missing")
    if not item.get("title") or len(item["title"]) > 80:
        errors.append("invalid title")
    if "'" in item.get("title", ""):
        errors.append("title contains an English single quote")
    if not isinstance(item.get("tid"), int) or item["tid"] <= 0:
        errors.append("tid is not approved")
    if not item.get("tags"):
        errors.append("tags missing")
    return errors


def load_credential(path: Path):
    from bilibili_api import Credential

    data = json.loads(path.read_text(encoding="utf-8"))
    source = data.get("bilibili", data)
    if not source.get("sessdata") or not source.get("bili_jct") or not source.get("buvid3"):
        raise ValueError("credentials require sessdata, bili_jct, and buvid3")
    kwargs = {
        "sessdata": source["sessdata"],
        "bili_jct": source["bili_jct"],
        "buvid3": source["buvid3"],
        "dedeuserid": source.get("dedeuserid", ""),
    }
    if source.get("ac_time_value"):
        kwargs["ac_time_value"] = source["ac_time_value"]
    return Credential(**kwargs)


async def upload_one(item: dict[str, Any], credential) -> dict[str, Any]:
    from bilibili_api.video_uploader import VideoMeta, VideoUploader, VideoUploaderPage
    from bilibili_api.utils import network

    network.select_client("httpx")
    page = VideoUploaderPage(path=item["video"], title=item["title"], description="")
    meta = VideoMeta(
        tid=item["tid"],
        title=item["title"],
        desc=item.get("description", ""),
        cover=item["cover"],
        tags=item.get("tags", [])[:10],
        original=item.get("copyright", 1) == 1,
        no_reprint=bool(item.get("no_reprint", False)),
    )
    uploader = VideoUploader(
        pages=[page],
        meta=meta,
        credential=credential,
        cover=item["cover"],
    )
    return await uploader.start()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--credentials")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--cooldown", type=int, default=300)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    only = set(args.only or [])
    selected = [item for item in manifest["items"] if not only or item["clip_id"] in only]
    pending = [item for item in selected if not (item.get("state") == "success" and item.get("bvid"))]
    failures = {item["clip_id"]: preflight(item) for item in pending}
    failures = {clip_id: errors for clip_id, errors in failures.items() if errors}

    if not args.execute:
        print(json.dumps({"mode": "dry-run", "selected": len(selected), "pending": len(pending), "blocked": failures}, ensure_ascii=False, indent=2))
        return 1 if failures else 0

    if args.confirm != CONFIRMATION:
        raise SystemExit(f"execution blocked: pass --confirm {CONFIRMATION}")
    if not args.credentials:
        raise SystemExit("execution blocked: --credentials is required")
    if args.batch_size < 1 or args.batch_size > 5:
        raise SystemExit("execution blocked: batch-size must be 1-5")
    if args.cooldown < 300:
        raise SystemExit("execution blocked: cooldown must be at least 300 seconds")
    if failures:
        print(json.dumps({"execution": "blocked", "items": failures}, ensure_ascii=False, indent=2))
        return 1

    credential = load_credential(Path(args.credentials).resolve())
    for index, item in enumerate(pending):
        item["state"] = "uploading"
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["updated_at"] = now_iso()
        manifest["updated_at"] = now_iso()
        atomic_json(manifest_path, manifest)
        try:
            result = asyncio.run(upload_one(item, credential))
            bvid = result.get("bvid", "") if isinstance(result, dict) else ""
            aid = result.get("aid", "") if isinstance(result, dict) else ""
            if not bvid:
                raise RuntimeError("upload returned no bvid")
            item["state"] = "success"
            item["bvid"] = bvid
            item["aid"] = aid
            item["last_error"] = ""
        except Exception as exc:
            item["state"] = "failed"
            item["last_error"] = str(exc)
        item["updated_at"] = now_iso()
        manifest["updated_at"] = now_iso()
        atomic_json(manifest_path, manifest)
        if (index + 1) % args.batch_size == 0 and index + 1 < len(pending):
            time.sleep(args.cooldown)
    return 0 if all(item.get("state") == "success" for item in pending) else 1


if __name__ == "__main__":
    raise SystemExit(main())

