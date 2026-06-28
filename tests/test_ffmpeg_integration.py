import logging
from pathlib import Path

from video_translator.commands import run_command
from video_translator.pipeline.media import mux_video, probe_media
from video_translator.runtime import resolve_media_binaries
from video_translator.settings import Settings


def test_mux_produces_dub_original_and_subtitle_tracks(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path)
    media = resolve_media_binaries(settings)
    source = tmp_path / "source.mp4"
    dub = tmp_path / "dub.wav"
    subtitles = tmp_path / "zh.srt"
    output = tmp_path / "output.mp4"

    run_command(
        [
            media.ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x243447:s=320x240:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            source,
        ]
    )
    run_command(
        [
            media.ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=2",
            "-ac",
            "1",
            "-ar",
            "24000",
            dub,
        ]
    )
    subtitles.write_text(
        "1\n00:00:00,200 --> 00:00:01,500\n测试字幕\n",
        encoding="utf-8",
    )

    mux_video(
        source,
        dub,
        subtitles,
        output,
        settings=settings,
        media=media,
        keep_original_audio=True,
        duck_original_audio=True,
        burn_subtitles=False,
        background_audio=None,
        logger=logging.getLogger("integration"),
    )
    streams = probe_media(output, media)["streams"]
    assert sum(item["codec_type"] == "video" for item in streams) == 1
    assert sum(item["codec_type"] == "audio" for item in streams) == 2
    assert sum(item["codec_type"] == "subtitle" for item in streams) == 1

