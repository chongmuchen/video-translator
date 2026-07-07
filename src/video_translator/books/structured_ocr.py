"""Optional structured OCR / document conversion backends."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..errors import ConfigurationError, PipelineError
from .models import BookBlock


def docling_install_hint() -> str:
    return (
        "Docling 结构化 OCR 后端未安装。可在兼容 Python 环境执行：\n"
        "  .venv/bin/python -m pip install 'docling>=2,<3'\n"
        "如果当前 Python 版本不兼容，可单独建 Python 3.12 环境后把 "
        "docling 命令放到 PATH。"
    )


def _clean_markdown(markdown: str) -> str:
    text = markdown.replace("\r\n", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _blocks_from_markdown(markdown: str) -> list[BookBlock]:
    blocks: list[BookBlock] = []
    paragraphs = [
        item.strip()
        for item in re.split(r"\n\s*\n", _clean_markdown(markdown))
        if item.strip()
    ]
    for index, text in enumerate(paragraphs):
        kind = "heading" if text.startswith("#") else "paragraph"
        cleaned = re.sub(r"^#{1,6}\s*", "", text).strip()
        if not cleaned:
            continue
        blocks.append(
            BookBlock(
                id=f"docling-{index:06d}",
                order=len(blocks),
                source_text=cleaned,
                kind=kind,
            )
        )
    if not blocks:
        raise PipelineError("Docling 没有抽取到可翻译文本。")
    return blocks


def extract_pdf_with_docling(
    source: Path,
    job_dir: Path,
) -> tuple[list[BookBlock], dict]:
    """Extract reading-order Markdown via Docling's Python API."""

    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        raise ConfigurationError(docling_install_hint()) from exc

    structured_dir = job_dir / "structured-ocr" / "docling"
    structured_dir.mkdir(parents=True, exist_ok=True)
    try:
        converter = DocumentConverter()
        result = converter.convert(str(source))
        markdown = result.document.export_to_markdown()
        json_path = structured_dir / "docling-document.json"
        try:
            payload = result.document.export_to_dict()
        except Exception:
            payload = {"warning": "Docling document JSON export failed."}
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        raise PipelineError(f"Docling 结构化抽取失败：{exc}") from exc

    markdown = _clean_markdown(markdown)
    markdown_path = structured_dir / "docling.md"
    markdown_path.write_text(markdown + "\n", encoding="utf-8")
    blocks = _blocks_from_markdown(markdown)
    return blocks, {
        "structured_ocr_backend": "docling",
        "structured_ocr_markdown_path": str(markdown_path),
        "structured_ocr_json_path": str(json_path),
        "block_count": len(blocks),
    }
