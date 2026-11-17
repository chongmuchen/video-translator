"""Offline Mandarin synthesis using the project's installed Kokoro runtime."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from ..models import Segment
from ..settings import PROJECT_ROOT

VOICE = "zm_010"
SPEED = 1.12
SAMPLE_RATE = 24_000


class LocalSpeech:
    def __init__(self, threads: int) -> None:
        runtime = PROJECT_ROOT / ".runtime"
        packages = str(runtime / "puzzle-kokoro")
        if packages not in sys.path:
            sys.path.insert(0, packages)

        import numpy as np
        import soundfile as sf
        import torch
        from kokoro import KModel, KPipeline
        from misaki.espeak import EspeakFallback

        model_dir = runtime / "puzzle-kokoro-model"
        repo_id = "hexgrad/Kokoro-82M-v1.1-zh"
        torch.set_num_threads(threads)
        if torch.get_num_interop_threads() != 1:
            torch.set_num_interop_threads(1)
        model = KModel(
            repo_id=repo_id,
            config=str(model_dir / "config.json"),
            model=str(model_dir / "kokoro-v1_1-zh.pth"),
        ).eval()
        english = EspeakFallback(british=False, version="2.0")
        self.pipeline = KPipeline(
            lang_code="z",
            repo_id=repo_id,
            model=model,
            en_callable=lambda text: english(SimpleNamespace(text=text))[0] or "",
        )
        self.voice = torch.load(model_dir / f"{VOICE}.pt", map_location="cpu", weights_only=True)
        self._np = np
        self._write = sf.write

    def synthesize_segments(self, segments: list[Segment], output_dir: Path) -> list[Segment]:
        output_dir.mkdir(parents=True, exist_ok=True)
        for position, segment in enumerate(segments, 1):
            text = (segment.translated_text or "").strip()
            if not text:
                raise ValueError(f"No text for cue {segment.index}")
            key = hashlib.sha256(f"kokoro-82m-v1.1-zh/{VOICE}/{SPEED}\0{text}".encode()).hexdigest()
            output = output_dir / f"{segment.index:05d}-{key}.wav"
            if not output.is_file():
                audio = [
                    result.audio.numpy()
                    for result in self.pipeline(text, voice=self.voice, speed=SPEED)
                    if result.audio is not None and len(result.audio)
                ]
                if not audio:
                    raise ValueError(f"No local speech for cue {segment.index}: {text}")
                signal = self._np.concatenate(audio)
                active = self._np.flatnonzero(self._np.abs(signal) > 0.002)
                if not active.size:
                    raise ValueError(f"Silent local speech for cue {segment.index}: {text}")
                signal = signal[max(0, active[0] - 450):active[-1] + 1800]
                with TemporaryDirectory(dir=output_dir) as temporary:
                    staged = Path(temporary) / output.name
                    self._write(str(staged), signal, SAMPLE_RATE, subtype="PCM_16")
                    staged.replace(output)
            segment.tts_file = str(output)
            if position % 40 == 0:
                print(f"Local speech {position}/{len(segments)}", flush=True)
        return segments
