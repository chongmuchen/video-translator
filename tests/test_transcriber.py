import logging
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

from video_translator.models import Segment, UNCLEAR_TRANSCRIPT_TEXT
from video_translator.pipeline.transcriber import (
    MlxWhisperTranscriber,
    make_asr_segment,
    merge_segments,
    mlx_model_name,
)
from video_translator.settings import Settings


def test_merge_short_fragments() -> None:
    merged = merge_segments(
        [
            Segment(index=0, start=0.0, end=1.0, source_text="This is"),
            Segment(index=1, start=1.2, end=2.0, source_text="a test."),
            Segment(index=2, start=3.0, end=4.0, source_text="Next."),
        ]
    )
    assert len(merged) == 2
    assert merged[0].source_text == "This is a test."
    assert [segment.index for segment in merged] == [0, 1]


def test_low_confidence_speech_uses_visible_placeholder() -> None:
    segment = make_asr_segment(
        index=0,
        start=1.0,
        end=2.0,
        text="possibly wrong words",
        raw={"words": [{"probability": 0.2}]},
        unclear_threshold=0.45,
    )

    assert segment.source_text == UNCLEAR_TRANSCRIPT_TEXT
    assert segment.raw_source_text == "possibly wrong words"
    assert segment.asr_unclear is True
    assert segment.asr_confidence == 0.2


def test_maps_standard_model_names_to_mlx_community() -> None:
    assert (
        mlx_model_name("small.en")
        == "mlx-community/whisper-small.en-mlx"
    )
    assert (
        mlx_model_name("large-v3")
        == "mlx-community/whisper-large-v3-mlx"
    )
    assert mlx_model_name("custom/model") == "custom/model"


def test_mlx_transcriber_produces_standard_segments(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured = {}

    def fake_transcribe(audio_path, **kwargs):
        captured["audio_path"] = audio_path
        captured.update(kwargs)
        return {
            "language": "en",
            "segments": [
                {
                    "start": 1.25,
                    "end": 3.5,
                    "text": " Hello from MLX.",
                    "words": [],
                }
            ],
        }

    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        SimpleNamespace(transcribe=fake_transcribe),
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        asr_backend="mlx_whisper",
        asr_model="small.en",
    )
    transcriber = MlxWhisperTranscriber(
        settings,
        logging.getLogger("test-mlx"),
    )
    speech = tmp_path / "speech.wav"
    with wave.open(str(speech), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 1600)

    segments, metadata = transcriber.transcribe(
        str(speech),
        language="en",
    )

    assert segments[0].source_text == "Hello from MLX."
    assert segments[0].start == 1.25
    assert segments[0].end == 3.5
    assert captured["audio_path"].shape == (1600,)
    assert captured["path_or_hf_repo"] == (
        "mlx-community/whisper-small.en-mlx"
    )
    assert captured["word_timestamps"] is True
    assert metadata["asr_backend"] == "mlx_whisper"
    assert metadata["asr_device"] == "metal"
