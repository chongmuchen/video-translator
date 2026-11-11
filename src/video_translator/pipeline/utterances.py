"""Plan continuous Chinese speech separately from subtitle cue timing."""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..errors import PipelineError
from ..models import Segment

if TYPE_CHECKING:
    from .synthesizer import WordTiming


# Ignore dots within numbers, names and abbreviations. Keep closing quotation
# marks with the preceding sentence so they never become a separate request.
_SENTENCE_END = re.compile(
    r"""(?:[。！？!?…]+|\.{2,}|\.(?![A-Za-z0-9]))[。！？!?…."”’'」』）】》〉)\]]*"""
)
_CLOSING_MARKS = dict(zip("（([【《〈“‘「『", "）)]】》〉”’」』"))
_ASIDE_CLOSERS = set("）)]】》〉")
_TERMINAL = set("。！？!?….")


@dataclass(frozen=True)
class CueFragment:
    cue: Segment
    text: str
    start: float
    end: float

    @property
    def cue_index(self) -> int:
        return self.cue.index


@dataclass(frozen=True)
class Utterance:
    segment: Segment
    fragments: tuple[CueFragment, ...]


@dataclass(frozen=True)
class _SentencePiece:
    cue: Segment
    ordinal: int
    text: str
    start: float
    end: float
    closes_sentence: bool


def _spoken_characters(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _weight(text: str) -> int:
    return max(1, len(_spoken_characters(text)))


def _ordered_bounds(
    intervals: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Divide overlap without removing any cue or synthesizing on top of it."""

    earliest_ends = [end for _, end in intervals]
    for index in range(len(intervals) - 2, -1, -1):
        earliest_ends[index] = min(earliest_ends[index], earliest_ends[index + 1])

    bounds: list[tuple[float, float]] = []
    previous_end = 0.0
    for index, (original_start, end) in enumerate(intervals):
        start = max(original_start, previous_end)
        if index + 1 < len(intervals):
            next_start = intervals[index + 1][0]
            if next_start < end:
                shared_start = max(start, next_start)
                shared_end = earliest_ends[index]
                end = (shared_start + shared_end) / 2
        if end <= start:
            raise PipelineError("相邻字幕的时间范围不足以保留所有片段。")
        bounds.append((start, end))
        previous_end = end
    return bounds


def _track_closing_marks(text: str, pending: list[str]) -> bool:
    ending = False
    for character in text:
        if pending and character == pending[-1]:
            pending.pop()
        elif character in _CLOSING_MARKS:
            pending.append(_CLOSING_MARKS[character])
        elif character in _TERMINAL and _ASIDE_CLOSERS.isdisjoint(pending):
            ending = True
    return ending


def _split_cue(
    cue: Segment, start: float, end: float, pending: list[str]
) -> list[_SentencePiece]:
    text = (cue.translated_text or "").strip()
    if not _spoken_characters(text):
        raise PipelineError(f"片段 {cue.index} 的译稿没有可朗读的文字。")

    parts: list[tuple[str, bool]] = []
    offset = 0
    scanned = 0
    for boundary in _SENTENCE_END.finditer(text):
        boundary_end = boundary.end()
        _track_closing_marks(text[scanned:boundary.start()], pending)
        ending = _track_closing_marks(boundary.group(), pending)
        scanned = boundary_end
        piece = text[offset:boundary_end]
        if ending and not pending and _spoken_characters(piece):
            parts.append((piece, True))
            offset = boundary_end
    _track_closing_marks(text[scanned:], pending)
    tail = text[offset:]
    if _spoken_characters(tail):
        parts.append((tail, False))
    elif tail:
        previous, closes_sentence = parts[-1]
        parts[-1] = (previous + tail, closes_sentence)

    weights = [_weight(part) for part, _ in parts]
    total_weight = sum(weights)
    pieces: list[_SentencePiece] = []
    cumulative = 0
    piece_start = start
    for ordinal, ((part, closes_sentence), weight) in enumerate(zip(parts, weights)):
        cumulative += weight
        piece_end = start + (end - start) * cumulative / total_weight
        pieces.append(_SentencePiece(cue, ordinal, part, piece_start, piece_end, closes_sentence))
        piece_start = piece_end
    return pieces


def _pack_utterance(pieces: list[_SentencePiece], stride: int) -> Utterance:
    text = ""
    fragments = []
    for piece in pieces:
        spoken = piece.text.strip()
        if (
            text
            and text[-1].isascii()
            and text[-1].isalnum()
            and spoken[0].isascii()
            and spoken[0].isalnum()
        ):
            text += " "
        text += spoken
        fragments.append(CueFragment(piece.cue, piece.text, piece.start, piece.end))
    first = pieces[0]
    segment = Segment(
        index=first.cue.index + first.ordinal * stride,
        start=first.start,
        end=pieces[-1].end,
        source_text=first.cue.source_text,
        translated_text=text,
        speaker=first.cue.speaker,
    )
    return Utterance(segment=segment, fragments=tuple(fragments))


def build_utterances(
    segments: Sequence[Segment],
    *,
    total_duration: float | None = None,
) -> list[Utterance]:
    """Use complete sentences as speech requests, even across or within cues."""

    if not segments:
        return []
    cue_ids = {cue.index for cue in segments}
    if len(cue_ids) != len(segments):
        raise PipelineError("转写稿中存在重复片段编号。")
    stride = max(cue_ids) - min(cue_ids) + 1
    intervals = []
    previous_start = 0.0
    for cue in segments:
        end = cue.end if total_duration is None else min(cue.end, total_duration)
        if (
            not math.isfinite(cue.start)
            or not math.isfinite(end)
            or cue.start < previous_start
            or end <= cue.start
        ):
            raise PipelineError(f"片段 {cue.index} 的字幕时间无效。")
        intervals.append((cue.start, end))
        previous_start = cue.start

    utterances = []
    current: list[_SentencePiece] = []
    pending: list[str] = []
    for cue, (start, end) in zip(segments, _ordered_bounds(intervals)):
        if current and cue.speaker != current[-1].cue.speaker:
            pending.clear()
        for piece in _split_cue(cue, start, end, pending):
            if current and (
                current[-1].closes_sentence
                or piece.cue.speaker != current[-1].cue.speaker
            ):
                utterances.append(_pack_utterance(current, stride))
                current = []
            current.append(piece)
    if current:
        utterances.append(_pack_utterance(current, stride))
    return utterances


def _word_bounds(
    utterance: Utterance, words: Sequence[WordTiming]
) -> list[tuple[float, float]] | None:
    observed: list[str] = []
    times: list[tuple[float, float]] = []
    segment = utterance.segment
    for word in words:
        if not math.isfinite(word["start"]) or not math.isfinite(word["end"]):
            continue
        chars = _spoken_characters(word["text"])
        start = max(segment.start, word["start"])
        end = min(segment.end, word["end"])
        if not chars or not (start < end):
            continue
        width = (end - start) / len(chars)
        observed.extend(chars)
        times.extend((start + i * width, start + (i + 1) * width) for i in range(len(chars)))
    expected = "".join(_spoken_characters(fragment.text) for fragment in utterance.fragments)
    alignment = difflib.SequenceMatcher(None, expected, "".join(observed), autojunk=False)
    if alignment.ratio() < 0.85:
        return None
    matched = {
        original + i: spoken + i
        for original, spoken, size in alignment.get_matching_blocks()
        for i in range(size)
    }

    starts = []
    last_end = None
    position = 0
    for fragment in utterance.fragments:
        limit = position + len(_spoken_characters(fragment.text))
        indices = [matched[i] for i in range(position, limit) if i in matched]
        if not indices:
            return None
        starts.append(times[indices[0]][0])
        last_end = times[indices[-1]][1]
        position = limit
    assert last_end is not None
    bounds = list(zip(starts, [*starts[1:], last_end]))
    return bounds if all(end - start >= 0.001 for start, end in bounds) else None


def _estimated_bounds(
    utterance: Utterance, speech_end: float
) -> list[tuple[float, float]] | None:
    if not math.isfinite(speech_end):
        return None
    start = utterance.segment.start
    end = min(utterance.segment.end, speech_end)
    if end <= start:
        return None
    weights = [_weight(fragment.text) for fragment in utterance.fragments]
    total = sum(weights)
    positions = [start]
    cumulative = 0
    for weight in weights[:-1]:
        cumulative += weight
        positions.append(start + (end - start) * cumulative / total)
    bounds = list(zip(positions, [*positions[1:], end]))
    return bounds if all(end - start >= 0.001 for start, end in bounds) else None


def retime_chinese_cues(
    utterances: Sequence[Utterance],
    timings: Mapping[int, Sequence[WordTiming]] | None = None,
    *,
    speech_ends: Mapping[int, float] | None = None,
) -> list[Segment]:
    """Map final absolute speech times back to the unchanged original cue IDs."""

    cues: dict[int, Segment] = {}
    starts: dict[int, float] = {}
    ends: dict[int, float] = {}
    for utterance in utterances:
        index = utterance.segment.index
        words = timings.get(index, ()) if timings else ()
        speech_end = speech_ends.get(index) if speech_ends else None
        bounds = None
        if len(utterance.fragments) > 1:
            if words:
                bounds = _word_bounds(utterance, words)
            if bounds is None and speech_end is not None:
                bounds = _estimated_bounds(utterance, speech_end)
        if bounds is None:
            bounds = [(fragment.start, fragment.end) for fragment in utterance.fragments]
        for fragment, (start, end) in zip(utterance.fragments, bounds):
            cue_index = fragment.cue_index
            cues[cue_index] = fragment.cue
            starts[cue_index] = min(starts.get(cue_index, start), start)
            ends[cue_index] = max(ends.get(cue_index, end), end)

    intervals = [(starts[index], ends[index]) for index in cues]
    return [
        cue.model_copy(update={"start": start, "end": end})
        for cue, (start, end) in zip(cues.values(), _ordered_bounds(intervals))
    ]
