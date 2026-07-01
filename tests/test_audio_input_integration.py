import logging
from pathlib import Path

from video_translator.commands import run_command
from video_translator.models import PipelineOptions, Segment
from video_translator.pipeline.media import probe_media
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


def test_local_audio_can_be_muxed_to_dual_track_m4a(
    tmp_path: Path,
) -> None:
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

    store.write_segments(
        manifest,
        [
            Segment(
                index=0,
                start=0.1,
                end=0.8,
                source_text="Hello.",
                translated_text="你好。",
            ).model_dump(mode="json")
        ],
    )
    dub = store.job_dir(manifest.id) / "dub-timeline.wav"
    run_command(
        [
            media.ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=1",
            "-ac",
            "1",
            "-ar",
            "24000",
            dub,
        ]
    )
    manifest.dub_audio_path = str(dub)
    manifest.completed_steps = [
        "download",
        "extract",
        "transcribe",
        "translate",
        "synthesize",
        "align",
    ]
    store.save(manifest)

    completed = pipeline.run_step(manifest, PipelineStep.mux)

    output = Path(completed.output_path or "")
    assert output.suffix == ".m4a"
    assert output.is_file()
    streams = probe_media(output, media)["streams"]
    assert sum(item["codec_type"] == "audio" for item in streams) == 2
