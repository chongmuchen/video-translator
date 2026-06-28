"""Discover and select entries from Bilibili anthologies or site playlists."""

from __future__ import annotations

import sys
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .errors import InvalidSourceError, PipelineError
from .pipeline.downloader import explain_download_error, validate_remote_url
from .settings import Settings


@dataclass(frozen=True)
class CollectionEntry:
    index: int
    url: str
    title: str


@dataclass(frozen=True)
class VideoCollection:
    source_url: str
    title: str
    entries: list[CollectionEntry]

    @property
    def total(self) -> int:
        return len(self.entries)


def bilibili_bvid(url: str) -> str | None:
    match = re.search(r"/(BV[0-9A-Za-z]{10})(?:[/#?]|$)", url)
    return match.group(1) if match else None


def without_bilibili_part(url: str) -> str:
    """Remove only the Bilibili ``p`` query so the extractor sees all parts."""

    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host not in {"bilibili.com", "www.bilibili.com"}:
        return url
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "p"
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


def parse_part_spec(spec: str, total: int) -> list[int]:
    """Parse ``1-3,5,68`` into sorted, unique, one-based indexes."""

    if total < 1:
        raise ValueError("合集没有可选分集。")
    selected: set[int] = set()
    for raw_token in spec.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ValueError(f"无效分集范围：{token}") from exc
            if start > end:
                raise ValueError(f"分集范围起点不能大于终点：{token}")
            selected.update(range(start, end + 1))
        else:
            try:
                selected.add(int(token))
            except ValueError as exc:
                raise ValueError(f"无效分集编号：{token}") from exc
    if not selected:
        raise ValueError("没有选择任何分集。")
    invalid = sorted(index for index in selected if index < 1 or index > total)
    if invalid:
        raise ValueError(
            f"分集编号超出 1-{total}：{', '.join(map(str, invalid))}"
        )
    return sorted(selected)


def _collection_options(settings: Settings) -> dict:
    options: dict = {
        "extract_flat": "in_playlist",
        "lazy_playlist": False,
        "noplaylist": False,
        "skip_download": True,
        "quiet": True,
        "no_warnings": False,
        "extractor_retries": 3,
        "socket_timeout": settings.download_socket_timeout,
    }
    venv_deno = Path(sys.prefix) / "bin" / "deno"
    if venv_deno.is_file():
        options["js_runtimes"] = {"deno": {"path": str(venv_deno)}}
    if settings.cookies_from_browser:
        options["cookiesfrombrowser"] = (settings.cookies_from_browser,)
    if settings.cookie_file:
        options["cookiefile"] = str(settings.cookie_file.expanduser())
    if settings.download_proxy is not None:
        options["proxy"] = settings.download_proxy
    if settings.download_impersonate:
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget

            options["impersonate"] = ImpersonateTarget.from_str(
                settings.download_impersonate
            )
        except (ImportError, ValueError) as exc:
            raise PipelineError(
                f"无效的浏览器模拟目标：{settings.download_impersonate}"
            ) from exc
    return options


def _discover_bilibili_collection(
    url: str,
    settings: Settings,
) -> VideoCollection | None:
    """Use Bilibili's lightweight view API, avoiding webpage/playurl 412s."""

    bvid = bilibili_bvid(url)
    if not bvid:
        return None
    api_url = "https://api.bilibili.com/x/web-interface/view"
    proxy = settings.download_proxy or None
    try:
        with httpx.Client(
            timeout=settings.download_socket_timeout,
            proxy=proxy,
            trust_env=False,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/133 Safari/537.36"
                ),
                "Referer": f"https://www.bilibili.com/video/{bvid}/",
            },
        ) as client:
            response = client.get(api_url, params={"bvid": bvid})
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    if payload.get("code") != 0:
        return None
    data = payload.get("data") or {}
    pages = data.get("pages") or []
    if not pages:
        return None
    entries = [
        CollectionEntry(
            index=int(page.get("page") or fallback_index),
            url=(
                f"https://www.bilibili.com/video/{bvid}"
                f"?p={int(page.get('page') or fallback_index)}"
            ),
            title=str(page.get("part") or f"P{fallback_index:03d}"),
        )
        for fallback_index, page in enumerate(pages, start=1)
    ]
    entries.sort(key=lambda item: item.index)
    return VideoCollection(
        source_url=f"https://www.bilibili.com/video/{bvid}",
        title=str(data.get("title") or bvid),
        entries=entries,
    )


def discover_collection(url: str, settings: Settings) -> VideoCollection:
    """Return flat collection entries without downloading their media."""

    validated = validate_remote_url(url, settings)
    collection_url = without_bilibili_part(validated)
    is_bilibili = "bilibili.com" in (urlsplit(collection_url).hostname or "")
    if is_bilibili:
        direct_result = _discover_bilibili_collection(
            collection_url,
            settings,
        )
        if direct_result:
            return direct_result
    try:
        import yt_dlp
    except ImportError as exc:
        raise PipelineError("缺少 yt-dlp，请重新运行 bootstrap。") from exc

    try:
        with yt_dlp.YoutubeDL(_collection_options(settings)) as ydl:
            info = ydl.extract_info(collection_url, download=False)
    except Exception as exc:
        raise PipelineError(
            explain_download_error(exc, is_bilibili=is_bilibili)
        ) from exc

    if not info:
        raise PipelineError("没有读取到合集信息。")
    raw_entries = info.get("entries")
    if raw_entries is None:
        single_url = (
            info.get("webpage_url")
            or info.get("original_url")
            or collection_url
        )
        return VideoCollection(
            source_url=collection_url,
            title=info.get("title") or "单集视频",
            entries=[
                CollectionEntry(
                    index=1,
                    url=validate_remote_url(single_url, settings),
                    title=info.get("title") or "P001",
                )
            ],
        )

    entries: list[CollectionEntry] = []
    for fallback_index, raw_entry in enumerate(raw_entries, start=1):
        if not raw_entry:
            continue
        entry_url = raw_entry.get("webpage_url") or raw_entry.get("url")
        if not entry_url:
            continue
        if not str(entry_url).startswith(("http://", "https://")):
            raise InvalidSourceError(f"合集分集 URL 无效：{entry_url}")
        index = int(raw_entry.get("playlist_index") or fallback_index)
        entries.append(
            CollectionEntry(
                index=index,
                url=validate_remote_url(str(entry_url), settings),
                title=raw_entry.get("title") or f"P{index:03d}",
            )
        )
    if not entries:
        raise PipelineError("合集存在，但没有解析出任何分集 URL。")
    entries.sort(key=lambda item: item.index)
    return VideoCollection(
        source_url=collection_url,
        title=info.get("title") or info.get("id") or "视频合集",
        entries=entries,
    )
