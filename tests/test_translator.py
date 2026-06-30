import logging
import subprocess
from pathlib import Path

import pytest

from video_translator.errors import PipelineError
from video_translator.models import Segment, UNCLEAR_TRANSCRIPT_TEXT
from video_translator.pipeline import translator as translator_module
from video_translator.pipeline.translator import (
    SegmentTranslator,
    apply_translation_response,
)
from video_translator.settings import Settings


def test_apply_translation_response() -> None:
    segments = [
        Segment(index=0, start=0, end=1, source_text="hello"),
        Segment(index=1, start=1, end=2, source_text="world"),
    ]
    apply_translation_response(
        segments,
        """```json
        {"segments":[{"id":0,"text":"你好"},{"id":1,"text":"世界"}]}
        ```""",
    )
    assert [item.translated_text for item in segments] == ["你好", "世界"]


def test_translation_response_requires_all_ids() -> None:
    segments = [
        Segment(index=0, start=0, end=1, source_text="hello"),
        Segment(index=1, start=1, end=2, source_text="world"),
    ]
    with pytest.raises(PipelineError):
        apply_translation_response(
            segments,
            '{"segments":[{"id":0,"text":"你好"}]}',
        )


def test_codex_cli_translation_uses_safe_structured_exec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        assert kwargs["cwd"]
        assert kwargs["input"].endswith(
            "直接返回符合指定 JSON Schema 的最终答案。"
        )
        schema_index = command.index("--output-schema") + 1
        assert '"segments"' in Path(command[schema_index]).read_text(
            encoding="utf-8"
        )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"segments":[{"id":0,"text":"你好"}]}',
            stderr="",
        )

    monkeypatch.setattr(
        translator_module.shutil,
        "which",
        lambda _: "/opt/homebrew/bin/codex",
    )
    monkeypatch.setattr(translator_module.subprocess, "run", fake_run)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        translator_provider="codex_cli",
        translator_codex_model="gpt-test",
    )
    segments = [
        Segment(index=0, start=0, end=1, source_text="hello"),
    ]

    SegmentTranslator(settings, logging.getLogger("test")).translate(
        segments,
        target_language="简体中文",
        glossary={},
    )

    command = captured["command"]
    assert command[:2] == ["/opt/homebrew/bin/codex", "exec"]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "gpt-test"
    assert (
        command[command.index("--config") + 1]
        == 'model_reasoning_effort="medium"'
    )
    assert command[-1] == "-"
    assert captured["check"] is False
    assert segments[0].translated_text == "你好"


@pytest.mark.parametrize(
    ("provider", "base_url", "model"),
    [
        ("kimi", "https://api.moonshot.cn/v1", "kimi-k2.6"),
        ("minimax", "https://api.minimaxi.com/v1", "MiniMax-M2.7"),
        ("deepseek", "https://api.deepseek.com", "deepseek-v4-flash"),
    ],
)
def test_cloud_provider_defaults(
    provider: str,
    base_url: str,
    model: str,
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        translator_provider=provider,
    )

    assert settings.translator_base_url == base_url
    assert settings.translator_model == model


def test_unclear_segments_are_not_sent_for_translation(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        translator_provider="passthrough",
    )
    segments = [
        Segment(
            index=0,
            start=0,
            end=1,
            source_text=UNCLEAR_TRANSCRIPT_TEXT,
            raw_source_text="garbled",
            asr_unclear=True,
        )
    ]

    SegmentTranslator(settings, logging.getLogger("test")).translate(
        segments,
        target_language="简体中文",
        glossary={},
    )

    assert segments[0].translated_text == UNCLEAR_TRANSCRIPT_TEXT
