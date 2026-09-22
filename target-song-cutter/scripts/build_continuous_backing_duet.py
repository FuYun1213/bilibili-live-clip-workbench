from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


TRANSFORM_RE = re.compile(r"\\t\((\d+),(\d+),")


@dataclass
class Cue:
    start: float
    end: float
    text: str


def parse_time(value: str) -> float:
    hours, minutes, seconds = value.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def format_time(value: float) -> str:
    centiseconds = max(0, round(value * 100))
    hours, centiseconds = divmod(centiseconds, 360_000)
    minutes, centiseconds = divmod(centiseconds, 6_000)
    seconds, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def parse_ass(path: Path) -> list[Cue]:
    cues: list[Cue] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) == 10:
            cues.append(Cue(parse_time(parts[1]), parse_time(parts[2]), parts[9]))
    return cues


def scale_transforms(text: str, factor: float) -> str:
    def replacement(match: re.Match[str]) -> str:
        return f"\\t({round(int(match.group(1)) * factor)},{round(int(match.group(2)) * factor)},"

    return TRANSFORM_RE.sub(replacement, text)


def resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def ass_color(rgb: str) -> str:
    value = rgb.lstrip("#")
    return f"&H00{value[4:6]}{value[2:4]}{value[0:2]}"


def run(command: list[str], cwd: Path | None = None) -> None:
    print("RUN", " ".join(command))
    subprocess.run(command, cwd=cwd, check=True)


def guest_video_filter(index: int, crop_x: int, duration: float, segments: list[dict[str, float]]) -> tuple[list[str], str]:
    filters: list[str] = []
    labels: list[str] = []
    cursor = 0.0
    freeze_at = float(segments[0]["source_start"])
    part = 0
    required_inputs = len(segments) * 2 + 1
    source_labels = [f"guestsrc{item}" for item in range(required_inputs)]
    filters.append(f"[{index}:v]split={required_inputs}" + "".join(f"[{label}]" for label in source_labels))
    source_cursor = 0

    def add_freeze(length: float, source_time: float) -> None:
        nonlocal part, source_cursor
        if length <= 0.001:
            return
        label = f"gv{part}"
        source_label = source_labels[source_cursor]
        source_cursor += 1
        filters.append(
            f"[{source_label}]trim=start={source_time:.6f}:end={source_time + 0.08:.6f},setpts=PTS-STARTPTS,"
            f"scale=-2:1080,crop=960:1080:{crop_x}:0,tpad=stop_mode=clone:stop_duration={length:.6f},"
            f"trim=duration={length:.6f},fps=30,format=yuv420p[{label}]"
        )
        labels.append(f"[{label}]")
        part += 1

    for segment in segments:
        target_start = float(segment["target_start"])
        target_end = float(segment["target_end"])
        source_start = float(segment["source_start"])
        source_end = float(segment["source_end"])
        add_freeze(target_start - cursor, freeze_at)
        target_length = target_end - target_start
        source_length = source_end - source_start
        label = f"gv{part}"
        source_label = source_labels[source_cursor]
        source_cursor += 1
        filters.append(
            f"[{source_label}]trim=start={source_start:.6f}:end={source_end:.6f},"
            f"setpts={target_length / source_length:.10f}*(PTS-STARTPTS),scale=-2:1080,"
            f"crop=960:1080:{crop_x}:0,fps=30,trim=duration={target_length:.6f},format=yuv420p[{label}]"
        )
        labels.append(f"[{label}]")
        part += 1
        cursor = target_end
        freeze_at = max(source_start, source_end - 0.08)
    add_freeze(duration - cursor, freeze_at)
    output = "guestv"
    filters.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[{output}]")
    return filters, output


def intervals_expression(segments: list[dict[str, float]]) -> str:
    return "+".join(f"between(t,{float(item['target_start']):.6f},{float(item['target_end']):.6f})" for item in segments)


def write_ass(destination: Path, data: dict, master_cues: list[Cue], guest_cues: list[Cue], duration: float) -> int:
    master = data["master"]
    guest = data["guest"]
    segments = data["guest_segments"]
    remapped: list[tuple[float, float, str, str]] = []

    for cue in master_cues:
        if any(float(item["target_start"]) - 0.03 <= cue.start < float(item["target_end"]) + 0.03 for item in segments):
            continue
        remapped.append((cue.start, min(cue.end, duration), "MasterLyric", cue.text))

    for segment in segments:
        source_start = float(segment["source_start"])
        source_end = float(segment["source_end"])
        target_start = float(segment["target_start"])
        target_end = float(segment["target_end"])
        factor = (target_end - target_start) / (source_end - source_start)
        for cue in guest_cues:
            if cue.start < source_start - 0.03 or cue.start >= source_end - 0.03:
                continue
            start = target_start + (cue.start - source_start) * factor
            end = target_start + (min(cue.end, source_end) - source_start) * factor
            if end > start + 0.03:
                remapped.append((start, end, "GuestLyric", scale_transforms(cue.text, factor)))

    master_outline = ass_color(master["color"])
    guest_outline = ass_color(guest["color"])
    lines = [
        "[Script Info]",
        "; One continuous accompaniment master; only isolated vocal stems change singer.",
        f"Title: {data['title']}",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: MasterLyric,AR WeiBeiGBStd BD,62,&H00FFFCF7,&HFFFFFCF7,{master_outline},&H60211507,-1,0,0,0,100,100,0,0,1,4.0,1.5,2,90,90,74,1",
        f"Style: GuestLyric,AR WeiBeiGBStd BD,62,&H00FFFCF7,&HFFFFFCF7,{guest_outline},&H60211507,-1,0,0,0,100,100,0,0,1,4.0,1.5,2,90,90,74,1",
        f"Style: GuestLabel,Microsoft YaHei,42,{guest_outline},&H00FFFFFF,&H00111111,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,7,30,30,28,1",
        f"Style: MasterLabel,Microsoft YaHei,42,{master_outline},&H00FFFFFF,&H00111111,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,9,30,30,28,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
        f"Dialogue: 1,0:00:00.00,{format_time(duration)},GuestLabel,,0,0,0,,{{\\an7\\pos(32,24)}}{guest['name']}",
        f"Dialogue: 1,0:00:00.00,{format_time(duration)},MasterLabel,,0,0,0,,{{\\an9\\pos(1888,24)}}{master['name']}",
    ]
    for start, end, style, text in sorted(remapped, key=lambda item: (item[0], item[1])):
        if start < duration and end > start:
            lines.append(f"Dialogue: 0,{format_time(start)},{format_time(min(end, duration))},{style},,0,0,0,smooth-fade,{text}")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return len(remapped)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a two-singer duet on one uninterrupted accompaniment master.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    base = manifest_path.parent
    data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    master = data["master"]
    guest = data["guest"]
    duration = float(data["duration"])
    guest_segments = data["guest_segments"]
    if not guest_segments:
        raise ValueError("guest_segments cannot be empty")
    for previous, current in zip(guest_segments, guest_segments[1:]):
        if float(current["target_start"]) < float(previous["target_end"]):
            raise ValueError("guest target segments overlap")

    paths = {
        "master_video": resolve(base, master["video"]),
        "guest_video": resolve(base, guest["video"]),
        "instrumental": resolve(base, master["instrumental"]),
        "master_vocal": resolve(base, master["vocal"]),
        "guest_vocal": resolve(base, guest["vocal"]),
        "master_ass": resolve(base, master["ass"]),
        "guest_ass": resolve(base, guest["ass"]),
    }
    for path in paths.values():
        if not path.exists():
            raise FileNotFoundError(path)

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    work = output.parent / (output.stem + "-work")
    work.mkdir(parents=True, exist_ok=True)

    video_filters, guest_video_label = guest_video_filter(1, int(guest.get("crop_x", 840)), duration, guest_segments)
    master_crop = master.get("crop_x")
    master_crop_filter = "crop=960:1080" if master_crop is None else f"crop=960:1080:{int(master_crop)}:0"
    video_filters.append(
        f"[0:v]trim=duration={duration:.6f},setpts=PTS-STARTPTS,scale=-2:1080,{master_crop_filter},fps=30,format=yuv420p[masterv]"
    )
    active = intervals_expression(guest_segments)
    video_filters.extend([
        f"color=c=0x111111:s=1920x1080:r=30:d={duration:.6f}[base]",
        f"[base][{guest_video_label}]overlay=0:0[tmp]",
        "[tmp][masterv]overlay=960:0[split]",
        f"[split]drawbox=x=957:y=0:w=6:h=1080:c=white@0.65:t=fill,"
        f"drawbox=x=0:y=0:w=960:h=1080:c=0x{guest['color'].lstrip('#')}@0.92:t=12:enable='{active}',"
        f"drawbox=x=960:y=0:w=960:h=1080:c=0x{master['color'].lstrip('#')}@0.92:t=12:enable='not({active})'[vout]",
    ])

    audio_filters = [
        f"[2:a]atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,volume={float(master.get('instrument_gain_db', 0.0)):.3f}dB[inst]",
        f"[3:a]atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,volume='if({active},0,1)':eval=frame[mvoc]",
    ]
    guest_labels: list[str] = []
    timeline_rows: list[dict[str, object]] = []
    if len(guest_segments) > 1:
        audio_filters.append(
            f"[4:a]asplit={len(guest_segments)}"
            + "".join(f"[guestsrc{index}]" for index in range(1, len(guest_segments) + 1))
        )
    for index, segment in enumerate(guest_segments, 1):
        source_start = float(segment["source_start"])
        source_end = float(segment["source_end"])
        target_start = float(segment["target_start"])
        target_end = float(segment["target_end"])
        source_length = source_end - source_start
        target_length = target_end - target_start
        tempo = source_length / target_length
        delay = round(target_start * 1000)
        label = f"guest{index}"
        guest_input = f"[guestsrc{index}]" if len(guest_segments) > 1 else "[4:a]"
        audio_filters.append(
            f"{guest_input}atrim=start={source_start:.6f}:end={source_end:.6f},asetpts=PTS-STARTPTS,"
            f"rubberband=tempo={tempo:.10f}:pitch=1:transients=mixed:detector=soft:phase=laminar:"
            f"window=long:formant=preserved:pitchq=quality:channels=together,"
            f"atrim=duration={target_length:.6f},volume={float(segment.get('gain_db', 0.0)):.3f}dB,"
            f"afade=t=in:st=0:d=0.06,afade=t=out:st={max(0.0, target_length - 0.10):.6f}:d=0.10,"
            f"adelay={delay}|{delay}[{label}]"
        )
        guest_labels.append(f"[{label}]")
        timeline_rows.append({
            "singer": guest["name"],
            "source_start": f"{source_start:.3f}",
            "source_end": f"{source_end:.3f}",
            "target_start": f"{target_start:.3f}",
            "target_end": f"{target_end:.3f}",
            "tempo_factor": f"{tempo:.6f}",
            "gain_db": f"{float(segment.get('gain_db', 0.0)):.2f}",
            "section": segment.get("section", ""),
        })
    inputs = "[inst][mvoc]" + "".join(guest_labels)
    audio_filters.append(
        f"{inputs}amix=inputs={2 + len(guest_labels)}:duration=first:dropout_transition=0:normalize=0,"
        "alimiter=limit=0.95,loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    )

    ass = output.with_suffix(".ass")
    cue_count = write_ass(ass, data, parse_ass(paths["master_ass"]), parse_ass(paths["guest_ass"]), duration)
    local_ass = work / "duet.ass"
    shutil.copy2(ass, local_ass)
    video_filters.append("[vout]ass=duet.ass[vsub]")

    command = [
        args.ffmpeg, "-y", "-v", "warning",
        "-i", str(paths["master_video"]), "-i", str(paths["guest_video"]),
        "-i", str(paths["instrumental"]), "-i", str(paths["master_vocal"]), "-i", str(paths["guest_vocal"]),
        "-filter_complex", ";".join(video_filters + audio_filters),
        "-map", "[vsub]", "-map", "[aout]", "-t", f"{duration:.6f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(output),
    ]
    run(command, cwd=work)

    timeline = output.with_name(output.stem + "-vocal-map.csv")
    with timeline.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(timeline_rows[0]))
        writer.writeheader()
        writer.writerows(timeline_rows)
    report = {
        "output": str(output),
        "duration": duration,
        "continuous_instrumental": str(paths["instrumental"]),
        "guest_sections": len(guest_segments),
        "subtitle_cues": cue_count,
        "ass": str(ass),
        "vocal_map": str(timeline),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
