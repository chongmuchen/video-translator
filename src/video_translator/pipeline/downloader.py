"""Authorized URL and local-file acquisition."""

from __future__ import annotations

import ipaddress
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..errors import InvalidSourceError, PipelineError
from ..runtime import MediaBinaries
from ..settings import Settings


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    title: str
    metadata: dict


def validate_remote_url(url: str, settings: Settings) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise InvalidSourceError("只允许 http/https 视频链接。")
    if parsed.username or parsed.password:
        raise InvalidSourceError("链接中不能包含用户名或密码。")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise InvalidSourceError("视频链接缺少域名。")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise InvalidSourceError("不允许使用 IP 地址作为视频源。")

    allowed = settings.allowed_host_set
    if host not in allowed:
        raise InvalidSourceError(
            f"不支持域名 {host}。允许的域名：{', '.join(sorted(allowed))}"
        )
    return url.strip()


def _copy_local_file(source: Path, job_dir: Path) -> DownloadResult:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise InvalidSourceError(f"本地视频不存在：{source}")
    if source.suffix.lower() not in {
        ".mp4",
        ".mkv",
        ".mov",
        ".webm",
        ".m4v",
    }:
        raise InvalidSourceError("仅支持 mp4、mkv、mov、webm、m4v 文件。")
    target = job_dir / f"source{source.suffix.lower()}"
    shutil.copy2(source, target)
    return DownloadResult(
        path=target,
        title=source.stem,
        metadata={"source_type": "local", "original_path": str(source)},
    )


def acquire_source(
    source: str,
    job_dir: Path,
    settings: Settings,
    media: MediaBinaries,
    logger: logging.Logger,
) -> DownloadResult:
    """Copy a local source or download an authorized remote source."""

    if "://" not in source:
        possible_file = Path(source).expanduser()
        if possible_file.is_file():
            return _copy_local_file(possible_file, job_dir)

    url = validate_remote_url(source, settings)
    try:
        import yt_dlp
    except ImportError as exc:
        raise PipelineError("缺少 yt-dlp，请重新运行 bootstrap。") from exc

    height = settings.max_download_height
    options: dict = {
        "format": (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        ),
        "merge_output_format": "mp4",
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "restrictfilenames": True,
        "ffmpeg_location": media.directory,
        "continuedl": True,
        "overwrites": False,
    }
    venv_deno = Path(sys.prefix) / "bin" / "deno"
    if venv_deno.is_file():
        options["js_runtimes"] = {"deno": {"path": str(venv_deno)}}
    if settings.cookies_from_browser:
        options["cookiesfrombrowser"] = (settings.cookies_from_browser,)
    if settings.cookie_file:
        options["cookiefile"] = str(settings.cookie_file.expanduser())

    logger.info("读取视频信息: %s", url)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") in {"playlist", "multi_video"}:
                raise InvalidSourceError("当前版本只允许单个视频，不处理播放列表。")
            duration = int(info.get("duration") or 0)
            if duration and duration > settings.max_video_seconds:
                raise InvalidSourceError(
                    f"视频时长 {duration} 秒，超过限制 "
                    f"{settings.max_video_seconds} 秒。"
                )
            logger.info("开始下载: %s", info.get("title") or url)
            info = ydl.extract_info(url, download=True)
    except InvalidSourceError:
        raise
    except Exception as exc:
        raise PipelineError(f"下载失败：{exc}") from exc

    candidates = sorted(
        path
        for path in job_dir.glob("source.*")
        if path.suffix.lower()
        not in {".json", ".part", ".ytdl", ".description", ".vtt"}
        and path.is_file()
    )
    if not candidates:
        raise PipelineError("下载结束但没有找到视频文件。")
    video_path = max(candidates, key=lambda item: item.stat().st_size)
    public_metadata = {
        "source_type": "remote",
        "extractor": info.get("extractor"),
        "webpage_url": info.get("webpage_url") or url,
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "id": info.get("id"),
    }
    return DownloadResult(
        path=video_path,
        title=info.get("title") or video_path.stem,
        metadata=public_metadata,
    )
