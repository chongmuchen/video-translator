"""Chinese speech synthesis adapters."""

from __future__ import annotations

import asyncio
import logging
import wave
from pathlib import Path

import httpx

from ..errors import ConfigurationError, PipelineError
from ..models import Segment
from ..settings import Settings


class SpeechSynthesizer:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger

    async def _edge_save(self, text: str, output: Path) -> None:
        try:
            import edge_tts
        except ImportError as exc:
            raise ConfigurationError(
                "缺少 edge-tts，请重新运行 bootstrap。"
            ) from exc
        communicate = edge_tts.Communicate(
            text=text,
            voice=self.settings.tts_voice,
            rate=self.settings.tts_rate,
            volume=self.settings.tts_volume,
        )
        await communicate.save(str(output))

    def _http_save(self, text: str, output: Path) -> None:
        if not self.settings.tts_http_url:
            raise ConfigurationError(
                "VT_TTS_PROVIDER=http 时必须配置 VT_TTS_HTTP_URL。"
            )
        headers: dict[str, str] = {}
        if self.settings.tts_http_api_key:
            headers["Authorization"] = (
                f"Bearer {self.settings.tts_http_api_key}"
            )
        body = {
            "text": text,
            "voice": self.settings.tts_voice,
            "rate": self.settings.tts_rate,
        }
        try:
            with httpx.Client(timeout=180) as client:
                response = client.post(
                    self.settings.tts_http_url,
                    headers=headers,
                    json=body,
                )
                response.raise_for_status()
            output.write_bytes(response.content)
        except httpx.HTTPError as exc:
            raise PipelineError(f"调用 TTS 服务失败：{exc}") from exc

    def _cosyvoice_save(self, text: str, output: Path) -> None:
        mode = self.settings.cosyvoice_mode
        url = (
            self.settings.cosyvoice_base_url.rstrip("/")
            + f"/inference_{mode}"
        )
        data: dict[str, str] = {"tts_text": text}
        files = None
        if mode in {"sft", "instruct"}:
            data["spk_id"] = self.settings.tts_voice
        if mode in {"zero_shot", "cross_lingual", "instruct2"}:
            prompt = self.settings.cosyvoice_prompt_wav
            if not prompt or not prompt.expanduser().is_file():
                raise ConfigurationError(
                    f"CosyVoice {mode} 模式需要 "
                    "VT_COSYVOICE_PROMPT_WAV。"
                )
            files = {
                "prompt_wav": (
                    prompt.name,
                    prompt.expanduser().read_bytes(),
                    "audio/wav",
                )
            }
        if mode == "zero_shot":
            if not self.settings.cosyvoice_prompt_text:
                raise ConfigurationError(
                    "CosyVoice zero_shot 模式需要 "
                    "VT_COSYVOICE_PROMPT_TEXT。"
                )
            data["prompt_text"] = self.settings.cosyvoice_prompt_text
        if mode in {"instruct", "instruct2"}:
            data["instruct_text"] = self.settings.cosyvoice_instruct_text

        try:
            with httpx.Client(timeout=180) as client:
                response = client.post(url, data=data, files=files)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PipelineError(f"调用 CosyVoice 失败：{exc}") from exc
        if not response.content:
            raise PipelineError("CosyVoice 返回了空音频。")

        # The official FastAPI runtime streams headerless signed 16-bit PCM.
        with wave.open(str(output), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(self.settings.cosyvoice_sample_rate)
            audio.writeframes(response.content)

    def synthesize(self, text: str, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        if self.settings.tts_provider == "edge":
            asyncio.run(self._edge_save(text, output))
        elif self.settings.tts_provider == "http":
            self._http_save(text, output)
        elif self.settings.tts_provider == "cosyvoice":
            self._cosyvoice_save(text, output)
        else:
            raise ConfigurationError(
                f"不支持 TTS provider：{self.settings.tts_provider}"
            )
        if not output.is_file() or output.stat().st_size == 0:
            raise PipelineError("TTS 没有生成有效音频。")

    def synthesize_segments(
        self,
        segments: list[Segment],
        output_dir: Path,
    ) -> list[Segment]:
        output_dir.mkdir(parents=True, exist_ok=True)
        for position, segment in enumerate(segments, start=1):
            text = (segment.translated_text or "").strip()
            if not text:
                raise PipelineError(f"片段 {segment.index} 没有可朗读的译文。")
            extension = (
                ".wav"
                if self.settings.tts_provider == "cosyvoice"
                else ".mp3"
            )
            output = output_dir / f"{segment.index:05d}{extension}"
            self.logger.info(
                "生成配音 %s/%s: %s",
                position,
                len(segments),
                text[:60],
            )
            self.synthesize(text, output)
            segment.tts_file = str(output)
        return segments
