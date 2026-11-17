"""Chinese speech synthesis adapters."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import TypedDict

from ..errors import ConfigurationError
from ..models import Segment
from ..settings import Settings
from .kokoro import LocalSpeech


class WordTiming(TypedDict):
    start: float
    end: float
    text: str


def _word_timing(start: object, end: object, text: object) -> WordTiming | None:
    if type(start) not in (float, int) or type(end) not in (float, int):
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        start_seconds = float(start)
        end_seconds = float(end)
    except OverflowError:
        return None
    if not 0 <= start_seconds < end_seconds < math.inf:
        return None
    return {"start": start_seconds, "end": end_seconds, "text": text}


def read_word_timings(audio_path: str | Path | None) -> list[WordTiming]:
    """Read optional word timings relative to the original synthesized audio."""
    if audio_path is None:
        return []
    sidecar = Path(audio_path).with_suffix(".words.json")
    try:
        rows = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    timings = []
    for row in rows:
        if not isinstance(row, dict):
            return []
        word = _word_timing(row.get("start"), row.get("end"), row.get("text"))
        if word is None or (timings and word["start"] < timings[-1]["start"]):
            return []
        timings.append(word)
    return timings


class SpeechSynthesizer:
    """Use the local Kokoro engine with the active job settings."""

    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        self._validate_configuration()

    def _validate_configuration(self) -> None:
        if self.settings.tts_provider != "kokoro":
            raise ConfigurationError("配音只支持本机 Kokoro，请设置 VT_TTS_PROVIDER=kokoro。")
        self.settings.validate_tts_voice(self.settings.tts_voice)

    def _engine(self) -> LocalSpeech:
        self._validate_configuration()
        return LocalSpeech(
            self.settings.tts_threads,
            voice=self.settings.tts_voice,
            speed=self.settings.tts_rate,
        )

    def synthesize(self, text: str, output: Path) -> None:
        self._engine().synthesize(text, output)

    def synthesize_segments(
        self, segments: list[Segment], output_dir: Path
    ) -> list[Segment]:
        self.logger.info("使用 Kokoro 音色 %s 生成中文配音", self.settings.tts_voice)
        return self._engine().synthesize_segments(segments, output_dir)
