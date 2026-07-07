"""In-process background job manager."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
import threading

from .control import clear_cancel_request, mark_canceled, request_cancel
from .models import JobManifest, PipelineOptions
from .pipeline.runner import VideoTranslationPipeline
from .pipeline.stepwise import PipelineStep, StepwiseVideoTranslationPipeline
from .settings import Settings
from .store import JobStore
from .task_queue import LocalTaskQueue


class JobManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = JobStore(settings)
        self.queue = LocalTaskQueue(settings)
        self.pipeline = VideoTranslationPipeline(settings, self.store)
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, settings.worker_count),
            thread_name_prefix="video-translator",
        )
        self._futures: dict[str, Future[JobManifest]] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        source: str,
        options: PipelineOptions,
        *,
        settings: Settings | None = None,
        metadata: dict | None = None,
    ) -> JobManifest:
        manifest = self.store.create(source, options)
        return self.submit_existing(
            manifest,
            settings=settings,
            metadata=metadata,
        )

    def submit_existing(
        self,
        manifest: JobManifest,
        *,
        settings: Settings | None = None,
        metadata: dict | None = None,
    ) -> JobManifest:
        """Submit an already-created task to the full pipeline.

        Keeping task creation separate from background execution makes the
        manifest, log path, and local directory visible immediately, even when
        the first network step later fails.
        """

        if metadata:
            manifest.metadata.update(metadata)
        clear_cancel_request(manifest)
        self.store.save(manifest)
        pipeline = (
            VideoTranslationPipeline(settings, self.store)
            if settings is not None
            else self.pipeline
        )
        future = self._submit_future(
            manifest.id,
            lambda: pipeline.run(manifest),
            task_type="run_all",
            payload={"source": manifest.source},
        )
        return manifest

    def _submit_future(
        self,
        job_id: str,
        function: Callable[[], JobManifest],
        *,
        task_type: str,
        payload: dict | None = None,
    ) -> Future[JobManifest]:
        task_id = self.queue.enqueue(
            resource_type="job",
            resource_id=job_id,
            task_type=task_type,
            payload=payload,
        )

        def run() -> JobManifest:
            self.queue.mark(task_id, "running")
            try:
                result = function()
            except Exception as exc:
                self.queue.mark(task_id, "failed", str(exc))
                raise
            self.queue.mark(task_id, "completed")
            return result

        future = self.executor.submit(run)
        with self._lock:
            self._futures[job_id] = future
        future.add_done_callback(
            lambda done: self._forget(job_id, done)
        )
        return future

    def create(
        self,
        source: str,
        options: PipelineOptions,
    ) -> JobManifest:
        return self.store.create(source, options)

    def submit_step(
        self,
        job_id: str,
        step: PipelineStep,
        *,
        force: bool = False,
        settings: Settings | None = None,
    ) -> JobManifest:
        with self._lock:
            existing = self._futures.get(job_id)
            if existing is not None and not existing.done():
                raise RuntimeError("任务正在执行，请等待当前步骤完成。")

        manifest = self.store.get(job_id)
        clear_cancel_request(manifest)
        self.store.save(manifest)
        pipeline = StepwiseVideoTranslationPipeline(
            settings or self.settings,
            self.store,
        )
        self._submit_future(
            job_id,
            lambda: pipeline.run_step(
                manifest,
                step,
                force=force,
            ),
            task_type=f"step:{step.value}",
            payload={"force": force},
        )
        return manifest

    def submit_segments(
        self,
        job_id: str,
        *,
        segment_ids: list[int],
        mode: str,
        settings: Settings | None = None,
    ) -> JobManifest:
        with self._lock:
            existing = self._futures.get(job_id)
            if existing is not None and not existing.done():
                raise RuntimeError("任务正在执行，请等待当前步骤完成。")

        manifest = self.store.get(job_id)
        clear_cancel_request(manifest)
        self.store.save(manifest)
        pipeline = StepwiseVideoTranslationPipeline(
            settings or self.settings,
            self.store,
        )
        self._submit_future(
            job_id,
            lambda: pipeline.rerun_segments(
                manifest,
                segment_ids=segment_ids,
                mode=mode,
            ),
            task_type=f"segments:{mode}",
            payload={"segment_ids": segment_ids},
        )
        return manifest

    def cancel(self, job_id: str) -> JobManifest:
        manifest = self.store.get(job_id)
        with self._lock:
            future = self._futures.get(job_id)
            if future is None or future.done():
                return manifest
            queued = future.cancel()
        if queued:
            return mark_canceled(manifest, self.store)
        return request_cancel(manifest, self.store)

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            future = self._futures.get(job_id)
            return future is not None and not future.done()

    def run_sync(
        self,
        source: str,
        options: PipelineOptions,
    ) -> JobManifest:
        manifest = self.store.create(source, options)
        return self.pipeline.run(manifest)

    def _forget(
        self,
        job_id: str,
        future: Future[JobManifest],
    ) -> None:
        with self._lock:
            if self._futures.get(job_id) is future:
                self._futures.pop(job_id, None)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
