"""Application settings loaded from environment variables and .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    translator_provider: Literal["openai_compatible", "passthrough"] = (
        "openai_compatible"
    )
    translator_base_url: str = "http://127.0.0.1:11434/v1"
    translator_model: str = "qwen3:8b"
    translator_api_key: str | None = None
    translator_timeout_seconds: float = 180
    translation_batch_size: int = 12

    tts_provider: Literal["edge", "http", "cosyvoice"] = "edge"
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    tts_rate: str = "+0%"
    tts_volume: str = "+0%"
    tts_http_url: str | None = None
    tts_http_api_key: str | None = None
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

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    worker_count: int = 1

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
    settings = Settings()
    settings.ensure_directories()
    return settings
