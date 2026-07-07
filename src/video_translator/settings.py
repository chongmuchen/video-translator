"""Application settings loaded from environment variables and .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .translation_providers import (
    DEFAULT_TRANSLATOR_BASE_URL,
    DEFAULT_TRANSLATOR_MODEL,
    TRANSLATION_PROVIDER_PRESETS,
    TranslatorProvider,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration.

    Every field can be overridden with the matching ``VT_*`` variable.
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_prefix="VT_",
        extra="ignore",
    )

    data_dir: Path = PROJECT_ROOT / "data"
    max_video_seconds: int = 7200
    max_download_height: int = 1080
    allowed_hosts: str = (
        "youtube.com,www.youtube.com,m.youtube.com,youtu.be,"
        "bilibili.com,www.bilibili.com,b23.tv,podcasts.apple.com"
    )

    ffmpeg_bin: str | None = None
    ffprobe_bin: str | None = None
    cookies_from_browser: str | None = None
    cookie_file: Path | None = None
    download_backend: Literal["auto", "native", "curl"] = "auto"
    download_proxy: str | None = None
    download_impersonate: str | None = None
    download_retries: int = 10
    download_fragment_retries: int = 10
    download_socket_timeout: float = 30
    download_http_chunk_size: int = 10 * 1024 * 1024

    asr_backend: Literal["faster_whisper", "mlx_whisper"] = "faster_whisper"
    asr_model: str = "large-v3"
    asr_device: str = "auto"
    asr_compute_type: str = "auto"
    source_language: str | None = None
    enable_diarization: bool = False
    diarization_backend: Literal["pyannote"] = "pyannote"
    diarization_model: str = "pyannote/speaker-diarization-3.1"
    diarization_auth_token: str | None = None

    translator_provider: TranslatorProvider = "openai_compatible"
    translator_base_url: str = DEFAULT_TRANSLATOR_BASE_URL
    translator_model: str = DEFAULT_TRANSLATOR_MODEL
    translator_api_key: str | None = None
    translator_timeout_seconds: float = 180
    translator_retries: int = 2
    translator_retry_backoff_seconds: float = 3.0
    translation_batch_size: int = 12
    translator_codex_bin: str = "codex"
    translator_codex_model: str | None = None
    translator_codex_strategy: Literal[
        "economy",
        "balanced",
        "quality",
        "account_default",
    ] = "balanced"
    asr_unclear_threshold: float = 0.45

    tts_provider: Literal["edge", "http", "cosyvoice"] = "edge"
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    tts_rate: str = "+0%"
    tts_volume: str = "+0%"
    tts_http_url: str | None = None
    tts_http_api_key: str | None = None
    speaker_voice_map: str = ""
    tts_timeout_seconds: float = 180
    tts_retries: int = 2
    tts_retry_backoff_seconds: float = 2.0
    cosyvoice_base_url: str = "http://127.0.0.1:50000"
    cosyvoice_mode: Literal[
        "sft",
        "zero_shot",
        "cross_lingual",
        "instruct",
        "instruct2",
    ] = "sft"
    cosyvoice_prompt_wav: Path | None = None
    cosyvoice_prompt_text: str = ""
    cosyvoice_instruct_text: str = ""
    cosyvoice_sample_rate: int = 22050

    keep_original_audio: bool = True
    duck_original_audio: bool = True
    burn_subtitles: bool = False
    enable_demucs: bool = False
    dub_sample_rate: int = 24000
    max_tempo_factor: float = 1.8
    auto_shorten_overlong_tts: bool = True
    tts_shorten_retries: int = 1
    enable_lip_sync: bool = False
    lip_sync_command: str | None = None

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    worker_count: int = 1
    require_content_authorization: bool = False
    max_jobs_per_day: int = 0

    @model_validator(mode="after")
    def resolve_paths(self) -> "Settings":
        if not self.data_dir.is_absolute():
            self.data_dir = (PROJECT_ROOT / self.data_dir).resolve()
        preset = TRANSLATION_PROVIDER_PRESETS.get(
            self.translator_provider
        )
        if preset:
            if self.translator_base_url == DEFAULT_TRANSLATOR_BASE_URL:
                self.translator_base_url = preset["base_url"]
            if self.translator_model == DEFAULT_TRANSLATOR_MODEL:
                self.translator_model = preset["model"]
        if (
            self.translator_provider == "codex_cli"
            and self.translation_batch_size == 12
        ):
            self.translation_batch_size = 48
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
    settings = Settings()
    settings.ensure_directories()
    return settings
