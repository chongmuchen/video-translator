"""In-process background job manager."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
import threading

from .control import clear_cancel_request, mark_canceled, request_cancel
from .errors import PipelineCanceled
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
        self._execution_states: dict[str, str] = {}
        self._lock = threading.Lock()
        self._asr_slot = threading.BoundedSemaphore(
            max(1, settings.asr_worker_count)
        )
        self._translation_slot = threading.BoundedSemaphore(
            max(1, settings.translation_worker_count)
        )
        self._media_slot = threading.BoundedSemaphore(
            max(1, settings.media_worker_count)
        )

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
            lambda update_state: self._run_all(
                pipeline,
                manifest,
                update_state,
            ),
            task_type="run_all",
            payload={"source": manifest.source},
        )
        return manifest

    def _slot_for_step(
        self,
        step: PipelineStep,
    ) -> threading.BoundedSemaphore | None:
        if step == PipelineStep.transcribe:
            return self._asr_slot
        if step == PipelineStep.translate:
            return self._translation_slot
        if step in {
            PipelineStep.extract,
            PipelineStep.align,
            PipelineStep.mux,
        }:
            return self._media_slot
        return None

    def _run_with_slot(
        self,
        slot: threading.BoundedSemaphore | None,
        update_state: Callable[[str], None],
        function: Callable[[], JobManifest],
    ) -> JobManifest:
        if slot is None:
            update_state("running")
            return function()
        update_state("queued")
        with slot:
            update_state("running")
            return function()

    def _run_pipeline_step(
        self,
        pipeline: StepwiseVideoTranslationPipeline,
        manifest: JobManifest,
        step: PipelineStep,
        update_state: Callable[[str], None],
        *,
        force: bool = False,
    ) -> JobManifest:
        return self._run_with_slot(
            self._slot_for_step(step),
            update_state,
            lambda: pipeline.run_step(manifest, step, force=force),
        )

    def _run_all(
        self,
        pipeline: StepwiseVideoTranslationPipeline,
        manifest: JobManifest,
        update_state: Callable[[str], None],
    ) -> JobManifest:
        while (step := pipeline.next_step(manifest)) is not None:
            self._run_pipeline_step(
                pipeline,
                manifest,
                step,
                update_state,
            )
        return manifest

    def _submit_future(
        self,
        job_id: str,
        function: Callable[[Callable[[str], None]], JobManifest],
        *,
        task_type: str,
        payload: dict | None = None,
    ) -> Future[JobManifest]:
        with self._lock:
            active = self._futures.get(job_id)
            if active is not None and not active.done():
                raise RuntimeError("任务正在执行，请等待当前步骤完成。")
            task_id = self.queue.enqueue(
                resource_type="job",
                resource_id=job_id,
                task_type=task_type,
                payload=payload,
            )
            self._execution_states[job_id] = "queued"

            def update_state(state: str) -> None:
                if state not in {"queued", "running"}:
                    raise ValueError(f"无效执行状态：{state}")
                self.queue.mark(task_id, state)
                with self._lock:
                    future = self._futures.get(job_id)
                    if future is not None and not future.done():
                        self._execution_states[job_id] = state

            def run() -> JobManifest:
                try:
                    result = function(update_state)
                except PipelineCanceled as exc:
                    self.queue.mark(task_id, "canceled", str(exc))
                    raise
                except Exception as exc:
                    self.queue.mark(task_id, "failed", str(exc))
                    raise
                self.queue.mark(task_id, "completed")
                return result

            future = self.executor.submit(run)
            self._futures[job_id] = future
        future.add_done_callback(
            lambda done: self._forget(job_id, done, task_id)
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
            lambda update_state: self._run_pipeline_step(
                pipeline,
                manifest,
                step,
                update_state,
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
            lambda update_state: self._run_with_slot(
                self._translation_slot
                if mode in {"translate", "both"}
                else None,
                update_state,
                lambda: pipeline.rerun_segments(
                    manifest,
                    segment_ids=segment_ids,
                    mode=mode,
                ),
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

    def execution_state(self, job_id: str) -> str | None:
        with self._lock:
            future = self._futures.get(job_id)
            if future is None or future.done():
                return None
            return self._execution_states.get(
                job_id,
                "running" if future.running() else "queued",
            )

    def is_active(self, job_id: str) -> bool:
        with self._lock:
            future = self._futures.get(job_id)
            return future is not None and not future.done()

    def is_running(self, job_id: str) -> bool:
        return self.execution_state(job_id) == "running"

    def is_queued(self, job_id: str) -> bool:
        return self.execution_state(job_id) == "queued"

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
        task_id: str,
    ) -> None:
        if future.cancelled():
            self.queue.mark(task_id, "canceled")
        with self._lock:
            if self._futures.get(job_id) is future:
                self._futures.pop(job_id, None)
                self._execution_states.pop(job_id, None)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
