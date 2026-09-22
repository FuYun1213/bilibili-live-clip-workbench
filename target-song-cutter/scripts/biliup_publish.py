#!/usr/bin/env python3
"""Preview or explicitly publish same-folder delivery clips with biliup."""

from __future__ import annotations

import argparse
import base64
import csv
from functools import wraps
import hashlib
import http.cookiejar
import inspect
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence
from campaign_rules import merge_campaign_tags
from validate_selection_tags import validate_tag_row
from windows_process import hidden_subprocess_kwargs
from publish_diagnostics import PROGRESS_PREFIX, publish_error_summary
from process_file_lock import exclusive_file_lock


SKILL_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = SKILL_ROOT / "assets" / "creator-profiles.json"
REQUIRED_UPLOAD_FLAGS = {
    "--title", "--cover", "--tid", "--tag", "--copyright",
    "--source", "--no-reprint", "--limit", "--desc", "--extra-fields", "--submit",
}
COOKIE_REQUIRED_NAMES = ("SESSDATA", "bili_jct")
FLOW5_UPLOAD_BATCH_SIZE = 5
FLOW5_UPLOAD_COOLDOWN_SECONDS = 10 * 60
FLOW5_RATE_LIMIT_MAX_COOLDOWN_SECONDS = 60 * 60
FLOW5_THROTTLE_SCHEMA = "target-song-cutter.flow5-upload-throttle.v1"
BILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BILI_QR_STATUS = {
    86101: "等待扫码",
    86090: "已扫码，请在手机上确认",
    86038: "二维码已过期",
    0: "登录成功",
}
COOKIE_TEMPLATE = {
    "cookie_info": {
        "cookies": [
            {"name": "SESSDATA", "value": ""},
            {"name": "bili_jct", "value": ""},
            {"name": "DedeUserID", "value": ""},
            {"name": "DedeUserID__ckMd5", "value": ""},
            {"name": "sid", "value": ""},
            {"name": "buvid3", "value": ""},
        ]
    },
    "sso": [],
    "token_info": {
        "access_token": "",
        "expires_in": 0,
        "mid": 0,
        "refresh_token": "",
    },
    "platform": None,
}

LEADING_INDEX_RE = re.compile(
    r"^\s*(?:(?:[A-Za-z]{2,8}-)?\d{1,4})[\s._、：:-]+"
)
TRAILING_CREATOR_TAGS_RE = re.compile(r"\s*((?:【[^】]+】)+)\s*$")


def normalize_upload_title(value: str) -> str:
    """Remove delivery numbering and place a trailing creator tag at the front."""
    title = LEADING_INDEX_RE.sub("", value.strip())
    if title.startswith("【"):
        return title
    match = TRAILING_CREATOR_TAGS_RE.search(title)
    if match:
        tags = match.group(1)
        body = title[:match.start()].strip()
        title = f"{tags}{body}"
    return title


def default_cookie_path(environment: dict[str, str] | None = None) -> Path:
    env = environment if environment is not None else os.environ
    if env.get("LOCALAPPDATA"):
        base = Path(env["LOCALAPPDATA"])
    elif env.get("XDG_CONFIG_HOME"):
        base = Path(env["XDG_CONFIG_HOME"])
    else:
        base = Path.home() / ".config"
    return (base / "target-song-cutter" / "biliup" / "cookies.json").resolve()


def default_collection_cookie_path(environment: dict[str, str] | None = None) -> Path:
    return default_cookie_path(environment).with_name("web-cookies.json")


def default_flow5_throttle_path(environment: dict[str, str] | None = None) -> Path:
    return default_cookie_path(environment).with_name("flow5-upload-throttle.json")


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate_cookie_location(path: Path, delivery: Path | None = None) -> None:
    resolved = path.resolve()
    if any(part.lower().startswith("onedrive") for part in resolved.parts):
        raise ValueError("cookie file must not be stored in OneDrive")
    if is_inside(resolved, SKILL_ROOT):
        raise ValueError("cookie file must not be stored inside the skill")
    if delivery is not None and is_inside(resolved, delivery):
        raise ValueError("cookie file must not be stored in the delivery folder")


def cookie_json_status(path: Path) -> str:
    """Return missing, invalid, template, or ready without exposing credential values."""
    if not path.is_file():
        return "missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    if not isinstance(value, dict):
        return "invalid"
    cookie_info = value.get("cookie_info")
    token_info = value.get("token_info")
    if not isinstance(cookie_info, dict) or not isinstance(token_info, dict):
        return "invalid"
    cookies = cookie_info.get("cookies")
    if not isinstance(cookies, list):
        return "invalid"
    names: dict[str, str] = {}
    for item in cookies:
        if not isinstance(item, dict):
            return "invalid"
        name = item.get("name")
        cookie_value = item.get("value")
        if not isinstance(name, str) or not isinstance(cookie_value, str):
            return "invalid"
        names[name] = cookie_value
    if not all(name in names for name in COOKIE_REQUIRED_NAMES):
        return "invalid"
    if not all(key in token_info for key in ("access_token", "refresh_token")):
        return "invalid"
    return "ready" if all(names[name].strip() for name in COOKIE_REQUIRED_NAMES) else "template"


def validate_cookie_json(path: Path, require_ready: bool = True) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"cookie file not found: {path}")
    status = cookie_json_status(path)
    if status == "invalid":
        raise ValueError(
            "cookie file is not a biliup cookies.json: expected cookie_info.cookies and token_info"
        )
    if require_ready and status != "ready":
        raise ValueError("cookie template is incomplete: fill non-empty SESSDATA and bili_jct values")


def create_cookie_template(destination: Path, force: bool = False) -> Path:
    destination = destination.expanduser().resolve()
    validate_cookie_location(destination)
    if destination.exists():
        status = cookie_json_status(destination)
        if status == "ready":
            raise FileExistsError("refusing to replace an existing ready cookie file")
        if not force:
            raise FileExistsError(
                f"cookie file already exists with status={status}; use --force-template"
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(COOKIE_TEMPLATE, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    restrict_file_permissions(destination)
    validate_cookie_json(destination, require_ready=False)
    return destination


def restrict_file_permissions(path: Path) -> None:
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def import_cookie_file(source: Path, destination: Path) -> Path:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    validate_cookie_location(destination)
    validate_cookie_json(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source != destination:
        shutil.copyfile(source, destination)
    restrict_file_permissions(destination)
    validate_cookie_json(destination)
    return destination


def find_biliup(explicit: str = "") -> str:
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        found = shutil.which(explicit)
    else:
        found = shutil.which("biliup")
        runtime = Path(sys.executable).resolve().parent
        runtime_candidates = [
            runtime / ("biliup.exe" if os.name == "nt" else "biliup"),
            runtime / "Scripts" / "biliup.exe",
            runtime.parent / "Scripts" / "biliup.exe",
        ]
        if not found:
            found = next(
                (str(path.resolve()) for path in runtime_candidates if path.is_file()),
                None,
            )
        if not found and os.environ.get("LOCALAPPDATA"):
            local = Path(os.environ["LOCALAPPDATA"])
            known = [
                local / "Programs" / "biliup-cli" / "Scripts" / "biliup.exe",
                local / "Programs" / "biliup-cli" / "bin" / "biliup.exe",
            ]
            found = next((str(path.resolve()) for path in known if path.is_file()), None)
    if not found:
        raise FileNotFoundError(
            "biliup was not found. Install the current official release, then run doctor again."
        )
    return found


def _bili_json_request(opener: Any, url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.bilibili.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
        },
    )
    with opener.open(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("哔哩哔哩扫码接口返回了无效数据")
    if int(payload.get("code", -1)) != 0:
        raise RuntimeError(str(payload.get("message") or "哔哩哔哩扫码接口请求失败"))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("哔哩哔哩扫码接口缺少 data")
    return data


def start_bili_qr_login() -> dict[str, Any]:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )
    data = _bili_json_request(opener, BILI_QR_GENERATE_URL)
    login_url = str(data.get("url", "")).strip()
    qrcode_key = str(data.get("qrcode_key", "")).strip()
    if not login_url or not qrcode_key:
        raise RuntimeError("哔哩哔哩没有返回可用的二维码")
    return {
        "url": login_url,
        "qrcode_key": qrcode_key,
        "opener": opener,
        "cookie_jar": cookie_jar,
    }


def poll_bili_qr_login(session: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode({"qrcode_key": session["qrcode_key"]})
    return _bili_json_request(session["opener"], f"{BILI_QR_POLL_URL}?{query}")


def cookie_jar_values(cookie_jar: http.cookiejar.CookieJar) -> dict[str, str]:
    return {
        str(cookie.name): str(cookie.value)
        for cookie in cookie_jar
        if str(cookie.name).strip() and str(cookie.value).strip()
    }


def save_qr_login_credentials(
    destination: Path,
    cookies: dict[str, str],
    *,
    refresh_token: str = "",
) -> Path:
    destination = destination.expanduser().resolve()
    validate_cookie_location(destination)
    missing = [name for name in COOKIE_REQUIRED_NAMES if not cookies.get(name, "").strip()]
    if missing:
        raise ValueError("扫码成功但登录响应缺少：" + "、".join(missing))
    payload = json.loads(json.dumps(COOKIE_TEMPLATE))
    template_rows = payload["cookie_info"]["cookies"]
    known_names = {str(row["name"]) for row in template_rows}
    for row in template_rows:
        row["value"] = str(cookies.get(str(row["name"]), ""))
    for name, value in sorted(cookies.items()):
        if name not in known_names and str(value).strip():
            template_rows.append({"name": str(name), "value": str(value)})
    payload["token_info"]["refresh_token"] = str(refresh_token or "")
    try:
        payload["token_info"]["mid"] = int(cookies.get("DedeUserID", "0") or 0)
    except ValueError:
        payload["token_info"]["mid"] = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    restrict_file_permissions(destination)
    validate_cookie_json(destination)
    return destination


def run_login(binary: str, cookie_path: Path) -> Path:
    validate_cookie_location(cookie_path)
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [binary, "-u", str(cookie_path), "login"],
        check=False,
        **hidden_subprocess_kwargs(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"biliup login failed with exit code {result.returncode}")
    validate_cookie_json(cookie_path)
    restrict_file_permissions(cookie_path)
    return cookie_path


def credential_interface(binary: str, cookie_path: Path) -> Path:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "credentials are missing; run the credentials command in a local interactive terminal"
        )
    while True:
        print("\n需要哔哩哔哩登录凭证：")
        print("  1. 调用 biliup 扫码登录（推荐）")
        print("  2. 导入已有 cookies.json")
        print("  q. 退出，不投稿")
        choice = input("请选择：").strip().lower()
        if choice == "1":
            return run_login(binary, cookie_path)
        if choice == "2":
            source = input("请输入已有 cookies.json 的完整路径：").strip().strip('"')
            if not source:
                print("未输入路径。")
                continue
            return import_cookie_file(Path(source), cookie_path)
        if choice in {"q", "quit", "exit"}:
            raise RuntimeError("credential setup cancelled; nothing was uploaded")
        print("无法识别该选项。")


def ensure_credentials(binary: str, cookie_path: Path, delivery: Path | None = None) -> Path:
    validate_cookie_location(cookie_path, delivery)
    if cookie_path.is_file():
        validate_cookie_json(cookie_path)
        return cookie_path
    return credential_interface(binary, cookie_path)


def split_tags(raw: str, fallback: Sequence[str]) -> list[str]:
    values = [str(tag).strip() for tag in fallback if str(tag).strip()]
    values.extend(
        part.strip() for part in re.split(r"[,，;；]", raw.strip()) if part.strip()
    )
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique[:10]


def load_profiles() -> dict[str, dict[str, Any]]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8-sig"))["profiles"]


def default_collection_title(profile: dict[str, Any], kind: str) -> str:
    """Build the required Chinese-only collection name for one creator."""
    if kind not in {"narrative", "song"}:
        raise ValueError(f"unsupported collection kind: {kind}")
    display_name = str(profile.get("display_name") or profile.get("title_tag") or "")
    chinese_name = "".join(re.findall(r"[\u3400-\u9fff]+", display_name))
    if not chinese_name:
        raise ValueError(
            "cannot derive a Chinese collection name; configure upload.collections.title"
        )
    return f"{chinese_name}{'切片' if kind == 'narrative' else '歌'}"


def persist_profile_collection(
    creator: str,
    kind: str,
    title: str,
    season_id: int,
    section_id: int,
    *,
    profiles_path: Path = PROFILE_PATH,
) -> None:
    if kind not in {"narrative", "song"}:
        raise ValueError(f"unsupported collection kind: {kind}")
    if not title.strip() or season_id <= 0 or section_id <= 0:
        raise ValueError("collection title, season_id, and section_id must be valid")
    payload = json.loads(profiles_path.read_text(encoding="utf-8-sig"))
    profiles = payload.get("profiles") or {}
    if creator not in profiles:
        raise ValueError(f"unknown creator profile: {creator}")
    upload = profiles[creator].setdefault("upload", {})
    collections = upload.setdefault("collections", {})
    collection = collections.setdefault(kind, {})
    collection.update({
        "title": title.strip(),
        "season_id": int(season_id),
        "section_id": int(section_id),
    })
    temporary = profiles_path.with_name(f".{profiles_path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, profiles_path)


def compose_description(
    profile_description: str,
    row_description: str = "",
    default_description: str = "",
) -> str:
    """Prepend an optional clip lead without dropping the creator's fixed block."""
    fixed = str(profile_description or "").strip()
    lead = str(row_description or default_description or "").strip()
    if not lead:
        return fixed
    if not fixed or fixed in lead:
        return lead
    return f"{lead}\n\n{fixed}"


def resolve_input(value: str, base: Path) -> Path | None:
    if not value.strip():
        return None
    path = Path(value.strip())
    return (path if path.is_absolute() else base / path).resolve()


def find_video(delivery: Path, clip_id: str, explicit: str, csv_base: Path) -> Path | None:
    if explicit.strip():
        return resolve_input(explicit, csv_base)
    matches = sorted(delivery.glob(f"{clip_id}*.mp4"))
    if not matches:
        matches = sorted(delivery.glob(f"*{clip_id}*.mp4"))
    return matches[0].resolve() if matches else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_song_timing_gate(video: Path, ass: Path) -> dict[str, str]:
    gate = video.with_name(video.stem + ".song-timing-gate.json")
    if not gate.is_file():
        raise FileNotFoundError(
            f"song timing gate not found: {gate.name}; run audit_song_publish_gate.py"
        )
    try:
        payload = json.loads(gate.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid song timing gate {gate.name}: {exc}") from exc
    if payload.get("schema") != "target-song-cutter.song-timing-gate.v1":
        raise ValueError(f"unsupported song timing gate schema: {gate.name}")
    if payload.get("status") != "PASS":
        raise ValueError(f"song timing gate is not PASS: {gate.name}")
    for label, path in (("video", video), ("ass", ass)):
        bound = payload.get(label)
        if not isinstance(bound, dict):
            raise ValueError(f"song timing gate missing {label}: {gate.name}")
        if bound.get("name") != path.name:
            raise ValueError(f"song timing gate {label} filename mismatch: {gate.name}")
        expected = str(bound.get("sha256") or "").casefold()
        actual = sha256_file(path)
        if not expected or expected != actual:
            raise ValueError(
                f"song timing gate invalidated because {label} changed: {gate.name}"
            )
    return {"file": gate.name, "status": "PASS", "clip": str(payload.get("clip") or "")}


def load_upload_items(
    delivery: Path,
    copy_path: Path,
    creator: str,
    tid: int,
    copyright_value: int,
    source: str,
    no_reprint: int,
    default_tags: str,
    default_description: str,
    only: set[str] | None = None,
    song_tid: int = 130,
) -> list[dict[str, Any]]:
    delivery = delivery.resolve()
    copy_path = copy_path.resolve()
    if not delivery.is_dir():
        raise FileNotFoundError(f"delivery folder not found: {delivery}")
    if not copy_path.is_file():
        raise FileNotFoundError(f"titles-and-covers.csv not found: {copy_path}")
    profiles = load_profiles()
    if creator not in profiles:
        raise ValueError(f"unknown creator profile: {creator}")
    if tid <= 0:
        raise ValueError("tid must be a positive Bilibili category id")
    if song_tid <= 0:
        raise ValueError("song_tid must be a positive Bilibili category id")
    if copyright_value == 2 and not source.strip():
        raise ValueError("reposted content requires --source")

    profile = profiles[creator]
    if profile.get("archived"):
        raise ValueError(f"creator profile is archived: {creator}")
    profile_upload = profile.get("upload", {})
    profile_tags = profile_upload.get("tags", [])
    with copy_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("titles-and-covers.csv has no rows")

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, 1):
        clip_id = (row.get("clip_id") or f"{index:03d}").strip()
        if only and clip_id not in only:
            continue
        if clip_id in seen:
            raise ValueError(f"duplicate clip_id: {clip_id}")
        seen.add(clip_id)
        video = find_video(delivery, clip_id, row.get("video", ""), copy_path.parent)
        if video is None or not video.is_file():
            raise FileNotFoundError(f"video not found for clip_id {clip_id}")
        if video.parent.resolve() != delivery:
            raise ValueError(f"video must be directly inside the delivery folder: {video}")
        ass = video.with_suffix(".ass")
        cover = video.with_name(f"{video.stem}-cover.jpg")
        if not ass.is_file():
            raise FileNotFoundError(f"matching ASS not found: {ass.name}")
        if not cover.is_file():
            raise FileNotFoundError(f"matching cover not found: {cover.name}")
        title = normalize_upload_title(
            row.get("title") or row.get("title_a") or ""
        )
        if not title or len(title) > 80:
            raise ValueError(f"{clip_id}: title must contain 1-80 characters")
        content_type = (row.get("content_type") or "").strip().lower()
        is_song = content_type in {"song", "music"} or content_type.startswith("song-")
        song_timing_gate = validate_song_timing_gate(video, ass) if is_song else None
        collection_kind = "song" if is_song else "narrative"
        profile_collections = profile_upload.get("collections", {})
        default_collection = profile_collections.get(collection_kind, {})
        if not isinstance(default_collection, dict):
            raise ValueError(f"{clip_id}: invalid {collection_kind} collection profile")
        row_collection = (row.get("collection") or "").strip()
        row_season_id = str(row.get("season_id") or "").strip()
        row_section_id = str(row.get("section_id") or "").strip()
        collection_name = (
            row_collection
            or str(default_collection.get("title") or "").strip()
            or default_collection_title(profile, collection_kind)
        )
        raw_season_id = str(
            row_season_id or default_collection.get("season_id") or ""
        ).strip()
        if raw_season_id:
            if not raw_season_id.isdigit() or int(raw_season_id) <= 0:
                raise ValueError(f"{clip_id}: season_id must be a positive integer")
            season_id = int(raw_season_id)
        else:
            season_id = None
        raw_section_id = str(
            row_section_id or default_collection.get("section_id") or ""
        ).strip()
        if raw_section_id:
            if not raw_section_id.isdigit() or int(raw_section_id) <= 0:
                raise ValueError(f"{clip_id}: section_id must be a positive integer")
            section_id = int(raw_section_id)
        else:
            section_id = None
        if (season_id is None) != (section_id is None):
            raise ValueError(
                f"{clip_id}: season_id and section_id must both be set or both be blank"
            )
        raw_tid = (row.get("tid") or "").strip()
        if raw_tid:
            if not raw_tid.isdigit() or int(raw_tid) <= 0:
                raise ValueError(f"{clip_id}: tid must be a positive integer")
            selected_tid = int(raw_tid)
        else:
            selected_tid = song_tid if is_song else tid
        tag_errors = validate_tag_row(row, creator, profile_tags)
        if tag_errors:
            raise ValueError(f"{clip_id}: " + "; ".join(tag_errors))
        content_tags = ["歌切", "音乐"] if is_song else []
        campaign_tags = merge_campaign_tags(
            split_tags(row.get("campaign_tags", ""), []), title
        )
        tags = split_tags(
            row.get("tags", "") or default_tags,
            [*profile_tags, *campaign_tags, *content_tags],
        )
        if not tags:
            raise ValueError(f"{clip_id}: at least one tag is required")
        selected.append({
            "clip_id": clip_id,
            "video": video,
            "ass": ass,
            "cover": cover,
            "title": title,
            "content_type": content_type or "fallback",
            "description": compose_description(
                profile_upload.get("description", ""),
                row.get("description", ""),
                default_description,
            ),
            "tags": tags,
            "tid": selected_tid,
            "copyright": copyright_value,
            "source": source.strip(),
            "no_reprint": no_reprint,
            "collection": collection_name,
            "season_id": season_id,
            "section_id": section_id,
            "collection_kind": collection_kind,
            "collection_explicit": bool(
                row_collection or row_season_id or row_section_id
            ),
            "song_timing_gate": song_timing_gate,
        })
    if only:
        missing = sorted(only - {item["clip_id"] for item in selected})
        if missing:
            raise ValueError(f"requested clip_id values were not found: {', '.join(missing)}")
    if not selected:
        raise ValueError("no clips selected")
    return selected


def public_plan(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "clip_id": item["clip_id"],
        "video": item["video"].name,
        "cover": item["cover"].name,
        "title": item["title"],
        "content_type": item["content_type"],
        "tid": item["tid"],
        "copyright": item["copyright"],
        "source": item["source"],
        "collection": item["collection"],
        "season_id": item["season_id"],
        "section_id": item["section_id"],
        "tags": item["tags"],
        "song_timing_gate": item.get("song_timing_gate"),
    } for item in items]


def require_collection_routing(items: Sequence[dict[str, Any]]) -> None:
    missing = [
        item["clip_id"]
        for item in items
        if (
            not item.get("collection")
            or not item.get("season_id")
            or not item.get("section_id")
        )
    ]
    if missing:
        raise ValueError(
            "execution blocked; missing collection name, season_id, or section_id for: "
            + ", ".join(missing)
        )


def build_upload_command(
    binary: str,
    cookie_path: Path,
    item: dict[str, Any],
    limit: int,
    line: str = "",
    submit: str = "web",
) -> list[str]:
    command = [
        binary, "-u", str(cookie_path), "upload", str(item["video"]),
        "--submit", submit,
        "--limit", str(limit),
        "--copyright", str(item["copyright"]),
        "--source", item["source"],
        "--tid", str(item["tid"]),
        "--cover", str(item["cover"]),
        "--title", item["title"],
        "--desc", item["description"],
        "--tag", ",".join(item["tags"]),
        "--no-reprint", str(item["no_reprint"]),
    ]
    if item.get("season_id"):
        command.extend([
            "--extra-fields",
            json.dumps(
                {"season_id": item["season_id"]},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ])
    if line:
        command.extend(["--line", line])
    return command


def build_append_command(
    binary: str,
    cookie_path: Path,
    item: dict[str, Any],
    bvid: str,
    limit: int,
    line: str = "",
    submit: str = "web",
) -> list[str]:
    """Upload a revision as a temporary extra part without removing the old media."""
    command = [
        binary, "-u", str(cookie_path), "append", "--vid", bvid, str(item["video"]),
        "--submit", submit,
        "--limit", str(limit),
        "--copyright", str(item["copyright"]),
        "--source", item["source"],
        "--tid", str(item["tid"]),
        "--cover", str(item["cover"]),
        "--title", item["title"],
        "--desc", item["description"],
        "--tag", ",".join(item["tags"]),
        "--no-reprint", str(item["no_reprint"]),
    ]
    if line:
        command.extend(["--line", line])
    return command


BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")


def extract_bvid(output: str) -> str:
    matches = BVID_RE.findall(output)
    if not matches:
        raise RuntimeError(
            "upload succeeded but no BV id was returned; collection verification stopped"
        )
    return matches[-1]


def load_collection_credentials(path: Path) -> tuple[str, str]:
    validate_cookie_json(path)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    values = {
        item["name"]: item["value"]
        for item in payload["cookie_info"]["cookies"]
        if item.get("name") and item.get("value")
    }
    cookie_header = "; ".join(f"{name}={value}" for name, value in values.items())
    return cookie_header, values["bili_jct"]


def bilibili_json(
    url: str,
    cookie_header: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://member.bilibili.com/platform/upload-manager/ep",
        "Origin": "https://member.bilibili.com",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=UTF-8"
    try:
        request = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Bilibili collection request failed: {exc}") from exc
    if result.get("code") != 0:
        raise RuntimeError(
            f"Bilibili collection request failed: code={result.get('code')} "
            f"message={result.get('message', '')}"
        )
    return result


def bilibili_form(
    url: str,
    cookie_header: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://member.bilibili.com/platform/upload-manager/ep",
        "Origin": "https://member.bilibili.com",
        "Cookie": cookie_header,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    body = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        request = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Bilibili collection request failed: {exc}") from exc
    if result.get("code") != 0:
        raise RuntimeError(
            f"Bilibili collection request failed: code={result.get('code')} "
            f"message={result.get('message', '')}"
        )
    return result


def list_creator_collections(cookie_header: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page_size = 30
    for page in range(1, 101):
        query = urllib.parse.urlencode({
            "pn": page,
            "ps": page_size,
            "order": "ctime",
            "sort": "desc",
        })
        data = bilibili_json(
            f"https://member.bilibili.com/x2/creative/web/seasons?{query}",
            cookie_header,
        ).get("data") or {}
        page_rows = list(data.get("seasons") or [])
        rows.extend(item for item in page_rows if isinstance(item, dict))
        total = int(data.get("total") or len(rows))
        if not page_rows or len(rows) >= total:
            break
    return rows


def collection_route(row: dict[str, Any]) -> dict[str, Any] | None:
    season = row.get("season") or {}
    if not isinstance(season, dict):
        return None
    season_id = int(season.get("id") or 0)
    title = str(season.get("title") or "").strip()
    sections_value = row.get("sections") or {}
    if isinstance(sections_value, dict):
        sections = sections_value.get("sections") or []
    elif isinstance(sections_value, list):
        sections = sections_value
    else:
        sections = []
    valid_sections = [
        section
        for section in sections
        if isinstance(section, dict)
        and int(section.get("id") or 0) > 0
        and int(section.get("seasonId") or season_id) == season_id
    ]
    if season_id <= 0 or not title or not valid_sections:
        return None
    section = min(valid_sections, key=lambda item: int(item.get("order") or 1))
    return {
        "title": title,
        "season_id": season_id,
        "section_id": int(section["id"]),
    }


def resolve_collection_by_title(
    title: str,
    cookie_header: str,
) -> dict[str, Any] | None:
    exact_rows = [
        row
        for row in list_creator_collections(cookie_header)
        if str((row.get("season") or {}).get("title") or "").strip() == title.strip()
    ]
    if len(exact_rows) > 1:
        raise RuntimeError(
            f"found multiple Bilibili collections named {title}; resolve duplicates manually"
        )
    if not exact_rows:
        return None
    route = collection_route(exact_rows[0])
    if route is None:
        raise RuntimeError(
            f"Bilibili collection {title} exists but has no usable section yet"
        )
    return route


def upload_collection_cover(
    cover_path: Path,
    cookie_header: str,
    csrf: str,
) -> str:
    cover = cover_path.resolve()
    if not cover.is_file():
        raise FileNotFoundError(f"collection cover not found: {cover}")
    if cover.stat().st_size <= 0 or cover.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("collection cover must be a non-empty image no larger than 20 MB")
    suffix = cover.suffix.casefold()
    if suffix not in {".jpg", ".jpeg", ".png"}:
        raise ValueError("collection cover must be JPG or PNG")
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    encoded = base64.b64encode(cover.read_bytes()).decode("ascii")
    query = urllib.parse.urlencode({"ts": int(time.time() * 1000)})
    result = bilibili_form(
        f"https://member.bilibili.com/x/vu/web/cover/up?{query}",
        cookie_header,
        {"csrf": csrf, "cover": f"data:{mime};base64,{encoded}"},
    )
    url = str((result.get("data") or {}).get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("Bilibili cover upload returned no usable URL")
    return url


def create_or_resolve_collection(
    collection_cookie_path: Path,
    title: str,
    cover_path: Path,
    description: str = "",
) -> dict[str, Any]:
    """Resolve an exact existing title, otherwise create once and read back its section."""
    cookie_header, csrf = load_collection_credentials(collection_cookie_path)
    existing = resolve_collection_by_title(title, cookie_header)
    if existing is not None:
        return existing
    cover_url = upload_collection_cover(cover_path, cookie_header, csrf)
    result = bilibili_form(
        "https://member.bilibili.com/x2/creative/web/season/add",
        cookie_header,
        {
            "title": title,
            "desc": description.strip(),
            "cover": cover_url,
            "season_price": 0,
            "csrf": csrf,
        },
    )
    created_season_id = int(result.get("data") or 0)
    if created_season_id <= 0:
        raise RuntimeError("Bilibili collection creation returned no season id")
    for attempt in range(8):
        rows = list_creator_collections(cookie_header)
        for row in rows:
            route = collection_route(row)
            if route and route["season_id"] == created_season_id:
                if route["title"] != title:
                    raise RuntimeError("created collection title did not match the request")
                return route
        if attempt < 7:
            time.sleep(2)
    raise RuntimeError(
        f"collection {title} was created as season {created_season_id}, "
        "but its section is not ready; retry upload later and it will be reused"
    )


def prepare_creator_collections(
    creator: str,
    items: Sequence[dict[str, Any]],
    collection_cookie_path: Path,
    *,
    profiles_path: Path = PROFILE_PATH,
) -> dict[str, dict[str, Any]]:
    """Ensure narrative first, then any additional collection required by this batch."""
    if not items:
        raise ValueError("no upload items available for collection preparation")
    payload = json.loads(profiles_path.read_text(encoding="utf-8-sig"))
    profiles = payload.get("profiles") or {}
    if creator not in profiles:
        raise ValueError(f"unknown creator profile: {creator}")
    profile = profiles[creator]
    if profile.get("archived"):
        raise ValueError(f"creator profile is archived: {creator}")
    upload = profile.get("upload") or {}
    collections = upload.get("collections") or {}
    requested_kinds = {
        str(item.get("collection_kind") or "narrative") for item in items
    }
    ordered_kinds = ["narrative"]
    if "song" in requested_kinds:
        ordered_kinds.append("song")
    narrative_cover = next(
        (
            Path(item["cover"])
            for item in items
            if item.get("collection_kind") == "narrative"
        ),
        Path(items[0]["cover"]),
    )
    routes: dict[str, dict[str, Any]] = {}
    for kind in ordered_kinds:
        configured = collections.get(kind) or {}
        title = str(configured.get("title") or "").strip()
        if not title:
            title = default_collection_title(profile, kind)
        season_id = int(configured.get("season_id") or 0)
        section_id = int(configured.get("section_id") or 0)
        if season_id > 0 and section_id > 0:
            route = {
                "title": title,
                "season_id": season_id,
                "section_id": section_id,
            }
        else:
            cover = narrative_cover
            if kind == "song":
                cover = next(
                    (
                        Path(item["cover"])
                        for item in items
                        if item.get("collection_kind") == "song"
                    ),
                    narrative_cover,
                )
            route = create_or_resolve_collection(
                collection_cookie_path,
                title,
                cover,
                f"{profile.get('display_name', title)}直播切片合集",
            )
            persist_profile_collection(
                creator,
                kind,
                route["title"],
                int(route["season_id"]),
                int(route["section_id"]),
                profiles_path=profiles_path,
            )
        routes[kind] = route

    for item in items:
        if item.get("collection_explicit"):
            if item.get("season_id") and item.get("section_id"):
                continue
            title = str(item.get("collection") or "").strip()
            if not title:
                raise ValueError(
                    f"{item.get('clip_id', 'unknown')}: explicit collection title is empty"
                )
            route = create_or_resolve_collection(
                collection_cookie_path,
                title,
                Path(item["cover"]),
                f"{profile.get('display_name', title)}直播切片合集",
            )
            item["collection"] = route["title"]
            item["season_id"] = int(route["season_id"])
            item["section_id"] = int(route["section_id"])
            continue
        kind = str(item.get("collection_kind") or "narrative")
        route = routes[kind]
        item["collection"] = route["title"]
        item["season_id"] = int(route["season_id"])
        item["section_id"] = int(route["section_id"])
    return routes


def find_recent_bvid_by_title(
    collection_cookie_path: Path,
    expected_title: str,
    not_before: int,
) -> str:
    cookie_header, _ = load_collection_credentials(collection_cookie_path)
    query = urllib.parse.urlencode({
        "status": "pubed,pubing,not_pubed",
        "pn": 1,
        "ps": 20,
        "coop": 1,
        "interactive": 1,
    })
    for attempt in range(8):
        data = bilibili_json(
            f"https://member.bilibili.com/x/web/archives?{query}",
            cookie_header,
        )["data"]
        matches: list[tuple[int, str]] = []
        for key in ("arc_audits", "archives"):
            for item in data.get(key) or []:
                archive = item.get("Archive", item)
                if (
                    archive.get("title") == expected_title
                    and int(archive.get("ctime") or 0) >= not_before - 120
                    and archive.get("bvid")
                ):
                    matches.append(
                        (int(archive.get("ctime") or 0), str(archive["bvid"]))
                    )
        if matches:
            return max(matches)[1]
        if attempt < 7:
            time.sleep(3)
    raise RuntimeError(
        "upload succeeded but no matching recent BV id could be recovered"
    )


def get_creator_archive(
    bvid: str,
    cookie_header: str,
) -> dict[str, Any]:
    query = urllib.parse.urlencode({"bvid": bvid})
    data = bilibili_json(
        f"https://member.bilibili.com/x/vupre/web/archive/view?{query}",
        cookie_header,
    ).get("data") or {}
    archive = data.get("archive") or {}
    videos = data.get("videos") or []
    if not archive.get("aid") or not videos:
        raise RuntimeError(f"archive metadata incomplete for {bvid}")
    return {"archive": archive, "videos": videos}


def get_archive_info(
    bvid: str,
    cookie_header: str = "",
) -> dict[str, Any]:
    query = urllib.parse.urlencode({"bvid": bvid})
    if cookie_header:
        data = get_creator_archive(bvid, cookie_header)
        archive = data.get("archive") or {}
        videos = data.get("videos") or []
        cid = videos[0].get("cid") if videos else None
        if not archive.get("aid") or not cid:
            raise RuntimeError(f"archive metadata incomplete for {bvid}")
        return {
            "aid": int(archive["aid"]),
            "cid": int(cid),
            "title": str(archive["title"]),
        }

    data = bilibili_json(
        f"https://api.bilibili.com/x/web-interface/view?{query}"
    )["data"]
    pages = data.get("pages") or []
    cid = data.get("cid") or (pages[0].get("cid") if pages else None)
    if not data.get("aid") or not cid:
        raise RuntimeError(f"archive metadata incomplete for {bvid}")
    return {
        "aid": int(data["aid"]),
        "cid": int(cid),
        "title": str(data["title"]),
    }

def get_section_episodes(
    section_id: int,
    cookie_header: str,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"id": section_id})
    result = bilibili_json(
        f"https://member.bilibili.com/x2/creative/web/season/section?{query}",
        cookie_header,
    )
    return list(result["data"].get("episodes") or [])


def ensure_collection_membership(
    collection_cookie_path: Path,
    section_id: int,
    bvid: str,
    expected_title: str,
) -> None:
    cookie_header, csrf = load_collection_credentials(collection_cookie_path)
    archive = get_archive_info(bvid, cookie_header)
    if archive["title"] != expected_title:
        raise RuntimeError(
            f"archive title mismatch for {bvid}; "
            "refusing to add the wrong video to collection"
        )
    episodes = get_section_episodes(section_id, cookie_header)
    if not any(int(item.get("aid") or 0) == archive["aid"] for item in episodes):
        query = urllib.parse.urlencode({"csrf": csrf})
        bilibili_json(
            "https://member.bilibili.com/x2/creative/web/"
            f"season/section/episodes/add?{query}",
            cookie_header,
            {
                "sectionId": section_id,
                "episodes": [{
                    "aid": archive["aid"],
                    "cid": archive["cid"],
                    "title": archive["title"],
                    "charging_pay": 0,
                }],
                "csrf": csrf,
            },
        )
    for attempt in range(3):
        episodes = get_section_episodes(section_id, cookie_header)
        if any(int(item.get("aid") or 0) == archive["aid"] for item in episodes):
            return
        if attempt < 2:
            time.sleep(1)
    raise RuntimeError(
        f"collection verification failed for {bvid} in section {section_id}"
    )

def check_biliup_cli(binary: str) -> str:
    version = subprocess.run(
        [binary, "--version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_subprocess_kwargs(),
    )
    if version.returncode != 0:
        raise RuntimeError("biliup --version failed")
    help_result = subprocess.run(
        [binary, "upload", "--help"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_subprocess_kwargs(),
    )
    help_text = f"{help_result.stdout}\n{help_result.stderr}"
    if help_result.returncode != 0:
        raise RuntimeError("biliup upload --help failed")
    missing = sorted(flag for flag in REQUIRED_UPLOAD_FLAGS if flag not in help_text)
    if missing:
        raise RuntimeError(
            "installed biliup is incompatible; missing upload flags: " + ", ".join(missing)
        )
    return version.stdout.strip() or version.stderr.strip()


def check_biliup_append_cli(binary: str) -> None:
    result = subprocess.run(
        [binary, "append", "--help"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_subprocess_kwargs(),
    )
    help_text = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or "--vid" not in help_text:
        raise RuntimeError(
            "installed biliup is incompatible; append --vid is required for safe replacement"
        )


def expected_confirmation(count: int) -> str:
    return f"发布 {count} 条"


def confirm_execution(count: int, supplied: str) -> None:
    expected = expected_confirmation(count)
    if supplied == expected:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(f"execution blocked; pass --confirm \"{expected}\"")
    entered = input(f"即将分别投稿 {count} 条视频。输入“{expected}”继续：").strip()
    if entered != expected:
        raise RuntimeError("confirmation did not match; nothing was uploaded")


def _archive_rows(collection_cookie_path: Path) -> list[dict[str, Any]]:
    cookie_header, _ = load_collection_credentials(collection_cookie_path)
    query = urllib.parse.urlencode({
        "status": "pubed,pubing,not_pubed",
        "pn": 1,
        "ps": 50,
        "coop": 1,
        "interactive": 1,
    })
    data = bilibili_json(
        f"https://member.bilibili.com/x/web/archives?{query}", cookie_header
    )["data"]
    rows: list[dict[str, Any]] = []
    for key in ("arc_audits", "archives"):
        rows.extend(
            item.get("Archive", item) for item in (data.get(key) or [])
            if isinstance(item, dict)
        )
    return rows


def find_existing_bvid_by_title(
    collection_cookie_path: Path, expected_title: str
) -> str | None:
    matches = [
        (int(row.get("ctime") or 0), str(row["bvid"]))
        for row in _archive_rows(collection_cookie_path)
        if row.get("title") == expected_title and row.get("bvid")
    ]
    return max(matches)[1] if matches else None


def _receipt_file(items: Sequence[dict[str, Any]]) -> Path:
    if not items:
        raise ValueError("cannot create upload receipt for an empty batch")
    return Path(items[0]["video"]).resolve().parent / "publish-receipts.json"


def _load_receipts(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else {"items": {}}
    except (OSError, json.JSONDecodeError):
        return {"schema": "target-song-cutter.publish-receipts.v1", "items": {}}


def _save_receipt(
    path: Path, receipts: dict[str, Any], item: dict[str, Any], bvid: str
) -> None:
    receipts.setdefault("schema", "target-song-cutter.publish-receipts.v1")
    receipts.setdefault("items", {})[str(item["clip_id"])] = {
        "title": item["title"],
        "bvid": bvid,
        "collection": item.get("collection", ""),
        "completed_at": int(time.time()),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _atomic_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


FLOW5_RATE_LIMIT_MESSAGES = {
    "601": "您上传视频过快",
    "137022": "投稿过于频繁，请稍后再试",
}


def upload_rate_limit_code(output: str) -> str:
    """Recognize upload and archive-submission limits reported by biliup."""
    codes = re.findall(
        r"[\"']?code[\"']?\s*[:=]\s*(601|137022)\b", output, re.IGNORECASE,
    )
    if codes:
        return codes[-1]
    if "投稿过于频繁" in output:
        return "137022"
    if re.search(r"upload rate limit|您上传视频过快", output, re.IGNORECASE):
        return "601"
    return ""


def is_upload_rate_limited(output: str) -> bool:
    return bool(upload_rate_limit_code(output))


def _load_flow5_throttle(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict) or payload.get("schema") != FLOW5_THROTTLE_SCHEMA:
        payload = {}
    return {
        **payload,
        "schema": FLOW5_THROTTLE_SCHEMA,
        "batch_size": FLOW5_UPLOAD_BATCH_SIZE,
        "cooldown_seconds": FLOW5_UPLOAD_COOLDOWN_SECONDS,
        "completed_in_window": max(
            0, int(payload.get("completed_in_window") or 0)
        ),
        "cooldown_until": max(0, int(payload.get("cooldown_until") or 0)),
    }


def _save_flow5_throttle(path: Path, payload: dict[str, Any]) -> None:
    _atomic_json_file(path.expanduser().resolve(), payload)


def emit_flow5_progress(phase: str, detail: str, **values: Any) -> None:
    print(PROGRESS_PREFIX + json.dumps({"phase": phase, "detail": detail, **values}, ensure_ascii=False), flush=True)


def _flow5_state_lock(path: Path):
    return exclusive_file_lock(path.with_name(path.name + ".state.lock"))


def _serialize_flow5_call(function):
    """Include receipt lookup and writes in the account's publication lock."""
    signature = inspect.signature(function)

    @wraps(function)
    def serialized(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs).arguments
        path = Path(arguments.get("flow5_throttle_path") or Path(arguments["cookie_path"]).with_name("flow5-upload-throttle.json")).expanduser().resolve()

        def waiting():
            detail = "另一项任务正在投稿或等待冷却；当前任务等待共享发布锁，结束后按队列继续。"
            print(detail, flush=True)
            emit_flow5_progress("waiting_lock", detail)

        with exclusive_file_lock(path.with_name(path.name + ".publish.lock"), on_wait=waiting):
            return function(*args, **kwargs)

    return serialized


def wait_for_flow5_upload_slot(
    path: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Wait out a batch cooldown or a server-imposed limit across projects."""
    throttle_path = path.expanduser().resolve()
    while True:
        with _flow5_state_lock(throttle_path):
            state = _load_flow5_throttle(throttle_path)
            current = float(now())
            completed = int(state.get("completed_in_window") or 0)
            cooldown_until = int(state.get("cooldown_until") or 0)
            if cooldown_until <= current:
                if completed >= FLOW5_UPLOAD_BATCH_SIZE or cooldown_until:
                    state["completed_in_window"] = 0
                    state["cooldown_until"] = 0
                    state.pop("cooldown_reason", None)
                    state["last_cooldown_completed_at"] = int(current)
                    _save_flow5_throttle(throttle_path, state)
                return state
        remaining = max(1, int(math.ceil(cooldown_until - current)))
        minutes, seconds = divmod(remaining, 60)
        code = str(state.get("cooldown_reason", "")).removeprefix("rate_limit_")
        reason = (
            f"B站限制投稿频率（{code}），本地退避等待；"
            if code in FLOW5_RATE_LIMIT_MESSAGES
            else "Flow 5 已连续投稿 5 条，进入 10 分钟冷却；"
        )
        detail = reason + f"剩余 {minutes:02d}:{seconds:02d}，结束后再次尝试。"
        if code in FLOW5_RATE_LIMIT_MESSAGES:
            detail += "平台未提供解限时间，本地等待结束不代表已解限。"
        print(detail, flush=True)
        emit_flow5_progress(
            "cooldown", detail,
            code=code if code in FLOW5_RATE_LIMIT_MESSAGES else "",
            cooldown_until=cooldown_until,
        )
        sleep(remaining)
        # Re-read under the lock: another process may have extended the deadline
        # while we slept. Never clear a newer cooldown from an old snapshot.


def record_flow5_upload_rate_limit(
    path: Path,
    *,
    clip_id: str,
    code: str = "601",
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Persist progressively longer backoff for unresolved server rejections."""
    if code not in FLOW5_RATE_LIMIT_MESSAGES:
        raise ValueError(f"unsupported upload rate limit code: {code}")
    throttle_path = path.expanduser().resolve()
    with _flow5_state_lock(throttle_path):
        state = _load_flow5_throttle(throttle_path)
        current = int(now())
        previous = int(state.get("consecutive_rate_limits") or 0)
        # Import unresolved legacy rejections without resetting across tasks.
        if "consecutive_rate_limits" not in state and int(state.get("last_rate_limit_at") or 0) > int(state.get("last_upload_at") or 0):
            previous = 1
        count = previous + 1
        backoff = min(
            FLOW5_UPLOAD_COOLDOWN_SECONDS * (2 ** min(count - 1, 3)),
            FLOW5_RATE_LIMIT_MAX_COOLDOWN_SECONDS,
        )
        state.update({
            "cooldown_reason": f"rate_limit_{code}",
            "cooldown_started_at": current,
            "cooldown_until": max(int(state.get("cooldown_until") or 0), current + backoff),
            "consecutive_rate_limits": count,
            "rate_limit_backoff_seconds": backoff,
            "last_rate_limit_at": current,
            "last_rate_limit_code": code,
            "last_rate_limited_clip_id": str(clip_id),
        })
        _save_flow5_throttle(throttle_path, state)
        return state


def record_flow5_upload_success(
    path: Path,
    *,
    clip_id: str,
    bvid: str,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Persist accepted archives without discarding an in-flight newer cooldown."""
    throttle_path = path.expanduser().resolve()
    with _flow5_state_lock(throttle_path):
        state = _load_flow5_throttle(throttle_path)
        current = int(now())
        completed = int(state.get("completed_in_window") or 0)
        cooldown_until = int(state.get("cooldown_until") or 0)
        if cooldown_until and cooldown_until <= current:
            completed = 0
            state["cooldown_until"] = 0
            state.pop("cooldown_reason", None)
        completed += 1
        state.update({
            "completed_in_window": completed,
            "last_upload_at": current,
            "last_clip_id": str(clip_id),
            "last_bvid": str(bvid),
            "consecutive_rate_limits": 0,
            "rate_limit_backoff_seconds": 0,
        })
        # A legacy in-flight publisher may finish after a newer process set
        # cooldown. Retain that deadline and the accepted BV, without raising.
        if completed >= FLOW5_UPLOAD_BATCH_SIZE:
            state["cooldown_started_at"] = current
            state["cooldown_until"] = max(cooldown_until, current + FLOW5_UPLOAD_COOLDOWN_SECONDS)
        _save_flow5_throttle(throttle_path, state)
        return state


def load_replacement_targets(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    raw_items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw_items, dict):
        raise ValueError("旧投稿回执缺少 items 映射")
    targets: dict[str, dict[str, Any]] = {}
    for clip_id, row in raw_items.items():
        if not isinstance(row, dict):
            continue
        bvid = str(row.get("bvid") or "").strip()
        if bvid and BVID_RE.fullmatch(bvid):
            targets[str(clip_id)] = row
    if not targets:
        raise ValueError("旧投稿回执没有可用的 BV 号")
    return targets


def replacement_plan(
    items: Sequence[dict[str, Any]],
    targets: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    missing: list[str] = []
    used_bvids: set[str] = set()
    for item in items:
        clip_id = str(item["clip_id"])
        target = targets.get(clip_id)
        if not target:
            missing.append(clip_id)
            continue
        bvid = str(target.get("bvid") or "").strip()
        if not BVID_RE.fullmatch(bvid):
            missing.append(clip_id)
            continue
        if bvid in used_bvids:
            raise ValueError(f"旧投稿回执把多个切片映射到同一稿件：{bvid}")
        used_bvids.add(bvid)
        plan.append({
            "item": item,
            "bvid": bvid,
            "previous_title": str(target.get("title") or ""),
        })
    if missing:
        raise ValueError(
            "以下新切片无法按 clip_id 对应到旧投稿，已停止自动换源："
            + ", ".join(missing)
        )
    return plan


def public_replacement_plan(plan: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "clip_id": row["item"]["clip_id"],
        "bvid": row["bvid"],
        "previous_title": row["previous_title"],
        "new_title": row["item"]["title"],
        "video": Path(row["item"]["video"]).name,
    } for row in plan]


def expected_replacement_confirmation(count: int) -> str:
    return f"替换 {count} 条"


def confirm_replacement(count: int, supplied: str) -> None:
    expected = expected_replacement_confirmation(count)
    if supplied == expected:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(f'execution blocked; pass --confirm "{expected}"')
    entered = input(
        f"即将换源 {count} 个已发布稿件并保留原 BV 号。输入“{expected}”继续："
    ).strip()
    if entered != expected:
        raise RuntimeError("confirmation did not match; no published media was changed")


def _video_identity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "cid": int(row.get("cid") or 0),
        "filename": str(row.get("filename") or ""),
    }


def _video_identity_key(row: dict[str, Any]) -> tuple[int, str]:
    identity = _video_identity(row)
    return identity["cid"], identity["filename"]


def _source_signature(item: dict[str, Any]) -> dict[str, Any]:
    video = Path(item["video"]).resolve()
    stat_result = video.stat()
    return {
        "path": str(video),
        "size": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
    }


def build_replacement_edit_payload(
    archive_data: dict[str, Any],
    item: dict[str, Any],
    new_video: dict[str, Any],
    cover_url: str = "",
) -> dict[str, Any]:
    archive = archive_data.get("archive") or {}
    if not archive.get("aid") or not new_video.get("filename"):
        raise RuntimeError("换源元数据不完整，拒绝提交删除旧分 P 的编辑请求")
    mission_id = int(archive.get("mission_id") or 0) or None
    dtime = int(archive.get("dtime") or 0) or None
    return {
        "copyright": int(item["copyright"]),
        "source": str(item.get("source") or ""),
        "tid": int(item["tid"]),
        "cover": str(cover_url or archive.get("cover") or ""),
        "title": str(item["title"]),
        "desc_format_id": int(archive.get("desc_format_id") or 0),
        "desc": str(item.get("description") or ""),
        "desc_v2": archive.get("desc_v2"),
        "dynamic": str(archive.get("dynamic") or ""),
        "subtitle": {"open": 0, "lan": ""},
        "tag": ",".join(item.get("tags") or []),
        "videos": [{
            "title": str(new_video.get("title") or item["title"]),
            "filename": str(new_video["filename"]),
            "desc": str(new_video.get("desc") or ""),
        }],
        "dtime": dtime,
        "open_subtitle": False,
        "interactive": int(archive.get("interactive") or 0),
        "mission_id": mission_id,
        "dolby": int(archive.get("is_dolby") or 0),
        "lossless_music": int(archive.get("lossless_music") or 0),
        "no_reprint": int(item.get("no_reprint") or 0),
        "is_only_self": int(archive.get("is_only_self") or 0),
        "charging_pay": int(archive.get("charging_pay") or 0),
        "aid": int(archive["aid"]),
        "up_selection_reply": False,
        "up_close_reply": False,
        "up_close_danmu": False,
    }


def submit_replacement_edit(
    cookie_header: str,
    csrf: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    query = urllib.parse.urlencode({
        "t": int(time.time() * 1000),
        "csrf": csrf,
    })
    return bilibili_json(
        f"https://member.bilibili.com/x/vu/web/edit?{query}",
        cookie_header,
        payload,
    )


def _replacement_receipt_file(items: Sequence[dict[str, Any]]) -> Path:
    return Path(items[0]["video"]).resolve().parent / "replacement-receipts.json"


def _replacement_state_file(items: Sequence[dict[str, Any]]) -> Path:
    return Path(items[0]["video"]).resolve().parent / ".replacement-state.json"


@_serialize_flow5_call
def execute_replacements(
    binary: str,
    cookie_path: Path,
    plan: Sequence[dict[str, Any]],
    limit: int,
    line: str,
    submit: str,
    collection_cookie_path: Path,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    get_archive: Callable[[str, str], dict[str, Any]] = get_creator_archive,
    edit_archive: Callable[[str, str, dict[str, Any]], dict[str, Any]] = submit_replacement_edit,
    upload_cover: Callable[[Path, str, str], str] = upload_collection_cover,
    sleep: Callable[[float], None] = time.sleep,
    flow5_throttle_path: Path | None = None,
    now: Callable[[], float] = time.time,
) -> int:
    if not plan:
        raise ValueError("没有可换源的稿件")
    items = [row["item"] for row in plan]
    receipt_path = _replacement_receipt_file(items)
    state_path = _replacement_state_file(items)
    receipts = _load_receipts(receipt_path)
    receipts["schema"] = "target-song-cutter.replacement-receipts.v1"
    journal = _load_receipts(state_path)
    journal["schema"] = "target-song-cutter.replacement-state.v1"
    cookie_header, csrf = load_collection_credentials(collection_cookie_path)
    throttle_path = Path(flow5_throttle_path or cookie_path.with_name("flow5-upload-throttle.json"))

    def current_cover_url(item: dict[str, Any], saved: dict[str, Any]) -> str:
        signature = sha256_file(Path(item["cover"]))
        existing = str(saved.get("cover_url") or "").strip()
        if existing and saved.get("cover_sha256") == signature:
            return existing
        uploaded = str(
            upload_cover(Path(item["cover"]), cookie_header, csrf) or ""
        ).strip()
        if not uploaded.startswith(("http://", "https://")):
            raise RuntimeError("新版封面上传后没有返回可用 URL；旧稿件保持不变")
        saved["cover_url"] = uploaded
        saved["cover_sha256"] = signature
        _atomic_json_file(state_path, journal)
        return uploaded

    for index, row in enumerate(plan, 1):
        wait_for_flow5_upload_slot(throttle_path, sleep=sleep, now=now)
        item = row["item"]
        clip_id = str(item["clip_id"])
        bvid = str(row["bvid"])
        signature = _source_signature(item)
        cover_signature = sha256_file(Path(item["cover"]))
        completed = receipts.get("items", {}).get(clip_id, {})
        completed_video = (
            completed.get("bvid") == bvid
            and completed.get("source_signature") == signature
        )
        if completed_video and completed.get("cover_sha256") == cover_signature:
            print(f"[{index}/{len(plan)}] 已有换源成功回执，跳过：{clip_id} / {bvid}")
            continue

        saved = journal.setdefault("items", {}).get(clip_id, {})
        if completed_video:
            # A repaired cover needs a metadata update, not another video append.
            # Legacy receipts without an image hash are refreshed once as well.
            saved = {**completed, "phase": "edit_submitted"}
            journal["items"][clip_id] = saved
            _atomic_json_file(state_path, journal)
        phase = str(saved.get("phase") or "")
        if phase in {"appended", "edit_submitted"}:
            if saved.get("bvid") != bvid or saved.get("source_signature") != signature:
                raise RuntimeError(
                    f"{clip_id}: 检测到未完成换源，但本地素材或目标 BV 已变化；"
                    "请先人工核对线上分 P"
                )
            current = get_archive(bvid, cookie_header)
            expected_new = saved.get("new_video") or {}
            current_matches = [
                video for video in current.get("videos") or []
                if _video_identity_key(video) == _video_identity_key(expected_new)
            ]
            if len(current_matches) != 1:
                raise RuntimeError(
                    f"{clip_id}: 无法在稿件 {bvid} 找到上次已上传的新版分 P，"
                    "旧素材仍被保留，请人工核对"
                )
            new_video = current_matches[0]
            # Reapply current metadata even if a previous attempt already removed
            # the old video. Its cover may have been repaired since that attempt.
            cover_url = current_cover_url(item, saved)
            payload = build_replacement_edit_payload(current, item, new_video, cover_url)
            edit_archive(cookie_header, csrf, payload)
            saved["phase"] = "edit_submitted"
            _atomic_json_file(state_path, journal)
            verified = {}
        else:
            before = get_archive(bvid, cookie_header)
            old_videos = list(before.get("videos") or [])
            if len(old_videos) != 1:
                raise RuntimeError(
                    f"{clip_id}: 稿件 {bvid} 当前有 {len(old_videos)} 个分 P；"
                    "为避免误删，只允许自动替换原本为单 P 的稿件"
                )
            saved = {
                "phase": "uploading",
                "bvid": bvid,
                "source_signature": signature,
                "old_videos": [_video_identity(video) for video in old_videos],
                "started_at": int(time.time()),
            }
            journal.setdefault("items", {})[clip_id] = saved
            _atomic_json_file(state_path, journal)

            kwargs: dict[str, Any] = {
                "check": False,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
            if run is subprocess.run:
                kwargs.update(hidden_subprocess_kwargs())
            result = run(
                build_append_command(
                    binary, cookie_path, item, bvid, limit, line, submit
                ),
                **kwargs,
            )
            output = (
                f"{getattr(result, 'stdout', '') or ''}\n"
                f"{getattr(result, 'stderr', '') or ''}"
            )
            if output.strip():
                print(output.rstrip())

            rate_limit_code = upload_rate_limit_code(output)
            if result.returncode != 0 and rate_limit_code:
                record_flow5_upload_rate_limit(
                    throttle_path, clip_id=clip_id, code=rate_limit_code, now=now,
                )
            after = get_archive(bvid, cookie_header)
            old_keys = {_video_identity_key(video) for video in old_videos}
            added = [
                video for video in (after.get("videos") or [])
                if _video_identity_key(video) not in old_keys
            ]
            if len(added) != 1:
                if result.returncode != 0:
                    reason = publish_error_summary(output)["detail"]
                    raise RuntimeError(
                        f"{clip_id}: 新版上传失败（退出码 {result.returncode}）；"
                        f"{reason + '；' if reason else ''}旧稿件未被删除"
                    )
                raise RuntimeError(
                    f"{clip_id}: 上传后无法唯一识别新版分 P；旧稿件仍被保留"
                )
            new_video = added[0]
            saved["phase"] = "appended"
            saved["new_video"] = _video_identity(new_video)
            _atomic_json_file(state_path, journal)

            cover_url = current_cover_url(item, saved)
            payload = build_replacement_edit_payload(
                after, item, new_video, cover_url
            )
            edit_archive(cookie_header, csrf, payload)
            saved["phase"] = "edit_submitted"
            _atomic_json_file(state_path, journal)
            verified = {}

        if not verified:
            for attempt in range(4):
                current = get_archive(bvid, cookie_header)
                videos = list(current.get("videos") or [])
                if (
                    len(videos) == 1
                    and _video_identity_key(videos[0]) == _video_identity_key(new_video)
                ):
                    verified = current
                    break
                if attempt < 3:
                    sleep(1)
            if not verified:
                raise RuntimeError(
                    f"{clip_id}: B站已接收编辑请求，但尚未回读到唯一新版分 P；"
                    "旧素材可能仍作为额外分 P 保留，请稍后重试"
                )

        receipts.setdefault("items", {})[clip_id] = {
            "title": item["title"],
            "bvid": bvid,
            "old_videos": saved.get("old_videos") or [],
            "new_video": _video_identity(new_video),
            "source_signature": signature,
            "cover_url": str(saved.get("cover_url") or ""),
            "cover_sha256": str(saved.get("cover_sha256") or ""),
            "tags": list(item.get("tags") or []),
            "completed_at": int(time.time()),
        }
        _atomic_json_file(receipt_path, receipts)
        saved["phase"] = "completed"
        saved["completed_at"] = int(time.time())
        _atomic_json_file(state_path, journal)
        print(f"[{index}/{len(plan)}] 换源完成并回读确认：{clip_id} / {bvid}")

    return 0


@_serialize_flow5_call
def execute_uploads(
    binary: str,
    cookie_path: Path,
    items: Sequence[dict[str, Any]],
    limit: int,
    line: str,
    cooldown: int,
    submit: str = "web",
    collection_cookie_path: Path | None = None,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    flow5_throttle_path: Path | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> int:
    if collection_cookie_path is None:
        raise RuntimeError("collection cookie path is required for upload recovery")
    receipt_path = _receipt_file(items)
    receipts = _load_receipts(receipt_path)
    throttle_path = (
        flow5_throttle_path.expanduser().resolve()
        if flow5_throttle_path is not None
        else cookie_path.expanduser().resolve().with_name("flow5-upload-throttle.json")
    )
    sleep_fn = sleep or time.sleep
    now_fn = now or time.time
    inter_item_delay = max(8, int(cooldown or 0))
    retry_delay = max(10, int(cooldown or 0))
    for index, item in enumerate(items, 1):
        clip_id = str(item["clip_id"])
        recorded = receipts.get("items", {}).get(clip_id, {})
        bvid = ""
        if recorded.get("title") == item["title"] and recorded.get("bvid"):
            bvid = str(recorded["bvid"])
            print(f"[{index}/{len(items)}] 已有成功回执，跳过重复投稿：{clip_id} / {bvid}")
        else:
            existing = find_existing_bvid_by_title(
                collection_cookie_path, item["title"]
            )
            if existing:
                bvid = existing
                print(f"[{index}/{len(items)}] B站已存在同标题稿件，跳过重复投稿：{clip_id} / {bvid}")
        submitted_now = False
        if not bvid:
            for attempt in range(1, 4):
                wait_for_flow5_upload_slot(
                    throttle_path,
                    sleep=sleep_fn,
                    now=now_fn,
                )
                detail = f"[{index}/{len(items)}] 正在投稿 {clip_id}：{item['title']}"
                print(detail, flush=True)
                emit_flow5_progress("uploading", detail, attempt=attempt, clip_id=clip_id)
                upload_started = int(now_fn())
                kwargs: dict[str, Any] = {
                    "check": False,
                    "capture_output": True,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                }
                if run is subprocess.run:
                    kwargs.update(hidden_subprocess_kwargs())
                result = run(
                    build_upload_command(binary, cookie_path, item, limit, line, submit),
                    **kwargs,
                )
                output = (
                    f"{getattr(result, 'stdout', '') or ''}\n"
                    f"{getattr(result, 'stderr', '') or ''}"
                )
                if output.strip():
                    print(output.rstrip())
                if result.returncode == 0:
                    try:
                        bvid = extract_bvid(output)
                    except RuntimeError:
                        bvid = find_recent_bvid_by_title(
                            collection_cookie_path, item["title"], upload_started
                        )
                        print(f"已按完全一致标题找回稿件号：{bvid}")
                    break
                # A native biliup process can exit -1 after the server accepted the archive.
                # Query the manager before retrying so a crash never creates a duplicate.
                sleep_fn(2)
                rate_limit_code = upload_rate_limit_code(output)
                try:
                    bvid = find_existing_bvid_by_title(
                        collection_cookie_path, item["title"]
                    ) or ""
                except Exception:
                    # Do not retry an uncertain submission, but keep the server's
                    # cooldown active for every subsequent task in this account.
                    if rate_limit_code:
                        record_flow5_upload_rate_limit(
                            throttle_path, clip_id=clip_id, code=rate_limit_code, now=now_fn,
                        )
                        print(
                            f"Flow 5 投稿受限（B站 {rate_limit_code}）："
                            f"{FLOW5_RATE_LIMIT_MESSAGES[rate_limit_code]}；"
                            "稿件回读也失败，已暂停重试并保留共享冷却。",
                            file=sys.stderr, flush=True,
                        )
                    raise
                if bvid:
                    print(f"biliup 异常退出，但已回读到稿件：{bvid}")
                    break
                if rate_limit_code:
                    throttle = record_flow5_upload_rate_limit(
                        throttle_path, clip_id=clip_id, code=rate_limit_code, now=now_fn,
                    )
                    backoff_minutes = int(throttle["rate_limit_backoff_seconds"]) // 60
                    rate_limit_message = FLOW5_RATE_LIMIT_MESSAGES[rate_limit_code]
                    if attempt < 3:
                        print(
                            f"B站返回 {rate_limit_code}（{rate_limit_message}），"
                            f"本地等待 {backoff_minutes} 分钟后重试 {attempt + 1}/3（平台未提供解限时间）。",
                            flush=True,
                        )
                        continue
                    print(
                        f"Flow 5 投稿受限（B站 {rate_limit_code}）：{rate_limit_message}；"
                        f"本次自动重试已用完，已保留 {backoff_minutes} 分钟共享等待；"
                        "再次投递会先等待，平台实际解限时间未知。",
                        file=sys.stderr,
                        flush=True,
                    )
                    return result.returncode or 1
                if attempt < 3:
                    print(
                        f"投稿进程异常退出（{result.returncode}），{retry_delay} 秒后重试 "
                        f"{attempt + 1}/3。"
                    )
                    sleep_fn(retry_delay)
                else:
                    print(f"投稿失败，已停止后续任务：{clip_id}", file=sys.stderr)
                    return result.returncode or 1
            submitted_now = True
        if submitted_now:
            throttle = record_flow5_upload_success(
                throttle_path,
                clip_id=clip_id,
                bvid=bvid,
                now=now_fn,
            )
            completed = int(throttle.get("completed_in_window") or 0)
            print(f"Flow 5 投稿额度：本轮 {completed}/{FLOW5_UPLOAD_BATCH_SIZE} 条。")
            if completed >= FLOW5_UPLOAD_BATCH_SIZE:
                print("已达到 5 条上限；下一条投稿前将自动冷却 10 分钟。")
        ensure_collection_membership(
            collection_cookie_path,
            item["section_id"],
            bvid,
            item["title"],
        )
        _save_receipt(receipt_path, receipts, item, bvid)
        print(f"合集已确认：{item['collection']} / {bvid}")
        if index < len(items):
            print(f"等待 {inter_item_delay} 秒后继续下一条，避免连续投稿冲突。")
            sleep_fn(inter_item_delay)
    return 0

def command_credentials(args: argparse.Namespace) -> int:
    cookie_path = Path(args.cookie_file).expanduser().resolve()
    validate_cookie_location(cookie_path)
    if args.create_template:
        create_cookie_template(cookie_path, force=args.force_template)
    elif args.login:
        binary = find_biliup(args.biliup)
        run_login(binary, cookie_path)
    elif args.import_file:
        import_cookie_file(Path(args.import_file), cookie_path)
    else:
        binary = find_biliup(args.biliup)
        credential_interface(binary, cookie_path)
    print(f"凭证已保存到本机私有目录：{cookie_path.parent}")
    return 0


def command_renew(args: argparse.Namespace) -> int:
    binary = find_biliup(args.biliup)
    cookie_path = ensure_credentials(binary, Path(args.cookie_file).expanduser().resolve())
    result = subprocess.run(
        [binary, "-u", str(cookie_path), "renew"],
        check=False,
        **hidden_subprocess_kwargs(),
    )
    return result.returncode


def command_doctor(args: argparse.Namespace) -> int:
    binary = find_biliup(args.biliup)
    version = check_biliup_cli(binary)
    cookie_path = Path(args.cookie_file).expanduser().resolve()
    validate_cookie_location(cookie_path)
    credential_state = cookie_json_status(cookie_path)
    print(f"biliup={version}")
    print(f"credentials={credential_state}")
    print("upload_mode=one independent submission per CSV row")
    return 0


def command_upload(args: argparse.Namespace) -> int:
    delivery = Path(args.delivery).resolve()
    copy_path = Path(args.copy).resolve() if args.copy else (delivery / "titles-and-covers.csv").resolve()
    items = load_upload_items(
        delivery=delivery,
        copy_path=copy_path,
        creator=args.creator,
        tid=args.tid,
        copyright_value=args.copyright,
        source=args.source,
        no_reprint=args.no_reprint,
        default_tags=args.tags,
        default_description=args.description,
        only=set(args.only or []),
        song_tid=args.song_tid,
    )

    print(json.dumps({
        "mode": "execute" if args.execute else "preview",
        "flow5_upload_policy": {
            "batch_size": FLOW5_UPLOAD_BATCH_SIZE,
            "cooldown_seconds": FLOW5_UPLOAD_COOLDOWN_SECONDS,
            "resume_after_cooldown": True,
        },
        "items": public_plan(items),
    }, ensure_ascii=False, indent=2))
    if not args.execute:
        print("预览完成：未读取 Cookie，未连接哔哩哔哩，未投稿。")
        return 0

    binary = find_biliup(args.biliup)
    check_biliup_cli(binary)
    cookie_path = ensure_credentials(
        binary,
        Path(args.cookie_file).expanduser().resolve(),
        delivery,
    )
    collection_cookie_path = ensure_credentials(
        binary,
        Path(args.collection_cookie_file).expanduser().resolve(),
        delivery,
    )
    confirm_execution(len(items), args.confirm)
    routes = prepare_creator_collections(
        args.creator,
        items,
        collection_cookie_path,
    )
    require_collection_routing(items)
    print(json.dumps({
        "collections_ready": routes,
        "items": public_plan(items),
    }, ensure_ascii=False, indent=2))
    return execute_uploads(
        binary=binary,
        cookie_path=cookie_path,
        items=items,
        limit=args.limit,
        line=args.line,
        cooldown=args.cooldown,
        submit=args.submit,
        collection_cookie_path=collection_cookie_path,
    )


def command_replace(args: argparse.Namespace) -> int:
    delivery = Path(args.delivery).resolve()
    copy_path = (
        Path(args.copy).resolve()
        if args.copy
        else (delivery / "titles-and-covers.csv").resolve()
    )
    items = load_upload_items(
        delivery=delivery,
        copy_path=copy_path,
        creator=args.creator,
        tid=args.tid,
        copyright_value=args.copyright,
        source=args.source,
        no_reprint=args.no_reprint,
        default_tags=args.tags,
        default_description=args.description,
        only=set(args.only or []),
        song_tid=args.song_tid,
    )
    targets = load_replacement_targets(Path(args.previous_receipts).resolve())
    plan = replacement_plan(items, targets)
    print(json.dumps({
        "mode": "execute" if args.execute else "preview",
        "operation": "replace-published-media",
        "items": public_replacement_plan(plan),
    }, ensure_ascii=False, indent=2))
    if not args.execute:
        print("换源预览完成：未读取 Cookie，未连接哔哩哔哩，线上旧素材未变化。")
        return 0

    binary = find_biliup(args.biliup)
    check_biliup_cli(binary)
    check_biliup_append_cli(binary)
    cookie_path = ensure_credentials(
        binary,
        Path(args.cookie_file).expanduser().resolve(),
        delivery,
    )
    collection_cookie_path = ensure_credentials(
        binary,
        Path(args.collection_cookie_file).expanduser().resolve(),
        delivery,
    )
    confirm_replacement(len(plan), args.confirm)
    return execute_replacements(
        binary=binary,
        cookie_path=cookie_path,
        plan=plan,
        limit=args.limit,
        line=args.line,
        submit=args.submit,
        collection_cookie_path=collection_cookie_path,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--biliup", default="", help="biliup executable name or path")
    parser.add_argument(
        "--cookie-file",
        default=str(default_cookie_path()),
        help="private cookies.json path outside OneDrive and the skill",
    )
    parser.add_argument(
        "--collection-cookie-file",
        default=str(default_collection_cookie_path()),
        help="private web cookies.json used only for collection add/verification",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    credentials = sub.add_parser("credentials", help="open the local credential interface")
    mode = credentials.add_mutually_exclusive_group()
    mode.add_argument("--login", action="store_true", help="run biliup QR/login flow")
    mode.add_argument("--import-file", help="import an existing cookies.json")
    mode.add_argument(
        "--create-template",
        action="store_true",
        help="create an empty private cookies.json that can be filled locally",
    )
    credentials.add_argument(
        "--force-template",
        action="store_true",
        help="replace an existing incomplete template, never a ready credential file",
    )
    credentials.set_defaults(func=command_credentials)

    renew = sub.add_parser("renew", help="ask biliup to renew existing credentials")
    renew.set_defaults(func=command_renew)

    doctor = sub.add_parser("doctor", help="check biliup compatibility without uploading")
    doctor.set_defaults(func=command_doctor)

    upload = sub.add_parser("upload", help="preview by default; execute only after confirmation")
    upload.add_argument("--delivery", required=True)
    upload.add_argument("--copy", help="defaults to DELIVERY/titles-and-covers.csv")
    upload.add_argument("--creator", required=True)
    upload.add_argument(
        "--tid", type=int, required=True,
        help="fallback tid, normally Animation/General (27)",
    )
    upload.add_argument(
        "--song-tid", type=int, default=130,
        help="tid for rows whose content_type is song/music (default Music General 130)",
    )
    upload.add_argument("--copyright", type=int, choices=(1, 2), required=True)
    upload.add_argument("--source", default="")
    upload.add_argument("--no-reprint", type=int, choices=(0, 1), default=0)
    upload.add_argument("--tags", default="")
    upload.add_argument("--description", default="")
    upload.add_argument("--only", nargs="*")
    upload.add_argument("--limit", type=int, default=3)
    upload.add_argument("--line", default="")
    upload.add_argument(
        "--submit",
        choices=("app", "web", "b-cut-android"),
        default="web",
    )
    upload.add_argument("--max-items", type=int, default=None, help=argparse.SUPPRESS)
    upload.add_argument("--cooldown", type=int, default=0)
    upload.add_argument("--execute", action="store_true")
    upload.add_argument("--confirm", default="")
    upload.set_defaults(func=command_upload)

    replace = sub.add_parser(
        "replace",
        help="replace media in previously published single-part archives; preview by default",
    )
    replace.add_argument("--delivery", required=True)
    replace.add_argument("--copy", help="defaults to DELIVERY/titles-and-covers.csv")
    replace.add_argument("--previous-receipts", required=True)
    replace.add_argument("--creator", required=True)
    replace.add_argument("--tid", type=int, required=True)
    replace.add_argument("--song-tid", type=int, default=130)
    replace.add_argument("--copyright", type=int, choices=(1, 2), required=True)
    replace.add_argument("--source", default="")
    replace.add_argument("--no-reprint", type=int, choices=(0, 1), default=0)
    replace.add_argument("--tags", default="")
    replace.add_argument("--description", default="")
    replace.add_argument("--only", nargs="*")
    replace.add_argument("--limit", type=int, default=3)
    replace.add_argument("--line", default="")
    replace.add_argument(
        "--submit",
        choices=("app", "web", "b-cut-android"),
        default="web",
    )
    replace.add_argument("--cooldown", type=int, default=0)
    replace.add_argument("--execute", action="store_true")
    replace.add_argument("--confirm", default="")
    replace.set_defaults(func=command_replace)
    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "limit", 1) < 1:
        parser.error("--limit must be at least 1")
    if getattr(args, "max_items", None) is not None and args.max_items < 1:
        parser.error("--max-items must be at least 1")
    if getattr(args, "cooldown", 0) < 0:
        parser.error("--cooldown must not be negative")
    try:
        return args.func(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
