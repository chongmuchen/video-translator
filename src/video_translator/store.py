"""Persist local job state and serialize writes to the same job."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import PipelineError
from .models import JobManifest, JobStatus, PipelineOptions, utc_now
from .settings import Settings


JOB_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


class JobStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.ensure_directories()

    def job_dir(self, job_id: str) -> Path:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise PipelineError("任务 ID 无效。")
        return self.settings.jobs_dir / job_id

    def manifest_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "manifest.json"

    @contextmanager
    def lock_job(self, job_id: str) -> Iterator[None]:
        import fcntl

        with (self.job_dir(job_id) / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PipelineError(f"任务 {job_id} 正在另一个进程中运行。") from exc
            yield

    def create(
        self,
        source: str,
        options: PipelineOptions,
    ) -> JobManifest:
        job_id = uuid.uuid4().hex
        self.job_dir(job_id).mkdir(parents=True)
        manifest = JobManifest(id=job_id, source=source, options=options)
        return self.save(manifest)

    def save(self, manifest: JobManifest) -> JobManifest:
        manifest.updated_at = utc_now()
        self._write_text(self.manifest_path(manifest.id), manifest.model_dump_json(indent=2))
        return manifest

    def get(self, job_id: str) -> JobManifest:
        path = self.manifest_path(job_id)
        if not path.is_file():
            raise FileNotFoundError(job_id)
        return JobManifest.model_validate_json(path.read_text(encoding="utf-8"))

    def set_stage(
        self,
        manifest: JobManifest,
        status: JobStatus,
        progress: int,
        message: str,
    ) -> None:
        manifest.status = status
        manifest.progress = progress
        manifest.stage_message = message
        manifest.error = None
        manifest.failed_stage = None
        self.save(manifest)

    def fail(self, manifest: JobManifest, error: str) -> None:
        manifest.failed_stage = manifest.status
        manifest.status = JobStatus.failed
        manifest.stage_message = "处理失败"
        manifest.error = error
        self.save(manifest)

    def write_segments(
        self,
        manifest: JobManifest,
        payload: list[dict],
    ) -> Path:
        path = self.job_dir(manifest.id) / "segments.json"
        self._write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
        manifest.segments_path = str(path)
        self.save(manifest)
        return path

    def write_translations(self, manifest: JobManifest, payload: list[dict]) -> None:
        path = self.job_dir(manifest.id) / "translations.json"
        self._write_text(path, json.dumps({"segments": payload}, ensure_ascii=False, indent=2))
        manifest.translations_path = str(path)
        self.save(manifest)

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

