import wave
from pathlib import Path

import pytest

from video_translator.models import Segment
from video_translator.pipeline.media import (
    _format_srt_timestamp,
    atempo_chain,
    compose_dub_timeline,
    write_srt,
)


def make_silence(path: Path, seconds: float, rate: int = 24000) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\x00\x00" * round(seconds * rate))


def test_srt_timestamp_rollover() -> None:
    assert _format_srt_timestamp(3661.234) == "01:01:01,234"


def test_atempo_chain() -> None:
    assert atempo_chain(1.25) == "atempo=1.250000"
    assert atempo_chain(4.5) == (
        "atempo=2.000000,atempo=2.000000,atempo=1.125000"
    )
    with pytest.raises(ValueError):
        atempo_chain(0)


def test_write_srt(tmp_path: Path) -> None:
    output = write_srt(
        [
            Segment(
                index=0,
                start=1.2,
                end=2.3,
                source_text="hello",
                translated_text="你好",
            )
        ],
        tmp_path / "subtitles.srt",
    )
    text = output.read_text(encoding="utf-8")
    assert "00:00:01,200 --> 00:00:02,300" in text
    assert "你好" in text


def test_compose_timeline_preserves_duration(tmp_path: Path) -> None:
    clip = tmp_path / "clip.wav"
    output = tmp_path / "timeline.wav"
    make_silence(clip, 1.0)
    compose_dub_timeline(
        [
            Segment(
                index=0,
                start=1.0,
                end=2.0,
                source_text="hello",
                translated_text="你好",
            )
        ],
        [clip],
        output,
        total_duration=3.0,
        sample_rate=24000,
        logger=__import__("logging").getLogger("test"),
    )
    with wave.open(str(output), "rb") as result:
        assert result.getnframes() == 3 * 24000

