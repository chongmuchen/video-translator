"""In-process background job manager."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor

from .models import JobManifest, PipelineOptions
from .pipeline.runner import VideoTranslationPipeline
from .settings import Settings
from .store import JobStore


class JobManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = JobStore(settings)
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
    ) -> JobManifest:
        manifest = self.store.create(source, options)
        future = self.executor.submit(self.pipeline.run, manifest)
        with self._lock:
            self._futures[manifest.id] = future
        future.add_done_callback(lambda _: self._forget(manifest.id))
        return manifest

    def run_sync(
        self,
        source: str,
        options: PipelineOptions,
    ) -> JobManifest:
        manifest = self.store.create(source, options)
        return self.pipeline.run(manifest)

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)

