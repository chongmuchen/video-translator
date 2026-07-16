from pathlib import Path

import pytest

from video_translator.control import (
    cancellation_requested,
    clear_cancel_request,
    raise_if_canceled,
    request_cancel,
)
from video_translator.errors import PipelineCanceled
from video_translator.models import PipelineOptions
from video_translator.settings import Settings
from video_translator.store import JobStore


def test_cancel_marker_survives_stale_manifest_save(tmp_path: Path) -> None:
    store = JobStore(Settings(data_dir=tmp_path / "data"))
    manifest = store.create("lecture.mp4", PipelineOptions())
    stale_worker_copy = store.get(manifest.id)

    request_cancel(store.get(manifest.id), store)
    store.save(stale_worker_copy)

    restored = store.get(manifest.id)
    assert "cancel_requested" not in restored.metadata
    assert cancellation_requested(restored, store)
    with pytest.raises(PipelineCanceled):
        raise_if_canceled(stale_worker_copy, store)

    clear_cancel_request(stale_worker_copy, store)
    store.save(stale_worker_copy)
    assert not cancellation_requested(store.get(manifest.id), store)
