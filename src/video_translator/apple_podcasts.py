"""Apple Podcasts catalog lookup and RSS episode download."""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import httpx

from .commands import run_command
from .errors import InvalidSourceError, PipelineError


ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"


@dataclass(frozen=True)
class PodcastEpisode:
    index: int
    title: str
    audio_url: str
    guid: str | None
    published: str | None
    duration: str | None
    media_type: str | None


@dataclass(frozen=True)
class PodcastFeed:
    show_id: str
    title: str
    feed_url: str
    episodes: list[PodcastEpisode]


def apple_show_id(url: str) -> str:
    match = re.search(r"/id(\d+)(?:[/?#]|$)", url)
    if not match:
        raise InvalidSourceError(
            "Apple Podcasts URL 中没有节目 ID，预期包含 /id123456789。"
        )
    return match.group(1)


def apple_episode_id(url: str) -> str | None:
    match = re.search(r"[?&]i=(\d+)", url)
    return match.group(1) if match else None


def _episode_slug(url: str) -> str | None:
    if not apple_episode_id(url):
        return None
    parts = [part for part in urlsplit(url).path.split("/") if part]
    try:
        podcast_index = parts.index("podcast")
    except ValueError:
        return None
    tail = parts[podcast_index + 1 :]
    if len(tail) < 2:
        return None
    return unquote(tail[-2])


def _normalized_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", unquote(value)).casefold()
    return "".join(character for character in value if character.isalnum())


def _duration_text(item: ElementTree.Element) -> str | None:
    return item.findtext(f"{{{ITUNES_NS}}}duration")


def discover_podcast(apple_url: str, *, timeout: float = 30) -> PodcastFeed:
    """Resolve an Apple show URL to its publisher RSS feed."""

    parsed = urlsplit(apple_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "podcasts.apple.com":
        raise InvalidSourceError("请提供 podcasts.apple.com 的节目或单集链接。")
    show_id = apple_show_id(apple_url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/133 Safari/537.36"
        )
    }
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
            headers=headers,
        ) as client:
            lookup = client.get(
                "https://itunes.apple.com/lookup",
                params={"id": show_id, "entity": "podcast"},
            )
            lookup.raise_for_status()
            results = lookup.json().get("results") or []
            show = next(
                (
                    result
                    for result in results
                    if result.get("feedUrl")
                ),
                None,
            )
            if not show:
                raise PipelineError("Apple Lookup API 没有返回节目 RSS 地址。")
            feed_url = str(show["feedUrl"])
            feed_response = client.get(feed_url)
            feed_response.raise_for_status()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise PipelineError(f"读取 Apple Podcasts/RSS 失败：{exc}") from exc

    try:
        root = ElementTree.fromstring(feed_response.content)
    except ElementTree.ParseError as exc:
        raise PipelineError(f"播客 RSS XML 无效：{exc}") from exc
    channel = root.find("channel")
    if channel is None:
        raise PipelineError("RSS 中没有 channel。")

    episodes: list[PodcastEpisode] = []
    for index, item in enumerate(channel.findall("item"), start=1):
        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url") if enclosure is not None else None
        if not audio_url:
            continue
        audio_parsed = urlsplit(audio_url)
        if (
            audio_parsed.scheme not in {"http", "https"}
            or not audio_parsed.hostname
            or audio_parsed.username
            or audio_parsed.password
        ):
            continue
        title = (item.findtext("title") or f"Episode {index}").strip()
        episodes.append(
            PodcastEpisode(
                index=index,
                title=title,
                audio_url=audio_url,
                guid=(item.findtext("guid") or "").strip() or None,
                published=(item.findtext("pubDate") or "").strip() or None,
                duration=_duration_text(item),
                media_type=(
                    enclosure.get("type") if enclosure is not None else None
                ),
            )
        )
    if not episodes:
        raise PipelineError("RSS 中没有找到带 enclosure 的音频节目。")
    return PodcastFeed(
        show_id=show_id,
        title=str(
            show.get("collectionName")
            or channel.findtext("title")
            or show_id
        ),
        feed_url=feed_url,
        episodes=episodes,
    )


def match_apple_episode(
    apple_url: str,
    feed: PodcastFeed,
) -> PodcastEpisode | None:
    """Best-effort match of an Apple episode share-link slug to RSS title."""

    slug = _episode_slug(apple_url)
    if not slug:
        return None
    normalized_slug = _normalized_title(slug)
    exact = [
        episode
        for episode in feed.episodes
        if _normalized_title(episode.title) == normalized_slug
    ]
    if len(exact) == 1:
        return exact[0]
    partial = [
        episode
        for episode in feed.episodes
        if normalized_slug
        and (
            normalized_slug in _normalized_title(episode.title)
            or _normalized_title(episode.title) in normalized_slug
        )
    ]
    return partial[0] if len(partial) == 1 else None


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value, flags=re.UNICODE)
    return cleaned.strip("-._")[:100] or "podcast-episode"


def _audio_extension(episode: PodcastEpisode) -> str:
    suffix = Path(urlsplit(episode.audio_url).path).suffix.lower()
    if suffix in {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}:
        return suffix
    media_type = (episode.media_type or "").lower()
    return {
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/aac": ".aac",
        "audio/ogg": ".ogg",
    }.get(media_type, ".mp3")


def download_podcast_episode(
    episode: PodcastEpisode,
    output_dir: Path,
) -> Path:
    """Download one publisher-authorized RSS enclosure using resumable curl."""

    curl = shutil.which("curl")
    if not curl:
        raise PipelineError("系统中没有找到 curl。")
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / (
        f"{episode.index:03d}-{_safe_filename(episode.title)}"
        f"{_audio_extension(episode)}"
    )
    run_command(
        [
            curl,
            "--location",
            "--fail",
            "--retry",
            "10",
            "--retry-all-errors",
            "--retry-delay",
            "2",
            "--connect-timeout",
            "20",
            "--continue-at",
            "-",
            "--output",
            output,
            episode.audio_url,
        ]
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise PipelineError(f"播客音频下载后为空：{output}")
    return output


def write_podcast_summary(
    feed: PodcastFeed,
    downloaded: list[tuple[PodcastEpisode, Path]],
    output_dir: Path,
) -> Path:
    summary = output_dir / "download-summary.json"
    summary.write_text(
        json.dumps(
            {
                "show_id": feed.show_id,
                "title": feed.title,
                "feed_url": feed.feed_url,
                "downloads": [
                    {**asdict(episode), "path": str(path)}
                    for episode, path in downloaded
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return summary

