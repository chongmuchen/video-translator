"""Serializable pipeline models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .translation_providers import TranslatorProvider


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


UNCLEAR_TRANSCRIPT_TEXT = "【原音不清，未能可靠识别】"


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
    canceled = "canceled"
    failed = "failed"


class Segment(BaseModel):
    index: int
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    source_text: str
    translated_text: str | None = None
    speaker: str | None = None
    tts_file: str | None = None
    raw_source_text: str | None = None
    asr_confidence: float | None = Field(default=None, ge=0, le=1)
    asr_unclear: bool = False

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


class ContentAuthorization(BaseModel):
    authorized: bool = False
    rights_basis: Literal[
        "own",
        "licensed",
        "public_domain",
        "fair_use",
        "permission",
        "other",
    ] = "other"
    notes: str = Field(default="", max_length=2000)


class ContentAuthorizationRequest(BaseModel):
    authorization: ContentAuthorization


class JobCreateRequest(BaseModel):
    url: str
    options: PipelineOptions = Field(default_factory=PipelineOptions)
    authorization: ContentAuthorization | None = None


class PipelineOptionsUpdate(BaseModel):
    target_language: str | None = None
    source_language: str | None = None
    keep_original_audio: bool | None = None
    duck_original_audio: bool | None = None
    burn_subtitles: bool | None = None
    glossary: dict[str, str] | None = None


class RuntimeSettingsUpdate(BaseModel):
    max_download_height: int | None = Field(default=None, ge=144, le=4320)
    cookies_from_browser: str | None = None
    download_backend: Literal["auto", "native", "curl"] | None = None
    download_proxy: str | None = None
    download_impersonate: str | None = None

    asr_backend: Literal["faster_whisper", "mlx_whisper"] | None = None
    asr_model: str | None = None
    asr_device: Literal["auto", "cpu", "cuda"] | None = None
    asr_compute_type: Literal[
        "auto",
        "int8",
        "int8_float32",
        "int8_float16",
        "float16",
        "float32",
    ] | None = None
    asr_unclear_threshold: float | None = Field(
        default=None,
        ge=0,
        le=1,
    )
    enable_diarization: bool | None = None
    diarization_backend: Literal["pyannote"] | None = None
    diarization_model: str | None = None
    diarization_auth_token: str | None = None

    translator_provider: TranslatorProvider | None = None
    translator_base_url: str | None = None
    translator_model: str | None = None
    translator_api_key: str | None = None
    translator_timeout_seconds: float | None = Field(
        default=None,
        ge=1,
        le=3600,
    )
    translator_retries: int | None = Field(default=None, ge=0, le=10)
    translator_retry_backoff_seconds: float | None = Field(
        default=None,
        ge=0,
        le=300,
    )
    translation_batch_size: int | None = Field(default=None, ge=1, le=100)
    translator_codex_bin: str | None = None
    translator_codex_model: str | None = None
    translator_codex_strategy: Literal[
        "economy",
        "balanced",
        "quality",
        "account_default",
    ] | None = None

    tts_provider: Literal["edge", "http", "cosyvoice"] | None = None
    tts_voice: str | None = None
    tts_rate: str | None = None
    tts_volume: str | None = None
    tts_http_url: str | None = None
    tts_http_api_key: str | None = None
    speaker_voice_map: str | None = None
    tts_timeout_seconds: float | None = Field(
        default=None,
        ge=1,
        le=3600,
    )
    tts_retries: int | None = Field(default=None, ge=0, le=10)
    tts_retry_backoff_seconds: float | None = Field(
        default=None,
        ge=0,
        le=300,
    )
    cosyvoice_base_url: str | None = None
    cosyvoice_mode: Literal[
        "sft",
        "zero_shot",
        "cross_lingual",
        "instruct",
        "instruct2",
    ] | None = None
    cosyvoice_sample_rate: int | None = Field(
        default=None,
        ge=8000,
        le=96000,
    )

    dub_sample_rate: int | None = Field(default=None, ge=8000, le=96000)
    max_tempo_factor: float | None = Field(default=None, ge=1.0, le=4.0)
    auto_shorten_overlong_tts: bool | None = None
    tts_shorten_retries: int | None = Field(default=None, ge=0, le=3)
    enable_demucs: bool | None = None
    enable_lip_sync: bool | None = None
    lip_sync_command: str | None = None


class StagedJobCreateRequest(JobCreateRequest):
    settings: RuntimeSettingsUpdate = Field(
        default_factory=RuntimeSettingsUpdate
    )


class AutomatedJobCreateRequest(StagedJobCreateRequest):
    """Create a job and run all seven stages with one settings snapshot."""

    translator_api_key_ref: str | None = None


class SecretSaveRequest(BaseModel):
    value: str = Field(min_length=1)
    reference: str | None = None


class StepRunRequest(BaseModel):
    force: bool = False
    options: PipelineOptionsUpdate = Field(
        default_factory=PipelineOptionsUpdate
    )
    settings: RuntimeSettingsUpdate = Field(
        default_factory=RuntimeSettingsUpdate
    )


class SegmentRerunRequest(BaseModel):
    segment_ids: list[int] = Field(min_length=1, max_length=200)
    mode: Literal["translate", "synthesize", "both"] = "both"
    options: PipelineOptionsUpdate = Field(
        default_factory=PipelineOptionsUpdate
    )
    settings: RuntimeSettingsUpdate = Field(
        default_factory=RuntimeSettingsUpdate
    )


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
