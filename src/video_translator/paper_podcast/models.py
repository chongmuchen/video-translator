"""Serializable models for paper explainer podcasts."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import RuntimeSettingsUpdate, utc_now


PodcastStyle = Literal["deep_dive", "narration"]
PodcastScriptBackend = Literal["builtin", "notebooklm", "podcastfy"]


class PaperPodcastStatus(str, Enum):
    imported = "imported"
    extracting = "extracting"
    extracted = "extracted"
    scripting = "scripting"
    scripted = "scripted"
    synthesizing = "synthesizing"
    completed = "completed"
    canceled = "canceled"
    failed = "failed"


class PaperPodcastStep(str, Enum):
    extract = "extract"
    script = "script"
    synthesize = "synthesize"


class PodcastScriptLine(BaseModel):
    speaker: str = "旁白"
    text: str


class PaperPodcastManifest(BaseModel):
    id: str
    title: str
    source_path: str
    status: PaperPodcastStatus = PaperPodcastStatus.imported
    target_language: str = "简体中文"
    style: PodcastStyle = "deep_dive"
    duration_minutes: int = Field(default=8, ge=2, le=60)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    text_path: str | None = None
    notes_path: str | None = None
    script_json_path: str | None = None
    script_markdown_path: str | None = None
    audio_path: str | None = None
    video_path: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    completed_steps: list[str] = Field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data.pop("source_path", None)
        return data


class PaperPodcastRequest(BaseModel):
    target_language: str = "简体中文"
    style: PodcastStyle = "deep_dive"
    script_backend: PodcastScriptBackend = "builtin"
    script_compare_models: list[str] = Field(default_factory=list, max_length=5)
    duration_minutes: int = Field(default=8, ge=2, le=60)
    glossary: dict[str, str] = Field(default_factory=dict)
    voice_a: str = "zh-CN-XiaoxiaoNeural"
    voice_b: str = "zh-CN-YunxiNeural"
    silence_ms: int = Field(default=220, ge=0, le=2000)
    make_video: bool = False
    settings: RuntimeSettingsUpdate = Field(
        default_factory=RuntimeSettingsUpdate
    )
    translator_api_key_ref: str | None = None
