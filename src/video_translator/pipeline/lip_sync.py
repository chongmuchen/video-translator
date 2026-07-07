"""Optional external lip-sync integration."""

from __future__ import annotations

import logging
import shlex
from pathlib import Path

from ..commands import run_command
from ..errors import ConfigurationError, PipelineError
from ..runtime import MediaBinaries
from ..settings import Settings


def apply_lip_sync(
    *,
    source_video: Path,
    muxed_video: Path,
    dub_audio: Path,
    subtitles: Path,
    output: Path,
    settings: Settings,
    media: MediaBinaries,
    logger: logging.Logger,
) -> Path:
    """Run a user-configured lip-sync command template.

    The command can reference:
    ``{video}`` original source video, ``{muxed}`` already mixed/subtitled MP4,
    ``{audio}`` dub timeline WAV, ``{subtitles}`` SRT, ``{output}`` target MP4,
    ``{ffmpeg}`` and ``{ffprobe}``.
    """

    if not settings.lip_sync_command:
        raise ConfigurationError(
            "启用 lip-sync 需要配置 VT_LIP_SYNC_COMMAND，例如：\n"
            "python /path/Wav2Lip/inference.py --checkpoint_path /path/model.pth "
            "--face {video} --audio {audio} --outfile {output}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        command_text = settings.lip_sync_command.format(
            video=str(source_video),
            muxed=str(muxed_video),
            audio=str(dub_audio),
            subtitles=str(subtitles),
            output=str(output),
            ffmpeg=media.ffmpeg,
            ffprobe=media.ffprobe,
        )
    except KeyError as exc:
        raise ConfigurationError(
            f"lip-sync 命令模板包含未知变量：{exc}"
        ) from exc
    logger.info("执行 lip-sync 外部命令：%s", command_text)
    run_command(shlex.split(command_text), logger=logger)
    if not output.is_file() or output.stat().st_size == 0:
        raise PipelineError("lip-sync 命令没有生成有效输出。")
    return output
