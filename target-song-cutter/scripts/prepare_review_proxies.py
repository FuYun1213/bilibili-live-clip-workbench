#!/usr/bin/env python3
"""Prepare source-backed streaming review timelines without padded proxy video."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import review_workspace


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-dir", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    args = parser.parse_args()
    review_dir = args.review_dir.expanduser().resolve()
    videos = sorted(review_dir.glob("*.mp4"))
    if not videos:
        raise SystemExit(f"审核目录没有 MP4：{review_dir}")
    for index, video in enumerate(videos, 1):
        if review_workspace.has_streaming_review(video):
            print(
                f"[{index}/{len(videos)}] 整场源时间轴已就绪，跳过：{video.name}",
                flush=True,
            )
            continue
        print(
            f"[{index}/{len(videos)}] 准备整场源时间轴：{video.name}",
            flush=True,
        )
        review_workspace.prepare_streaming_review(video, args.ffmpeg)
    print(
        f"完成：{len(videos)} 条流式审核时间轴；未生成前置/后置代理视频",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())