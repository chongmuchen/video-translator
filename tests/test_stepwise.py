from pathlib import Path

from video_translator.models import PipelineOptions
from video_translator.pipeline import stepwise as stepwise_module
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


def test_uploaded_local_video_can_force_revalidate_download(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(data_dir=tmp_path)
    store = JobStore(settings)
    pipeline = StepwiseVideoTranslationPipeline(settings, store)
    manifest = store.create("Lecture One.mp4", PipelineOptions())
    source = store.job_dir(manifest.id) / "source.mp4"
    source.write_bytes(b"local-video")
    monkeypatch.setattr(
        stepwise_module,
        "resolve_media_binaries",
        lambda settings: object(),
    )
    monkeypatch.setattr(
        stepwise_module,
        "has_audio_stream",
        lambda path, media: True,
    )
    monkeypatch.setattr(
        stepwise_module,
        "has_video_stream",
        lambda path, media: True,
    )
    monkeypatch.setattr(
        stepwise_module,
        "probe_duration",
        lambda path, media: 12.5,
    )

    pipeline.register_local_source(
        manifest,
        source,
        title="Lecture One",
        original_filename="Lecture One.mp4",
    )
    first_path = Path(manifest.source_path)
    assert first_path.is_file()
    assert manifest.completed_steps == ["download"]

    pipeline.run_step(manifest, PipelineStep.download, force=True)

    restored = store.get(manifest.id)
    assert restored.title == "Lecture One"
    assert restored.completed_steps == ["download"]
    assert Path(restored.source_path).is_file()
    assert restored.source == restored.source_path
    assert list(store.job_dir(manifest.id).glob("source.*")) == [
        Path(restored.source_path)
    ]
