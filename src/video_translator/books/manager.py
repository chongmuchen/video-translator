"""Background execution for book translation jobs."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor

from ..settings import Settings
from .models import BookManifest, BookStep, BookStepRequest
from .pipeline import BookTranslationPipeline
from .store import BookStore


class BookManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = BookStore(settings)
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

    def _submit(self, manifest: BookManifest, function) -> BookManifest:
        with self._lock:
            active = self._futures.get(manifest.id)
            if active is not None and not active.done():
                raise RuntimeError("书籍任务正在执行。")
            future = self.executor.submit(function)
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
        if step == BookStep.translate and "extract" not in manifest.completed_steps:
            raise RuntimeError("翻译前必须先完成书籍文本抽取。")
        if step == BookStep.render and "translate" not in manifest.completed_steps:
            raise RuntimeError("排版前必须先完成书籍翻译。")
        if step == BookStep.extract:
            function = lambda: pipeline.extract(manifest)
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
            )
        return self._submit(manifest, function)

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
            if "extract" not in current.completed_steps:
                pipeline.extract(current)
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
            return pipeline.render(current, mode=request.output_mode)

        return self._submit(manifest, run)

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
