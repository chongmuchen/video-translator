"""Application settings loaded from environment variables and .env."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigurationError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
KOKORO_DEFAULT_VOICE = "zm_010"
KOKORO_DEFAULT_SPEED = 1.12


class Settings(BaseSettings):
    """Runtime configuration.

    Every field can be overridden with the matching ``VT_*`` variable.
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_prefix="VT_",
        extra="ignore",
    )

    data_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / ".runtime" / "cli")
    max_video_seconds: int = 7200
    max_download_height: int = 1080
    allowed_hosts: str = (
        "youtube.com,www.youtube.com,m.youtube.com,youtu.be,"
        "bilibili.com,www.bilibili.com,b23.tv"
    )

    ffmpeg_bin: str | None = None
    ffprobe_bin: str | None = None
    cookies_from_browser: str | None = None
    cookie_file: Path | None = None

    asr_model: str = "large-v3"
    asr_device: str = "auto"
    asr_compute_type: str = "auto"
    source_language: str | None = None

    translator_provider: Literal["codex", "openai_compatible", "passthrough"] = "codex"
    translator_base_url: str = "http://127.0.0.1:11434/v1"
    translator_model: str = "qwen3:8b"
    translator_api_key: str | None = None
    translator_timeout_seconds: float = 180
    translation_batch_size: int = 12

    tts_provider: Literal["kokoro"] = "kokoro"
    tts_voice: str = KOKORO_DEFAULT_VOICE
    tts_rate: float = Field(default=KOKORO_DEFAULT_SPEED, gt=0, allow_inf_nan=False)
    tts_threads: int = Field(default=4, gt=0)

    keep_original_audio: bool = True
    duck_original_audio: bool = True
    burn_subtitles: bool = False
    enable_demucs: bool = False
    dub_sample_rate: int = 24000
    max_tempo_factor: float = 1.8

    @field_validator("tts_voice")
    @classmethod
    def validate_tts_voice(cls, voice: str) -> str:
        voice = voice.strip().lower()
        if not re.fullmatch(r"z[fm]_[0-9]{3}", voice):
            raise ConfigurationError(
                f"只支持 Kokoro 普通话音色。请将 VT_TTS_VOICE 设置为已安装的音色，例如 {KOKORO_DEFAULT_VOICE}。"
            )
        return voice

    @model_validator(mode="after")
    def resolve_paths(self) -> "Settings":
        if not self.data_dir.is_absolute():
            self.data_dir = (PROJECT_ROOT / self.data_dir).resolve()
        return self

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def runtime_dir(self) -> Path:
        return PROJECT_ROOT / ".runtime"

    @property
    def allowed_host_set(self) -> set[str]:
        return {
            host.strip().lower()
            for host in self.allowed_hosts.split(",")
            if host.strip()
        }

    def ensure_directories(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
