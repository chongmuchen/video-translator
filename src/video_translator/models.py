"""Serializable pipeline models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    queued = "queued"
    downloading = "downloading"
    downloaded = "downloaded"
    extracting = "extracting"
    extracted = "extracted"
    transcribing = "transcribing"
    transcribed = "transcribed"
    translating = "translating"
    translated = "translated"
    synthesizing = "synthesizing"
    synthesized = "synthesized"
    aligning = "aligning"
    aligned = "aligned"
    muxing = "muxing"
    completed = "completed"
    failed = "failed"


class Segment(BaseModel):
    index: int
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    source_text: str
    translated_text: str | None = None
    speaker: str | None = None
    tts_file: str | None = None

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


class JobCreateRequest(BaseModel):
    url: str
    options: PipelineOptions = Field(default_factory=PipelineOptions)


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
    dub_audio_path: str | None = None
    output_path: str | None = None
    segments_path: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    completed_steps: list[str] = Field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        for key in (
            "source_path",
            "audio_path",
            "subtitle_path",
            "dub_audio_path",
            "segments_path",
        ):
            data.pop(key, None)
        data["download_ready"] = bool(
            self.status == JobStatus.completed
            and self.output_path
            and Path(self.output_path).is_file()
        )
        return data
