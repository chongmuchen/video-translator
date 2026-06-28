"""Locate or provision media executables."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError
from .settings import Settings


@dataclass(frozen=True)
class MediaBinaries:
    ffmpeg: str
    ffprobe: str

    @property
    def directory(self) -> str:
        return str(Path(self.ffmpeg).resolve().parent)


def _valid_executable(value: str | None) -> str | None:
    if not value:
        return None
    located = shutil.which(value)
    if located:
        return located
    path = Path(value).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return str(path.resolve())
    return None


def _local_media_pair(runtime_dir: Path) -> MediaBinaries | None:
    """Find static-ffmpeg layouts and repair executable bits after unzip."""

    candidates = [runtime_dir / "ffmpeg"]
    if runtime_dir.is_dir():
        candidates.extend(path for path in runtime_dir.iterdir() if path.is_dir())
    for directory in candidates:
        ffmpeg_path = directory / "ffmpeg"
        ffprobe_path = directory / "ffprobe"
        if ffmpeg_path.is_file() and ffprobe_path.is_file():
            for executable in (ffmpeg_path, ffprobe_path):
                executable.chmod(executable.stat().st_mode | 0o755)
            return MediaBinaries(
                ffmpeg=str(ffmpeg_path.resolve()),
                ffprobe=str(ffprobe_path.resolve()),
            )
    return None


def resolve_media_binaries(
    settings: Settings,
    *,
    allow_download: bool = False,
) -> MediaBinaries:
    """Find ffmpeg/ffprobe, optionally fetching project-local static builds."""

    ffmpeg = _valid_executable(settings.ffmpeg_bin) or _valid_executable("ffmpeg")
    ffprobe = _valid_executable(settings.ffprobe_bin) or _valid_executable("ffprobe")

    if ffmpeg and ffprobe:
        return MediaBinaries(ffmpeg=ffmpeg, ffprobe=ffprobe)

    local = _local_media_pair(settings.runtime_dir)
    if local:
        return local

    if allow_download:
        try:
            from static_ffmpeg.run import (  # type: ignore[import-untyped]
                get_platform_key,
                get_or_fetch_platform_executables_else_raise,
            )
        except ImportError as exc:
            raise ConfigurationError(
                "缺少 static-ffmpeg；请安装 .[media] 或自行安装 FFmpeg。"
            ) from exc

        platform_dir = settings.runtime_dir / get_platform_key()
        fetched_ffmpeg, fetched_ffprobe = (
            get_or_fetch_platform_executables_else_raise(
                download_dir=str(platform_dir)
            )
        )
        return MediaBinaries(ffmpeg=fetched_ffmpeg, ffprobe=fetched_ffprobe)

    raise ConfigurationError(
        "没有找到 ffmpeg 和 ffprobe。请运行 scripts/bootstrap.sh，"
        "或在 .env 中配置 VT_FFMPEG_BIN/VT_FFPROBE_BIN。"
    )
