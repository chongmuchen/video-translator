"""Chinese speech synthesis adapters."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import wave
from collections.abc import Callable
from pathlib import Path

import httpx

from ..errors import ConfigurationError, PipelineError
from ..models import Segment
from ..settings import Settings


def parse_speaker_voice_map(value: str) -> dict[str, str]:
    """Parse JSON or simple ``speaker=voice`` mappings."""

    raw = (value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        result: dict[str, str] = {}
        for item in raw.replace("\n", ",").split(","):
            if not item.strip() or "=" not in item:
                continue
            speaker, voice = item.split("=", 1)
            speaker = speaker.strip()
            voice = voice.strip()
            if speaker and voice:
                result[speaker] = voice
        return result
    if not isinstance(parsed, dict):
        raise ConfigurationError("speaker_voice_map 必须是 JSON 对象。")
    return {
        str(speaker): str(voice)
        for speaker, voice in parsed.items()
        if str(speaker).strip() and str(voice).strip()
    }


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
        await asyncio.wait_for(
            communicate.save(str(output)),
            timeout=self.settings.tts_timeout_seconds,
        )

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
            with httpx.Client(timeout=self.settings.tts_timeout_seconds) as client:
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
            with httpx.Client(timeout=self.settings.tts_timeout_seconds) as client:
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
        attempts = max(1, self.settings.tts_retries + 1)
        delay = max(0.0, self.settings.tts_retry_backoff_seconds)
        for attempt in range(1, attempts + 1):
            try:
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
                return
            except ConfigurationError:
                raise
            except Exception as exc:
                output.unlink(missing_ok=True)
                if attempt >= attempts:
                    raise PipelineError(f"TTS 重试失败：{exc}") from exc
                self.logger.warning(
                    "TTS 失败，将在 %.1fs 后重试（%s/%s）：%s",
                    delay,
                    attempt,
                    attempts - 1,
                    exc,
                )
                if delay:
                    time.sleep(delay)
                    delay *= 2

    def _write_silence(self, segment: Segment, output: Path) -> None:
        """Keep an unclear subtitle slot without speaking its marker aloud."""

        frame_count = max(
            1,
            round(segment.duration * self.settings.dub_sample_rate),
        )
        with wave.open(str(output), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(self.settings.dub_sample_rate)
            audio.writeframes(b"\x00\x00" * frame_count)

    def synthesize_segments(
        self,
        segments: list[Segment],
        output_dir: Path,
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> list[Segment]:
        output_dir.mkdir(parents=True, exist_ok=True)
        voice_map = parse_speaker_voice_map(self.settings.speaker_voice_map)
        for position, segment in enumerate(segments, start=1):
            if cancel_check:
                cancel_check()
            text = (segment.translated_text or "").strip()
            if not text:
                raise PipelineError(f"片段 {segment.index} 没有可朗读的译文。")
            if segment.asr_unclear:
                output = output_dir / f"{segment.index:05d}.wav"
                self.logger.info(
                    "片段 %s 原音不清：保留字幕占位并生成静音",
                    segment.index,
                )
                self._write_silence(segment, output)
                segment.tts_file = str(output)
                continue
            extension = (
                ".wav"
                if self.settings.tts_provider == "cosyvoice"
                else ".mp3"
            )
            output = output_dir / f"{segment.index:05d}{extension}"
            voice = voice_map.get(segment.speaker or "", self.settings.tts_voice)
            self.logger.info(
                "生成配音 %s/%s%s: %s",
                position,
                len(segments),
                f" [{segment.speaker}→{voice}]" if segment.speaker else "",
                text[:60],
            )
            if voice != self.settings.tts_voice:
                line_settings = self.settings.model_copy(
                    update={"tts_voice": voice}
                )
                SpeechSynthesizer(line_settings, self.logger).synthesize(
                    text,
                    output,
                )
            else:
                self.synthesize(text, output)
            segment.tts_file = str(output)
            if cancel_check:
                cancel_check()
        return segments
