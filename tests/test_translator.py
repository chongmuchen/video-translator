import pytest

from video_translator.errors import PipelineError
from video_translator.models import Segment
from video_translator.pipeline.translator import apply_translation_response


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

