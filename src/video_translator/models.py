"""Serializable pipeline models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    queued = "queued"
    downloading = "downloading"
    extracting = "extracting"
    transcribing = "transcribing"
    prepared = "prepared"
    translating = "translating"
    synthesizing = "synthesizing"
    aligning = "aligning"
    muxing = "muxing"
    completed = "completed"
    failed = "failed"


class Pronunciation(BaseModel):
    """One contextual reading, indexed by Unicode character in the translated text."""

    start: int = Field(ge=0, strict=True)
    char: str = Field(pattern=r"^[\u3400-\u9fff\uf900-\ufaff\U00020000-\U000323af]$")
    pinyin: str = Field(pattern=r"^[a-z]+[1-5]$")


class Segment(BaseModel):
    index: int
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    source_text: str
    translated_text: str | None = None
    pronunciations: list[Pronunciation] = Field(default_factory=list)
    speaker: str | None = None
    tts_file: str | None = None

    @model_validator(mode="after")
    def validate_pronunciations(self) -> "Segment":
        text = self.translated_text or ""
        positions = set()
        for reading in self.pronunciations:
            if text[reading.start:reading.start + 1] != reading.char:
                raise ValueError(f"读音标记位置 {reading.start} 与译文中的 {reading.char} 不匹配")
            if reading.start in positions:
                raise ValueError(f"读音标记位置 {reading.start} 重复")
            positions.add(reading.start)
        self.pronunciations.sort(key=lambda reading: reading.start)
        return self

    @property
    def duration(self) -> float:
        return max(0.05, self.end - self.start)

    @property
    def max_zh_chars(self) -> int:
        # Around 4–5 Mandarin characters per second is comfortable narration.
        return max(4, int(self.duration * 4.5))


class PipelineOptions(BaseModel):
    target_language: str = "简体中文"
    keep_original_audio: bool | None = None
    duck_original_audio: bool | None = None
    burn_subtitles: bool | None = None
    source_language: str | None = None
    glossary: dict[str, str] = Field(default_factory=dict)


class JobManifest(BaseModel):
    id: str
    source: str
    status: JobStatus = JobStatus.queued
    progress: int = Field(default=0, ge=0, le=100)
    stage_message: str = "等待处理"
    options: PipelineOptions = Field(default_factory=PipelineOptions)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    title: str | None = None
    source_path: str | None = None
    audio_path: str | None = None
    subtitle_path: str | None = None
    english_subtitle_path: str | None = None
    dub_audio_path: str | None = None
    output_path: str | None = None
    segments_path: str | None = None
    translations_path: str | None = None
    error: str | None = None
    failed_stage: JobStatus | None = None
    overflow_segments: list[dict[str, int | float | list[int]]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

