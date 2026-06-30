import logging
import wave
from pathlib import Path

from video_translator.models import Segment, UNCLEAR_TRANSCRIPT_TEXT
from video_translator.pipeline.synthesizer import SpeechSynthesizer
from video_translator.settings import Settings


def test_unclear_segment_uses_silence_instead_of_tts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        dub_sample_rate=8000,
    )
    synthesizer = SpeechSynthesizer(settings, logging.getLogger("test"))
    monkeypatch.setattr(
        synthesizer,
        "synthesize",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("不应朗读模糊语音占位文本")
        ),
    )
    segment = Segment(
        index=7,
        start=1,
        end=1.5,
        source_text=UNCLEAR_TRANSCRIPT_TEXT,
        translated_text=UNCLEAR_TRANSCRIPT_TEXT,
        asr_unclear=True,
    )

    synthesizer.synthesize_segments([segment], tmp_path / "tts")

    output = Path(segment.tts_file or "")
    with wave.open(str(output), "rb") as audio:
        assert audio.getframerate() == 8000
        assert audio.getnframes() == 4000
