#!/usr/bin/env python3
"""Burn matching ASS files into rendered MP4 clips."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from windows_process import hidden_subprocess_kwargs
from media_packaging import build_burn_command, load_media_packaging


def validate_ass_styles(path: Path) -> None:
    from asr_hotword_guard import require_clean_artifact
    require_clean_artifact(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    defined = {
        line.split(",", 1)[0].split(":", 1)[1].strip()
        for line in lines
        if line.startswith("Style:") and "," in line
    }
    used = {
        fields[3].strip()
        for line in lines
        if line.startswith("Dialogue:") and len(fields := line.split(",", 9)) == 10
    }
    missing = sorted(used - defined)
    if missing:
        raise ValueError(
            f"{path.name}: dialogue uses undefined ASS styles: {', '.join(missing)}"
        )


def filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    return value.replace(":", r"\:").replace("'", r"\'")


FONT_SUFFIXES = {".ttf", ".ttc", ".otf", ".otc"}


def prepare_fontsdir(
    root: Path | None,
) -> tuple[Path | None, tempfile.TemporaryDirectory[str] | None]:
    """Flatten nested bundled fonts because libass does not recurse fontsdir."""
    if root is None:
        return None, None
    root = root.resolve()
    fonts = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in FONT_SUFFIXES
    )
    if not fonts:
        raise FileNotFoundError(f"No font files found under {root}")
    if all(path.parent == root for path in fonts):
        return root, None
    temporary = tempfile.TemporaryDirectory(prefix="ass-fonts-")
    flat = Path(temporary.name)
    used: set[str] = set()
    for index, font in enumerate(fonts, 1):
        name = font.name
        key = name.casefold()
        if key in used:
            name = f"{index:03d}-{name}"
        used.add(name.casefold())
        shutil.copy2(font, flat / name)
    return flat, temporary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clips", type=Path, required=True)
    parser.add_argument("--ass", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument(
        "--fontsdir",
        type=Path,
        help="Optional directory of local fonts made available to libass.",
    )
    parser.add_argument(
        "--prefix",
        action="append",
        help="Only burn clips whose filename starts with one of these prefixes.",
    )
    parser.add_argument(
        "--trim",
        action="append",
        default=[],
        metavar="PREFIX=SECONDS",
        help="Optionally stop a selected output at an exact duration.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Maximum concurrent burns. Defaults to 2 to keep the PC responsive.",
    )
    parser.add_argument("--media-packaging", type=Path, help="Optional enabled intro/outro configuration JSON")
    parser.add_argument("--transitions", type=Path, help="Optional video-name to local narrative transitions JSON")
    args = parser.parse_args()
    packaging = load_media_packaging(args.media_packaging, check_files=True) if args.media_packaging else None
    transition_map = json.loads(args.transitions.read_text(encoding="utf-8-sig")) if args.transitions else {}
    packaging_metadata = {}
    args.output.mkdir(parents=True, exist_ok=True)
    fontsdir, fonts_workspace = prepare_fontsdir(args.fontsdir)
    trims: dict[str, float] = {}
    for item in args.trim:
        prefix, separator, seconds = item.partition("=")
        if not separator or not prefix or float(seconds) <= 0:
            raise ValueError(f"Invalid --trim value: {item!r}; expected PREFIX=SECONDS")
        trims[prefix] = float(seconds)

    tasks: list[tuple[Path, Path, Path, list[str]]] = []
    for clip in sorted(args.clips.glob("*.mp4")):
        if args.prefix and not any(clip.name.startswith(prefix) for prefix in args.prefix):
            continue
        subtitle = args.ass / f"{clip.stem}.ass"
        if not subtitle.is_file():
            raise FileNotFoundError(subtitle)
        validate_ass_styles(subtitle)
        destination = args.output / clip.name
        subtitle_filter = f"ass='{filter_path(subtitle)}'"
        if fontsdir:
            subtitle_filter += f":fontsdir='{filter_path(fontsdir)}'"
        trim = next(
            (seconds for prefix, seconds in trims.items() if clip.name.startswith(prefix)), None,
        )
        command, metadata = build_burn_command(
            args.ffmpeg, clip, subtitle_filter, destination,
            packaging=packaging, transitions=transition_map.get(clip.name, []), trim=trim,
        )
        packaging_metadata[destination] = metadata
        tasks.append((clip, subtitle, destination, command))

    def burn_one(task: tuple[Path, Path, Path, list[str]]) -> Path:
        _clip, _subtitle, destination, command = task
        subprocess.run(command, check=True, **hidden_subprocess_kwargs())
        metadata = packaging_metadata[destination]
        if metadata["intro_seconds"] or metadata["outro_seconds"] or metadata["transitions"]:
            destination.with_suffix(".packaging.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
        else:
            destination.with_suffix(".packaging.json").unlink(missing_ok=True)
        return destination

    workers = max(1, min(int(args.workers), len(tasks) or 1))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="subtitle-burn") as pool:
        futures = {pool.submit(burn_one, task): task for task in tasks}
        for future in as_completed(futures):
            destination = future.result()
            safe_destination = (
                str(destination).encode("ascii", "backslashreplace").decode("ascii")
            )
            print(safe_destination, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
