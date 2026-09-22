"""Bounded, read-only diagnostics for current and legacy Flow 5 logs."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import time
from datetime import datetime
from typing import Any

PROGRESS_PREFIX = "FLOW5_PROGRESS "
RATE_MESSAGES = {"601": "您上传视频过快", "137022": "投稿过于频繁，请稍后再试"}
SECRET = re.compile(r"(?i)(SESSDATA|bili_jct|access_token|refresh_token|cookie)\s*[:=]\s*[^\s,;]+")


def safe_detail(text: str) -> str:
    return SECRET.sub(lambda match: f"{match.group(1)}=<redacted>", text).strip()[:700]


def parse_progress_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if line.startswith(PROGRESS_PREFIX):
        try:
            event = json.loads(line[len(PROGRESS_PREFIX):])
            if not isinstance(event, dict) or event.get("phase") not in {"uploading", "cooldown", "failed", "waiting_lock"}:
                return None
            return {
                "phase": event["phase"],
                "detail": safe_detail(str(event.get("detail") or "")),
                "code": str(event.get("code") or ""),
                "cooldown_until": max(0, float(event.get("cooldown_until") or 0)),
            }
        except (ValueError, TypeError):
            return None
    if re.match(r"\[\d+/\d+\] 正在投稿 ", line):
        return {"phase": "uploading", "detail": safe_detail(line), "code": "", "cooldown_until": 0}
    if line.startswith(("B站限制投稿频率", "B站限制上传频率", "Flow 5 已连续投稿")) and "冷却" in line:
        match = re.search(r"（(601|137022)）", line)
        return {"phase": "cooldown", "detail": safe_detail(line), "code": match.group(1) if match else "", "cooldown_until": 0}
    if line.startswith("Flow 5 投稿受限（B站 "):
        match = re.search(r"B站 (601|137022)", line)
        return {"phase": "failed", "detail": safe_detail(line), "code": match.group(1) if match else "", "cooldown_until": 0}
    return None


def publish_error_summary(output: str) -> dict[str, str]:
    """Prefer the actual API error over biliup's generic Unknown Error wrapper."""
    for line in reversed(output.splitlines()):
        event = parse_progress_line(line)
        if event and event["phase"] == "failed":
            return {"detail": event["detail"], "code": event["code"]}
    fallback = ""
    for line in reversed(output.splitlines()):
        responses = re.findall(r'"?code"?\s*:\s*(-?\d+).*?"?message"?\s*:\s*"([^"\n]+)"', line)
        if responses:
            code, message = responses[-1]
            if code in RATE_MESSAGES:
                return {"detail": f"B站限制投稿频率（{code}）：{RATE_MESSAGES[code]}。本地成片仍可复用；平台未提供解限时间，工具会逐步延长等待后再试。", "code": code}
            return {"detail": safe_detail(f"B站返回 {code}：{message}"), "code": code}
        codes = re.findall(r"[\"']?code[\"']?\s*[:=]\s*(601|137022)\b", line, re.IGNORECASE)
        if codes:
            code = codes[-1]
            return {"detail": f"B站限制投稿频率（{code}）：{RATE_MESSAGES[code]}。本地成片仍可复用；平台未提供解限时间，工具会逐步延长等待后再试。", "code": code}
        if re.match(r"^\w*(?:Error|Exception):\s+", line.strip()):
            if "Unknown Error" not in line and "after retries" not in line:
                return {"detail": safe_detail(line), "code": ""}
            fallback = fallback or safe_detail(line)
    if fallback:
        return {"detail": fallback, "code": ""}
    return {"detail": "", "code": ""}


def _timestamp(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _current_publish_started_at(project: Path, job: dict[str, Any]) -> float:
    attempts = []
    try:
        state = json.loads((project / "workflow-project.json").read_text(encoding="utf-8-sig"))
        for key in ("publish_attempts", "replacement_attempts"):
            rows = state.get(key) or []
            if rows and isinstance(rows[-1], dict):
                attempts.append(rows[-1])
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    latest = max(attempts, key=lambda row: _timestamp(row.get("started_at")), default={})
    if latest.get("status") == "running":
        return _timestamp(latest.get("started_at"))
    # Before preview has produced the new attempt, the queue's explicit restart
    # timestamp is the only current start marker available.
    return _timestamp(job.get("updated_at"))


def describe_publish_job(job: dict[str, Any]) -> dict[str, Any]:
    """Read only the newest execution log; never reuse a prior attempt's error."""
    value = str(job.get("project") or "").strip()
    result: dict[str, Any] = {"phase": "unknown", "detail": "", "code": "", "cooldown_until": 0, "log_path": ""}
    if not value:
        return result
    project = Path(value)
    try:
        logs = list((project / "logs").glob("*-flow5-*-execute.log"))
        if not logs:
            return result
        log = max(logs, key=lambda path: (path.stat().st_mtime_ns, path.name))
        if str(job.get("status") or "") == "failed":
            references = re.findall(r"日志[：:]\s*([^\r\n]+\.log)", str(job.get("detail") or ""))
            if references:
                referenced = Path(references[-1].strip())
                if referenced.is_file() and referenced.resolve().is_relative_to((project / "logs").resolve()):
                    # A fresh preview/cover failure must not be replaced by an
                    # older upload attempt's platform error.
                    log = referenced
        log_stat = log.stat()
        with log.open("rb") as handle:
            handle.seek(max(0, log_stat.st_size - 128 * 1024))
            output = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return result
    if str(job.get("status") or "") == "running":
        started_at = _current_publish_started_at(project, job)
        filename = re.match(r"(\d{8}-\d{6})-", log.name)
        log_started_at = 0.0
        if filename:
            try:
                log_started_at = datetime.strptime(filename[1], "%Y%m%d-%H%M%S").timestamp()
            except (ValueError, OverflowError):
                pass
        if started_at and (
            log_stat.st_mtime < started_at - 1
            or log_started_at and log_started_at < started_at - 1
        ):
            result.update(phase="uploading", detail="正在校验成片并准备再次投递")
            return result
    result["log_path"] = str(log)
    for line in output.splitlines():
        event = parse_progress_line(line)
        if event:
            result.update(event)
    status = str(job.get("status") or "")
    if status == "running":
        if result["phase"] == "cooldown":
            # Older publishers logged the remaining duration just before sleeping.
            # Their last write time anchors that local wait without reading cookies.
            match = re.search(r"剩余 (\d+):(\d{2})", result["detail"])
            if not result["cooldown_until"] and match:
                result["cooldown_until"] = log_stat.st_mtime + int(match[1]) * 60 + int(match[2])
            if result["cooldown_until"]:
                remaining = max(0, int(math.ceil(result["cooldown_until"] - time.time())))
                minutes, seconds = divmod(remaining, 60)
                result["detail"] = re.sub(r"剩余 \d+:\d{2}", f"剩余 {minutes:02d}:{seconds:02d}", result["detail"])
        if result["phase"] == "failed":
            # A just-started retry can still see its predecessor's log until
            # its preview finishes. The running queue owns the current state.
            result.update(phase="uploading", detail="正在校验成片并准备再次投递", code="", cooldown_until=0)
        elif result["phase"] == "unknown":
            result.update(phase="uploading", detail="正在执行投稿并等待平台返回")
        return result
    if status == "failed":
        summary = publish_error_summary(output)
        if summary["detail"]:
            result.update(phase="failed", **summary, cooldown_until=0)
        elif "仍在运行" in str(job.get("detail") or ""):
            result.update(phase="failed", detail="上次投稿被中断；再次投递会先检查现有稿件，避免重复发布。", cooldown_until=0)
    return result
