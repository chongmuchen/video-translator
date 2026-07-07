"""Step timing and lightweight quality reports."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import Segment


def _metrics(manifest: Any) -> dict:
    value = manifest.metadata.setdefault("metrics", {})
    return value if isinstance(value, dict) else {}


@contextmanager
def record_step_metric(
    manifest: Any,
    store: Any,
    step: str,
) -> Iterator[None]:
    started = time.perf_counter()
    status = "success"
    error = None
    try:
        yield
    except Exception as exc:
        status = "failed"
        error = str(exc)
        raise
    finally:
        elapsed = round(time.perf_counter() - started, 3)
        metrics = _metrics(manifest)
        step_metrics = metrics.setdefault("steps", {})
        previous = step_metrics.get(step, {})
        attempts = int(previous.get("attempts", 0)) + 1
        step_metrics[step] = {
            "attempts": attempts,
            "last_status": status,
            "last_seconds": elapsed,
            "total_seconds": round(
                float(previous.get("total_seconds", 0)) + elapsed,
                3,
            ),
            **({"last_error": error} if error else {}),
        }
        try:
            store.save(manifest)
        except Exception:
            # Metrics must never mask the real pipeline result.
            pass


def summarize_segments(segments: list[Segment]) -> dict[str, Any]:
    total = len(segments)
    translated = sum(
        1 for item in segments if (item.translated_text or "").strip()
    )
    synthesized = sum(1 for item in segments if item.tts_file)
    unclear = sum(1 for item in segments if item.asr_unclear)
    total_duration = sum(item.duration for item in segments)
    zh_chars = sum(len(item.translated_text or "") for item in segments)
    return {
        "segments": total,
        "translated": translated,
        "synthesized": synthesized,
        "asr_unclear": unclear,
        "asr_unclear_ratio": round(unclear / total, 4) if total else 0,
        "source_duration_seconds": round(total_duration, 3),
        "translated_characters": zh_chars,
        "avg_zh_chars_per_second": round(zh_chars / total_duration, 3)
        if total_duration
        else 0,
    }


def write_quality_report(
    manifest: Any,
    *,
    job_dir: Path,
    segments: list[Segment] | None = None,
) -> Path:
    report = {
        "job_id": manifest.id,
        "title": getattr(manifest, "title", None),
        "status": getattr(manifest.status, "value", str(manifest.status)),
        "completed_steps": list(getattr(manifest, "completed_steps", [])),
        "metrics": manifest.metadata.get("metrics", {}),
    }
    if segments is not None:
        report["segments"] = summarize_segments(segments)
    path = job_dir / "quality-report.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    manifest.metadata["quality_report_path"] = str(path)
    return path
