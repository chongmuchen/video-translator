"""SQLite-backed local task queue journal.

This is intentionally small: execution still happens in the local
ThreadPoolExecutor, while task submissions and terminal states are durable.
It gives the single-machine app a persistent queue/audit trail without
requiring Redis/Celery during MVP development.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .settings import Settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalTaskQueue:
    def __init__(self, settings: Settings):
        settings.ensure_directories()
        self.path = settings.data_dir / "task-queue.sqlite3"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS task_queue (
                  id TEXT PRIMARY KEY,
                  resource_type TEXT NOT NULL,
                  resource_id TEXT NOT NULL,
                  task_type TEXT NOT NULL,
                  status TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  error TEXT,
                  created_at TEXT NOT NULL,
                  started_at TEXT,
                  finished_at TEXT,
                  updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_queue_resource
                ON task_queue(resource_type, resource_id, updated_at)
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_queue_status
                ON task_queue(status, updated_at)
                """
            )

    def enqueue(
        self,
        *,
        resource_type: str,
        resource_id: str,
        task_type: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        task_id = uuid.uuid4().hex
        now = _now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO task_queue
                (id, resource_type, resource_id, task_type, status,
                 payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    task_id,
                    resource_type,
                    resource_id,
                    task_type,
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return task_id

    def mark(self, task_id: str, status: str, error: str | None = None) -> None:
        now = _now()
        fields = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, now]
        if status == "running":
            fields.append("started_at = COALESCE(started_at, ?)")
            values.append(now)
        if status in {"completed", "failed", "canceled"}:
            fields.append("finished_at = ?")
            values.append(now)
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        values.append(task_id)
        with self._connect() as db:
            db.execute(
                f"UPDATE task_queue SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    def list(
        self,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM task_queue"
        clauses: list[str] = []
        values: list[Any] = []
        if resource_type:
            clauses.append("resource_type = ?")
            values.append(resource_type)
        if resource_id:
            clauses.append("resource_id = ?")
            values.append(resource_id)
        if status:
            clauses.append("status = ?")
            values.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        values.append(max(1, min(int(limit), 1000)))
        with self._connect() as db:
            rows = db.execute(query, values).fetchall()
        return [
            {
                "id": row["id"],
                "resource_type": row["resource_type"],
                "resource_id": row["resource_id"],
                "task_type": row["task_type"],
                "status": row["status"],
                "payload": json.loads(row["payload_json"] or "{}"),
                "error": row["error"],
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
