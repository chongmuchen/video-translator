"""Serializable book translation models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import RuntimeSettingsUpdate, utc_now


BookOutputMode = Literal[
    "translated_only",
    "bilingual",
    "paper_reference",
    "paper_reflow",
    "paper_translated_reflow",
    "paper_translated_reference",
    "paper_bilingual_stacked",
]


class BookStatus(str, Enum):
    imported = "imported"
    extracted = "extracted"
    translating = "translating"
    translated = "translated"
    rendered = "rendered"
    failed = "failed"


class BookStep(str, Enum):
    extract = "extract"
    translate = "translate"
    render = "render"


class BookBlock(BaseModel):
    id: str
    order: int
    source_text: str
    translated_text: str | None = None
    translation_key: str | None = None
    kind: str = "paragraph"
    file: str | None = None
    page: int | None = None
    tag: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    font_size: float | None = None


class BookManifest(BaseModel):
    id: str
    title: str
    format: Literal["pdf", "epub"]
    source_path: str
    status: BookStatus = BookStatus.imported
    target_language: str = "简体中文"
    output_mode: BookOutputMode = "translated_only"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    blocks_path: str | None = None
    output_path: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    completed_steps: list[str] = Field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data.pop("source_path", None)
        data.pop("blocks_path", None)
        return data


class BookStepRequest(BaseModel):
    target_language: str = "简体中文"
    output_mode: BookOutputMode = "translated_only"
    glossary: dict[str, str] = Field(default_factory=dict)
    settings: RuntimeSettingsUpdate = Field(
        default_factory=RuntimeSettingsUpdate
    )
    translator_api_key_ref: str | None = None
