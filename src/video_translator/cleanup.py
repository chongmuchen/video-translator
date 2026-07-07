"""Explicit maintenance cleanup for local intermediate files."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .settings import Settings


INTERMEDIATE_NAMES = {
    "aligned",
    "separated",
    "tts",
    "speech-16k.wav",
    "original.wav",
    "duck-control.wav",
}
INTERMEDIATE_SUFFIXES = {".part", ".tmp", ".ytdl"}


@dataclass
class CleanupResult:
    dry_run: bool
    older_than_days: float
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    bytes_reclaimable: int = 0

    def model_dump(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "older_than_days": self.older_than_days,
            "removed": self.removed,
            "skipped": self.skipped,
            "bytes_reclaimable": self.bytes_reclaimable,
        }


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def _candidate(path: Path) -> bool:
    return path.name in INTERMEDIATE_NAMES or path.suffix in INTERMEDIATE_SUFFIXES


def cleanup_intermediates(
    settings: Settings,
    *,
    older_than_days: float = 7,
    dry_run: bool = True,
) -> CleanupResult:
    settings.ensure_directories()
    cutoff = time.time() - older_than_days * 86400
    roots = [
        settings.jobs_dir,
        settings.data_dir / "books" / "jobs",
        settings.data_dir / "paper-podcasts" / "jobs",
        settings.runtime_dir,
    ]
    result = CleanupResult(
        dry_run=dry_run,
        older_than_days=older_than_days,
    )
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if not _candidate(path):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime > cutoff:
                result.skipped.append(str(path))
                continue
            size = _size(path)
            result.bytes_reclaimable += size
            result.removed.append(str(path))
            if dry_run:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
    return result
