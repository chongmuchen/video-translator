"""Offline Mandarin synthesis with the project-local Kokoro runtime."""

from __future__ import annotations

import hashlib
import logging
import math
import sys
import wave
from functools import cached_property
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from types import SimpleNamespace

from ..errors import ConfigurationError, PipelineError
from ..models import Segment
from ..settings import KOKORO_DEFAULT_SPEED, KOKORO_DEFAULT_VOICE, PROJECT_ROOT, Settings

VOICE = KOKORO_DEFAULT_VOICE
SPEED = KOKORO_DEFAULT_SPEED
SAMPLE_RATE = 24_000
REPO_ID = "hexgrad/Kokoro-82M-v1.1-zh"
MODEL_FILENAME = "kokoro-v1_1-zh.pth"
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
PACKAGE_DIR = RUNTIME_DIR / "puzzle-kokoro"
MODEL_DIR = RUNTIME_DIR / "puzzle-kokoro-model"
LOG = logging.getLogger(__name__)
# Kokoro's phonemizer dictionaries are shared between voices in the same process.
ENGINE_LOCK = Lock()


def check_runtime(voice: str) -> None:
    """Check that the selected local voice and runtime are ready."""
    if not (PACKAGE_DIR / "kokoro").is_dir():
        raise ConfigurationError("缺少 Kokoro 运行时，请运行 ./scripts/bootstrap.sh。")
    if not (MODEL_DIR / MODEL_FILENAME).is_file() or not (MODEL_DIR / "config.json").is_file():
        raise ConfigurationError("缺少 Kokoro 中文模型，请运行 ./scripts/bootstrap.sh。")
    if not (MODEL_DIR / f"{voice}.pt").is_file():
        raise ConfigurationError(
            f"未安装 Kokoro 音色 {voice}。请选择已安装音色，或将官方 .pt 文件放入 {MODEL_DIR}。"
        )


def _valid_audio(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, SAMPLE_RATE):
                return False
            frames = audio.getnframes()
            if frames == 0:
                return False
            audio.setpos(frames - 1)
            return len(audio.readframes(1)) == 2
    except (OSError, EOFError, wave.Error):
        return False


class LocalSpeech:
    def __init__(self, threads: int, *, voice: str = VOICE, speed: float = SPEED) -> None:
        self.voice_id = Settings.validate_tts_voice(voice)
        if not isinstance(speed, (int, float)) or not math.isfinite(speed) or speed <= 0:
            raise ConfigurationError("VT_TTS_RATE 必须是大于 0 的 Kokoro 语速倍率，例如 1.12。")
        if not isinstance(threads, int) or threads < 1:
            raise ConfigurationError("Kokoro 线程数必须是正整数。")
        self.speed = float(speed)
        self.threads = threads

    @cached_property
    def _engine(self):
        check_runtime(self.voice_id)
        packages = str(PACKAGE_DIR)
        if packages not in sys.path:
            sys.path.insert(0, packages)

        import numpy as np
        import soundfile as sf
        import torch
        from kokoro import KModel, KPipeline
        from misaki.espeak import EspeakFallback

        torch.set_num_threads(self.threads)
        if torch.get_num_interop_threads() != 1:
            torch.set_num_interop_threads(1)
        model = KModel(
            repo_id=REPO_ID,
            config=str(MODEL_DIR / "config.json"),
            model=str(MODEL_DIR / MODEL_FILENAME),
        ).eval()
        english = EspeakFallback(british=False, version="2.0")
        pipeline = KPipeline(
            lang_code="z",
            repo_id=REPO_ID,
            model=model,
            en_callable=lambda text: english(SimpleNamespace(text=text))[0] or "",
        )
        style = torch.load(MODEL_DIR / f"{self.voice_id}.pt", map_location="cpu", weights_only=True)
        return pipeline, style, np, sf.write

    def synthesize(self, text: str, output: Path) -> None:
        if not text.strip():
            raise PipelineError("配音文本为空。")
        with ENGINE_LOCK:
            pipeline, style, np, write = self._engine
            audio = [
                result.audio.numpy()
                for result in pipeline(text, voice=style, speed=self.speed)
                if result.audio is not None and len(result.audio)
            ]
        if not audio:
            raise PipelineError("Kokoro 没有生成音频。")
        signal = np.concatenate(audio)
        active = np.flatnonzero(np.abs(signal) > 0.002)
        if not active.size:
            raise PipelineError("Kokoro 生成的音频没有可听内容。")
        signal = signal[max(0, active[0] - 450):active[-1] + 1800]
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=output.parent) as temporary:
            staged = Path(temporary) / "speech.wav"
            write(str(staged), signal, SAMPLE_RATE, subtype="PCM_16")
            staged.replace(output)

    def synthesize_segments(self, segments: list[Segment], output_dir: Path) -> list[Segment]:
        for position, segment in enumerate(segments, 1):
            text = (segment.translated_text or "").strip()
            if not text:
                raise PipelineError(f"片段 {segment.index} 没有可朗读的译文。")
            key = hashlib.sha256(
                f"kokoro-82m-v1.1-zh/{self.voice_id}/{self.speed}\0{text}".encode()
            ).hexdigest()
            output = output_dir / f"{segment.index:05d}-{key}.wav"
            if not _valid_audio(output):
                self.synthesize(text, output)
            segment.tts_file = str(output)
            if position % 40 == 0:
                LOG.info("已准备 %d/%d 个 Kokoro 配音片段", position, len(segments))
        return segments
