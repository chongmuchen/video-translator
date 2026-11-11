"""Chinese speech synthesis adapters."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import tempfile
import wave
from pathlib import Path
from typing import TypedDict

import httpx

from ..errors import ConfigurationError, PipelineError
from ..models import Segment
from ..runtime import resolve_media_binaries
from ..settings import Settings
from .media import probe_audio_duration


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
        kwargs = {
            "text": text,
            "voice": self.settings.tts_voice,
            "rate": self.settings.tts_rate,
            "volume": self.settings.tts_volume,
        }
        try:
            communicate = edge_tts.Communicate(**kwargs, boundary="WordBoundary")
        except TypeError as exc:
            if "unexpected keyword argument 'boundary'" not in str(exc):
                raise
            communicate = edge_tts.Communicate(**kwargs)

        ticks_per_second = 10_000_000
        timings: list[WordTiming] = []
        sidecar = output.with_suffix(".words.json")
        sidecar.unlink(missing_ok=True)
        with output.open("wb") as audio:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    offset, duration = chunk.get("offset"), chunk.get("duration")
                    if isinstance(offset, (int, float)) and isinstance(duration, (int, float)):
                        word = _word_timing(offset, offset + duration, chunk.get("text"))
                        if word:
                            word["start"] /= ticks_per_second
                            word["end"] /= ticks_per_second
                            timings.append(word)
        if timings:
            sidecar.write_text(json.dumps(timings, ensure_ascii=False), encoding="utf-8")

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

    def _voice_fingerprint(self) -> bytes:
        settings = self.settings
        options: list[str | int | None] = [
            settings.tts_provider, settings.tts_voice, settings.tts_rate
        ]
        if settings.tts_provider == "edge":
            options.append(settings.tts_volume)
        elif settings.tts_provider == "http":
            options.extend([settings.tts_http_url, settings.tts_http_api_key])
        else:
            options.extend(
                [
                    settings.cosyvoice_base_url,
                    settings.cosyvoice_mode,
                    settings.cosyvoice_prompt_text,
                    settings.cosyvoice_instruct_text,
                    settings.cosyvoice_sample_rate,
                ]
            )
            if settings.cosyvoice_mode in {"zero_shot", "cross_lingual", "instruct2"}:
                prompt = settings.cosyvoice_prompt_wav
                if prompt and prompt.expanduser().is_file():
                    contents = prompt.expanduser().read_bytes()
                    options.append(hashlib.sha256(contents).hexdigest())
        return hashlib.sha256(json.dumps(options).encode("utf-8")).digest()

    def synthesize_segments(
        self,
        segments: list[Segment],
        output_dir: Path,
    ) -> list[Segment]:
        output_dir.mkdir(parents=True, exist_ok=True)
        voice_fingerprint = self._voice_fingerprint()
        extension = ".wav" if self.settings.tts_provider == "cosyvoice" else ".mp3"
        media = None
        for position, segment in enumerate(segments, start=1):
            text = (segment.translated_text or "").strip()
            if not text:
                raise PipelineError(f"片段 {segment.index} 没有可朗读的译文。")
            key = hashlib.sha256(voice_fingerprint + text.encode("utf-8")).hexdigest()
            output = output_dir / f"{segment.index:05d}-{key}{extension}"
            if output.is_file():
                try:
                    if media is None:
                        media = resolve_media_binaries(self.settings)
                    probe_audio_duration(output, media)
                except PipelineError:
                    self.logger.warning(
                        "片段 %s 的缓存音频无效，重新生成。", segment.index
                    )
                else:
                    segment.tts_file = str(output)
                    continue
            self.logger.info(
                "生成配音 %s/%s: %s",
                position,
                len(segments),
                text[:60],
            )
            with tempfile.NamedTemporaryFile(
                prefix=".tts-", suffix=extension, dir=output_dir, delete=False
            ) as file:
                temporary = Path(file.name)
            temporary_words = temporary.with_suffix(".words.json")
            output_words = output.with_suffix(".words.json")
            try:
                self.synthesize(text, temporary)
                output_words.unlink(missing_ok=True)
                temporary.replace(output)
                if temporary_words.is_file():
                    temporary_words.replace(output_words)
            finally:
                temporary.unlink(missing_ok=True)
                temporary_words.unlink(missing_ok=True)
            segment.tts_file = str(output)
        return segments
