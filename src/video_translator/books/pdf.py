"""Fixed-layout PDF extraction and translated overlay rendering."""

from __future__ import annotations

import re
from pathlib import Path

from ..errors import PipelineError
from .models import BookBlock, BookOutputMode


def _fitz():
    try:
        import pymupdf
    except ImportError as exc:
        raise PipelineError(
            "PDF 翻译需要 PyMuPDF；请重新运行 bootstrap。"
        ) from exc
    return pymupdf


def printing_font() -> Path:
    """Return a serif CJK font suitable for embedded print output."""

    candidates = [
        Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("/Library/Fonts/NotoSerifCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise PipelineError(
        "找不到可嵌入的中文印刷字体；请安装 Noto Serif CJK。"
    )


def extract_pdf(source: Path) -> tuple[list[BookBlock], dict]:
    fitz = _fitz()
    document = fitz.open(source)
    blocks: list[BookBlock] = []
    order = 0
    for page_index, page in enumerate(document):
        page_dict = page.get_text("dict", sort=True)
        for raw in page_dict.get("blocks", []):
            if raw.get("type") != 0:
                continue
            lines = []
            sizes = []
            for line in raw.get("lines", []):
                text = "".join(
                    str(span.get("text", ""))
                    for span in line.get("spans", [])
                ).strip()
                if text:
                    lines.append(text)
                sizes.extend(
                    float(span.get("size", 10))
                    for span in line.get("spans", [])
                    if span.get("text", "").strip()
                )
            text = "\n".join(lines).strip()
            if not text:
                continue
            trailing_numbers = sum(
                bool(re.search(r"\s\d+\s*$", line))
                for line in lines
            )
            blocks.append(
                BookBlock(
                    id=f"pdf-{order:07d}",
                    order=order,
                    source_text=text,
                    kind="toc" if trailing_numbers >= 2 else "paragraph",
                    page=page_index + 1,
                    bbox=tuple(float(value) for value in raw["bbox"]),
                    font_size=(
                        sum(sizes) / len(sizes) if sizes else 10.0
                    ),
                )
            )
            order += 1
    toc = document.get_toc()
    page_count = document.page_count
    document.close()
    if not blocks:
        raise PipelineError(
            "PDF 没有可提取文本；扫描版 PDF 需要后续 OCR 支持。"
        )
    return blocks, {
        "page_count": page_count,
        "toc": toc,
        "pdf_render_strategy": "fixed-layout-overlay",
    }


def _mapped_toc_text(
    text: str,
    blocks: list[BookBlock],
) -> str:
    normalized = " ".join(text.split())
    for block in blocks:
        source = " ".join(block.source_text.split())
        if source == normalized and block.translated_text:
            return block.translated_text.replace("\n", " ")
    return text


def render_pdf(
    source: Path,
    blocks: list[BookBlock],
    output: Path,
    *,
    mode: BookOutputMode,
) -> tuple[Path, list[str]]:
    fitz = _fitz()
    original = fitz.open(source)
    rendered = fitz.open()
    font_file = printing_font()
    by_page: dict[int, list[BookBlock]] = {}
    for block in blocks:
        if block.page:
            by_page.setdefault(block.page, []).append(block)
    warnings: list[str] = []

    for page_index, source_page in enumerate(original):
        page = rendered.new_page(
            width=source_page.rect.width,
            height=source_page.rect.height,
        )
        page.show_pdf_page(page.rect, original, page_index)
        for block in by_page.get(page_index + 1, []):
            if not block.translated_text or not block.bbox:
                continue
            rect = fitz.Rect(block.bbox)
            page.draw_rect(
                rect,
                color=None,
                fill=(1, 1, 1),
                overlay=True,
            )
            text = block.translated_text
            if mode == "bilingual":
                text = f"{block.source_text}\n\n{text}"
            font_size = min(12.0, max(7.0, block.font_size or 10.0))
            result = -1.0
            while font_size >= 5.5 and result < 0:
                result = page.insert_textbox(
                    rect,
                    text,
                    fontname="book-serif",
                    fontfile=str(font_file),
                    fontsize=font_size,
                    lineheight=1.25,
                    color=(0.08, 0.08, 0.08),
                    overlay=True,
                )
                if result < 0:
                    font_size -= 0.5
            if result < 0:
                warnings.append(
                    f"第 {page_index + 1} 页块 {block.id} 文字过长"
                )

    toc = original.get_toc()
    if toc:
        rendered.set_toc(
            [
                [
                    level,
                    _mapped_toc_text(title, blocks),
                    page_number,
                    *entry[3:],
                ]
                for entry in toc
                for level, title, page_number in [entry[:3]]
            ]
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered.save(output, garbage=4, deflate=True)
    rendered.close()
    original.close()
    return output, warnings
