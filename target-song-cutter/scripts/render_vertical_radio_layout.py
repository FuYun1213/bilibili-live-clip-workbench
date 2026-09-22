from __future__ import annotations

import argparse
import math
import re
import subprocess
from pathlib import Path


def normalize_hex(value: str) -> str:
    value = value.strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        raise argparse.ArgumentTypeError("color must be a six-digit RGB hex value")
    return value.upper()


def rgb_to_ass_bgr(rgb: str) -> str:
    rgb = normalize_hex(rgb)
    return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}"


def mix_with_white(rgb: str, white_ratio: float) -> str:
    channels = [int(rgb[index : index + 2], 16) for index in (0, 2, 4)]
    mixed = [round(channel * (1 - white_ratio) + 255 * white_ratio) for channel in channels]
    return "".join(f"{channel:02X}" for channel in mixed)


def text_units(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+(?:[._/+:-][A-Za-z0-9]+)*|\\N|.", text)


def unit_width(unit: str) -> int:
    if re.fullmatch(r"[A-Za-z0-9._/+:-]+", unit):
        return max(1, math.ceil(len(unit) * 0.58))
    return 1


def wrap_ass_text(text: str, line_chars: int) -> str:
    del line_chars
    return " ".join(text.replace(r"\N", " ").split())


def convert_ass(
    source: Path,
    destination: Path,
    *,
    rgb: str,
    subtitle_rgb: str,
    subtitle_outline_rgb: str,
    font: str,
    font_size: int,
    style_name: str,
    speaker: str,
    line_chars: int,
) -> None:
    source_text = source.read_text(encoding="utf-8-sig")
    dialogue_lines = [line for line in source_text.splitlines() if line.startswith("Dialogue:")]
    del rgb
    ass_primary = rgb_to_ass_bgr(subtitle_rgb)
    ass_outline = rgb_to_ass_bgr(subtitle_outline_rgb)
    header = f"""[Script Info]
Title: Vertical livestream radio layout
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: 1920
PlayResY: 1080

[Aegisub Project Garbage]
Audio File: {destination.with_suffix('.mp4').name}
Video File: {destination.with_suffix('.mp4').name}
Video AR Mode: 4
Video AR Value: 1.777778

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {style_name},{font},{font_size},{ass_primary},{ass_primary},{ass_outline},&H50000000,-1,0,0,0,100,100,0.5,0,1,5,2.5,5,720,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    converted: list[str] = []
    for line in dialogue_lines:
        fields = line.split(",", 9)
        if len(fields) != 10:
            raise ValueError(f"Malformed ASS dialogue in {source.name}: {line}")
        fields[3] = style_name
        fields[4] = speaker
        fields[9] = wrap_ass_text(fields[9], line_chars)
        converted.append(",".join(fields))
    destination.write_text(header + "\n".join(converted) + "\n", encoding="utf-8-sig")


def render_video(ffmpeg: Path, source: Path, destination: Path, rgb: str) -> None:
    rgb = normalize_hex(rgb)
    canvas_tint = mix_with_white(rgb, 0.84)
    panel_tint = mix_with_white(rgb, 0.91)
    filter_graph = (
        "[0:v]split=3[leftsrc][bgsrc][panelsrc];"
        "[bgsrc]scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,boxblur=24:2,eq=saturation=0.45:brightness=0.08[bg];"
        f"color=c=0x{canvas_tint}:s=1920x1080:r=5,format=rgba,"
        "colorchannelmixer=aa=0.78[canvaswash];"
        "[bg][canvaswash]overlay=shortest=1[canvas];"
        "[panelsrc]scale=1228:1000:force_original_aspect_ratio=increase,"
        "crop=1228:1000,boxblur=32:2,eq=saturation=0.35:brightness=0.12[panelbg];"
        f"color=c=0x{panel_tint}:s=1228x1000:r=5,format=rgba,"
        "colorchannelmixer=aa=0.68[panelwash];"
        "[panelbg][panelwash]overlay=shortest=1[panel];"
        "[canvas][panel]overlay=x=666:y=40[withpanel];"
        "[leftsrc]scale=624:1080:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "setsar=1[left];"
        "[withpanel][left]overlay=x=16+(624-overlay_w)/2:y=(1080-overlay_h)/2[withleft];"
        f"[withleft]drawbox=x=640:y=0:w=4:h=1080:color=0x{rgb}@0.40:t=fill[outv]"
    )
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-filter_complex", filter_graph, "-map", "[outv]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", "30", "-c:a", "copy", "-movflags", "+faststart", "-shortest", str(destination),
    ]
    subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render 9:16 livestream/radio clips into the reviewed 16:9 radio layout."
    )
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--color", required=True, type=normalize_hex)
    parser.add_argument("--subtitle-fill-color", type=normalize_hex)
    parser.add_argument("--subtitle-outline-color", type=normalize_hex)
    parser.add_argument("--speaker", required=True)
    parser.add_argument("--style-name", default="CharacterRadio")
    parser.add_argument("--font", default="Microsoft YaHei")
    parser.add_argument("--font-size", type=int, default=65)
    parser.add_argument("--line-chars", type=int, default=14)
    args = parser.parse_args()

    if not args.ffmpeg.is_file():
        parser.error(f"FFmpeg not found: {args.ffmpeg}")
    clips = sorted(args.input_dir.glob("*.mp4"))
    if not clips:
        parser.error(f"No MP4 clips found in {args.input_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    for video in clips:
        source_ass = video.with_suffix(".ass")
        if not source_ass.is_file():
            raise SystemExit(f"Missing matching ASS: {source_ass}")
        output_video = args.output_dir / video.name
        output_ass = args.output_dir / source_ass.name
        safe_name = video.name.encode("ascii", "backslashreplace").decode("ascii")
        print(f"Rendering {safe_name}", flush=True)
        render_video(args.ffmpeg, video, output_video, args.color)
        convert_ass(
            source_ass,
            output_ass,
            rgb=args.color,
            subtitle_rgb=args.subtitle_fill_color or args.color,
            subtitle_outline_rgb=args.subtitle_outline_color or "#111318",
            font=args.font,
            font_size=args.font_size,
            style_name=args.style_name,
            speaker=args.speaker,
            line_chars=args.line_chars,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
