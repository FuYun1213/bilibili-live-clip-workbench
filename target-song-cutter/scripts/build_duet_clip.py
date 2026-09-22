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


def ass_time(value: str) -> float:
    hours, minutes, seconds = value.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def format_ass_time(value: float) -> str:
    value = max(0.0, value)
    hours = int(value // 3600)
    minutes = int(value % 3600 // 60)
    seconds = value % 60
    return f"{hours}:{minutes:02d}:{seconds:05.2f}"


def parse_ass(path: Path) -> list[Cue]:
    cues: list[Cue] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) == 10:
            cues.append(Cue(ass_time(parts[1]), ass_time(parts[2]), parts[9]))
    return cues


def ass_color(rgb: str) -> str:
    value = rgb.lstrip("#")
    return f"&H00{value[4:6]}{value[2:4]}{value[0:2]}"


def ffmpeg_color(rgb: str) -> str:
    return "0x" + rgb.lstrip("#")


def resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def run(command: list[str], cwd: Path | None = None) -> None:
    print("RUN", " ".join(command))
    subprocess.run(command, cwd=cwd, check=True)


def scale_transforms(text: str, factor: float) -> str:
    def replacement(match: re.Match[str]) -> str:
        start = round(int(match.group(1)) * factor)
        end = round(int(match.group(2)) * factor)
        return f"\\t({start},{end},"

    return TRANSFORM_RE.sub(replacement, text)


def atempo_chain(factor: float) -> str:
    values: list[float] = []
    while factor > 2.0:
        values.append(2.0)
        factor /= 2.0
    while factor < 0.5:
        values.append(0.5)
        factor /= 0.5
    values.append(factor)
    return ",".join(f"atempo={value:.8f}" for value in values)


def video_chain(label: str, duration: float, target: float, freeze: bool, crop_x: int | None = None) -> str:
    crop = "crop=960:1080" if crop_x is None else f"crop=960:1080:{crop_x}:0"
    base = f"[{label}:v]scale=-2:1080,{crop}"
    if freeze:
        return base + f",trim=duration=0.08,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={target:.6f},trim=duration={target:.6f},fps=30,format=yuv420p"
    ratio = target / duration
    return base + f",setpts={ratio:.10f}*(PTS-STARTPTS),fps=30,trim=duration={target:.6f},format=yuv420p"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an equal-half, phrase-aligned two-creator duet from an auditable manifest.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    base = manifest_path.parent
    data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    left = data["left"]
    right = data["right"]
    left_video = resolve(base, left["video"])
    right_video = resolve(base, right["video"])
    left_ass = resolve(base, left["ass"])
    right_ass = resolve(base, right["ass"])
    for path in (left_video, right_video, left_ass, right_ass):
        if not path.exists():
            raise FileNotFoundError(path)

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    work = (args.work_dir or output.parent / (output.stem + "-work")).resolve()
    work.mkdir(parents=True, exist_ok=True)
    segments_dir = work / "segments"
    segments_dir.mkdir(exist_ok=True)
    left_cues = parse_ass(left_ass)
    right_cues = parse_ass(right_ass)
    remapped: list[tuple[float, float, str, str]] = []
    timeline_rows: list[dict[str, object]] = []
    segment_paths: list[Path] = []
    output_cursor = 0.0

    for index, segment in enumerate(data["segments"], 1):
        left_start = float(segment["left_start"])
        left_end = float(segment["left_end"])
        right_start = float(segment["right_start"])
        right_end = float(segment["right_end"])
        left_duration = left_end - left_start
        right_duration = right_end - right_start
        if left_duration <= 0 or right_duration <= 0:
            raise ValueError(f"segment {index} has a non-positive source duration")
        active = segment["active"]
        target = float(segment.get("duration", left_duration if active == "left" else right_duration))
        if target <= 0:
            raise ValueError(f"segment {index} has a non-positive output duration")
        segment_path = segments_dir / f"{index:03d}.mp4"
        segment_paths.append(segment_path)

        if not (args.resume and segment_path.exists() and segment_path.stat().st_size > 10000):
            left_filter = video_chain("0", left_duration, target, bool(segment.get("left_freeze")), left.get("crop_x")) + "[lv]"
            right_filter = video_chain("1", right_duration, target, bool(segment.get("right_freeze")), right.get("crop_x")) + "[rv]"
            active_left = active == "left"
            color = ffmpeg_color(left["color"] if active_left else right["color"])
            box_x = 0 if active_left else 960
            video_filter = (
                left_filter + ";" + right_filter + ";"
                f"color=c=0x111111:s=1920x1080:r=30:d={target:.6f}[base];"
                "[base][lv]overlay=0:0[tmp];[tmp][rv]overlay=960:0[split];"
                f"[split]drawbox=x=957:y=0:w=6:h=1080:c=white@0.65:t=fill,"
                f"drawbox=x={box_x}:y=0:w=960:h=1080:c={color}@0.92:t=10[vout]"
            )
            active_duration = left_duration if active_left else right_duration
            speed = active_duration / target
            active_start = left_start if active_left else right_start
            active_end = left_end if active_left else right_end
            previous = data["segments"][index - 2] if index > 1 else None
            following = data["segments"][index] if index < len(data["segments"]) else None

            def source_boundary(item: dict[str, object] | None, side: str, which: str) -> float | None:
                if item is None or item.get("active") != side:
                    return None
                return float(item[f"{side}_{which}"])

            previous_end = source_boundary(previous, active, "end")
            following_start = source_boundary(following, active, "start")
            fade_in = previous_end is None or abs(previous_end - active_start) > 0.08
            fade_out = following_start is None or abs(following_start - active_end) > 0.08
            audio_parts = [
                f"[{0 if active_left else 1}:a]asetpts=PTS-STARTPTS",
                atempo_chain(speed),
                f"atrim=duration={target:.6f}",
            ]
            if fade_in:
                audio_parts.append("afade=t=in:st=0:d=0.06")
            if fade_out:
                audio_parts.append(f"afade=t=out:st={max(0.0, target - 0.08):.6f}:d=0.08")
            audio_filter = ",".join(audio_parts) + "[aout]"
            command = [
                args.ffmpeg, "-y", "-v", "warning",
                "-ss", f"{left_start:.6f}", "-t", f"{left_duration:.6f}", "-i", str(left_video),
                "-ss", f"{right_start:.6f}", "-t", f"{right_duration:.6f}", "-i", str(right_video),
                "-filter_complex", video_filter + ";" + audio_filter,
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart", str(segment_path),
            ]
            run(command)

        if segment.get("subtitles", True):
            source_cues = left_cues if active == "left" else right_cues
            source_start = left_start if active == "left" else right_start
            source_end = left_end if active == "left" else right_end
            factor = target / (source_end - source_start)
            for cue in source_cues:
                if cue.start < source_start - 0.03 or cue.start >= source_end - 0.03:
                    continue
                start = output_cursor + max(0.0, cue.start - source_start) * factor
                end = output_cursor + min(source_end - source_start, cue.end - source_start) * factor
                if end > start + 0.03:
                    remapped.append((start, end, "LyricLeft" if active == "left" else "LyricRight", scale_transforms(cue.text, factor)))

        timeline_rows.append({
            "order": index,
            "active": active,
            "left_start": f"{left_start:.3f}",
            "left_end": f"{left_end:.3f}",
            "right_start": f"{right_start:.3f}",
            "right_end": f"{right_end:.3f}",
            "output_start": f"{output_cursor:.3f}",
            "output_end": f"{output_cursor + target:.3f}",
            "section": segment.get("section", ""),
        })
        output_cursor += target

    concat_path = work / "concat.txt"
    concat_path.write_text("".join(f"file '{path.as_posix()}'\n" for path in segment_paths), encoding="utf-8")
    raw_path = work / "duet-raw.mp4"
    run([args.ffmpeg, "-y", "-v", "warning", "-f", "concat", "-safe", "0", "-i", str(concat_path), "-c:v", "copy", "-af", "aresample=async=1:first_pts=0", "-c:a", "aac", "-b:a", "192k", str(raw_path)])

    ass_path = output.with_suffix(".ass")
    left_outline = ass_color(left["color"])
    right_outline = ass_color(right["color"])
    ass_lines = [
        "[Script Info]",
        "; Phrase-aligned duet subtitles remapped from the final composite timeline.",
        f"Title: {data['title']}",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: LyricLeft,AR WeiBeiGBStd BD,62,&H00FFFCF7,&HFFFFFCF7,{left_outline},&H60211507,-1,0,0,0,100,100,0,0,1,4.0,1.5,2,90,90,74,1",
        f"Style: LyricRight,AR WeiBeiGBStd BD,62,&H00FFFCF7,&HFFFFFCF7,{right_outline},&H60211507,-1,0,0,0,100,100,0,0,1,4.0,1.5,2,90,90,74,1",
        f"Style: LabelLeft,Microsoft YaHei,42,{left_outline},&H00FFFFFF,&H00111111,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,7,30,30,28,1",
        f"Style: LabelRight,Microsoft YaHei,42,{right_outline},&H00FFFFFF,&H00111111,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,9,30,30,28,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
        f"Dialogue: 1,0:00:00.00,{format_ass_time(output_cursor)},LabelLeft,,0,0,0,,{{\\an7\\pos(32,24)}}{left['name']}",
        f"Dialogue: 1,0:00:00.00,{format_ass_time(output_cursor)},LabelRight,,0,0,0,,{{\\an9\\pos(1888,24)}}{right['name']}",
    ]
    for start, end, style, text in sorted(remapped, key=lambda item: (item[0], item[1])):
        ass_lines.append(f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},{style},,0,0,0,smooth-fade,{text}")
    ass_path.write_text("\n".join(ass_lines) + "\n", encoding="utf-8-sig")

    timeline_path = output.with_name(output.stem + "-timeline.csv")
    with timeline_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(timeline_rows[0]))
        writer.writeheader()
        writer.writerows(timeline_rows)

    local_ass = work / "duet.ass"
    shutil.copy2(ass_path, local_ass)
    run([
        args.ffmpeg, "-y", "-v", "warning", "-i", str(raw_path),
        "-vf", "ass=duet.ass",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart", str(output),
    ], cwd=work)
    print(json.dumps({"output": str(output), "ass": str(ass_path), "timeline": str(timeline_path), "duration": round(output_cursor, 3)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
