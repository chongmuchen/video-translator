from pathlib import Path
import threading
import time

from video_translator.manager import JobManager
from video_translator.models import PipelineOptions
from video_translator.settings import Settings


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_manager_distinguishes_queued_from_running(tmp_path: Path) -> None:
    manager = JobManager(
        Settings(data_dir=tmp_path / "data", worker_count=1)
    )
    first = manager.store.create("first.mp4", PipelineOptions())
    second = manager.store.create("second.mp4", PipelineOptions())
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def run_first(update_state):
        update_state("running")
        first_started.set()
        assert release_first.wait(2)
        return first

    def run_second(update_state):
        update_state("running")
        second_started.set()
        return second

    first_future = manager._submit_future(
        first.id,
        run_first,
        task_type="test:first",
    )
    assert first_started.wait(1)
    second_future = manager._submit_future(
        second.id,
        run_second,
        task_type="test:second",
    )

    assert manager.execution_state(first.id) == "running"
    assert manager.execution_state(second.id) == "queued"
    assert manager.is_active(first.id)
    assert manager.is_active(second.id)
    assert manager.is_running(first.id)
    assert not manager.is_running(second.id)
    assert not second_started.is_set()

    release_first.set()
    assert first_future.result(timeout=2) is first
    assert second_future.result(timeout=2) is second
    assert _wait_until(lambda: not manager.is_active(second.id))
    manager.shutdown()


def test_translation_slot_limits_parallel_work(tmp_path: Path) -> None:
    manager = JobManager(
        Settings(
            data_dir=tmp_path / "data",
            worker_count=3,
            translation_worker_count=2,
        )
    )
    manifests = [
        manager.store.create(f"lecture-{index}.mp4", PipelineOptions())
        for index in range(3)
    ]
    release = threading.Event()
    two_started = threading.Event()
    counter_lock = threading.Lock()
    active_count = 0
    maximum_active = 0

    def make_work(manifest):
        def work(update_state):
            def translate():
                nonlocal active_count, maximum_active
                with counter_lock:
                    active_count += 1
                    maximum_active = max(maximum_active, active_count)
                    if active_count == 2:
                        two_started.set()
                assert release.wait(2)
                with counter_lock:
                    active_count -= 1
                return manifest

            return manager._run_with_slot(
                manager._translation_slot,
                update_state,
                translate,
            )

        return work

    futures = [
        manager._submit_future(
            manifest.id,
            make_work(manifest),
            task_type="test:translate",
        )
        for manifest in manifests
    ]

    assert two_started.wait(1)
    states = [manager.execution_state(item.id) for item in manifests]
    assert states.count("running") == 2
    assert states.count("queued") == 1
    assert maximum_active == 2

    release.set()
    for future, manifest in zip(futures, manifests, strict=True):
        assert future.result(timeout=2) is manifest
    manager.shutdown()
