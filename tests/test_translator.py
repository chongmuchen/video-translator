import json
import logging

import pytest

from video_translator.errors import ConfigurationError, PipelineError
from video_translator.models import Segment
from video_translator.pipeline.translator import SegmentTranslator, apply_translation_response
from video_translator.settings import Settings


def test_apply_translation_response() -> None:
    segments = [
        Segment(index=0, start=0, end=1, source_text="hello"),
        Segment(index=1, start=1, end=2, source_text="world"),
    ]
    apply_translation_response(
        segments,
        """```json
        {"segments":[{"id":0,"text":"你好"},{"id":1,"text":"世界"}]}
        ```""",
    )
    assert [item.translated_text for item in segments] == ["你好", "世界"]


def test_translation_response_requires_all_ids() -> None:
    segments = [
        Segment(index=0, start=0, end=1, source_text="hello"),
        Segment(index=1, start=1, end=2, source_text="world"),
    ]
    with pytest.raises(PipelineError):
        apply_translation_response(
            segments,
            '{"segments":[{"id":0,"text":"你好"}]}',
        )
def test_pronunciations_are_imported_with_whitespace_offsets_and_cleared_on_revision():
    segments = [Segment(index=0, start=0, end=1, source_text="Remember.")]
    apply_translation_response(segments, json.dumps({"segments": [{
        "id": 0, "text": "  你得记住。  ",
        "pronunciations": [{"start": 3, "char": "得", "pinyin": "dei3"}],
    }]}))
    assert segments[0].translated_text == "你得记住。"
    assert [reading.model_dump() for reading in segments[0].pronunciations] == [
        {"start": 1, "char": "得", "pinyin": "dei3"},
    ]
    apply_translation_response(segments, '{"segments":[{"id":0,"text":"记住。"}]}')
    assert segments[0].pronunciations == []


@pytest.mark.parametrize("annotations", [
    [{"start": 0, "char": "得", "pinyin": "dei3"}],
    [{"start": 10, "char": "得", "pinyin": "dei3"}],
    [{"start": True, "char": "得", "pinyin": "dei3"}],
    [{"start": 1, "char": "得", "pinyin": "dei"}],
    [{"start": 1, "char": "得", "pinyin": "dei3"}] * 2,
    None,
])
def test_invalid_pronunciations_do_not_partially_update_translation(annotations):
    segments = [
        Segment(index=i, start=i, end=i+1, source_text="Remember.", translated_text="原文")
        for i in range(2)
    ]
    with pytest.raises(PipelineError, match="读音标记无效"):
        apply_translation_response(segments, json.dumps({"segments": [
            {"id": 0, "text": "已经改好。"},
            {"id": 1, "text": "你得记住。", "pronunciations": annotations},
        ]}))
    assert [segment.translated_text for segment in segments] == ["原文", "原文"]


@pytest.mark.parametrize("provider", ["codex", "openai_compatible"])
def test_legacy_api_translation_is_rejected_before_request(monkeypatch, provider):
    import httpx

    def unexpected_client(*args, **kwargs):
        pytest.fail("Translation must not create an API client")

    monkeypatch.setattr(httpx, "Client", unexpected_client)
    translator = SegmentTranslator(Settings(translator_provider=provider), logging.getLogger(__name__))
    with pytest.raises(ConfigurationError, match="Codex Pro"):
        translator.translate(
            [Segment(index=0, start=0, end=1, source_text="Remember.")],
            target_language="简体中文", glossary={},
        )
