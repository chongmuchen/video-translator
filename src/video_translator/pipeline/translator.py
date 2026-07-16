"""Context-aware subtitle translation."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx

from ..errors import ConfigurationError, PipelineError
from ..models import Segment, UNCLEAR_TRANSCRIPT_TEXT
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

        self.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_validator=lambda value: apply_translation_response(
                segments,
                json.dumps(value, ensure_ascii=False),
            ),
        )

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_validator: Callable[[dict], None] | None = None,
    ) -> dict:
        """Run the provider and return a JSON object that passes validation."""

        attempts = max(1, self.settings.translator_retries + 1)
        delay = max(0.0, self.settings.translator_retry_backoff_seconds)
        for attempt in range(1, attempts + 1):
            try:
                value = self._complete_json_once(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )
                if response_validator:
                    response_validator(value)
                return value
            except ConfigurationError:
                raise
            except PipelineError as exc:
                if attempt >= attempts:
                    raise
                self.logger.warning(
                    "翻译批次失败，将在 %.1fs 后重试（%s/%s）：%s",
                    delay,
                    attempt,
                    attempts - 1,
                    exc,
                )
                if delay:
                    time.sleep(delay)
                    delay *= 2
        raise PipelineError("翻译重试失败。")

    def _complete_json_once(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> dict:
        """Run the configured provider once and return one JSON object."""

        if self.settings.translator_provider == "passthrough":
            raise ConfigurationError(
                "passthrough 不支持通用结构化翻译。"
            )
        if self.settings.translator_provider == "codex_cli":
            content = self._translate_with_codex(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            return _extract_json_object(content)
        if not self.settings.translator_base_url:
            raise ConfigurationError("没有配置 VT_TRANSLATOR_BASE_URL。")

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
        return _extract_json_object(str(content))

    def _translate_with_codex(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        executable = shutil.which(self.settings.translator_codex_bin)
        if not executable:
            raise ConfigurationError(
                "找不到 Codex CLI。请先确认 `codex --version` 可执行，"
                "或配置 VT_TRANSLATOR_CODEX_BIN。"
            )

        schema = {
            "type": "object",
            "properties": {
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "text": {"type": "string"},
                        },
                        "required": ["id", "text"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["segments"],
            "additionalProperties": False,
        }
        prompt = (
            f"{system_prompt}\n\n{user_prompt}\n\n"
            "这是纯文本翻译任务。不要读取文件，不要执行命令或调用工具；"
            "直接返回符合指定 JSON Schema 的最终答案。"
        )

        try:
            with tempfile.TemporaryDirectory(
                prefix="video-translator-codex-"
            ) as temporary:
                schema_path = Path(temporary) / "translation-schema.json"
                schema_path.write_text(
                    json.dumps(schema, ensure_ascii=False),
                    encoding="utf-8",
                )
                command = [
                    executable,
                    "exec",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--sandbox",
                    "read-only",
                    "--skip-git-repo-check",
                    "--output-schema",
                    str(schema_path),
                    "--color",
                    "never",
                ]
                strategy = self.settings.translator_codex_strategy
                strategy_defaults = {
                    "economy": ("gpt-5.4-mini", "low"),
                    "balanced": ("gpt-5.4-mini", "medium"),
                    "quality": ("gpt-5.5", "medium"),
                    "account_default": (None, "medium"),
                }
                default_model, reasoning_effort = strategy_defaults[strategy]
                selected_model = (
                    self.settings.translator_codex_model
                    or default_model
                )
                if selected_model:
                    command.extend(
                        [
                            "--model",
                            selected_model,
                        ]
                    )
                command.extend(
                    [
                        "--config",
                        (
                            "model_reasoning_effort="
                            f'"{reasoning_effort}"'
                        ),
                    ]
                )
                command.append("-")
                result = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=self.settings.translator_timeout_seconds,
                    cwd=temporary,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise PipelineError(
                "Codex CLI 翻译超时。请调小每批片段数或提高超时时间。"
            ) from exc
        except OSError as exc:
            raise PipelineError(f"启动 Codex CLI 失败：{exc}") from exc

        if result.returncode != 0:
            detail = result.stderr.strip()[-2000:] or "未知错误"
            raise PipelineError(
                f"Codex CLI 翻译失败（退出码 {result.returncode}）："
                f"{detail}"
            )
        if not result.stdout.strip():
            raise PipelineError("Codex CLI 没有返回翻译结果。")
        return result.stdout

    def translate(
        self,
        segments: list[Segment],
        *,
        target_language: str,
        glossary: dict[str, str],
        on_batch_completed: (
            Callable[[list[Segment]], None] | None
        ) = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> list[Segment]:
        for segment in segments:
            if segment.asr_unclear:
                segment.translated_text = UNCLEAR_TRANSCRIPT_TEXT
        pending = [
            segment
            for segment in segments
            if not (segment.translated_text or "").strip()
        ]
        batch_size = max(1, self.settings.translation_batch_size)
        for offset in range(0, len(pending), batch_size):
            if cancel_check:
                cancel_check()
            batch = pending[offset : offset + batch_size]
            self.logger.info(
                "翻译片段 %s–%s / %s",
                offset + 1,
                offset + len(batch),
                len(pending),
            )
            self._translate_batch(
                batch,
                target_language=target_language,
                glossary=glossary,
            )
            if on_batch_completed:
                on_batch_completed(segments)
            if cancel_check:
                cancel_check()
        return segments

    def shorten_for_tts(
        self,
        segments: list[Segment],
        *,
        target_language: str,
        glossary: dict[str, str],
        reason: str,
    ) -> list[Segment]:
        """Rewrite translations so they fit their original time slots."""

        if self.settings.translator_provider == "passthrough":
            return segments
        payload = [
            {
                "id": segment.index,
                "seconds": round(segment.duration, 2),
                "max_zh_chars": segment.max_zh_chars,
                "source": segment.source_text,
                "current_translation": segment.translated_text or "",
            }
            for segment in segments
            if not segment.asr_unclear
        ]
        if not payload:
            return segments
        system_prompt = (
            "你是中文配音压缩编辑。保持事实、术语、数字和专名不变，"
            "把已有译文改短到适合 TTS 在时间槽内读完。"
            "不要新增原文没有的信息；如果原文不清，保留【原音不清，未能可靠识别】。"
            "只返回 JSON：{\"segments\":[{\"id\":0,\"text\":\"短译文\"}]}。"
        )
        user_prompt = (
            f"目标语言：{target_language}\n"
            f"术语表：{json.dumps(glossary, ensure_ascii=False)}\n"
            f"缩写原因：{reason}\n"
            "需要缩短的片段：\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        result = self.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        apply_translation_response(
            [segment for segment in segments if not segment.asr_unclear],
            json.dumps(result, ensure_ascii=False),
        )
        return segments
