from video_translator.models import Segment
from video_translator.pipeline.transcriber import merge_segments


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

