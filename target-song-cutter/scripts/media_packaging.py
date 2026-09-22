"""Optional delivery bumpers and zero-duration narrative blur transitions.

Source order and clip-local subtitle times stay authoritative. Bumpers are
added only to the delivery, and their measured duration offsets its ASS copy.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

from windows_process import hidden_subprocess_kwargs

DEFAULT_TRANSITION_SECONDS = 0.5


def normalize_media_packaging(value=None, base_dir=None, check_files=False):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("片头片尾配置必须是对象")
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("片头片尾 enabled 必须是 true 或 false")
    result = {"enabled": enabled}
    for key in ("intro_path", "outro_path"):
        raw = str(value.get(key) or "").strip()
        if raw:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = Path(base_dir or Path.cwd()) / path
            path = path.resolve()
            if check_files and enabled and not path.is_file():
                raise ValueError(f"{'片头' if key == 'intro_path' else '片尾'}素材不存在：{path}")
            raw = str(path)
        result[key] = raw
    return result


def load_media_packaging(path, check_files=False):
    path = Path(path)
    value = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    return normalize_media_packaging(value, path.parent, check_files)


def save_media_packaging(path, value):
    path = Path(path)
    result = normalize_media_packaging(value, path.parent, check_files=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return result


def normalize_narrative_structure(raw, ranges, content_type="narrative"):
    """Accept explicit new contracts and infer legacy nonchronological JSON."""
    if any(not math.isfinite(float(span[key])) for span in ranges for key in ("start_seconds", "end_seconds")):
        raise ValueError("源时间戳必须是有限数值")
    reverse = any(float(ranges[i]["start_seconds"]) < float(ranges[i-1]["start_seconds"])
                  for i in range(1, len(ranges)))
    if content_type == "song" and reverse:
        raise ValueError("歌切时间戳必须顺叙；禁止倒叙或反放演唱")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("叙事结构必须是对象")
    kind = str(raw.get("类型", raw.get("type", "倒叙" if reverse else "顺叙"))).strip()
    kinds = {"倒叙": "callback", "callback": "callback", "flashback": "callback",
             "顺叙": "chronological", "chronological": "chronological"}
    if kind not in kinds:
        raise ValueError("叙事结构类型必须为顺叙或倒叙")
    kind = kinds[kind]
    if content_type == "song" and kind != "chronological":
        raise ValueError("歌切不能使用倒叙结构")
    if reverse and kind != "callback":
        raise ValueError("非升序时间戳须声明倒叙结构，或省略叙事结构由程序识别")
    roles = raw.get("段落角色", raw.get("roles"))
    role_aliases = {"冷开场": "cold_open", "cold_open": "cold_open", "前情": "setup",
                    "setup": "setup", "flashback": "setup", "回归": "return", "return": "return",
                    "结果": "payoff", "payoff": "payoff", "正文": "body", "body": "body"}
    if roles is not None:
        if not isinstance(roles, list) or len(roles) != len(ranges):
            raise ValueError("段落角色数量必须与时间戳数量一致")
        if any(str(role) not in role_aliases for role in roles):
            raise ValueError("未知段落角色；使用冷开场、前情、回归、结果或正文")
        roles = [role_aliases[str(role)] for role in roles]
    elif kind == "callback":
        roles = ["cold_open"] + ["setup"] * (len(ranges) - 1)
        for index in range(2, len(ranges)):
            if ranges[index]["start_seconds"] >= ranges[0]["end_seconds"] - 0.001:
                roles[index] = "return"
    else:
        roles = ["body"] * len(ranges)
    if kind == "callback":
        if len(ranges) < 2 or not reverse or roles[0] != "cold_open":
            raise ValueError("倒叙须以冷开场开始，并包含较早源事件；各段仍正常向前播放")
        ordered = sorted(ranges, key=lambda span: span["start_seconds"])
        if any(ordered[i]["start_seconds"] < ordered[i-1]["end_seconds"] - 0.001
               for i in range(1, len(ordered))):
            raise ValueError("倒叙各源时间段不得重叠或重复播放同一句")
    duration = float(raw.get("转场秒数", raw.get("transition_seconds", DEFAULT_TRANSITION_SECONDS)))
    if not math.isfinite(duration) or not 0.1 <= duration <= 2:
        raise ValueError("转场秒数必须在 0.1–2 秒之间，推荐 0.5")
    reason = str(raw.get("理由", raw.get("reason", ""))).strip()
    return {"type": kind, "roles": roles, "transition_seconds": duration, "reason": reason}


def remap_narrative_structure(structure, source_ranges, ranges):
    """Inherit roles when cleanup splits ranges, and demote a removed callback."""
    if not structure:
        return normalize_narrative_structure(None, ranges)
    roles = []
    original_roles = structure.get("roles", [])
    for span in ranges:
        overlaps = [max(0.0, min(span["end_seconds"], original["end_seconds"]) -
                        max(span["start_seconds"], original["start_seconds"])) for original in source_ranges]
        best = max(range(len(overlaps)), key=overlaps.__getitem__) if overlaps else None
        roles.append(original_roles[best] if best is not None and overlaps[best] > 0 and best < len(original_roles) else "body")
    updated = dict(structure)
    reverse = any(ranges[index]["start_seconds"] < ranges[index-1]["start_seconds"] for index in range(1, len(ranges)))
    if not reverse:
        if structure.get("type") == "callback":
            updated["reason"] = str(structure.get("reason") or "") + "；边界处理后已无倒叙跳转，自动恢复顺叙"
        updated.update(type="chronological", roles=["body"] * len(ranges))
    else:
        if not roles or roles[0] != "cold_open":
            raise ValueError("边界处理移除了倒叙冷开场，剩余非顺序片段需要重新选择")
        updated.update(type="callback", roles=roles)
    return normalize_narrative_structure(updated, ranges)


def narrative_transitions(ranges, roles=None, content_type="narrative", duration=0.5):
    """Return local boundaries; ordinary forward deletions retain hard cuts."""
    if content_type == "song" or len(ranges) < 2:
        return []
    reverse = any(ranges[i][0] < ranges[i-1][0] for i in range(1, len(ranges)))
    if not reverse:
        return []
    roles = list(roles or [])
    cursor = ranges[0][1] - ranges[0][0]
    result = []
    for index in range(1, len(ranges)):
        backward = ranges[index][0] < ranges[index-1][0]
        returning = (len(roles) == len(ranges) and roles[index] == "return"
                     and roles[index-1] != "return") or (
            ranges[index-1][0] < ranges[0][0] and ranges[index][0] >= ranges[0][1] - 0.001)
        if backward or returning:
            available = 2 * min(ranges[index-1][1] - ranges[index-1][0],
                                ranges[index][1] - ranges[index][0])
            result.append({"at_seconds": round(cursor, 6), "duration_seconds": min(float(duration), available),
                           "kind": "gaussian_blur", "sigma": 10.0})
        cursor += ranges[index][1] - ranges[index][0]
    return result


def gaussian_blur_graph(input_label, output_label, transitions):
    """Blend a Gaussian-blurred full frame in/out over a 0.5-second window.

No overlap, extra frames, audio filters, or timestamp changes are introduced.
"""
    if not transitions:
        return f"[{input_label}]null[{output_label}]"
    envelopes = []
    windows = []
    for item in transitions:
        center = float(item["at_seconds"])
        half = float(item.get("duration_seconds", 0.5)) / 2
        if not math.isfinite(center) or not math.isfinite(half) or center < 0 or half <= 0:
            raise ValueError("Invalid narrative transition boundary")
        envelopes.append(f"max(0,1-abs(T-{center:.6f})/{half:.6f})")
        windows.append(f"between(t,{max(0, center-half):.6f},{center+half:.6f})")
    strength = envelopes[0]
    for expression in envelopes[1:]:
        strength = f"max({strength},{expression})"
    enabled = "+".join(windows)
    return (f"[{input_label}]split[clearframe][blurframe];"
            f"[blurframe]gblur=sigma=10:steps=2:enable='{enabled}'[softframe];"
            f"[clearframe][softframe]blend=all_expr='A*(1-({strength}))+B*({strength})':enable='{enabled}'[{output_label}]")


def probe_media(ffmpeg, path):
    probe = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    command = [str(probe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=True,
                            **hidden_subprocess_kwargs())
    data = json.loads(result.stdout)
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError(f"素材没有视频轨：{path}")
    duration = float(video.get("duration") or data["format"]["duration"])
    # FLV can report a 1000 Hz timestamp clock as r_frame_rate. Use the
    # measured average first and reject clocks/invalid rationals as frame rates.
    fps = 30.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            rate = str(video.get(key) or "")
            numerator, separator, denominator = rate.partition("/")
            candidate = float(numerator) / (float(denominator) if separator else 1.0)
        except (ValueError, ZeroDivisionError, OverflowError):
            continue
        if math.isfinite(candidate) and 0 < candidate <= 240:
            fps = candidate
            break
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"素材时长无效：{path}")
    # Matroska's stream DURATION excludes an audio-only tail; format.duration does not.
    video_end = None
    try:
        tagged_end = video.get("tags", {}).get("DURATION")
        if tagged_end:
            hours, minutes, seconds = map(float, str(tagged_end).split(":"))
            video_end = hours * 3600 + minutes * 60 + seconds
        elif video.get("duration"):
            video_end = float(video.get("start_time") or 0) + float(video["duration"])
        if video_end is not None and (not math.isfinite(video_end) or video_end <= 0):
            video_end = None
    except (TypeError, ValueError):
        video_end = None
    return {"duration": duration, "video_end_seconds": video_end,
            "width": int(video["width"]), "height": int(video["height"]),
            "fps": fps if fps > 0 else 30,
            "has_audio": any(s.get("codec_type") == "audio" for s in data["streams"])}


def build_burn_command(ffmpeg, clip, subtitle_filter, destination, packaging=None, transitions=None, trim=None):
    packaging = normalize_media_packaging(packaging, check_files=True)
    bumpers = [Path(packaging[key]) if packaging["enabled"] and packaging[key] else None
               for key in ("intro_path", "outro_path")]
    command = [str(ffmpeg), "-y", "-i", str(clip)]
    metadata = {"schema_version": 1, "intro_seconds": 0.0, "outro_seconds": 0.0,
                "transitions": list(transitions or []), "audio_policy": "source audio; no fades or reverse"}
    if not any(bumpers):
        if trim is not None:
            command += ["-t", f"{trim:.6f}"]
        if transitions:
            graph = f"[0:v]{subtitle_filter}[subtitled];" + gaussian_blur_graph("subtitled", "outv", transitions)
            command += ["-filter_complex", graph, "-map", "[outv]", "-map", "0:a:0?"]
        else:
            command += ["-vf", subtitle_filter]
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-c:a", "copy",
                    "-movflags", "+faststart", str(destination)]
        return command, metadata
    main = probe_media(ffmpeg, clip)
    if not main["has_audio"]:
        raise ValueError(f"正文素材缺少原声：{clip}")
    duration = min(main["duration"], float(trim)) if trim is not None else main["duration"]
    metadata["main_seconds"] = duration
    fps = main["fps"]
    width, height = main["width"], main["height"]
    graph = [f"[0:v]trim=duration={duration:.6f},setpts=PTS-STARTPTS,{subtitle_filter}[subtitled]",
             gaussian_blur_graph("subtitled", "mainvisual", transitions or []),
             # Keep the main frame timestamps through the blur framesync branch.
             # A second fps filter here can discard its EOF frame; output -r
             # performs final CFR normalization without losing that source frame.
             "[mainvisual]setsar=1,format=yuv420p[mainv]",
             f"[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[maina]"]
    streams = ["[mainv][maina]"]
    input_index = 1
    for position, path in enumerate(bumpers):
        if path is None:
            continue
        info = probe_media(ffmpeg, path)
        # A whole frame duration makes the ASS offset match the concat boundary.
        seconds = math.ceil(info["duration"] * fps - 1e-6) / fps
        key = "intro" if position == 0 else "outro"
        metadata[f"{key}_seconds"] = seconds
        metadata[f"{key}_path"] = str(path)
        command += ["-i", str(path)]
        graph += [f"[{input_index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                  f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:.9f},format=yuv420p,"
                  f"tpad=stop_mode=clone:stop_duration=1,trim=duration={seconds:.6f},setpts=PTS-STARTPTS[{key}v]"]
        audio = f"[{input_index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad" if info["has_audio"] else "anullsrc=r=48000:cl=stereo"
        graph += [f"{audio},atrim=duration={seconds:.6f},asetpts=PTS-STARTPTS[{key}a]"]
        if position == 0:
            streams.insert(0, f"[{key}v][{key}a]")
        else:
            streams.append(f"[{key}v][{key}a]")
        input_index += 1
    graph += ["".join(streams) + f"concat=n={len(streams)}:v=1:a=1[outv][outa]"]
    command += ["-filter_complex", ";".join(graph), "-map", "[outv]", "-map", "[outa]",
                "-r", f"{fps:.9f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(destination)]
    return command, metadata


def shift_ass_file(source, destination, seconds):
    text = Path(source).read_text(encoding="utf-8-sig")
    lines = []
    def clock(value):
        ticks = max(0, round(value * 100))
        return f"{ticks // 360000}:{ticks // 6000 % 60:02}:{ticks // 100 % 60:02}.{ticks % 100:02}"
    fields = []
    in_events = False
    for line in text.splitlines():
        if line.startswith("["):
            in_events = line.strip().lower() == "[events]"
        if in_events and line.lower().startswith("format:"):
            fields = [item.strip().lower() for item in line.split(":", 1)[1].split(",")]
        if in_events and line.lower().startswith(("dialogue:", "comment:")):
            prefix, body = line.split(":", 1)
            values = body.lstrip().split(",", len(fields) - 1)
            for name in ("start", "end"):
                index = fields.index(name)
                hours, minutes, rest = values[index].split(":")
                values[index] = clock(int(hours) * 3600 + int(minutes) * 60 + float(rest) + seconds)
            line = prefix + ": " + ",".join(values)
        lines.append(line)
    Path(destination).write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
