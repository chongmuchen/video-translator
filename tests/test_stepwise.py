from pathlib import Path

from video_translator.models import PipelineOptions
from video_translator.pipeline.stepwise import (
    PipelineStep,
    StepwiseVideoTranslationPipeline,
)
from video_translator.settings import Settings
from video_translator.store import JobStore


def test_next_step_follows_declared_order(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    store = JobStore(settings)
    pipeline = StepwiseVideoTranslationPipeline(settings, store)
    manifest = store.create("local.mp4", PipelineOptions())

    assert pipeline.next_step(manifest) == PipelineStep.download
    manifest.completed_steps = ["download", "extract"]
    assert pipeline.next_step(manifest) == PipelineStep.transcribe


def test_force_invalidation_keeps_only_previous_steps(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    store = JobStore(settings)
    pipeline = StepwiseVideoTranslationPipeline(settings, store)
    manifest = store.create("local.mp4", PipelineOptions())
    manifest.completed_steps = [
        "download",
        "extract",
        "transcribe",
        "translate",
        "synthesize",
        "align",
    ]
    manifest.subtitle_path = "/tmp/old.srt"
    manifest.dub_audio_path = "/tmp/old.wav"
    manifest.output_path = "/tmp/old.mp4"
    store.save(manifest)

    pipeline._invalidate_from(manifest, PipelineStep.translate)

    assert manifest.completed_steps == [
        "download",
        "extract",
        "transcribe",
    ]
    assert manifest.subtitle_path is None
    assert manifest.dub_audio_path is None
    assert manifest.output_path is None

