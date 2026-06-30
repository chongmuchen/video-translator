"""Command-line interface for resumable PDF/EPUB translation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..errors import VideoTranslatorError
from ..settings import get_settings
from .pipeline import BookTranslationPipeline
from .store import BookStore


def _glossary(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("术语表必须是 JSON 对象。")
    return {str(key): str(item) for key, item in value.items()}


def _runtime(args) -> tuple[BookTranslationPipeline, BookStore]:
    settings = get_settings()
    updates = {}
    if getattr(args, "provider", None):
        updates["translator_provider"] = args.provider
    if getattr(args, "codex_strategy", None):
        updates["translator_codex_strategy"] = args.codex_strategy
        if args.provider == "codex_cli":
            updates["translation_batch_size"] = (
                24 if args.codex_strategy == "quality" else 48
            )
    if getattr(args, "model", None):
        if args.provider == "codex_cli":
            updates["translator_codex_model"] = args.model
        else:
            updates["translator_model"] = args.model
    if updates:
        settings = settings.model_copy(update=updates)
    store = BookStore(settings)
    return BookTranslationPipeline(settings, store), store


def _print(manifest) -> None:
    print(f"书籍任务 ID：{manifest.id}")
    print(f"状态：{manifest.status.value}")
    print(f"格式：{manifest.format}")
    print(f"中间目录：{Path(manifest.source_path).parent}")
    if manifest.blocks_path:
        print(f"文本块：{manifest.blocks_path}")
    if manifest.output_path:
        print(f"输出：{manifest.output_path}")
    if manifest.error:
        print(f"错误：{manifest.error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="分步翻译 EPUB/PDF，保留图片和中间译文缓存",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="导入并执行全部步骤")
    run.add_argument("source")
    run.add_argument(
        "--mode",
        choices=["translated_only", "bilingual"],
        default="translated_only",
    )
    run.add_argument("--target-language", default="简体中文")
    run.add_argument("--glossary")

    imported = commands.add_parser("import", help="只导入书籍")
    imported.add_argument("source")
    imported.add_argument(
        "--mode",
        choices=["translated_only", "bilingual"],
        default="translated_only",
    )

    extract = commands.add_parser("extract", help="抽取结构和文本")
    extract.add_argument("book_id")

    translate = commands.add_parser("translate", help="翻译并逐批缓存")
    translate.add_argument("book_id")
    translate.add_argument("--target-language", default="简体中文")
    translate.add_argument("--glossary")

    render = commands.add_parser("render", help="从缓存译文重新排版")
    render.add_argument("book_id")
    render.add_argument(
        "--mode",
        choices=["translated_only", "bilingual"],
        default="translated_only",
    )

    status = commands.add_parser("status", help="查看任务")
    status.add_argument("book_id", nargs="?")

    for command in (run, translate):
        command.add_argument(
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
            default="codex_cli",
        )
        command.add_argument(
            "--codex-strategy",
            choices=[
                "economy",
                "balanced",
                "quality",
                "account_default",
            ],
            default="quality",
        )
        command.add_argument("--model")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        pipeline, store = _runtime(args)
        if args.command == "run":
            manifest = pipeline.run(
                Path(args.source),
                output_mode=args.mode,
                target_language=args.target_language,
                glossary=_glossary(args.glossary),
            )
        elif args.command == "import":
            manifest = pipeline.import_book(
                Path(args.source),
                output_mode=args.mode,
            )
        elif args.command == "extract":
            manifest = pipeline.extract(store.get(args.book_id))
        elif args.command == "translate":
            manifest = pipeline.translate(
                store.get(args.book_id),
                target_language=args.target_language,
                glossary=_glossary(args.glossary),
            )
        elif args.command == "render":
            manifest = pipeline.render(
                store.get(args.book_id),
                mode=args.mode,
            )
        else:
            if args.book_id:
                _print(store.get(args.book_id))
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
