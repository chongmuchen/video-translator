"""Resumable extraction, translation, and rendering for books."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from ..errors import PipelineError
from ..settings import Settings
from ..pipeline.translator import SegmentTranslator
from .epub import extract_epub, render_epub
from .models import BookBlock, BookManifest, BookOutputMode, BookStatus
from .pdf import PAPER_OUTPUT_MODES, extract_pdf, render_pdf
from .store import BookStore


BOOK_PROMPT_VERSION = "book-v1"


class BookTranslationPipeline:
    def __init__(self, settings: Settings, store: BookStore):
        self.settings = settings
        self.store = store
        self.logger = logging.getLogger("book-translator")

    def import_book(
        self,
        source: Path,
        *,
        output_mode: BookOutputMode,
        title: str | None = None,
    ) -> BookManifest:
        path = source.expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in {".pdf", ".epub"}:
            raise PipelineError("书籍输入必须是存在的 PDF 或 EPUB 文件。")
        return self.store.create(
            path,
            output_mode=output_mode,
            title=title,
        )

    def extract(self, manifest: BookManifest) -> BookManifest:
        try:
            source = Path(manifest.source_path)
            if manifest.format == "pdf":
                blocks, metadata = extract_pdf(source)
            else:
                blocks, metadata = extract_epub(
                    source,
                    self.store.job_dir(manifest.id),
                )
            self.store.write_blocks(manifest, blocks)
            manifest.metadata.update(metadata)
            manifest.status = BookStatus.extracted
            if "extract" not in manifest.completed_steps:
                manifest.completed_steps.append("extract")
            manifest.error = None
            return self.store.save(manifest)
        except Exception as exc:
            self.store.fail(manifest, exc)
            raise

    def _translation_key(
        self,
        block: BookBlock,
        *,
        target_language: str,
        glossary: dict[str, str],
    ) -> str:
        payload = json.dumps(
            {
                "version": BOOK_PROMPT_VERSION,
                "provider": self.settings.translator_provider,
                "base_url": self.settings.translator_base_url,
                "model": self.settings.translator_model,
                "codex_model": self.settings.translator_codex_model,
                "codex_strategy": (
                    self.settings.translator_codex_strategy
                ),
                "target": target_language,
                "glossary": glossary,
                "source": block.source_text,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _batches(
        blocks: list[BookBlock],
        *,
        max_items: int,
        max_chars: int = 12000,
    ) -> list[list[BookBlock]]:
        batches: list[list[BookBlock]] = []
        current: list[BookBlock] = []
        chars = 0
        for block in blocks:
            size = len(block.source_text)
            if current and (
                len(current) >= max_items or chars + size > max_chars
            ):
                batches.append(current)
                current = []
                chars = 0
            current.append(block)
            chars += size
        if current:
            batches.append(current)
        return batches

    def translate(
        self,
        manifest: BookManifest,
        *,
        target_language: str,
        glossary: dict[str, str],
    ) -> BookManifest:
        blocks = self.store.read_blocks(manifest)
        pending = []
        for block in blocks:
            key = self._translation_key(
                block,
                target_language=target_language,
                glossary=glossary,
            )
            if block.translated_text and block.translation_key == key:
                continue
            block.translation_key = key
            block.translated_text = None
            pending.append(block)
        manifest.status = BookStatus.translating
        manifest.target_language = target_language
        self.store.save(manifest)
        self.store.write_blocks(manifest, blocks)

        try:
            if self.settings.translator_provider == "passthrough":
                for block in pending:
                    block.translated_text = block.source_text
                self.store.write_blocks(manifest, blocks)
            else:
                translator = SegmentTranslator(
                    self.settings,
                    self.logger,
                )
                batches = self._batches(
                    pending,
                    max_items=max(
                        1,
                        self.settings.translation_batch_size,
                    ),
                )
                for position, batch in enumerate(batches, start=1):
                    payload = [
                        {
                            "id": index,
                            "kind": block.kind,
                            "text": block.source_text,
                        }
                        for index, block in enumerate(batch)
                    ]
                    system_prompt = (
                        "你是严谨的出版级书籍译者和中文编辑。"
                        "忠实、完整翻译，不概括、不删减、不添加原文没有的内容；"
                        "保持段落功能、数字、引用、专名、术语和语气一致；"
                        "中文使用自然、准确、适合正式出版的书面语。"
                        "遇到原文确实残缺或不可辨认时写【原文不清】而不是猜测。"
                        "只返回 JSON："
                        '{"segments":[{"id":0,"text":"译文"}]}。'
                    )
                    user_prompt = (
                        f"目标语言：{target_language}\n"
                        "术语表："
                        f"{json.dumps(glossary, ensure_ascii=False)}\n"
                        f"批次：{position}/{len(batches)}\n"
                        "请逐项完整翻译：\n"
                        f"{json.dumps(payload, ensure_ascii=False)}"
                    )
                    result = translator.complete_json(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                    )
                    translated = result.get("segments")
                    if not isinstance(translated, list):
                        raise PipelineError("书籍翻译缺少 segments。")
                    values = {
                        int(item["id"]): str(item["text"]).strip()
                        for item in translated
                        if isinstance(item, dict)
                        and "id" in item
                        and str(item.get("text", "")).strip()
                    }
                    missing = [
                        index
                        for index in range(len(batch))
                        if index not in values
                    ]
                    if missing:
                        raise PipelineError(
                            f"书籍翻译批次缺少块：{missing}"
                        )
                    for index, block in enumerate(batch):
                        block.translated_text = values[index]
                    # Checkpoint after every model request.
                    self.store.write_blocks(manifest, blocks)
            manifest.status = BookStatus.translated
            if "translate" not in manifest.completed_steps:
                manifest.completed_steps.append("translate")
            manifest.error = None
            return self.store.save(manifest)
        except Exception as exc:
            self.store.write_blocks(manifest, blocks)
            self.store.fail(manifest, exc)
            raise

    def render(
        self,
        manifest: BookManifest,
        *,
        mode: BookOutputMode,
    ) -> BookManifest:
        blocks = self.store.read_blocks(manifest)
        if any(not block.translated_text for block in blocks):
            raise PipelineError("仍有书籍文本块没有译文。")
        if manifest.format != "pdf" and mode in PAPER_OUTPUT_MODES:
            raise PipelineError("论文排版模式仅支持 PDF。")
        suffix = ".pdf" if manifest.format == "pdf" else ".epub"
        output = self.store.outputs_dir / (
            f"{manifest.title}-zh-{mode}-{manifest.id[:8]}{suffix}"
        )
        try:
            if manifest.format == "pdf":
                _, warnings = render_pdf(
                    Path(manifest.source_path),
                    blocks,
                    output,
                    mode=mode,
                )
                manifest.metadata["layout_warnings"] = warnings
            else:
                extracted = Path(
                    manifest.metadata["extracted_directory"]
                )
                render_epub(
                    extracted,
                    blocks,
                    output,
                    mode=mode,
                )
            manifest.output_mode = mode
            manifest.output_path = str(output)
            rendered_outputs = manifest.metadata.setdefault(
                "rendered_outputs",
                {},
            )
            if isinstance(rendered_outputs, dict):
                rendered_outputs[mode] = str(output)
            manifest.status = BookStatus.rendered
            if "render" not in manifest.completed_steps:
                manifest.completed_steps.append("render")
            manifest.error = None
            return self.store.save(manifest)
        except Exception as exc:
            self.store.fail(manifest, exc)
            raise

    def run(
        self,
        source: Path,
        *,
        output_mode: BookOutputMode,
        target_language: str,
        glossary: dict[str, str],
    ) -> BookManifest:
        manifest = self.import_book(source, output_mode=output_mode)
        self.extract(manifest)
        self.translate(
            manifest,
            target_language=target_language,
            glossary=glossary,
        )
        return self.render(manifest, mode=output_mode)
