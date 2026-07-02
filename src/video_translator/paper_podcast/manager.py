"""Background execution for paper podcast jobs."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor

from ..settings import Settings
from .models import (
    PaperPodcastManifest,
    PaperPodcastRequest,
    PaperPodcastStep,
)
from .pipeline import PaperPodcastPipeline
from .store import PaperPodcastStore


class PaperPodcastManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = PaperPodcastStore(settings)
        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="paper-podcast",
        )
        self._futures: dict[str, Future[PaperPodcastManifest]] = {}
        self._lock = threading.Lock()

    def is_running(self, podcast_id: str) -> bool:
        with self._lock:
            future = self._futures.get(podcast_id)
            return future is not None and not future.done()

    def _pipeline(self, settings: Settings | None) -> PaperPodcastPipeline:
        return PaperPodcastPipeline(settings or self.settings, self.store)

    def _submit(
        self,
        manifest: PaperPodcastManifest,
        function,
    ) -> PaperPodcastManifest:
        with self._lock:
            active = self._futures.get(manifest.id)
            if active is not None and not active.done():
                raise RuntimeError("论文播客任务正在执行。")
            future = self.executor.submit(function)
            self._futures[manifest.id] = future
        future.add_done_callback(
            lambda done: self._forget(manifest.id, done)
        )
        return manifest

    def submit_step(
        self,
        podcast_id: str,
        step: PaperPodcastStep,
        request: PaperPodcastRequest,
        *,
        settings: Settings | None = None,
    ) -> PaperPodcastManifest:
        manifest = self.store.get(podcast_id)
        pipeline = self._pipeline(settings)
        if step == PaperPodcastStep.script and "extract" not in manifest.completed_steps:
            raise RuntimeError("生成脚本前必须先完成论文文本抽取。")
        if (
            step == PaperPodcastStep.synthesize
            and "script" not in manifest.completed_steps
        ):
            raise RuntimeError("合成音频前必须先生成论文播客脚本。")
        if step == PaperPodcastStep.extract:
            function = lambda: pipeline.extract(manifest)
        elif step == PaperPodcastStep.script:
            function = lambda: pipeline.script(
                manifest,
                target_language=request.target_language,
                style=request.style,
                duration_minutes=request.duration_minutes,
                glossary=request.glossary,
            )
        else:
            function = lambda: pipeline.synthesize(
                manifest,
                voice_a=request.voice_a,
                voice_b=request.voice_b,
                silence_ms=request.silence_ms,
            )
        return self._submit(manifest, function)

    def submit_all(
        self,
        podcast_id: str,
        request: PaperPodcastRequest,
        *,
        settings: Settings | None = None,
    ) -> PaperPodcastManifest:
        manifest = self.store.get(podcast_id)
        pipeline = self._pipeline(settings)

        def run() -> PaperPodcastManifest:
            current = self.store.get(podcast_id)
            if "extract" not in current.completed_steps:
                pipeline.extract(current)
            current = self.store.get(podcast_id)
            pipeline.script(
                current,
                target_language=request.target_language,
                style=request.style,
                duration_minutes=request.duration_minutes,
                glossary=request.glossary,
            )
            current = self.store.get(podcast_id)
            return pipeline.synthesize(
                current,
                voice_a=request.voice_a,
                voice_b=request.voice_b,
                silence_ms=request.silence_ms,
            )

        return self._submit(manifest, run)

    def _forget(
        self,
        podcast_id: str,
        future: Future[PaperPodcastManifest],
    ) -> None:
        with self._lock:
            if self._futures.get(podcast_id) is future:
                self._futures.pop(podcast_id, None)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
