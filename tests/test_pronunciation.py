import logging
import wave

import pytest

from video_translator.models import Pronunciation, Segment
from video_translator.pipeline.kokoro import LocalSpeech, PACKAGE_DIR
from video_translator.pipeline.pronunciation import PronunciationG2P


@pytest.fixture
def frontend(monkeypatch):
    if not (PACKAGE_DIR / "misaki").is_dir():
        pytest.skip("Local Kokoro runtime is required for Chinese frontend checks")
    monkeypatch.syspath_prepend(str(PACKAGE_DIR))
    from misaki.zh import ZHG2P

    return ZHG2P(version="1.1", en_callable=lambda text: "EN")


@pytest.mark.parametrize("text,index,pinyin,expected", [
    ("你得记住。", 1, "dei3", "ㄉㄟ3"),
    ("她得了第一名。", 1, "de2", "ㄉㄜ2"),
    ("说得很好。", 1, "de5", "ㄉㄜ5"),
    ("第12关，你得等10秒。", 6, "dei3", "ㄉㄟ3"),
    ("AI角色得等待。", 4, "dei3", "ㄉㄟ3"),
    ("绿色树叶。", 0, "lv4", "ㄌㄩ4"),
])
def test_contextual_readings_preserve_text_and_original_frontend(
    frontend, text, index, pinyin, expected
):
    baseline = frontend(text)[0]
    reading = Pronunciation(start=index, char=text[index], pinyin=pinyin)
    actual = PronunciationG2P(frontend, text, [reading])(text)[0]
    assert expected in actual
    assert frontend(text)[0] == baseline
    if "AI" in text:
        assert "EN" in actual


def test_repeated_chunks_and_multiple_occurrences(frontend):
    text = "你得记住。你得记住。"
    marked = PronunciationG2P(frontend, text, [Pronunciation(start=6, char="得", pinyin="dei3")])
    assert "ㄉㄟ3" not in marked("你得记住。")[0]
    assert "ㄉㄟ3" in marked("你得记住。")[0]
    text = "得得得。"
    marked = PronunciationG2P(frontend, text, [
        Pronunciation(start=0, char="得", pinyin="de2"),
        Pronunciation(start=2, char="得", pinyin="dei3"),
    ])
    assert marked(text)[0] == "ㄉㄜ2ㄉㄜ5/ㄉㄟ3."


def test_unsupported_reading_falls_back_and_logs(frontend, caplog):
    text = "你得记住。"
    reading = Pronunciation(start=1, char="得", pinyin="banana3")
    with caplog.at_level(logging.WARNING):
        assert PronunciationG2P(frontend, text, [reading])(text) == frontend(text)
    assert "使用默认读音" in caplog.text


def test_number_normalization_conflict_falls_back(frontend, caplog):
    text = "100万角色。"
    reading = Pronunciation(start=3, char="万", pinyin="wan4")
    marked = PronunciationG2P(frontend, text, [reading])
    # The marker can affect how cn2an interprets a mixed Chinese/Arabic number.
    # Either safe mapping or a whole-chunk fallback must preserve this reading.
    assert marked(text)[0] == frontend(text)[0]


def test_engine_restores_frontend_after_synthesis_failure(tmp_path):
    class FailingPipeline:
        g2p = object()

        def __call__(self, *args, **kwargs):
            raise RuntimeError("synthetic model failure")

    pipeline = FailingPipeline()
    original = pipeline.g2p
    engine = LocalSpeech(1)
    engine._engine = (pipeline, None, None, None)
    with pytest.raises(RuntimeError, match="synthetic model failure"):
        engine.synthesize("得", tmp_path / "voice.wav", pronunciations=[
            Pronunciation(start=0, char="得", pinyin="dei3"),
        ])
    assert pipeline.g2p is original
    assert not (tmp_path / "voice.wav").exists()


def test_cache_changes_only_for_changed_readings(tmp_path, monkeypatch):
    generated = []

    def synthesize(engine, text, output, *, pronunciations=None):
        generated.append((text, pronunciations))
        with wave.open(str(output), "wb") as audio:
            audio.setparams((1, 2, 24000, 24, "NONE", "not compressed"))
            audio.writeframes(b"\x01\x00" * 24)

    monkeypatch.setattr(LocalSpeech, "synthesize", synthesize)
    engine = LocalSpeech(1)
    segment = Segment(index=0, start=0, end=1, source_text="Remember", translated_text="你得记住。")
    engine.synthesize_segments([segment], tmp_path)
    original = segment.tts_file
    segment.pronunciations = [Pronunciation(start=1, char="得", pinyin="dei3")]
    engine.synthesize_segments([segment], tmp_path)
    annotated = segment.tts_file
    assert annotated != original
    engine.synthesize_segments([segment], tmp_path)
    assert len(generated) == 2
    segment.pronunciations = []
    engine.synthesize_segments([segment], tmp_path)
    assert segment.tts_file == original
    assert len(generated) == 2
