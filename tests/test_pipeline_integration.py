import logging
import wave
from pathlib import Path

from video_translator.commands import run_command
from video_translator.models import JobStatus, PipelineOptions, Segment
from video_translator.pipeline import runner
from video_translator.pipeline.media import probe_media
from video_translator.pipeline.runner import VideoTranslationPipeline
from video_translator.pipeline.stepwise import STEP_ORDER
from video_translator.runtime import resolve_media_binaries
from video_translator.settings import Settings
from video_translator.store import JobStore


class FakeTranscriber:
    def __init__(self, settings: Settings, logger: logging.Logger):
        pass

    def transcribe(self, path: str, *, language: str | None = None):
        return (
            [
                Segment(
                    index=0,
                    start=0.25,
                    end=1.5,
                    source_text="Hello world.",
                )
            ],
            {"detected_language": "en"},
        )


class FakeTranslator:
    def __init__(self, settings: Settings, logger: logging.Logger):
        pass

    def translate(
        self,
        segments: list[Segment],
        *,
        target_language: str,
        glossary: dict[str, str],
    ) -> list[Segment]:
        segments[0].translated_text = "你好，世界。"
        return segments


class FakeSynthesizer:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.rate = settings.dub_sample_rate

    def synthesize_segments(
        self,
        segments: list[Segment],
        output_dir: Path,
    ) -> list[Segment]:
        output_dir.mkdir(parents=True, exist_ok=True)
        for segment in segments:
            output = output_dir / f"{segment.index:05d}.wav"
            with wave.open(str(output), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(self.rate)
                audio.writeframes(b"\x00\x00" * self.rate)
            segment.tts_file = str(output)
        return segments


def test_end_to_end_runner_with_fake_model_adapters(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    media = resolve_media_binaries(settings)
    source = tmp_path / "source.mp4"
    run_command(
        [
            media.ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            source,
        ]
    )
    monkeypatch.setattr(runner, "FasterWhisperTranscriber", FakeTranscriber)
    monkeypatch.setattr(runner, "SegmentTranslator", FakeTranslator)
    monkeypatch.setattr(runner, "SpeechSynthesizer", FakeSynthesizer)

    store = JobStore(settings)
    manifest = store.create(str(source), PipelineOptions())
    completed = VideoTranslationPipeline(settings, store).run(manifest)

    assert completed.status == JobStatus.completed
    assert completed.completed_steps == [step.value for step in STEP_ORDER]
    output = Path(completed.output_path or "")
    assert output.is_file()
    streams = probe_media(output, media)["streams"]
    assert sum(stream["codec_type"] == "audio" for stream in streams) == 2
    assert sum(stream["codec_type"] == "subtitle" for stream in streams) == 1
