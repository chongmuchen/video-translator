import json
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from video_translator.api import create_app
from video_translator.settings import Settings


def test_real_local_video_upload_is_registered_without_duplicate_copy(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the local-video integration test")
    sample = tmp_path / "Local Lecture.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=1",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(sample),
        ],
        check=True,
        capture_output=True,
    )
    app = create_app(Settings(data_dir=tmp_path / "data"))

    with TestClient(app) as client:
        response = client.post(
            "/api/jobs/local",
            files=[
                (
                    "files",
                    (sample.name, sample.read_bytes(), "video/mp4"),
                )
            ],
            data={"config": json.dumps({"run_mode": "staged"})},
        )

    assert response.status_code == 202
    job = response.json()["jobs"][0]
    assert job["status"] == "downloaded"
    assert job["next_step"] == "extract"
    assert job["artifacts"]["source"] is True
    manifest = app.state.manager.store.get(job["id"])
    source = Path(manifest.source_path)
    assert source.is_file()
    assert source.name == "source.mp4"
    assert manifest.source == manifest.source_path
    assert manifest.metadata["source_type"] == "local_upload"
    assert manifest.metadata["media_kind"] == "video"
    assert 0.9 <= manifest.metadata["media_duration"] <= 1.1
    assert list(source.parent.glob("source.*")) == [source]
