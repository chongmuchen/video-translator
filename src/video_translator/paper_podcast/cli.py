"""Command-line interface for paper explainer podcasts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..errors import VideoTranslatorError
from ..settings import get_settings
from .models import PodcastStyle
from .pipeline import PaperPodcastPipeline
from .store import PaperPodcastStore


def _glossary(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("术语表必须是 JSON 对象。")
    return {str(key): str(item) for key, item in value.items()}


def _runtime(args) -> tuple[PaperPodcastPipeline, PaperPodcastStore]:
    settings = get_settings()
    updates = {}
    if getattr(args, "provider", None):
        updates["translator_provider"] = args.provider
    if getattr(args, "codex_strategy", None):
        updates["translator_codex_strategy"] = args.codex_strategy
        if args.provider == "codex_cli":
            updates["translation_batch_size"] = 24
    if getattr(args, "base_url", None):
        updates["translator_base_url"] = args.base_url
    if getattr(args, "model", None):
        if args.provider == "codex_cli":
            updates["translator_codex_model"] = args.model
        else:
            updates["translator_model"] = args.model
    if getattr(args, "tts_provider", None):
        updates["tts_provider"] = args.tts_provider
    if getattr(args, "tts_rate", None):
        updates["tts_rate"] = args.tts_rate
    if getattr(args, "http_tts_url", None):
        updates["tts_http_url"] = args.http_tts_url
    if getattr(args, "cosyvoice_url", None):
        updates["cosyvoice_base_url"] = args.cosyvoice_url
    if updates:
        settings = settings.model_copy(update=updates)
    store = PaperPodcastStore(settings)
    return PaperPodcastPipeline(settings, store), store


def _print(manifest) -> None:
    print(f"论文播客任务 ID：{manifest.id}")
    print(f"状态：{manifest.status.value}")
    print(f"风格：{manifest.style}")
    print(f"中间目录：{Path(manifest.source_path).parent}")
    if manifest.text_path:
        print(f"抽取文本：{manifest.text_path}")
    if manifest.script_markdown_path:
        print(f"讲解脚本：{manifest.script_markdown_path}")
    if manifest.audio_path:
        print(f"音频输出：{manifest.audio_path}")
    if manifest.error:
        print(f"错误：{manifest.error}")


def _add_generation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target-language", default="简体中文")
    parser.add_argument(
        "--style",
        choices=["deep_dive", "narration"],
        default="deep_dive",
    )
    parser.add_argument("--duration-minutes", type=int, default=8)
    parser.add_argument("--glossary")
    parser.add_argument(
        "--provider",
        choices=[
            "codex_cli",
            "ollama",
            "kimi",
            "minimax",
            "deepseek",
            "openai_compatible",
            "passthrough",
        ],
        default="ollama",
    )
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument(
        "--codex-strategy",
        choices=["economy", "balanced", "quality", "account_default"],
        default="balanced",
    )
    parser.add_argument(
        "--tts-provider",
        choices=["edge", "http", "cosyvoice"],
        default="edge",
    )
    parser.add_argument("--voice-a", default="zh-CN-XiaoxiaoNeural")
    parser.add_argument("--voice-b", default="zh-CN-YunxiNeural")
    parser.add_argument("--tts-rate", default="+0%")
    parser.add_argument("--http-tts-url")
    parser.add_argument("--cosyvoice-url", default="http://127.0.0.1:50000")
    parser.add_argument("--silence-ms", type=int, default=220)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从论文 PDF 生成中文讲解播客 MP3",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="导入并执行全部步骤")
    run.add_argument("source")
    _add_generation_options(run)

    imported = commands.add_parser("import", help="只导入论文 PDF")
    imported.add_argument("source")
    imported.add_argument(
        "--style",
        choices=["deep_dive", "narration"],
        default="deep_dive",
    )
    imported.add_argument("--duration-minutes", type=int, default=8)

    extract = commands.add_parser("extract", help="抽取论文文本")
    extract.add_argument("podcast_id")

    script = commands.add_parser("script", help="生成讲解脚本")
    script.add_argument("podcast_id")
    _add_generation_options(script)

    synthesize = commands.add_parser("synthesize", help="合成 MP3")
    synthesize.add_argument("podcast_id")
    _add_generation_options(synthesize)

    status = commands.add_parser("status", help="查看任务")
    status.add_argument("podcast_id", nargs="?")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        pipeline, store = _runtime(args)
        if args.command == "run":
            manifest = pipeline.run(
                Path(args.source),
                target_language=args.target_language,
                style=args.style,
                duration_minutes=args.duration_minutes,
                glossary=_glossary(args.glossary),
                voice_a=args.voice_a,
                voice_b=args.voice_b,
                silence_ms=args.silence_ms,
            )
        elif args.command == "import":
            manifest = pipeline.import_paper(
                Path(args.source),
                style=args.style,
                duration_minutes=args.duration_minutes,
            )
        elif args.command == "extract":
            manifest = pipeline.extract(store.get(args.podcast_id))
        elif args.command == "script":
            manifest = pipeline.script(
                store.get(args.podcast_id),
                target_language=args.target_language,
                style=args.style,
                duration_minutes=args.duration_minutes,
                glossary=_glossary(args.glossary),
            )
        elif args.command == "synthesize":
            manifest = pipeline.synthesize(
                store.get(args.podcast_id),
                voice_a=args.voice_a,
                voice_b=args.voice_b,
                silence_ms=args.silence_ms,
            )
        else:
            if args.podcast_id:
                _print(store.get(args.podcast_id))
            else:
                for item in store.list():
                    _print(item)
                    print()
            return
        _print(manifest)
    except (VideoTranslatorError, OSError, ValueError) as exc:
        raise SystemExit(f"错误：{exc}") from exc


if __name__ == "__main__":
    main()
