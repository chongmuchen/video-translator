"""Optional speaker diarization for video translation segments."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..errors import ConfigurationError, PipelineError
from ..models import Segment
from ..settings import Settings


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    speaker: str

    def overlap(self, segment: Segment) -> float:
        return max(0.0, min(self.end, segment.end) - max(self.start, segment.start))


def _turns_from_pyannote(annotation) -> list[SpeakerTurn]:
    turns: list[SpeakerTurn] = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        turns.append(
            SpeakerTurn(
                start=float(turn.start),
                end=float(turn.end),
                speaker=str(speaker),
            )
        )
    return turns


def assign_speakers(
    segments: list[Segment],
    turns: Iterable[SpeakerTurn],
) -> dict[str, int]:
    """Assign each ASR segment to the speaker with the largest overlap."""

    ordered = list(turns)
    counts: dict[str, int] = {}
    for segment in segments:
        best: SpeakerTurn | None = None
        best_overlap = 0.0
        for turn in ordered:
            overlap = turn.overlap(segment)
            if overlap > best_overlap:
                best = turn
                best_overlap = overlap
        if best is None or best_overlap <= 0:
            continue
        segment.speaker = best.speaker
        counts[best.speaker] = counts.get(best.speaker, 0) + 1
    return counts


class PyannoteDiarizer:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        if not settings.diarization_auth_token:
            raise ConfigurationError(
                "启用 pyannote 说话人分离需要配置 "
                "VT_DIARIZATION_AUTH_TOKEN。"
            )
        try:
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise ConfigurationError(
                "缺少 pyannote.audio。可安装：pip install '.[diarization]'"
            ) from exc
        self._pipeline_class = Pipeline

    def diarize(self, audio_path: Path) -> list[SpeakerTurn]:
        self.logger.info(
            "加载说话人分离模型 %s",
            self.settings.diarization_model,
        )
        pipeline = self._pipeline_class.from_pretrained(
            self.settings.diarization_model,
            use_auth_token=self.settings.diarization_auth_token,
        )
        annotation = pipeline(str(audio_path))
        turns = _turns_from_pyannote(annotation)
        if not turns:
            raise PipelineError("说话人分离没有识别到任何说话人片段。")
        return turns


def diarize_segments(
    audio_path: Path,
    segments: list[Segment],
    settings: Settings,
    logger: logging.Logger,
) -> dict[str, object]:
    """Run the configured diarization backend and mutate segment speakers."""

    if settings.diarization_backend != "pyannote":
        raise PipelineError(f"不支持说话人分离后端：{settings.diarization_backend}")
    turns = PyannoteDiarizer(settings, logger).diarize(audio_path)
    speaker_counts = assign_speakers(segments, turns)
    return {
        "diarization_backend": settings.diarization_backend,
        "diarization_model": settings.diarization_model,
        "speaker_count": len(speaker_counts),
        "speaker_segments": speaker_counts,
        "turn_count": len(turns),
    }
