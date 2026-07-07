"""Resumable extraction, translation, and rendering for books."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from ..control import mark_canceled, raise_if_canceled
from ..errors import PipelineCanceled, PipelineError
from ..metrics import record_step_metric
from ..settings import Settings
from ..pipeline.translator import SegmentTranslator
from .epub import extract_epub, render_epub
from .models import BookBlock, BookManifest, BookOutputMode, BookStatus
from .ocr import run_ocrmypdf
from .pdf import PAPER_OUTPUT_MODES, extract_pdf, render_pdf
from .professional_pdf import (
    PROFESSIONAL_PDF_OUTPUT_MODES,
    render_professional_pdf,
)
from .structured_ocr import extract_pdf_with_docling
from .store import BookStore


BOOK_PROMPT_VERSION = "book-v1"
EMPTY_PDF_TEXT_MARKER = "PDF 没有可提取文本"


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

    def extract(
        self,
        manifest: BookManifest,
        *,
        ocr_mode: str = "auto",
        ocr_languages: str = "eng",
        ocr_backend: str = "ocrmypdf",
    ) -> BookManifest:
        try:
            with record_step_metric(manifest, self.store, "extract"):
                raise_if_canceled(manifest, self.store)
                source = Path(manifest.source_path)
                if manifest.format == "pdf":
                    if ocr_backend == "docling":
                        blocks, metadata = extract_pdf_with_docling(
                            source,
                            self.store.job_dir(manifest.id),
                        )
                        metadata.update(
                            {
                                "ocr_backend": "docling",
                                "ocr_mode": ocr_mode,
                                "ocr_languages": ocr_languages,
                                "ocr_used": True,
                            }
                        )
                    else:
                        blocks, metadata = self._extract_pdf_with_optional_ocr(
                            manifest,
                            source,
                            ocr_mode=ocr_mode,
                            ocr_languages=ocr_languages,
                        )
                else:
                    blocks, metadata = extract_epub(
                        source,
                        self.store.job_dir(manifest.id),
                    )
                raise_if_canceled(manifest, self.store)
                self.store.write_blocks(manifest, blocks)
                manifest.metadata.update(metadata)
                manifest.status = BookStatus.extracted
                if "extract" not in manifest.completed_steps:
                    manifest.completed_steps.append("extract")
                manifest.error = None
                return self.store.save(manifest)
        except PipelineCanceled:
            mark_canceled(manifest, self.store)
            raise
        except Exception as exc:
            self.store.fail(manifest, exc)
            raise

    def _extract_pdf_with_optional_ocr(
        self,
        manifest: BookManifest,
        source: Path,
        *,
        ocr_mode: str,
        ocr_languages: str,
    ) -> tuple[list[BookBlock], dict]:
        selected_mode = ocr_mode if ocr_mode in {"auto", "always", "never"} else "auto"
        selected_languages = ocr_languages.strip() or "eng"
        metadata: dict = {
            "ocr_mode": selected_mode,
            "ocr_languages": selected_languages,
            "ocr_used": False,
        }
        if selected_mode == "always":
            result = run_ocrmypdf(
                source,
                self.store.job_dir(manifest.id),
                settings=self.settings,
                languages=selected_languages,
                force=True,
            )
            blocks, extracted = extract_pdf(result.pdf_path)
            extracted.update(metadata)
            extracted.update(
                {
                    "ocr_used": True,
                    "ocr_reason": "forced",
                    "ocr_source_path": str(result.pdf_path),
                    "ocr_sidecar_path": str(result.sidecar_path),
                    "ocr_log_path": str(result.log_path),
                }
            )
            return blocks, extracted

        try:
            blocks, extracted = extract_pdf(source)
            extracted.update(metadata)
            return blocks, extracted
        except PipelineError as exc:
            if (
                selected_mode == "never"
                or EMPTY_PDF_TEXT_MARKER not in str(exc)
            ):
                raise
            self.logger.info(
                "PDF 没有可提取文本，自动调用 OCRmyPDF：%s",
                manifest.id,
            )
            result = run_ocrmypdf(
                source,
                self.store.job_dir(manifest.id),
                settings=self.settings,
                languages=selected_languages,
                force=False,
            )
            blocks, extracted = extract_pdf(result.pdf_path)
            extracted.update(metadata)
            extracted.update(
                {
                    "ocr_used": True,
                    "ocr_reason": "empty_text_fallback",
                    "ocr_source_path": str(result.pdf_path),
                    "ocr_sidecar_path": str(result.sidecar_path),
                    "ocr_log_path": str(result.log_path),
                }
            )
            return blocks, extracted

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
            with record_step_metric(manifest, self.store, "translate"):
                if self.settings.translator_provider == "passthrough":
                    for block in pending:
                        raise_if_canceled(manifest, self.store)
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
                        raise_if_canceled(manifest, self.store)
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
                        self.store.write_blocks(manifest, blocks)
                manifest.status = BookStatus.translated
                if "translate" not in manifest.completed_steps:
                    manifest.completed_steps.append("translate")
                manifest.error = None
                return self.store.save(manifest)
        except PipelineCanceled:
            self.store.write_blocks(manifest, blocks)
            mark_canceled(manifest, self.store)
            raise
        except Exception as exc:
            self.store.write_blocks(manifest, blocks)
            self.store.fail(manifest, exc)
            raise

    def render(
        self,
        manifest: BookManifest,
        *,
        mode: BookOutputMode,
        target_language: str | None = None,
    ) -> BookManifest:
        if manifest.format != "pdf" and mode in PROFESSIONAL_PDF_OUTPUT_MODES:
            raise PipelineError("专业 PDF 引擎仅支持 PDF。")
        selected_target_language = target_language or manifest.target_language
        suffix = ".pdf" if manifest.format == "pdf" else ".epub"
        output = self.store.outputs_dir / (
            f"{manifest.title}-zh-{mode}-{manifest.id[:8]}{suffix}"
        )
        if mode in PROFESSIONAL_PDF_OUTPUT_MODES:
            try:
                path, warnings, generated, log_path = render_professional_pdf(
                    Path(manifest.source_path),
                    output,
                    mode=mode,
                    settings=self.settings,
                    job_dir=self.store.job_dir(manifest.id),
                    outputs_dir=self.store.outputs_dir,
                    title=manifest.title,
                    book_id=manifest.id,
                    target_language=selected_target_language,
                )
                manifest.target_language = selected_target_language
                manifest.output_mode = mode
                manifest.output_path = str(path)
                manifest.status = BookStatus.rendered
                manifest.metadata["layout_warnings"] = warnings
                professional_logs = manifest.metadata.setdefault(
                    "professional_pdf_logs",
                    {},
                )
                if isinstance(professional_logs, dict):
                    professional_logs[mode] = log_path
                rendered_outputs = manifest.metadata.setdefault(
                    "rendered_outputs",
                    {},
                )
                if isinstance(rendered_outputs, dict):
                    rendered_outputs.update(generated)
                for step in ("extract", "translate", "render"):
                    if step not in manifest.completed_steps:
                        manifest.completed_steps.append(step)
                manifest.error = None
                return self.store.save(manifest)
            except Exception as exc:
                self.store.fail(manifest, exc)
                raise

        blocks = self.store.read_blocks(manifest)
        if any(not block.translated_text for block in blocks):
            raise PipelineError("仍有书籍文本块没有译文。")
        if manifest.format != "pdf" and mode in PAPER_OUTPUT_MODES:
            raise PipelineError("论文排版模式仅支持 PDF。")
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
            manifest.target_language = selected_target_language
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
        ocr_mode: str = "auto",
        ocr_languages: str = "eng",
        ocr_backend: str = "ocrmypdf",
    ) -> BookManifest:
        manifest = self.import_book(source, output_mode=output_mode)
        if output_mode in PROFESSIONAL_PDF_OUTPUT_MODES:
            return self.render(
                manifest,
                mode=output_mode,
                target_language=target_language,
            )
        self.extract(
            manifest,
            ocr_mode=ocr_mode,
            ocr_languages=ocr_languages,
            ocr_backend=ocr_backend,
        )
        self.translate(
            manifest,
            target_language=target_language,
            glossary=glossary,
        )
        return self.render(
            manifest,
            mode=output_mode,
            target_language=target_language,
        )
