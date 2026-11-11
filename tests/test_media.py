import logging
import wave
from pathlib import Path

import pytest

from video_translator.models import Segment
from video_translator.pipeline import media, synthesizer
from video_translator.pipeline.media import (
    DubbingOverflowError,
    _format_srt_timestamp,
    atempo_chain,
    compose_dub_timeline,
    normalize_tts_segments,
    write_srt,
)
from video_translator.runtime import MediaBinaries
from video_translator.settings import Settings


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
    segment = Segment(
        index=0,
        start=1.2,
        end=2.3,
        source_text="hello",
        translated_text="你好",
    )
    output = write_srt(
        [segment],
        tmp_path / "subtitles.srt",
    )
    text = output.read_text(encoding="utf-8")
    assert "00:00:01,200 --> 00:00:02,300" in text
    assert "你好" in text
    source = write_srt(
        [segment],
        tmp_path / "en.srt",
        source_text=True,
    )
    assert source.read_text(encoding="utf-8") == (
        "1\n00:00:01,200 --> 00:00:02,300\nhello\n"
    )


def test_normalize_reports_all_overflow_before_aligning(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(media, "probe_duration", lambda path, binaries: 2.0)
    segments = [
        Segment(index=i, start=i, end=i + 1.0, source_text="hello", tts_file="dub.wav")
        for i in range(2)
    ]
    output = tmp_path / "aligned"
    with pytest.raises(DubbingOverflowError) as exc:
        normalize_tts_segments(
            segments,
            output,
            Settings(data_dir=tmp_path, max_tempo_factor=1.8),
            MediaBinaries("unused", "unused"),
            logging.getLogger("test"),
            reject_overflow=True,
        )
    assert exc.value.segments == [
        {"id": i, "audio_seconds": 2.0, "slot_seconds": 1.0, "required_tempo": 2.0}
        for i in range(2)
    ]
    assert not output.exists()


def test_synthesis_cache_reuses_unchanged_lines(tmp_path, monkeypatch) -> None:
    generated = []

    def save(text, path):
        generated.append(text)
        path.write_text(text, encoding="utf-8")

    settings = Settings(data_dir=tmp_path, tts_provider="edge", tts_voice="voice-a")
    speech = synthesizer.SpeechSynthesizer(settings, logging.getLogger("test"))
    monkeypatch.setattr(speech, "synthesize", save)
    monkeypatch.setattr(
        synthesizer, "resolve_media_binaries", lambda settings: MediaBinaries("", "")
    )
    monkeypatch.setattr(synthesizer, "probe_audio_duration", lambda path, media: 1.0)
    segments = [
        Segment(index=0, start=0, end=1, source_text="Hello", translated_text="你好"),
        Segment(index=1, start=1, end=2, source_text="Goodbye", translated_text="再见"),
    ]
    speech.synthesize_segments(segments, tmp_path)
    segments[1].translated_text = "回头见"
    speech.synthesize_segments(segments, tmp_path)
    assert generated == ["你好", "再见", "回头见"]

    settings.tts_voice = "voice-b"
    speech.synthesize_segments(segments, tmp_path)
    assert generated[-2:] == ["你好", "回头见"]


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

