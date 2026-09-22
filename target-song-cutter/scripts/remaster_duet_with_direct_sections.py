from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
from pathlib import Path


def parse_ass_time(value: str) -> float:
    hours, minutes, seconds = value.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def format_ass_time(value: float) -> str:
    centiseconds = max(0, round(value * 100))
    hours, centiseconds = divmod(centiseconds, 360_000)
    minutes, centiseconds = divmod(centiseconds, 6_000)
    seconds, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def shift_ass(source: Path, destination: Path, offset: float, duration: float) -> int:
    output: list[str] = []
    cue_count = 0
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        if not line.startswith("Dialogue:"):
            output.append(line)
            continue
        parts = line.split(",", 9)
        if len(parts) != 10:
            continue
        start = parse_ass_time(parts[1]) - offset
        end = parse_ass_time(parts[2]) - offset
        if end <= 0 or start >= duration:
            continue
        parts[1] = format_ass_time(max(0.0, start))
        parts[2] = format_ass_time(min(duration, end))
        if parse_ass_time(parts[2]) > parse_ass_time(parts[1]):
            output.append(",".join(parts))
            cue_count += 1
    destination.write_text("\n".join(output) + "\n", encoding="utf-8-sig")
    return cue_count


def resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replace separated duet vocals with long direct-mix sections and musical crossfades."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    base = manifest_path.parent
    data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    paths = {
        "composite_video": resolve(base, data["composite_video"]),
        "composite_ass": resolve(base, data["composite_ass"]),
        "master_audio": resolve(base, data["master_audio"]),
        "guest_audio": resolve(base, data["guest_audio"]),
    }
    for path in paths.values():
        if not path.exists():
            raise FileNotFoundError(path)

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    trim_start = float(data["trim_start"])
    duration = float(data["duration"])
    section = data["guest_section"]
    source_start = float(section["source_start"])
    source_end = float(section["source_end"])
    target_start = float(section["target_start"])
    target_end = float(section["target_end"])
    fade_in = float(section.get("fade_in", 1.0))
    fade_out = float(section.get("fade_out", 1.5))
    gain_db = float(section.get("gain_db", 0.0))
    if not 0 <= target_start < target_end <= duration:
        raise ValueError("guest target section must stay inside output duration")
    if source_end <= source_start:
        raise ValueError("guest source section is empty")
    if fade_in <= 0 or fade_out <= 0 or fade_in + fade_out >= target_end - target_start:
        raise ValueError("invalid crossfade lengths")

    source_length = source_end - source_start
    target_length = target_end - target_start
    tempo = source_length / target_length
    fade_in_end = target_start + fade_in
    fade_out_start = target_end - fade_out
    delay_ms = round(target_start * 1000)
    master_volume = (
        f"if(lt(t,{target_start:.6f}),1,"
        f"if(lt(t,{fade_in_end:.6f}),cos((t-{target_start:.6f})/{fade_in:.6f}*PI/2),"
        f"if(lt(t,{fade_out_start:.6f}),0,"
        f"if(lt(t,{target_end:.6f}),sin((t-{fade_out_start:.6f})/{fade_out:.6f}*PI/2),1))))"
    )
    filters = [
        f"[0:v]trim=start={trim_start:.6f}:end={trim_start + duration:.6f},"
        "setpts=PTS-STARTPTS,fps=30,format=yuv420p[vout]",
        f"[1:a]atrim=start={trim_start:.6f}:end={trim_start + duration:.6f},"
        f"asetpts=PTS-STARTPTS,volume='{master_volume}':eval=frame[master]",
        f"[2:a]atrim=start={source_start:.6f}:end={source_end:.6f},asetpts=PTS-STARTPTS,"
        f"rubberband=tempo={tempo:.10f}:pitch=1:transients=mixed:detector=soft:phase=laminar:"
        "window=long:formant=preserved:pitchq=quality:channels=together,"
        f"atrim=duration={target_length:.6f},volume={gain_db:.3f}dB,"
        f"afade=t=in:st=0:d={fade_in:.6f}:curve=qsin,"
        f"afade=t=out:st={target_length - fade_out:.6f}:d={fade_out:.6f}:curve=qsin,"
        f"adelay={delay_ms}|{delay_ms}[guest]",
        "[master][guest]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        "alimiter=limit=0.95,loudnorm=I=-14:TP=-1.5:LRA=11[aout]",
    ]
    command = [
        args.ffmpeg,
        "-y",
        "-v",
        "warning",
        "-i",
        str(paths["composite_video"]),
        "-i",
        str(paths["master_audio"]),
        "-i",
        str(paths["guest_audio"]),
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-t",
        f"{duration:.6f}",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "256k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(output),
    ]
    subprocess.run(command, check=True)

    ass = output.with_suffix(".ass")
    cue_count = shift_ass(paths["composite_ass"], ass, trim_start, duration)
    vocal_map = output.with_name(output.stem + "-audio-map.csv")
    with vocal_map.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "mode",
                "source_start",
                "source_end",
                "target_start",
                "target_end",
                "tempo_factor",
                "gain_db",
                "fade_in",
                "fade_out",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "mode": "direct original mix; complete section",
                "source_start": f"{source_start:.3f}",
                "source_end": f"{source_end:.3f}",
                "target_start": f"{target_start:.3f}",
                "target_end": f"{target_end:.3f}",
                "tempo_factor": f"{tempo:.6f}",
                "gain_db": f"{gain_db:.2f}",
                "fade_in": f"{fade_in:.2f}",
                "fade_out": f"{fade_out:.2f}",
            }
        )
    print(
        json.dumps(
            {
                "output": str(output),
                "duration": duration,
                "trimmed_front": trim_start,
                "audio_mode": "direct original mixes with one long guest section",
                "guest_section": [target_start, target_end],
                "subtitle_cues": cue_count,
                "ass": str(ass),
                "audio_map": str(vocal_map),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
