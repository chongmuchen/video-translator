"""Small atomic JSON job store suitable for a single-process MVP."""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .errors import PipelineError
from .models import JobManifest, JobStatus, PipelineOptions, utc_now
from .settings import Settings


JOB_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def safe_job_title(title: str) -> str:
    """Return a readable, filesystem-safe title for a job directory."""

    cleaned = re.sub(
        r"[^\w\u4e00-\u9fff.-]+",
        "-",
        title,
        flags=re.UNICODE,
    )
    return cleaned.strip("-._")[:60] or "untitled"


def initial_job_title(source: str) -> str:
    """Return a readable placeholder before the real media title is known."""

    if "://" not in source:
        path = Path(source).expanduser()
        hint = path.stem or path.name or "local-media"
        return safe_job_title(f"待处理-{hint}")

    parsed = urlparse(source.strip())
    host = (parsed.hostname or "remote").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = [host]

    path_parts = [
        unquote(part)
        for part in parsed.path.split("/")
        if part and part not in {"video", "watch"}
    ]
    if path_parts:
        parts.append(path_parts[-1])

    query = parse_qs(parsed.query)
    if query.get("v"):
        parts.append(query["v"][0])
    if query.get("p"):
        parts.append(f"p{query['p'][0]}")
    if query.get("i"):
        parts.append(f"i{query['i'][0]}")

    return safe_job_title("待下载-" + "-".join(parts))


class JobStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        settings.ensure_directories()

    def job_dir(self, job_id: str) -> Path:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise PipelineError("任务 ID 无效。")
        legacy = self.settings.jobs_dir / job_id
        if legacy.exists():
            return legacy
        matches = list(self.settings.jobs_dir.glob(f"*--{job_id}"))
        if len(matches) > 1:
            raise PipelineError(f"任务 ID 对应多个目录：{job_id}")
        return matches[0] if matches else legacy

    def manifest_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "manifest.json"

    def create(
        self,
        source: str,
        options: PipelineOptions,
    ) -> JobManifest:
        with self._lock:
            job_id = uuid.uuid4().hex
            job_dir = self.settings.jobs_dir / (
                f"{initial_job_title(source)}--{job_id}"
            )
            job_dir.mkdir(parents=True)
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

    def add_title_to_job_dir(
        self,
        manifest: JobManifest,
        title: str,
    ) -> Path:
        """Rename a job directory after its media title becomes known."""

        with self._lock:
            current = self.job_dir(manifest.id)
            target = self.settings.jobs_dir / (
                f"{safe_job_title(title)}--{manifest.id}"
            )
            if current == target:
                return target
            if target.exists():
                raise PipelineError(f"目标任务目录已经存在：{target}")
            current.rename(target)

            for field in (
                "source_path",
                "audio_path",
                "subtitle_path",
                "dub_audio_path",
                "output_path",
                "segments_path",
            ):
                value = getattr(manifest, field)
                if not value:
                    continue
                path = Path(value)
                try:
                    relative = path.relative_to(current)
                except ValueError:
                    continue
                setattr(manifest, field, str(target / relative))

            self.save(manifest)
            return target

    def get(self, job_id: str) -> JobManifest:
        path = self.manifest_path(job_id)
        if not path.is_file():
            raise FileNotFoundError(job_id)
        with self._lock:
            manifest = JobManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if self._repair_artifact_paths(manifest):
                self.save(manifest)
            return manifest

    def _repair_artifact_paths(self, manifest: JobManifest) -> bool:
        """Repair absolute artifact paths after the project directory moves."""

        changed = False
        job_dir = self.job_dir(manifest.id)
        for field in (
            "source_path",
            "audio_path",
            "subtitle_path",
            "dub_audio_path",
            "segments_path",
            "output_path",
        ):
            value = getattr(manifest, field)
            if not value:
                continue
            root = (
                self.settings.outputs_dir
                if field == "output_path"
                else job_dir
            )
            candidate = root / Path(value).name
            if Path(value) != candidate and candidate.is_file():
                setattr(manifest, field, str(candidate))
                changed = True
        return changed

    def list(self, *, limit: int = 100) -> list[JobManifest]:
        paths = sorted(
            self.settings.jobs_dir.glob("*/manifest.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        manifests: list[JobManifest] = []
        for path in paths:
            try:
                manifests.append(
                    JobManifest.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                )
            except (OSError, ValueError):
                continue
            if len(manifests) >= limit:
                break
        return manifests

    def delete(
        self,
        job_id: str,
        *,
        delete_outputs: bool = True,
    ) -> list[Path]:
        manifest = self.get(job_id)
        job_dir = self.job_dir(job_id)
        removed: list[Path] = []
        if delete_outputs:
            for value in (
                manifest.output_path,
                manifest.dub_audio_path,
                manifest.subtitle_path,
            ):
                if not value:
                    continue
                path = Path(value).resolve()
                try:
                    path.relative_to(self.settings.outputs_dir.resolve())
                except ValueError:
                    continue
                if path.exists():
                    path.unlink()
                    removed.append(path)
        if job_dir.exists():
            shutil.rmtree(job_dir)
            removed.append(job_dir)
        return removed

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
