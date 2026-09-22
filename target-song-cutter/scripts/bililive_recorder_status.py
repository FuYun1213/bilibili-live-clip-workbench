#!/usr/bin/env python3
"""Read mikufans BililiveRecorder room states without touching its secrets."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from windows_process import hidden_subprocess_kwargs


STATUS_SCRIPT = Path(__file__).with_name("bililive_recorder_status.ps1")


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _result(available: bool, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "available": bool(available),
        "source": "mikufans-wpf-ui",
        "reason": reason,
        "checked_at": _checked_at(),
        "rooms": [],
        **extra,
    }


def recorder_watch_root() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return None
    pointer = Path(local_app_data) / "BililiveRecorder" / "path.json"
    if not pointer.is_file():
        return None
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8-sig"))
        value = str(payload.get("Path") or "").strip()
        return Path(value).expanduser().resolve() if value else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def manages_watch_root(watch_root: Path) -> bool:
    managed = recorder_watch_root()
    if managed is None:
        return False
    try:
        return os.path.normcase(str(managed)) == os.path.normcase(str(watch_root.resolve()))
    except OSError:
        return False


def read_bililive_recorder_status(
    watch_root: Path,
    *,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    if os.name != "nt":
        return _result(False, "windows-only")
    if not manages_watch_root(watch_root):
        return _result(False, "watch-root-not-managed")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell or not STATUS_SCRIPT.is_file():
        return _result(False, "helper-unavailable")
    try:
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(STATUS_SCRIPT),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8-sig",
            errors="replace",
            timeout=max(1.0, float(timeout_seconds)),
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _result(False, "helper-failed", error=str(exc)[:300])
    output = completed.stdout.strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return _result(
            False,
            "invalid-helper-output",
            error=(completed.stderr.strip() or output)[:300],
        )
    if not isinstance(payload, dict):
        return _result(False, "invalid-helper-output")
    rooms = payload.get("rooms", [])
    if isinstance(rooms, dict):
        rooms = [rooms]
    payload["rooms"] = [room for room in rooms if isinstance(room, dict)]
    payload["available"] = bool(payload.get("available"))
    payload["source"] = "mikufans-wpf-ui"
    payload["checked_at"] = _checked_at()
    return payload


def live_room_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not snapshot.get("available"):
        return {}
    return {
        str(room.get("room_id") or "").strip(): room
        for room in snapshot.get("rooms", [])
        if str(room.get("room_id") or "").strip()
    }


if __name__ == "__main__":
    root = recorder_watch_root()
    snapshot = (
        read_bililive_recorder_status(root)
        if root is not None
        else _result(False, "recorder-path-unavailable")
    )
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
