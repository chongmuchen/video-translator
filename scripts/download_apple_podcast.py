#!/usr/bin/env python3
"""List and download Apple Podcasts episodes through the publisher RSS feed."""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from video_translator.apple_podcasts import (  # noqa: E402
    discover_podcast,
    download_podcast_episode,
    match_apple_episode,
    write_podcast_summary,
)
from video_translator.collection import parse_part_spec  # noqa: E402
from video_translator.errors import VideoTranslatorError  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 Apple Podcasts 节目 URL 解析 RSS 并下载音频",
    )
    parser.add_argument("url", help="Apple Podcasts 节目或单集 URL")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="下载 RSS 中全部节目")
    selection.add_argument("--latest", type=int, help="下载最新 N 集")
    selection.add_argument(
        "--episodes",
        help="按列表序号选择，如 1-3,8；1 通常是 RSS 最新一集",
    )
    parser.add_argument("--list-only", action="store_true", help="只列出节目")
    parser.add_argument(
        "--output-dir",
        default="work/podcasts",
        help="输出根目录，默认 work/podcasts",
    )
    parser.add_argument(
        "--sleep-between",
        type=float,
        default=1.0,
        help="多集下载间隔秒数",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        feed = discover_podcast(args.url)
        print(f"节目：{feed.title}")
        print(f"RSS： {feed.feed_url}")
        print(f"集数：{len(feed.episodes)}\n")
        for episode in feed.episodes:
            details = " | ".join(
                value
                for value in (episode.published, episode.duration)
                if value
            )
            print(
                f"  {episode.index:03d}  {episode.title}"
                f"{f'  [{details}]' if details else ''}"
            )

        matched = match_apple_episode(args.url, feed)
        if args.list_only:
            raise SystemExit(0)
        if args.all:
            selected = feed.episodes
        elif args.latest is not None:
            if args.latest < 1:
                raise ValueError("--latest 必须大于 0。")
            selected = feed.episodes[: args.latest]
        elif args.episodes:
            indexes = set(
                parse_part_spec(args.episodes, len(feed.episodes))
            )
            selected = [
                episode
                for episode in feed.episodes
                if episode.index in indexes
            ]
        elif matched:
            selected = [matched]
            print(f"\n已根据 Apple 单集链接匹配：{matched.title}")
        else:
            print(
                "\n当前只预览，没有下载。请使用 --latest N、"
                "--episodes 1-3 或 --all。"
            )
            raise SystemExit(0)

        show_dir = (
            Path(args.output_dir).expanduser().resolve()
            / re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", feed.title).strip("-._")
        )
        downloaded = []
        for position, episode in enumerate(selected, start=1):
            print(
                f"\n[{position}/{len(selected)}] 下载：{episode.title}"
            )
            output = download_podcast_episode(episode, show_dir)
            downloaded.append((episode, output))
            print(f"完成：{output}")
            if position < len(selected) and args.sleep_between > 0:
                time.sleep(args.sleep_between)
        summary = write_podcast_summary(feed, downloaded, show_dir)
        print(f"\n下载清单：{summary}")
        print("下一步示例：")
        for _, output in downloaded:
            print(f"  .venv/bin/python main.py download {output!s}")
    except (VideoTranslatorError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
