"""Speech recognition based on faster-whisper."""

from __future__ import annotations

import logging
from typing import Iterable
import wave

from ..errors import ConfigurationError
from ..models import Segment, UNCLEAR_TRANSCRIPT_TEXT
from ..settings import Settings


MLX_MODEL_ALIASES = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "tiny.en": "mlx-community/whisper-tiny.en-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "base.en": "mlx-community/whisper-base.en-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "small.en": "mlx-community/whisper-small.en-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "medium.en": "mlx-community/whisper-medium.en-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
}


def asr_segment_confidence(raw) -> float | None:
    """Estimate a comparable confidence from Whisper word/token metadata."""

    words = (
        raw.get("words", [])
        if isinstance(raw, dict)
        else getattr(raw, "words", [])
    ) or []
    probabilities = []
    for word in words:
        value = (
            word.get("probability")
            if isinstance(word, dict)
            else getattr(word, "probability", None)
        )
        if value is not None:
            probabilities.append(float(value))
    confidence = None
    if probabilities:
        confidence = sum(probabilities) / len(probabilities)

    avg_logprob = (
        raw.get("avg_logprob")
        if isinstance(raw, dict)
        else getattr(raw, "avg_logprob", None)
    )
    if confidence is None and avg_logprob is not None:
        import math

        confidence = math.exp(float(avg_logprob))
    no_speech_probability = (
        raw.get("no_speech_prob")
        if isinstance(raw, dict)
        else getattr(raw, "no_speech_prob", None)
    )
    if no_speech_probability is not None:
        speech_confidence = 1.0 - float(no_speech_probability)
        confidence = (
            speech_confidence
            if confidence is None
            else min(confidence, speech_confidence)
        )
    if confidence is None:
        return None
    return max(0.0, min(1.0, confidence))


def make_asr_segment(
    *,
    index: int,
    start: float,
    end: float,
    text: str,
    raw,
    unclear_threshold: float,
) -> Segment:
    confidence = asr_segment_confidence(raw)
    unclear = confidence is not None and confidence < unclear_threshold
    return Segment(
        index=index,
        start=max(0.0, start),
        end=max(end, start + 0.05),
        source_text=UNCLEAR_TRANSCRIPT_TEXT if unclear else text,
        raw_source_text=text if unclear else None,
        asr_confidence=confidence,
        asr_unclear=unclear,
    )


def mlx_model_name(model: str) -> str:
    """Map familiar Whisper names to validated MLX Community models."""

    return MLX_MODEL_ALIASES.get(model, model)


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
            and not previous.asr_unclear
            and not segment.asr_unclear
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
                make_asr_segment(
                    index=index,
                    start=float(raw.start),
                    end=float(raw.end),
                    text=text,
                    raw=raw,
                    unclear_threshold=self.settings.asr_unclear_threshold,
                )
            )
        segments = merge_segments(segments)
        metadata = {
            "detected_language": info.language,
            "language_probability": info.language_probability,
            "asr_backend": "faster_whisper",
            "asr_model": self.settings.asr_model,
            "asr_device": device,
        }
        return segments, metadata


class MlxWhisperTranscriber:
    """Apple Silicon GPU transcription through Apple's MLX runtime."""

    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        try:
            import mlx_whisper
        except ImportError as exc:
            raise ConfigurationError(
                "缺少 mlx-whisper。Apple Silicon 请运行："
                ".venv/bin/python -m pip install -e '.[mac]'"
            ) from exc
        self._module = mlx_whisper

    @staticmethod
    def _load_speech_wave(audio_path: str):
        try:
            import numpy as np
        except ImportError as exc:
            raise ConfigurationError(
                "mlx-whisper 缺少 numpy 运行依赖。"
            ) from exc
        with wave.open(audio_path, "rb") as audio:
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getframerate() != 16000
            ):
                raise ConfigurationError(
                    "MLX 输入必须是 extract 生成的 16 kHz "
                    "单声道 PCM WAV。"
                )
            frames = audio.readframes(audio.getnframes())
        return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
    ) -> tuple[list[Segment], dict]:
        model = mlx_model_name(self.settings.asr_model)
        selected_language = language or self.settings.source_language or None
        self.logger.info(
            "加载 MLX ASR 模型 %s (Apple GPU/Metal)",
            model,
        )
        samples = self._load_speech_wave(audio_path)
        result = self._module.transcribe(
            samples,
            path_or_hf_repo=model,
            language=selected_language,
            word_timestamps=True,
            condition_on_previous_text=True,
            verbose=False,
        )
        segments: list[Segment] = []
        for index, raw in enumerate(result.get("segments", [])):
            text = str(raw.get("text") or "").strip()
            if not text:
                continue
            start = max(0.0, float(raw.get("start") or 0.0))
            end = max(float(raw.get("end") or start), start + 0.05)
            segments.append(
                make_asr_segment(
                    index=index,
                    start=start,
                    end=end,
                    text=text,
                    raw=raw,
                    unclear_threshold=self.settings.asr_unclear_threshold,
                )
            )
        segments = merge_segments(segments)
        metadata = {
            "detected_language": result.get("language"),
            "language_probability": None,
            "asr_backend": "mlx_whisper",
            "asr_model": model,
            "asr_device": "metal",
        }
        return segments, metadata
