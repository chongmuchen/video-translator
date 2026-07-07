"""Local governance helpers: audit log, quota, and authorization metadata."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import ContentAuthorization
from .settings import Settings


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AuditEvent:
    id: str
    created_at: str
    actor: str
    action: str
    resource_type: str
    resource_id: str
    details: dict[str, Any]


class AuditLog:
    def __init__(self, settings: Settings):
        settings.ensure_directories()
        self.path = settings.data_dir / "governance.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                  id TEXT PRIMARY KEY,
                  created_at TEXT NOT NULL,
                  actor TEXT NOT NULL,
                  action TEXT NOT NULL,
                  resource_type TEXT NOT NULL,
                  resource_id TEXT NOT NULL,
                  details_json TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_resource
                ON audit_events(resource_type, resource_id, created_at)
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_actor_action
                ON audit_events(actor, action, created_at)
                """
            )

    def record(
        self,
        *,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=uuid.uuid4().hex,
            created_at=now_iso(),
            actor=actor or "local-user",
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
        )
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO audit_events
                (id, created_at, actor, action, resource_type, resource_id, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.created_at,
                    event.actor,
                    event.action,
                    event.resource_type,
                    event.resource_id,
                    json.dumps(event.details, ensure_ascii=False),
                ),
            )
        return event

    def list(
        self,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM audit_events"
        clauses: list[str] = []
        values: list[Any] = []
        if resource_type:
            clauses.append("resource_type = ?")
            values.append(resource_type)
        if resource_id:
            clauses.append("resource_id = ?")
            values.append(resource_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        values.append(max(1, min(int(limit), 1000)))
        with self._connect() as db:
            rows = db.execute(query, values).fetchall()
        return [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "actor": row["actor"],
                "action": row["action"],
                "resource_type": row["resource_type"],
                "resource_id": row["resource_id"],
                "details": json.loads(row["details_json"] or "{}"),
            }
            for row in rows
        ]

    def count_today(self, *, actor: str, action: str) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*) AS count
                FROM audit_events
                WHERE actor = ? AND action = ? AND substr(created_at, 1, 10) = ?
                """,
                (actor or "local-user", action, today),
            ).fetchone()
        return int(row["count"] if row else 0)


def authorization_metadata(
    authorization: ContentAuthorization | None,
) -> dict[str, Any]:
    if authorization is None:
        return {}
    return {
        "content_authorization": authorization.model_dump(mode="json"),
        "content_authorization_recorded_at": now_iso(),
    }


def assert_authorized(
    authorization: ContentAuthorization | None,
    *,
    required: bool,
) -> None:
    if required and (authorization is None or not authorization.authorized):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403,
            detail=(
                "当前配置要求先声明内容授权。请确认你拥有版权、授权、"
                "公共领域或其他合法依据后再创建任务。"
            ),
        )
