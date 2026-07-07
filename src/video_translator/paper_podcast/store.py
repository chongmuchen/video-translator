"""Atomic local store for paper podcast jobs."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path

from ..errors import PipelineError
from ..models import utc_now
from ..settings import Settings
from ..store import safe_job_title
from .models import PaperPodcastManifest, PaperPodcastStatus


PAPER_PODCAST_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


class PaperPodcastStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir / "paper-podcasts"
        self.jobs_dir = self.root / "jobs"
        self.outputs_dir = self.root / "outputs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)

    def job_dir(self, podcast_id: str) -> Path:
        if not PAPER_PODCAST_ID_PATTERN.fullmatch(podcast_id):
            raise PipelineError("论文播客任务 ID 无效。")
        matches = list(self.jobs_dir.glob(f"*--{podcast_id}"))
        if len(matches) > 1:
            raise PipelineError("论文播客任务目录重复。")
        return matches[0] if matches else self.jobs_dir / podcast_id

    def create(
        self,
        source: Path,
        *,
        title: str | None = None,
        style: str = "deep_dive",
        duration_minutes: int = 8,
    ) -> PaperPodcastManifest:
        podcast_id = uuid.uuid4().hex
        selected_title = title or source.stem
        job_dir = self.jobs_dir / (
            f"{safe_job_title(selected_title)}--{podcast_id}"
        )
        job_dir.mkdir(parents=True)
        target = job_dir / f"source{source.suffix.lower()}"
        target.write_bytes(source.read_bytes())
        manifest = PaperPodcastManifest(
            id=podcast_id,
            title=selected_title,
            source_path=str(target),
            style=style,
            duration_minutes=duration_minutes,
        )
        return self.save(manifest)

    def manifest_path(self, podcast_id: str) -> Path:
        return self.job_dir(podcast_id) / "podcast-manifest.json"

    def save(self, manifest: PaperPodcastManifest) -> PaperPodcastManifest:
        manifest.updated_at = utc_now()
        path = self.manifest_path(manifest.id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
        return manifest

    def get(self, podcast_id: str) -> PaperPodcastManifest:
        path = self.manifest_path(podcast_id)
        if not path.is_file():
            raise FileNotFoundError(podcast_id)
        return PaperPodcastManifest.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def list(self) -> list[PaperPodcastManifest]:
        paths = sorted(
            self.jobs_dir.glob("*/podcast-manifest.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        return [
            PaperPodcastManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            for path in paths
        ]

    def delete(
        self,
        podcast_id: str,
        *,
        delete_outputs: bool = True,
    ) -> list[Path]:
        manifest = self.get(podcast_id)
        removed: list[Path] = []
        if delete_outputs:
            for value in (manifest.audio_path, manifest.video_path):
                if not value:
                    continue
                path = Path(value).resolve()
                try:
                    path.relative_to(self.outputs_dir.resolve())
                except ValueError:
                    continue
                if path.exists():
                    path.unlink()
                    removed.append(path)
        job_dir = self.job_dir(podcast_id)
        if job_dir.exists():
            shutil.rmtree(job_dir)
            removed.append(job_dir)
        return removed

    def write_json(
        self,
        manifest: PaperPodcastManifest,
        filename: str,
        value,
    ) -> Path:
        path = self.job_dir(manifest.id) / filename
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def fail(self, manifest: PaperPodcastManifest, exc: Exception) -> None:
        manifest.status = PaperPodcastStatus.failed
        manifest.error = str(exc)
        self.save(manifest)
