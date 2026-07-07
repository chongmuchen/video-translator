"""Cooperative cancellation helpers for local background jobs."""

from __future__ import annotations

from typing import Any

from .errors import PipelineCanceled


CANCEL_REQUESTED_KEY = "cancel_requested"
CANCEL_MESSAGE_KEY = "cancel_message"


def cancellation_requested(manifest: Any) -> bool:
    return bool(getattr(manifest, "metadata", {}).get(CANCEL_REQUESTED_KEY))


def clear_cancel_request(manifest: Any) -> None:
    metadata = getattr(manifest, "metadata", None)
    if isinstance(metadata, dict):
        metadata.pop(CANCEL_REQUESTED_KEY, None)
        metadata.pop(CANCEL_MESSAGE_KEY, None)


def request_cancel(
    manifest: Any,
    store: Any,
    *,
    message: str = "用户请求取消，当前安全点会停止。",
) -> Any:
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
    clear_cancel_request(manifest)
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
    if cancellation_requested(latest):
        if latest is not manifest and hasattr(latest, "metadata"):
            manifest.metadata.update(latest.metadata)
        raise PipelineCanceled(
            latest.metadata.get(CANCEL_MESSAGE_KEY) or "任务已取消。"
        )
