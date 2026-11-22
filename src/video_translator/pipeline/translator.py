"""Context-aware subtitle translation."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

from pydantic import ValidationError

from ..errors import ConfigurationError, PipelineError
from ..models import Segment
from ..settings import Settings


def _extract_json_object(content: str) -> dict:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise PipelineError("翻译服务没有返回 JSON 对象。")
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise PipelineError(f"翻译服务返回了无效 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError("翻译服务返回值必须是 JSON 对象。")
    return value


def translation_map(
    segments: Sequence[Segment],
    content: str,
    *,
    field: str = "text",
) -> dict[int, str]:
    value = _extract_json_object(content)
    translated = value.get("segments")
    if not isinstance(translated, list):
        raise PipelineError("翻译结果缺少 segments 数组。")

    expected = {segment.index for segment in segments}
    by_id: dict[int, str] = {}
    for item in translated:
        if not isinstance(item, dict) or type(item.get("id")) is not int:
            raise PipelineError("译稿中的每个片段都必须有整数 id。")
        index = item["id"]
        if index not in expected:
            raise PipelineError(f"译稿包含未知片段 ID：{index}")
        if index in by_id:
            raise PipelineError(f"译稿包含重复片段 ID：{index}")
        text = item.get(field)
        if not isinstance(text, str) or not text.strip():
            raise PipelineError(f"译稿片段 {index} 的 {field} 必须是非空文本。")
        by_id[index] = text.strip()

    missing = [segment.index for segment in segments if segment.index not in by_id]
    if missing:
        raise PipelineError(f"翻译结果缺少片段 ID：{missing}")
    return by_id


def apply_translation_response(segments: Sequence[Segment], content: str) -> None:
    by_id = translation_map(segments, content)
    rows = {row["id"]: row for row in _extract_json_object(content)["segments"]}
    updates = []
    for segment in segments:
        row = rows[segment.index]
        try:
            validated = Segment.model_validate({
                **segment.model_dump(),
                "translated_text": row["text"],
                "pronunciations": row.get("pronunciations", []),
            })
        except ValidationError as exc:
            raise PipelineError(f"译稿片段 {segment.index} 的读音标记无效：{exc}") from exc
        leading_spaces = len(row["text"]) - len(row["text"].lstrip())
        annotations = [
            reading.model_copy(update={"start": reading.start - leading_spaces})
            for reading in validated.pronunciations
        ]
        updates.append((segment, by_id[segment.index], annotations))
    for segment, text, annotations in updates:
        segment.translated_text = text
        segment.pronunciations = annotations


def check_translation_provider(settings: Settings) -> None:
    """Prevent the legacy unattended entry point from consuming API credits."""
    if settings.translator_provider != "passthrough":
        raise ConfigurationError(
            "翻译仅使用 Codex Pro 会员额度。请运行 prepare，由已登录 ChatGPT 的 "
            "Codex 写入 translations.json，再运行 render。"
        )


class SegmentTranslator:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger

    def translate(
        self,
        segments: list[Segment],
        *,
        target_language: str,
        glossary: dict[str, str],
    ) -> list[Segment]:
        check_translation_provider(self.settings)
        for segment in segments:
            segment.translated_text = segment.source_text
            segment.pronunciations = []
        return segments
