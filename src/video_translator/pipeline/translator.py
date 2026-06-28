"""Context-aware subtitle translation."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

import httpx

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


def apply_translation_response(
    segments: Sequence[Segment],
    content: str,
) -> None:
    value = _extract_json_object(content)
    translated = value.get("segments")
    if not isinstance(translated, list):
        raise PipelineError("翻译结果缺少 segments 数组。")

    by_id: dict[int, str] = {}
    for item in translated:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item["id"])
            text = str(item["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if text:
            by_id[index] = text

    missing = [segment.index for segment in segments if segment.index not in by_id]
    if missing:
        raise PipelineError(f"翻译结果缺少片段 ID：{missing}")
    for segment in segments:
        segment.translated_text = by_id[segment.index]


class SegmentTranslator:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger

    def _translate_batch(
        self,
        segments: Sequence[Segment],
        *,
        target_language: str,
        glossary: dict[str, str],
    ) -> None:
        if self.settings.translator_provider == "passthrough":
            for segment in segments:
                segment.translated_text = segment.source_text
            return

        if not self.settings.translator_base_url:
            raise ConfigurationError("没有配置 VT_TRANSLATOR_BASE_URL。")

        glossary_text = (
            json.dumps(glossary, ensure_ascii=False)
            if glossary
            else "{}"
        )
        payload_segments = [
            {
                "id": segment.index,
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "seconds": round(segment.duration, 2),
                "max_zh_chars": segment.max_zh_chars,
                "text": segment.source_text,
            }
            for segment in segments
        ]
        system_prompt = (
            "你是专业影视字幕译者和中文配音改写编辑。"
            "忠实翻译，不补充原文没有的信息；结合整个批次理解上下文；"
            "人名、术语、数字和单位必须一致；中文要自然、适合朗读。"
            "在不损失关键信息的情况下，尽量不超过 max_zh_chars。"
            "只返回 JSON，格式严格为 "
            '{"segments":[{"id":0,"text":"译文"}]}，不得返回解释。'
        )
        user_prompt = (
            f"目标语言：{target_language}\n"
            f"术语表：{glossary_text}\n"
            "待翻译片段：\n"
            f"{json.dumps(payload_segments, ensure_ascii=False)}"
        )
        url = (
            self.settings.translator_base_url.rstrip("/")
            + "/chat/completions"
        )
        headers = {"Content-Type": "application/json"}
        if self.settings.translator_api_key:
            headers["Authorization"] = (
                f"Bearer {self.settings.translator_api_key}"
            )
        body = {
            "model": self.settings.translator_model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            with httpx.Client(
                timeout=self.settings.translator_timeout_seconds
            ) as client:
                response = client.post(url, headers=headers, json=body)
                response.raise_for_status()
                result = response.json()
            content = result["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise PipelineError(f"调用翻译服务失败：{exc}") from exc
        apply_translation_response(segments, str(content))

    def translate(
        self,
        segments: list[Segment],
        *,
        target_language: str,
        glossary: dict[str, str],
    ) -> list[Segment]:
        batch_size = max(1, self.settings.translation_batch_size)
        for offset in range(0, len(segments), batch_size):
            batch = segments[offset : offset + batch_size]
            self.logger.info(
                "翻译片段 %s–%s / %s",
                offset + 1,
                offset + len(batch),
                len(segments),
            )
            self._translate_batch(
                batch,
                target_language=target_language,
                glossary=glossary,
            )
        return segments

