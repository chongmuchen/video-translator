"""Small atomic JSON job store suitable for a single-process MVP."""

from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path

from .errors import PipelineError
from .models import JobManifest, JobStatus, PipelineOptions, utc_now
from .settings import Settings


JOB_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


class JobStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        settings.ensure_directories()

    def job_dir(self, job_id: str) -> Path:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise PipelineError("任务 ID 无效。")
        return self.settings.jobs_dir / job_id

    def manifest_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "manifest.json"

    def create(
        self,
        source: str,
        options: PipelineOptions,
    ) -> JobManifest:
        with self._lock:
            job_id = uuid.uuid4().hex
            self.job_dir(job_id).mkdir(parents=True)
            manifest = JobManifest(
                id=job_id,
                source=source,
                options=options,
            )
            self.save(manifest)
            return manifest

    def save(self, manifest: JobManifest) -> JobManifest:
        with self._lock:
            manifest.updated_at = utc_now()
            path = self.manifest_path(manifest.id)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                manifest.model_dump_json(indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
            return manifest

    def get(self, job_id: str) -> JobManifest:
        path = self.manifest_path(job_id)
        if not path.is_file():
            raise FileNotFoundError(job_id)
        with self._lock:
            return JobManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )

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
        self.save(manifest)

    def fail(self, manifest: JobManifest, error: str) -> None:
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
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
        manifest.segments_path = str(path)
        self.save(manifest)
        return path

