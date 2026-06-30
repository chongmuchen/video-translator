from pathlib import Path

from video_translator.models import JobStatus, PipelineOptions
from video_translator.settings import Settings
from video_translator.store import JobStore, safe_job_title


def test_job_store_roundtrip(tmp_path: Path) -> None:
    store = JobStore(Settings(data_dir=tmp_path))
    manifest = store.create("https://youtu.be/example", PipelineOptions())
    store.set_stage(manifest, JobStatus.transcribing, 25, "识别")
    restored = store.get(manifest.id)
    assert restored.status == JobStatus.transcribing
    assert restored.progress == 25
    assert restored.stage_message == "识别"


def test_job_directory_can_include_title_and_still_resolve_by_id(
    tmp_path: Path,
) -> None:
    store = JobStore(Settings(data_dir=tmp_path))
    manifest = store.create("https://youtu.be/example", PipelineOptions())
    original_dir = store.job_dir(manifest.id)
    source = original_dir / "source.mp4"
    source.write_bytes(b"media")
    manifest.title = "这是一个 视频 / Episode 233"
    manifest.source_path = str(source)

    titled_dir = store.add_title_to_job_dir(manifest, manifest.title)

    assert titled_dir.name == f"这是一个-视频-Episode-233--{manifest.id}"
    assert not original_dir.exists()
    assert Path(manifest.source_path or "") == titled_dir / "source.mp4"
    assert Path(manifest.source_path or "").is_file()
    assert store.job_dir(manifest.id) == titled_dir
    assert store.get(manifest.id).title == manifest.title


def test_safe_job_title_has_readable_fallback_and_length_limit() -> None:
    assert safe_job_title(" / : ? ") == "untitled"
    assert safe_job_title("a" * 100) == "a" * 60
