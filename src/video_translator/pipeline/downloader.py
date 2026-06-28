"""Authorized URL and local-file acquisition."""

from __future__ import annotations

import ipaddress
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..errors import InvalidSourceError, PipelineError
from ..runtime import MediaBinaries
from ..settings import Settings


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    title: str
    metadata: dict


def explain_download_error(
    error: Exception,
    *,
    is_bilibili: bool,
    is_apple_podcasts: bool = False,
) -> str:
    message = str(error)
    if is_apple_podcasts and (
        "No video formats found" in message
        or "serialized-server-data" in message
    ):
        return (
            "Apple Podcasts 网页提取失败。请改用 RSS 下载器："
            "make podcast URL='APPLE_PODCAST_URL' "
            "PODCAST_ARGS='--latest 1'。原始错误："
            f"{message}"
        )
    if "UNEXPECTED_EOF_WHILE_READING" in message:
        return (
            "媒体传输时 TLS 连接被提前断开。程序已启用 curl、分块和重试；"
            "若仍失败，请用 --proxy direct 绕过系统代理，或检查代理软件的"
            " B站分流规则。原始错误："
            f"{message}"
        )
    if is_bilibili and ("HTTP Error 412" in message or "Precondition Failed" in message):
        return (
            "B站拒绝了未通过风控校验的元数据请求（HTTP 412）。"
            "请稍后重试，并使用 --cookies-from-browser chrome；"
            "若当前开启全局代理，再尝试 --proxy direct。原始错误："
            f"{message}"
        )
    if "HTTP Error 403" in message:
        return (
            "站点拒绝访问（HTTP 403）。请确认视频权限并提供有效 Cookies。"
            f"原始错误：{message}"
        )
    return f"下载失败：{message}"


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
    if host == "podcasts.apple.com" and not parse_qs(parsed.query).get("i"):
        raise InvalidSourceError(
            "Apple Podcasts 当前只支持单集链接，URL 必须包含 ?i=单集ID。"
            "请在 Apple Podcasts 中打开具体一集后复制分享链接；"
            "整档节目批量下载尚未实现。"
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
        ".mp3",
        ".m4a",
        ".aac",
        ".wav",
        ".flac",
        ".ogg",
        ".opus",
    }:
        raise InvalidSourceError(
            "本地媒体仅支持 mp4、mkv、mov、webm、m4v、"
            "mp3、m4a、aac、wav、flac、ogg、opus。"
        )
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
    host = (urlparse(url).hostname or "").lower()
    is_bilibili = host in {
        "bilibili.com",
        "www.bilibili.com",
        "b23.tv",
    }
    is_apple_podcasts = host == "podcasts.apple.com"
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
        "retries": max(0, settings.download_retries),
        "fragment_retries": max(0, settings.download_fragment_retries),
        "file_access_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": settings.download_socket_timeout,
        "http_chunk_size": settings.download_http_chunk_size,
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

    curl_path = shutil.which("curl")
    if settings.download_backend == "curl" and not curl_path:
        raise PipelineError("指定了 curl 下载后端，但系统中没有找到 curl。")
    use_curl = (
        settings.download_backend == "curl"
        or (
            settings.download_backend == "auto"
            and is_bilibili
            and curl_path is not None
        )
    )
    if use_curl and curl_path:
        options["external_downloader"] = {"default": curl_path}
        options["external_downloader_args"] = {
            "curl": [
                "--retry-all-errors",
                "--retry-delay",
                "2",
                "--connect-timeout",
                "20",
            ]
        }
        logger.info("B站媒体下载后端: curl（带断点续传和重试）")
    else:
        logger.info("媒体下载后端: yt-dlp native（带分块和重试）")

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
        raise PipelineError(
            explain_download_error(
                exc,
                is_bilibili=is_bilibili,
                is_apple_podcasts=is_apple_podcasts,
            )
        ) from exc

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
