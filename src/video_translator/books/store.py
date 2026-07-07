"""Atomic local store for book jobs and translation memory."""

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
from .models import BookBlock, BookManifest, BookStatus


BOOK_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


class BookStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir / "books"
        self.jobs_dir = self.root / "jobs"
        self.outputs_dir = self.root / "outputs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)

    def job_dir(self, book_id: str) -> Path:
        if not BOOK_ID_PATTERN.fullmatch(book_id):
            raise PipelineError("书籍任务 ID 无效。")
        matches = list(self.jobs_dir.glob(f"*--{book_id}"))
        if len(matches) > 1:
            raise PipelineError("书籍任务目录重复。")
        return matches[0] if matches else self.jobs_dir / book_id

    def create(
        self,
        source: Path,
        *,
        output_mode: str,
        title: str | None = None,
    ) -> BookManifest:
        book_id = uuid.uuid4().hex
        selected_title = title or source.stem
        job_dir = self.jobs_dir / (
            f"{safe_job_title(selected_title)}--{book_id}"
        )
        job_dir.mkdir(parents=True)
        target = job_dir / f"source{source.suffix.lower()}"
        target.write_bytes(source.read_bytes())
        manifest = BookManifest(
            id=book_id,
            title=selected_title,
            format=source.suffix.lower().lstrip("."),
            source_path=str(target),
            output_mode=output_mode,
        )
        return self.save(manifest)

    def manifest_path(self, book_id: str) -> Path:
        return self.job_dir(book_id) / "book-manifest.json"

    def save(self, manifest: BookManifest) -> BookManifest:
        manifest.updated_at = utc_now()
        path = self.manifest_path(manifest.id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
        return manifest

    def get(self, book_id: str) -> BookManifest:
        path = self.manifest_path(book_id)
        if not path.is_file():
            raise FileNotFoundError(book_id)
        return BookManifest.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def list(
        self,
        *,
        tag: str | None = None,
        favorite: bool | None = None,
        reading_status: str | None = None,
        sort_by: str = "updated_at",
        descending: bool = True,
    ) -> list[BookManifest]:
        paths = sorted(
            self.jobs_dir.glob("*/book-manifest.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        manifests = [
            BookManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            for path in paths
        ]
        if tag:
            wanted = tag.strip().lower()
            manifests = [
                manifest
                for manifest in manifests
                if wanted
                in {
                    str(item).strip().lower()
                    for item in manifest.metadata.get("library", {}).get(
                        "tags",
                        [],
                    )
                }
            ]
        if favorite is not None:
            manifests = [
                manifest
                for manifest in manifests
                if bool(
                    manifest.metadata.get("library", {}).get("favorite")
                )
                is favorite
            ]
        if reading_status:
            wanted_status = reading_status.strip().lower()
            manifests = [
                manifest
                for manifest in manifests
                if str(
                    manifest.metadata.get("library", {}).get(
                        "reading_status",
                        "",
                    )
                ).lower()
                == wanted_status
            ]

        def sort_key(manifest: BookManifest):
            library = manifest.metadata.get("library", {})
            if sort_by == "title":
                return manifest.title.lower()
            if sort_by == "created_at":
                return manifest.created_at
            if sort_by == "priority":
                return int(library.get("priority", 0) or 0)
            if sort_by == "quality_score":
                return float(library.get("quality_score", 0) or 0)
            if sort_by == "reading_status":
                return str(library.get("reading_status", "unread"))
            return manifest.updated_at

        return sorted(manifests, key=sort_key, reverse=descending)

    def delete(
        self,
        book_id: str,
        *,
        delete_outputs: bool = True,
    ) -> list[Path]:
        manifest = self.get(book_id)
        removed: list[Path] = []
        if delete_outputs:
            candidates = [manifest.output_path]
            raw_outputs = manifest.metadata.get("rendered_outputs", {})
            if isinstance(raw_outputs, dict):
                candidates.extend(str(value) for value in raw_outputs.values())
            for value in candidates:
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
        job_dir = self.job_dir(book_id)
        if job_dir.exists():
            shutil.rmtree(job_dir)
            removed.append(job_dir)
        return removed

    def write_blocks(
        self,
        manifest: BookManifest,
        blocks: list[BookBlock],
    ) -> Path:
        path = self.job_dir(manifest.id) / "blocks.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                [block.model_dump(mode="json") for block in blocks],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
        manifest.blocks_path = str(path)
        self.save(manifest)
        return path

    def read_blocks(self, manifest: BookManifest) -> list[BookBlock]:
        if not manifest.blocks_path:
            raise PipelineError("书籍尚未抽取文本。")
        path = Path(manifest.blocks_path)
        if not path.is_file():
            raise PipelineError("书籍 blocks.json 不存在。")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [BookBlock.model_validate(item) for item in payload]

    def fail(self, manifest: BookManifest, exc: Exception) -> None:
        manifest.status = BookStatus.failed
        manifest.error = str(exc)
        self.save(manifest)

    def update_library(
        self,
        manifest: BookManifest,
        *,
        tags: list[str] | None = None,
        favorite: bool | None = None,
        summary: str | None = None,
        glossary: dict[str, str] | None = None,
        reading_status: str | None = None,
        priority: int | None = None,
        quality_score: float | None = None,
    ) -> BookManifest:
        library = manifest.metadata.setdefault("library", {})
        if tags is not None:
            library["tags"] = sorted(
                {
                    item.strip()
                    for item in tags
                    if isinstance(item, str) and item.strip()
                }
            )
        if favorite is not None:
            library["favorite"] = bool(favorite)
        if summary is not None:
            library["summary"] = summary.strip()
        if glossary is not None:
            library["glossary"] = {
                str(key): str(value)
                for key, value in glossary.items()
                if str(key).strip()
            }
        if reading_status is not None:
            library["reading_status"] = reading_status
        if priority is not None:
            library["priority"] = int(priority)
        if quality_score is not None:
            library["quality_score"] = round(float(quality_score), 2)
        return self.save(manifest)
