from pathlib import Path

from video_translator.models import JobStatus, PipelineOptions
from video_translator.settings import Settings
from video_translator.store import JobStore


def test_job_store_roundtrip(tmp_path: Path) -> None:
    store = JobStore(Settings(data_dir=tmp_path))
    manifest = store.create("https://youtu.be/example", PipelineOptions())
    store.set_stage(manifest, JobStatus.transcribing, 25, "识别")
    restored = store.get(manifest.id)
    assert restored.status == JobStatus.transcribing
    assert restored.progress == 25
    assert restored.stage_message == "识别"

