import logging
from pathlib import Path

from video_translator.commands import run_command
from video_translator.models import PipelineOptions
from video_translator.pipeline.stepwise import (
    PipelineStep,
    StepwiseVideoTranslationPipeline,
)
from video_translator.runtime import resolve_media_binaries
from video_translator.settings import Settings
from video_translator.store import JobStore


def test_local_audio_can_be_downloaded_and_extracted(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    media = resolve_media_binaries(settings)
    source = tmp_path / "podcast.m4a"
    run_command(
        [
            media.ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "aac",
            source,
        ]
    )

    store = JobStore(settings)
    pipeline = StepwiseVideoTranslationPipeline(settings, store)
    manifest = store.create(str(source), PipelineOptions())
    pipeline.run_step(manifest, PipelineStep.download)
    pipeline.run_step(manifest, PipelineStep.extract)

    assert manifest.metadata["media_kind"] == "audio"
    assert Path(manifest.source_path or "").suffix == ".m4a"
    assert Path(manifest.audio_path or "").is_file()
    assert manifest.completed_steps == ["download", "extract"]

