"""Speech recognition based on faster-whisper."""

from __future__ import annotations

import logging
from typing import Iterable

from ..errors import ConfigurationError
from ..models import Segment
from ..settings import Settings


def merge_segments(
    segments: Iterable[Segment],
    *,
    max_gap: float = 0.45,
    max_duration: float = 12.0,
    max_chars: int = 180,
) -> list[Segment]:
    """Merge very short adjacent ASR fragments into translation-sized units."""

    merged: list[Segment] = []
    for segment in segments:
        if not merged:
            merged.append(segment)
            continue
        previous = merged[-1]
        gap = segment.start - previous.end
        combined_text = f"{previous.source_text} {segment.source_text}".strip()
        can_merge = (
            0 <= gap <= max_gap
            and segment.end - previous.start <= max_duration
            and len(combined_text) <= max_chars
            and not previous.source_text.rstrip().endswith((".", "!", "?", "。", "！", "？"))
        )
        if can_merge:
            previous.end = segment.end
            previous.source_text = combined_text
        else:
            merged.append(segment)

    for index, segment in enumerate(merged):
        segment.index = index
    return merged


class FasterWhisperTranscriber:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ConfigurationError(
                "缺少 faster-whisper，请安装项目的 asr 可选依赖。"
            ) from exc
        self._model_class = WhisperModel

    def _runtime(self) -> tuple[str, str]:
        device = self.settings.asr_device
        compute_type = self.settings.asr_compute_type
        if device == "auto":
            try:
                import ctranslate2

                device = (
                    "cuda"
                    if ctranslate2.get_cuda_device_count() > 0
                    else "cpu"
                )
            except Exception:
                device = "cpu"
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        return device, compute_type

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
    ) -> tuple[list[Segment], dict]:
        device, compute_type = self._runtime()
        self.logger.info(
            "加载 ASR 模型 %s (%s/%s)",
            self.settings.asr_model,
            device,
            compute_type,
        )
        model = self._model_class(
            self.settings.asr_model,
            device=device,
            compute_type=compute_type,
        )
        raw_segments, info = model.transcribe(
            audio_path,
            language=language or self.settings.source_language or None,
            vad_filter=True,
            word_timestamps=True,
            condition_on_previous_text=True,
        )
        segments: list[Segment] = []
        for index, raw in enumerate(raw_segments):
            text = raw.text.strip()
            if not text:
                continue
            segments.append(
                Segment(
                    index=index,
                    start=max(0.0, float(raw.start)),
                    end=max(float(raw.end), float(raw.start) + 0.05),
                    source_text=text,
                )
            )
        segments = merge_segments(segments)
        metadata = {
            "detected_language": info.language,
            "language_probability": info.language_probability,
            "asr_model": self.settings.asr_model,
            "asr_device": device,
        }
        return segments, metadata

