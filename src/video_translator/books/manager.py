"""Background execution for book translation jobs."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from ..control import clear_cancel_request, mark_canceled, request_cancel
from ..settings import Settings
from ..task_queue import LocalTaskQueue
from .models import BookManifest, BookStep, BookStepRequest
from .pipeline import BookTranslationPipeline
from .professional_pdf import PROFESSIONAL_PDF_OUTPUT_MODES
from .store import BookStore


class BookManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = BookStore(settings)
        self.queue = LocalTaskQueue(settings)
        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="book-translator",
        )
        self._futures: dict[str, Future[BookManifest]] = {}
        self._lock = threading.Lock()

    def is_running(self, book_id: str) -> bool:
        with self._lock:
            future = self._futures.get(book_id)
            return future is not None and not future.done()

    def _pipeline(self, settings: Settings | None) -> BookTranslationPipeline:
        return BookTranslationPipeline(
            settings or self.settings,
            self.store,
        )

    def _submit(
        self,
        manifest: BookManifest,
        function: Callable[[], BookManifest],
        *,
        task_type: str,
        payload: dict | None = None,
    ) -> BookManifest:
        with self._lock:
            active = self._futures.get(manifest.id)
            if active is not None and not active.done():
                raise RuntimeError("书籍任务正在执行。")
            clear_cancel_request(manifest)
            self.store.save(manifest)
            task_id = self.queue.enqueue(
                resource_type="book",
                resource_id=manifest.id,
                task_type=task_type,
                payload=payload,
            )

            def run() -> BookManifest:
                self.queue.mark(task_id, "running")
                try:
                    result = function()
                except Exception as exc:
                    self.queue.mark(task_id, "failed", str(exc))
                    raise
                self.queue.mark(task_id, "completed")
                return result

            future = self.executor.submit(run)
            self._futures[manifest.id] = future
        future.add_done_callback(
            lambda done: self._forget(manifest.id, done)
        )
        return manifest

    def submit_step(
        self,
        book_id: str,
        step: BookStep,
        request: BookStepRequest,
        *,
        settings: Settings | None = None,
    ) -> BookManifest:
        manifest = self.store.get(book_id)
        pipeline = self._pipeline(settings)
        professional = request.output_mode in PROFESSIONAL_PDF_OUTPUT_MODES
        if step == BookStep.translate and "extract" not in manifest.completed_steps:
            raise RuntimeError("翻译前必须先完成书籍文本抽取。")
        if (
            step == BookStep.render
            and "translate" not in manifest.completed_steps
            and not professional
        ):
            raise RuntimeError("排版前必须先完成书籍翻译。")
        if step == BookStep.extract:
            function = lambda: pipeline.extract(
                manifest,
                ocr_mode=request.ocr_mode,
                ocr_languages=request.ocr_languages,
                ocr_backend=request.ocr_backend,
            )
        elif step == BookStep.translate:
            function = lambda: pipeline.translate(
                manifest,
                target_language=request.target_language,
                glossary=request.glossary,
            )
        else:
            function = lambda: pipeline.render(
                manifest,
                mode=request.output_mode,
                target_language=request.target_language,
            )
        return self._submit(
            manifest,
            function,
            task_type=f"step:{step.value}",
            payload={"output_mode": request.output_mode},
        )

    def submit_all(
        self,
        book_id: str,
        request: BookStepRequest,
        *,
        settings: Settings | None = None,
    ) -> BookManifest:
        manifest = self.store.get(book_id)
        pipeline = self._pipeline(settings)

        def run() -> BookManifest:
            current = self.store.get(book_id)
            if request.output_mode in PROFESSIONAL_PDF_OUTPUT_MODES:
                return pipeline.render(
                    current,
                    mode=request.output_mode,
                    target_language=request.target_language,
                )
            if "extract" not in current.completed_steps:
                pipeline.extract(
                    current,
                    ocr_mode=request.ocr_mode,
                    ocr_languages=request.ocr_languages,
                    ocr_backend=request.ocr_backend,
                )
            current = self.store.get(book_id)
            # Translation hashes make this a no-op when content settings did
            # not change, while still invalidating only affected blocks when
            # target language, model, strategy, or glossary changed.
            pipeline.translate(
                current,
                target_language=request.target_language,
                glossary=request.glossary,
            )
            current = self.store.get(book_id)
            return pipeline.render(
                current,
                mode=request.output_mode,
                target_language=request.target_language,
            )

        return self._submit(
            manifest,
            run,
            task_type="run_all",
            payload={"output_mode": request.output_mode},
        )

    def cancel(self, book_id: str) -> BookManifest:
        manifest = self.store.get(book_id)
        with self._lock:
            future = self._futures.get(book_id)
            if future is None or future.done():
                return manifest
            queued = future.cancel()
        if queued:
            return mark_canceled(manifest, self.store)
        return request_cancel(manifest, self.store)

    def _forget(
        self,
        book_id: str,
        future: Future[BookManifest],
    ) -> None:
        with self._lock:
            if self._futures.get(book_id) is future:
                self._futures.pop(book_id, None)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
