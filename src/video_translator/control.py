"""Cooperative cancellation helpers for local background jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import PipelineCanceled


CANCEL_REQUESTED_KEY = "cancel_requested"
CANCEL_MESSAGE_KEY = "cancel_message"
CANCEL_MARKER_NAME = ".cancel-requested"


def _cancel_marker_path(manifest: Any, store: Any | None) -> Path | None:
    if store is None:
        return None
    try:
        return Path(store.job_dir(manifest.id)) / CANCEL_MARKER_NAME
    except Exception:
        return None


def cancellation_requested(
    manifest: Any,
    store: Any | None = None,
) -> bool:
    metadata_requested = bool(
        getattr(manifest, "metadata", {}).get(CANCEL_REQUESTED_KEY)
    )
    marker = _cancel_marker_path(manifest, store)
    return metadata_requested or bool(marker and marker.is_file())


def clear_cancel_request(manifest: Any, store: Any | None = None) -> None:
    metadata = getattr(manifest, "metadata", None)
    if isinstance(metadata, dict):
        metadata.pop(CANCEL_REQUESTED_KEY, None)
        metadata.pop(CANCEL_MESSAGE_KEY, None)
    marker = _cancel_marker_path(manifest, store)
    if marker is not None:
        marker.unlink(missing_ok=True)


def request_cancel(
    manifest: Any,
    store: Any,
    *,
    message: str = "用户请求取消，当前安全点会停止。",
) -> Any:
    marker = _cancel_marker_path(manifest, store)
    if marker is not None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_name(f"{marker.name}.tmp")
        temporary.write_text(message, encoding="utf-8")
        temporary.replace(marker)
    manifest.metadata[CANCEL_REQUESTED_KEY] = True
    manifest.metadata[CANCEL_MESSAGE_KEY] = message
    if hasattr(manifest, "stage_message"):
        manifest.stage_message = "正在取消：等待当前安全点"
    manifest.error = None
    return store.save(manifest)


def mark_canceled(
    manifest: Any,
    store: Any,
    *,
    message: str = "任务已取消。",
) -> Any:
    clear_cancel_request(manifest, store)
    try:
        manifest.status = type(manifest.status)("canceled")
    except ValueError:
        # Defensive fallback for future manifests that have not added a
        # canceled state yet.
        pass
    if hasattr(manifest, "stage_message"):
        manifest.stage_message = message
    manifest.error = None
    return store.save(manifest)


def raise_if_canceled(manifest: Any, store: Any | None = None) -> None:
    """Raise when the latest on-disk manifest has a cancel request.

    Background workers keep an in-memory manifest. Reloading here lets the
    API flip ``metadata.cancel_requested`` while a long-running loop is
    between batches or clips.
    """

    latest = manifest
    if store is not None:
        try:
            latest = store.get(manifest.id)
        except Exception:
            latest = manifest
    marker = _cancel_marker_path(manifest, store)
    marker_message = ""
    if marker is not None and marker.is_file():
        try:
            marker_message = marker.read_text(encoding="utf-8").strip()
        except OSError:
            marker_message = ""
    if cancellation_requested(latest, store):
        if latest is not manifest and hasattr(latest, "metadata"):
            manifest.metadata.update(latest.metadata)
        if marker is not None and marker.is_file():
            manifest.metadata[CANCEL_REQUESTED_KEY] = True
            manifest.metadata[CANCEL_MESSAGE_KEY] = (
                marker_message or "任务已取消。"
            )
        raise PipelineCanceled(
            marker_message
            or latest.metadata.get(CANCEL_MESSAGE_KEY)
            or "任务已取消。"
        )
