#!/usr/bin/env python3
"""Recoverable recording-to-publish automation with content-bound review gates."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import biliup_publish


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
PROFILE_PATH = SKILL_ROOT / "assets" / "creator-profiles.json"
MEDIA_EXTENSIONS = [".flv", ".mp4", ".mkv", ".mov", ".ts", ".m4a", ".wav"]
COPY_FIELDS = [
    "clip_id", "video", "title", "content_type", "cover_mode", "cover_text_primary",
    "cover_text_secondary", "description", "tags", "tid",
    "reference_image", "evidence_image",
]


class AutomationError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def expand_path(value: str, base: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return (path if path.is_absolute() else base / path).resolve()


def load_config(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if config.get("version") != 1:
        raise AutomationError("configuration version must be 1")
    base = path.parent
    config["_path"] = path
    config["_base"] = base
    config["_watch_roots"] = [expand_path(item, base) for item in config.get("watch_roots", [])]
    if not config["_watch_roots"]:
        raise AutomationError("watch_roots must contain at least one folder")
    config["_output_root"] = expand_path(config.get("output_root", "automation-runs"), base)
    config["_state_file"] = expand_path(
        config.get("state_file", "automation-runs/automation-state.json"), base
    )
    cookie = config.get("cookie_file", str(biliup_publish.default_cookie_path()))
    config["_cookie_file"] = expand_path(cookie, base)
    biliup_publish.validate_cookie_location(config["_cookie_file"])
    return config


def load_state(config: dict[str, Any]) -> dict[str, Any]:
    path = config["_state_file"]
    if not path.is_file():
        raise AutomationError("automation is not initialized; run init first")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    atomic_json(config["_state_file"], state)


@contextmanager
def state_lock(config: dict[str, Any]):
    lock = config["_state_file"].with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    timeout = int(config.get("lock_timeout_seconds", 21600))
    if lock.exists():
        if time.time() - lock.stat().st_mtime <= timeout:
            raise AutomationError(f"another process holds the lock: {lock}")
        lock.unlink()
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "created_at": now_iso()}))
        yield
    finally:
        if lock.exists():
            lock.unlink()


def signature(path: Path) -> str:
    stat = path.stat()
    value = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def slug(value: str) -> str:
    return (re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-_.")[:48] or "recording").lower()


def discover(config: dict[str, Any]) -> list[Path]:
    extensions = {item.lower() for item in config.get("media_extensions", MEDIA_EXTENSIONS)}
    output = config["_output_root"]
    result: list[Path] = []
    for root in config["_watch_roots"]:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            try:
                path.resolve().relative_to(output)
                continue
            except ValueError:
                result.append(path.resolve())
    return sorted(set(result))


def load_profiles() -> dict[str, dict[str, Any]]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8-sig"))["profiles"]


def infer_creator(path: Path, config: dict[str, Any]) -> str:
    haystack = str(path).casefold()
    profiles = load_profiles()
    for rule in config.get("creator_rules", []):
        creator = str(rule.get("creator", ""))
        terms = [str(item).casefold() for item in rule.get("contains", [])]
        if creator in profiles and any(term and term in haystack for term in terms):
            return creator
    default = str(config.get("creator_default", ""))
    return default if default in profiles else ""


def companion_xml(source: Path) -> Path | None:
    exact = source.with_suffix(".xml")
    if exact.is_file():
        return exact.resolve()
    matches = sorted(source.parent.glob(f"{source.stem}*.xml"))
    return matches[0].resolve() if matches else None


def make_job(source: Path, source_signature: str, config: dict[str, Any]) -> dict[str, Any]:
    creator = infer_creator(source, config)
    job_id = f"{slug(source.stem)}-{source_signature[:10]}"
    return {
        "id": job_id,
        "source": str(source),
        "source_signature": source_signature,
        "xml": str(companion_xml(source) or ""),
        "creator": creator,
        "status": "discovered" if creator else "needs_creator",
        "work_dir": str(config["_output_root"] / "jobs" / job_id),
        "approvals": {},
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "last_error": "",
        "last_log": "",
    }


def scan_state(config: dict[str, Any], state: dict[str, Any]) -> list[str]:
    stable = int(config.get("stable_seconds", 120))
    registered: list[str] = []
    for source in discover(config):
        if time.time() - source.stat().st_mtime < stable:
            continue
        source_signature = signature(source)
        key = str(source)
        if state["known"].get(key) == source_signature:
            continue
        job = make_job(source, source_signature, config)
        base_id = job["id"]
        counter = 2
        while job["id"] in state["jobs"]:
            job["id"] = f"{base_id}-{counter}"
            job["work_dir"] = str(config["_output_root"] / "jobs" / job["id"])
            counter += 1
        state["jobs"][job["id"]] = job
        state["known"][key] = source_signature
        registered.append(job["id"])
    return registered


def initialize(config: dict[str, Any], force: bool = False) -> dict[str, Any]:
    if config["_state_file"].exists() and not force:
        raise AutomationError(f"state already exists: {config['_state_file']}")
    config["_output_root"].mkdir(parents=True, exist_ok=True)
    known: dict[str, str] = {}
    if config.get("initial_scan", "ignore-existing") == "ignore-existing":
        known = {str(path): signature(path) for path in discover(config)}
    state = {
        "version": 1, "created_at": now_iso(), "updated_at": now_iso(),
        "known": known, "jobs": {},
    }
    save_state(config, state)
    return state


def redact(value: str) -> str:
    return re.sub(
        r"(?i)(SESSDATA|bili_jct)([\"'=:\s]+)([^,\s}\"']+)",
        r"\1\2<redacted>",
        value,
    )


def run_command(config: dict[str, Any], job: dict[str, Any], stage: str, command: list[str]) -> None:
    logs = Path(job["work_dir"]) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{datetime.now():%Y%m%d-%H%M%S}-{stage}.log"
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        command, check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    visible = ["<cookie-file>" if item == str(config["_cookie_file"]) else item for item in command]
    report = (
        f"command={subprocess.list2cmdline(visible)}\nreturncode={result.returncode}\n\n"
        f"[stdout]\n{result.stdout}\n\n[stderr]\n{result.stderr}\n"
    )
    log.write_text(redact(report), encoding="utf-8")
    job["last_log"] = str(log)
    if result.returncode:
        raise AutomationError(f"{stage} failed with exit code {result.returncode}; see {log}")
    print(f"{job['id']}: {stage} complete")


def py(script: str, *args: object) -> list[str]:
    return [sys.executable, str(SCRIPTS / script), *[str(item) for item in args]]


def run_analysis(config: dict[str, Any], job: dict[str, Any]) -> None:
    source = Path(job["source"])
    work = Path(job["work_dir"])
    transcript = work / "analysis" / "transcript"
    transcript.mkdir(parents=True, exist_ok=True)
    asr = config.get("asr", {})
    command = py(
        "content_slicer.py", "transcribe", "--input", source, "--output", transcript,
        "--model", asr.get("model", "local:qwen3-asr-auto"),
        "--language", asr.get("language", "zh"),
        "--device", asr.get("device", "cuda"),
        "--compute-type", asr.get("compute_type", "float16"),
    )
    if str(asr.get("initial_prompt", "")).strip():
        command.extend(["--initial-prompt", str(asr["initial_prompt"])])
    run_command(config, job, "source-transcription", command)
    run_command(
        config, job, "gift-filter",
        py(
            "content_slicer.py", "filter-gifts",
            "--transcript", transcript / "transcript.csv",
            "--output", transcript / "transcript.filtered.csv",
            "--removed-output", transcript / "gift-acknowledgements.csv",
        ),
    )
    xml = Path(job["xml"]) if job.get("xml") else None
    engagement = work / "analysis" / "engagement"
    if xml and xml.is_file():
        command = py("analyze_engagement.py", "--xml", xml, "--output", engagement)
        for keyword in config.get("editorial", {}).get("keywords", []):
            command.extend(["--keyword", str(keyword)])
        run_command(config, job, "engagement-analysis", command)

    feedback = []
    for pattern in config.get("editorial", {}).get("feedback_globs", []):
        feedback.extend(path for path in config["_base"].glob(pattern) if path.is_file())
    if feedback:
        run_command(
            config, job, "feedback-summary",
            py(
                "summarize_clip_feedback.py", "--input", *sorted(set(feedback)),
                "--output", work / "analysis" / "feedback",
            ),
        )
    packet = [
        "# Editorial review packet", "",
        f"- Source: {source}",
        f"- Edit plan: {transcript / 'edit-plan.csv'}",
        f"- Transcript: {transcript / 'transcript.filtered.csv'}",
        f"- Hotspots: {engagement / 'engagement-hotspots.csv'}",
        f"- Super Chats: {engagement / 'superchats.csv'}", "",
        "Use source media as truth. Danmaku velocity discovers likely peaks; SC is a cause/response clue.",
        "Inspect innuendo, strong opinions about people or events, reversals, conflict, embarrassment,",
        "and complete payoff chains. Fill edit-plan.csv, then approve editorial.",
    ]
    (work / "review-packet.md").write_text("\n".join(packet) + "\n", encoding="utf-8")
    job["status"] = "awaiting_editorial"
    job["last_error"] = ""


def kept_plan(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    kept = [
        row for row in rows
        if (row.get("keep") or "").strip().lower() in {"1", "true", "yes", "y", "keep"}
    ]
    if not kept:
        raise AutomationError("edit plan has no rows marked keep")
    return kept


def approval_files(job: dict[str, Any], stage: str) -> list[Path]:
    work = Path(job["work_dir"])
    if stage == "editorial":
        return [work / "analysis" / "transcript" / "edit-plan.csv"]
    if stage == "delivery":
        return [
            work / "review" / "anchor-qa.csv",
            work / "review" / "subtitle-vad-qa.csv",
            work / "review" / "titles-and-covers.csv",
            *sorted((work / "review" / "ass").glob("*.ass")),
        ]
    if stage == "publish":
        delivery = work / "delivery"
        return [
            delivery / "titles-and-covers.csv",
            *sorted(delivery.glob("*.mp4")),
            *sorted(delivery.glob("*.ass")),
            *sorted(delivery.glob("*-cover.jpg")),
        ]
    raise AutomationError(f"unknown approval stage: {stage}")


def digest_files(paths: Iterable[Path], extra: object | None = None) -> str:
    digest = hashlib.sha256()
    count = 0
    for path in sorted({item.resolve() for item in paths}, key=lambda item: str(item).casefold()):
        if not path.is_file():
            raise AutomationError(f"approval input is missing: {path}")
        digest.update(str(path).encode("utf-8"))
        digest.update(path.read_bytes())
        count += 1
    if not count:
        raise AutomationError("approval has no input files")
    if extra is not None:
        digest.update(json.dumps(extra, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def approval_digest(config: dict[str, Any], job: dict[str, Any], stage: str) -> str:
    extra = config.get("publish", {}) if stage == "publish" else None
    return digest_files(approval_files(job, stage), extra)


def approval_valid(config: dict[str, Any], job: dict[str, Any], stage: str) -> bool:
    approval = job.get("approvals", {}).get(stage)
    if not approval:
        return False
    try:
        return approval["digest"] == approval_digest(config, job, stage)
    except AutomationError:
        return False


def validate_copy(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise AutomationError("title and cover CSV has no rows")
    missing = [index for index, row in enumerate(rows, 2) if not (row.get("title") or "").strip()]
    if missing:
        raise AutomationError(f"title is missing on rows: {missing}")
    return len(rows)


def validate_anchor_qa(path: Path) -> None:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    pending = [
        row for row in rows
        if (row.get("verified") or "").strip().lower() not in {"1", "true", "yes", "y", "pass"}
    ]
    if pending:
        raise AutomationError(f"{len(pending)} anchor QA rows are still unverified")


def validate_subtitle_vad_qa(path: Path) -> None:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    affirmative = {"1", "true", "yes", "y", "pass"}
    accepted_decisions = {"drop", "keep", "restore", "retime", "pass"}
    pending = []
    for row in rows:
        needs_review = (row.get("needs_review") or "").strip().lower() in affirmative
        if not needs_review:
            continue
        reviewed = (row.get("reviewed") or "").strip().lower() in affirmative
        decision = (row.get("decision") or "").strip().lower()
        if not reviewed or decision not in accepted_decisions:
            pending.append(row)
    if pending:
        raise AutomationError(
            f"{len(pending)} subtitle VAD QA rows still need listening review and a decision"
        )


def infer_content_type(row: dict[str, str]) -> str:
    text = " ".join(
        str(row.get(field, "")).casefold()
        for field in ("title", "outline", "hook", "reason")
    )
    song_terms = (
        "歌切", "翻唱", "演唱", "唱歌", "唱《",
        "karaoke", "cover", "song",
    )
    return "song" if any(term in text for term in song_terms) else "narrative"


def create_copy_template(work: Path) -> None:
    destination = work / "review" / "titles-and-covers.csv"
    if destination.exists():
        return
    with (work / "export" / "slices.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COPY_FIELDS)
        writer.writeheader()
        for index, row in enumerate(rows, 1):
            writer.writerow(
                {
                    "clip_id": f"{index:03d}", "video": row["file"],
                    "title": row.get("title", ""),
                    "content_type": infer_content_type(row),
                    "cover_mode": "quote-impact",
                    "cover_text_primary": "", "cover_text_secondary": "",
                    "description": "", "tags": "", "tid": "",
                    "reference_image": "", "evidence_image": "",
                }
            )


def subtitle_template(creator: str) -> Path:
    specific = SKILL_ROOT / "assets" / "subtitles" / f"narrative-{creator}-v2.ass"
    return specific if specific.is_file() else SKILL_ROOT / "assets" / "subtitles" / "narrative-v2.ass"


def subtitle_style(creator: str) -> str:
    return {
        "kioi": "Kioi",
        "sumire": "Sumire",
        "viridis": "Viridis",
        "yuchu": "Yuchu",
    }.get(creator, "Regular")

def run_review(config: dict[str, Any], job: dict[str, Any]) -> None:
    work = Path(job["work_dir"])
    source = Path(job["source"])
    transcript = work / "analysis" / "transcript"
    plan = transcript / "edit-plan.csv"
    gift = transcript / "gift-acknowledgements.csv"
    command = py("content_slicer.py", "validate-plan", "--plan", plan, "--audio", source)
    if gift.is_file():
        command.extend(["--gift-acks", str(gift)])
    run_command(config, job, "edit-plan-validation", command)
    command = py(
        "content_slicer.py", "export", "--audio", source, "--video", source,
        "--plan", plan, "--output", work / "export",
    )
    if gift.is_file():
        command.extend(["--gift-acks", str(gift)])
    run_command(config, job, "review-export", command)

    review = work / "review"
    ass = review / "ass"
    create_copy_template(work)
    profile = load_profiles()[job["creator"]]
    command = py(
        "content_slicer.py", "slice-authoritative",
        "--transcript", transcript / "transcript.filtered.csv",
        "--transcript-json", transcript / "transcript.json",
        "--completeness-report", transcript / "transcript-completeness.json",
        "--speech-json", transcript / "speech-activity.json",
        "--plan", plan,
        "--manifest", work / "export" / "slices.csv",
        "--output", review / "asr",
        "--content-types-csv", review / "titles-and-covers.csv",
        "--ass-output", ass,
        "--ass-template", subtitle_template(job["creator"]),
        "--ass-style", subtitle_style(job["creator"]),
        "--ass-name", profile["display_name"],
        "--ass-font", profile.get("dialogue_font", "Microsoft YaHei"),
        "--ass-font-size", profile.get("dialogue_font_size", 85),
        "--ass-primary-color", profile.get(
            "subtitle_fill_color", profile.get("role_color", "#FFFFFF")
        ),
        "--ass-outline-color", profile.get("subtitle_outline_color", "#111318"),
        "--ass-outline-width", profile.get("subtitle_outline_width", 5.0),
    )
    run_command(config, job, "authoritative-subtitle-remap", command)
    shutil.copy2(review / "asr" / "anchor-qa.csv", review / "anchor-qa.csv")
    shutil.copy2(
        review / "asr" / "subtitle-vad-qa.csv", review / "subtitle-vad-qa.csv"
    )
    job["status"] = "awaiting_delivery_review"
    job["last_error"] = ""


def executable(value: str) -> str:
    if Path(value).is_file():
        return str(Path(value).resolve())
    found = shutil.which(value)
    if not found:
        raise AutomationError(f"executable is unavailable: {value}")
    return found


def publish_ready(config: dict[str, Any]) -> bool:
    value = config.get("publish", {})
    return bool(
        value.get("enabled")
        and isinstance(value.get("tid"), int) and value["tid"] > 0
        and isinstance(value.get("song_tid"), int) and value["song_tid"] > 0
        and value.get("copyright") in (1, 2)
        and (value.get("copyright") == 1 or str(value.get("source", "")).strip())
    )


def upload_command(config: dict[str, Any], job: dict[str, Any], execute: bool) -> list[str]:
    value = config["publish"]
    delivery = Path(job["work_dir"]) / "delivery"
    command = py(
        "biliup_publish.py", "--cookie-file", config["_cookie_file"],
        "upload", "--delivery", delivery, "--creator", job["creator"],
        "--tid", value["tid"], "--song-tid", value["song_tid"],
        "--copyright", value["copyright"],
        "--source", value.get("source", ""),
        "--no-reprint", int(bool(value.get("no_reprint", False))),
        "--limit", value.get("limit", 3),
        "--max-items", value.get("max_items", 5),
        "--cooldown", value.get("cooldown_seconds", 60),
        "--tags", value.get("tags", ""),
        "--description", value.get("description", ""),
    )
    if value.get("line"):
        command.extend(["--line", str(value["line"])])
    if execute:
        count = validate_copy(delivery / "titles-and-covers.csv")
        command.extend(["--execute", "--confirm", biliup_publish.expected_confirmation(count)])
    return command


def publish_preview(config: dict[str, Any], job: dict[str, Any]) -> None:
    run_command(config, job, "publish-preview", upload_command(config, job, False))


def run_delivery(config: dict[str, Any], job: dict[str, Any]) -> None:
    work = Path(job["work_dir"])
    clips = work / "export" / "clips"
    review_ass = work / "review" / "ass"
    clip_asr = work / "review" / "asr"
    package_ass = work / "package-ass"
    delivery = work / "delivery"
    package_ass.mkdir(parents=True, exist_ok=True)
    delivery.mkdir(parents=True, exist_ok=True)
    ffmpeg = executable(str(config.get("ffmpeg", "ffmpeg")))
    with (work / "review" / "titles-and-covers.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        content_types = {
            Path(row.get("video", "")).name: (row.get("content_type") or "narrative").strip().lower()
            for row in csv.DictReader(handle)
        }

    for clip in sorted(clips.glob("*.mp4")):
        target = package_ass / f"{clip.stem}.ass"
        shutil.copy2(review_ass / target.name, target)
        run_command(
            config, job, f"subtitle-clamp-{clip.stem}",
            py("clamp_ass_to_media.py", "--media", clip, "--ass", target),
        )
        audit_command = py(
            "audit_media_subtitles.py", "--media", clip, "--ass", target,
            "--asr-json", clip_asr / clip.stem / "transcript.json",
        )
        if content_types.get(clip.name, "narrative") != "song":
            audit_command.extend([
                "--speech-json", str(clip_asr / clip.stem / "speech-activity.json")
            ])
        run_command(
            config, job, f"subtitle-audit-{clip.stem}", audit_command,
        )
    run_command(
        config, job, "subtitle-burn",
        py(
            "burn_ass_subtitles.py", "--clips", clips, "--ass", package_ass,
            "--output", delivery, "--ffmpeg", ffmpeg,
            "--fontsdir", SKILL_ROOT / "assets" / "fonts",
        ),
    )
    for video in delivery.glob("*.mp4"):
        shutil.copy2(package_ass / f"{video.stem}.ass", video.with_suffix(".ass"))
    shutil.copy2(work / "review" / "titles-and-covers.csv", delivery / "titles-and-covers.csv")
    run_command(
        config, job, "cover-render",
        py(
            "local_publish.py", "render-local",
            "--copy", delivery / "titles-and-covers.csv",
            "--clips-dir", delivery, "--creator", job["creator"], "--force",
        ),
    )
    run_command(
        config, job, "final-delivery-audit",
        py(
            "audit_final_delivery.py", delivery,
            "--report", work / "final-delivery-audit.csv",
        ),
    )
    if publish_ready(config):
        publish_preview(config, job)
        job["status"] = "awaiting_publish"
    else:
        atomic_json(
            work / "publish-setup-needed.json",
            {
                "message": "Set positive fallback/song tids and confirm copyright in the config.",
                "publish": config.get("publish", {}),
            },
        )
        job["status"] = "delivery_ready"
    job["last_error"] = ""


def approve(config: dict[str, Any], state: dict[str, Any], job_id: str, stage: str) -> None:
    job = state["jobs"].get(job_id)
    if not job:
        raise AutomationError(f"job not found: {job_id}")
    required = {
        "editorial": "awaiting_editorial",
        "delivery": "awaiting_delivery_review",
        "publish": "awaiting_publish",
    }[stage]
    if job["status"] != required:
        raise AutomationError(f"{stage} approval requires {required}; current={job['status']}")
    work = Path(job["work_dir"])
    if stage == "editorial":
        kept_plan(work / "analysis" / "transcript" / "edit-plan.csv")
    elif stage == "delivery":
        validate_anchor_qa(work / "review" / "anchor-qa.csv")
        validate_subtitle_vad_qa(work / "review" / "subtitle-vad-qa.csv")
        validate_copy(work / "review" / "titles-and-covers.csv")
        missing = [
            clip.name for clip in (work / "export" / "clips").glob("*.mp4")
            if not (work / "review" / "ass" / f"{clip.stem}.ass").is_file()
        ]
        if missing:
            raise AutomationError("reviewed ASS is missing for: " + ", ".join(missing))
    else:
        if not publish_ready(config):
            raise AutomationError("publish configuration is incomplete")
        if biliup_publish.cookie_json_status(config["_cookie_file"]) != "ready":
            raise AutomationError("cookie template is not filled or is invalid")
    job.setdefault("approvals", {})[stage] = {
        "digest": approval_digest(config, job, stage),
        "approved_at": now_iso(),
    }
    save_state(config, state)
    print(f"{job_id}: {stage} approved; run again to continue")


def process_job(config: dict[str, Any], state: dict[str, Any], job: dict[str, Any]) -> None:
    status = job["status"]
    if status == "discovered":
        run_analysis(config, job)
    elif status == "awaiting_editorial" and approval_valid(config, job, "editorial"):
        run_review(config, job)
    elif status == "awaiting_delivery_review" and approval_valid(config, job, "delivery"):
        run_delivery(config, job)
    elif status == "delivery_ready" and publish_ready(config):
        publish_preview(config, job)
        job["status"] = "awaiting_publish"
    elif status == "awaiting_publish" and approval_valid(config, job, "publish"):
        if biliup_publish.cookie_json_status(config["_cookie_file"]) != "ready":
            raise AutomationError("cookie is not ready")
        run_command(config, job, "publish-execute", upload_command(config, job, True))
        job["status"] = "published"
        job["published_at"] = now_iso()
    save_state(config, state)


def run_once(
    config: dict[str, Any], state: dict[str, Any],
    job_id: str | None, max_jobs: int, dry_run: bool,
) -> None:
    if job_id and job_id not in state["jobs"]:
        raise AutomationError(f"job not found: {job_id}")
    jobs = [state["jobs"][job_id]] if job_id else list(state["jobs"].values())
    advanced = 0
    for job in sorted(jobs, key=lambda item: item["created_at"]):
        if job["status"] in {"published", "needs_creator"}:
            continue
        if dry_run:
            print(f"{job['id']}: current={job['status']}")
            continue
        before = job["status"]
        try:
            process_job(config, state, job)
        except Exception as exc:
            job["resume_status"] = before
            job["status"] = "failed"
            job["last_error"] = redact(str(exc))
            save_state(config, state)
            print(f"{job['id']}: failed: {job['last_error']}", file=sys.stderr)
        if job["status"] != before:
            advanced += 1
        if advanced >= max_jobs:
            break


def status_text(state: dict[str, Any]) -> str:
    rows = [
        f"{job['id']}\t{job['status']}\t{job.get('creator') or '-'}\t{Path(job['source']).name}"
        for job in sorted(state["jobs"].values(), key=lambda item: item["created_at"])
    ]
    return "\n".join(rows) if rows else "No new recordings registered."


def doctor(config: dict[str, Any]) -> int:
    print(f"config={config['_path']}")
    print(f"state={config['_state_file']}")
    print(f"cookie={config['_cookie_file']} status={biliup_publish.cookie_json_status(config['_cookie_file'])}")
    print(f"publish_config={'ready' if publish_ready(config) else 'needs-one-time-setup'}")
    for root in config["_watch_roots"]:
        print(f"watch_root={root} exists={root.is_dir()}")
    print(f"ffmpeg={shutil.which(str(config.get('ffmpeg', 'ffmpeg'))) or 'missing'}")
    try:
        print(f"biliup={biliup_publish.find_biliup(config.get('biliup', ''))}")
    except FileNotFoundError:
        print("biliup=missing")
        return 1
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--config", type=Path, default=Path("recording-automation.json"))
    sub = root.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--force", action="store_true")
    sub.add_parser("scan")
    sub.add_parser("status")
    sub.add_parser("doctor")
    run = sub.add_parser("run")
    run.add_argument("--job")
    run.add_argument("--max-jobs", type=int)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--no-scan", action="store_true")
    watch = sub.add_parser("watch")
    watch.add_argument("--interval", type=int, default=60)
    watch.add_argument("--max-cycles", type=int, default=0)
    approval = sub.add_parser("approve")
    approval.add_argument("--job", required=True)
    approval.add_argument("--stage", choices=("editorial", "delivery", "publish"), required=True)
    assign = sub.add_parser("assign-creator")
    assign.add_argument("--job", required=True)
    assign.add_argument("--creator", required=True)
    retry = sub.add_parser("retry")
    retry.add_argument("--job", required=True)
    return root


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args()
    config = load_config(args.config)
    try:
        if args.command == "doctor":
            return doctor(config)
        if args.command == "watch":
            cycles = 0
            while True:
                with state_lock(config):
                    state = load_state(config)
                    scan_state(config, state)
                    save_state(config, state)
                    run_once(
                        config, state, None,
                        int(config.get("max_jobs_per_run", 1)), False,
                    )
                cycles += 1
                if args.max_cycles and cycles >= args.max_cycles:
                    break
                time.sleep(max(5, args.interval))
            return 0

        with state_lock(config):
            if args.command == "init":
                state = initialize(config, args.force)
                print(f"initialized: ignored_existing={len(state['known'])}")
                return 0
            state = load_state(config)
            if args.command == "scan":
                jobs = scan_state(config, state)
                save_state(config, state)
                print(f"registered={len(jobs)}")
                print("\n".join(jobs))
            elif args.command == "status":
                print(status_text(state))
            elif args.command == "approve":
                approve(config, state, args.job, args.stage)
            elif args.command == "assign-creator":
                if args.creator not in load_profiles():
                    raise AutomationError(f"unknown creator profile: {args.creator}")
                job = state["jobs"].get(args.job)
                if not job or job["status"] != "needs_creator":
                    raise AutomationError("job must exist with status=needs_creator")
                job["creator"] = args.creator
                job["status"] = "discovered"
                save_state(config, state)
            elif args.command == "retry":
                job = state["jobs"].get(args.job)
                if not job or job["status"] != "failed":
                    raise AutomationError("job must exist with status=failed")
                job["status"] = job.pop("resume_status", "discovered")
                job["last_error"] = ""
                save_state(config, state)
            elif args.command == "run":
                if not args.no_scan:
                    scan_state(config, state)
                    save_state(config, state)
                run_once(
                    config, state, args.job,
                    args.max_jobs or int(config.get("max_jobs_per_run", 1)),
                    args.dry_run,
                )
                print(status_text(state))
        return 0
    except (AutomationError, FileNotFoundError, ValueError) as exc:
        print(f"blocked: {redact(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

