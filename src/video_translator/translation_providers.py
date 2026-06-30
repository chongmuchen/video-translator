"""Translation provider names and editable default presets."""

from __future__ import annotations

from typing import Literal, TypedDict


TranslatorProvider = Literal[
    "openai_compatible",
    "ollama",
    "kimi",
    "minimax",
    "deepseek",
    "codex_cli",
    "passthrough",
]


class TranslationProviderPreset(TypedDict):
    base_url: str
    model: str


DEFAULT_TRANSLATOR_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_TRANSLATOR_MODEL = "qwen3:8b"

# These are conveniences, not protocol restrictions. The web page leaves both
# values editable, and "openai_compatible" can be used for any other service.
TRANSLATION_PROVIDER_PRESETS: dict[
    str,
    TranslationProviderPreset,
] = {
    "ollama": {
        "base_url": DEFAULT_TRANSLATOR_BASE_URL,
        "model": DEFAULT_TRANSLATOR_MODEL,
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2.6",
    },
    "minimax": {
        "base_url": "https://api.minimaxi.com/v1",
        "model": "MiniMax-M2.7",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
    },
}
