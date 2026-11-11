import pytest

from video_translator.models import Segment
from video_translator.pipeline.utterances import build_utterances, retime_chinese_cues


def cue(index, start, end, text, *, speaker=None):
    return Segment(
        index=index,
        start=start,
        end=end,
        source_text=f"Original cue {index}",
        translated_text=text,
        speaker=speaker,
    )


def test_sentence_boundary_inside_cue_preserves_all_original_text_and_ids():
    original = [
        cue(8, 0, 3, "开头。接着说"),
        cue(9, 3.2, 6, "完第一句。再开始，"),
        cue(15, 6.2, 7, "下一句！"),
    ]
    plan = build_utterances(original)

    assert [utterance.segment.translated_text for utterance in plan] == [
        "开头。", "接着说完第一句。", "再开始，下一句！"
    ]
    assert [[f.cue_index for f in utterance.fragments] for utterance in plan] == [
        [8], [8, 9], [9, 15]
    ]
    assert [u.segment.start for u in plan[1:]] == [u.segment.end for u in plan[:-1]]
    for original_cue in original:
        reconstructed = "".join(
            fragment.text
            for utterance in plan
            for fragment in utterance.fragments
            if fragment.cue_index == original_cue.index
        )
        assert reconstructed == original_cue.translated_text
    assert retime_chinese_cues(plan) == original

    changed = original.copy()
    changed[0] = original[0].model_copy(update={"translated_text": "接着说"})
    assert build_utterances(changed)[-1].segment.index == plan[-1].segment.index


def test_punctuation_and_speaker_boundaries_not_subtitle_gaps_or_lengths():
    original = [
        cue(0, 0, 12, "先把一个很长的句子说完，", speaker="A"),
        cue(1, 15, 25, "其中还引用了“版本 1.2。", speaker="A"),
        cue(2, 25, 29, "下一句话。”", speaker="A"),
        cue(3, 29, 31, "这个提议（真能行吗？）通过了……再看看。", speaker="A"),
        cue(4, 31, 32, "还有", speaker="A"),
        cue(5, 32, 33, "另一个人在说。", speaker="B"),
    ]
    plan = build_utterances(original)

    assert [u.segment.translated_text for u in plan] == [
        "先把一个很长的句子说完，其中还引用了“版本 1.2。下一句话。”",
        "这个提议（真能行吗？）通过了……",
        "再看看。",
        "还有",
        "另一个人在说。",
    ]
    assert (plan[0].segment.start, plan[0].segment.end) == (0, 29)


def test_word_timing_and_duration_fallback_retime_only_grouped_chinese_cues():
    original = [
        cue(3, 1, 2, "先"),
        cue(4, 2.5, 4, "看这里。"),
        cue(5, 4.1, 5, "另一个句子。"),
    ]
    plan = build_utterances(original)
    actual = retime_chinese_cues(
        plan,
        {
            3: [
                {"text": "先", "start": 1.1, "end": 1.3},
                {"text": "看这里", "start": 1.4, "end": 2.8},
            ],
            5: [{"text": "另一个句子", "start": 4.5, "end": 4.8}],
        },
        speech_ends={3: 3, 5: 4.9},
    )
    assert [(c.start, c.end) for c in actual] == [(1.1, 1.4), (1.4, 2.8), (4.1, 5)]
    assert [c.index for c in actual] == [3, 4, 5]
    assert original[1].start == 2.5

    estimated = retime_chinese_cues(
        plan,
        {3: [{"text": "不匹配", "start": 1.1, "end": 1.3}]},
        speech_ends={3: 3},
    )
    assert [(c.start, c.end) for c in estimated] == [(1, 1.5), (1.5, 3), (4.1, 5)]


def test_ascii_fragments_keep_spaces_and_overlaps_are_bounded_by_video():
    original = [
        cue(0, 0.1, 2.4, "Hello"),
        cue(1, 2, 4.1, "world."),
    ]
    plan = build_utterances(original, total_duration=4)
    assert plan[0].segment.translated_text == "Hello world."
    assert plan[0].segment.end == 4
    retimed = retime_chinese_cues(plan)
    assert retimed[0].end == pytest.approx(retimed[1].start)
    assert retimed[1].end == 4
