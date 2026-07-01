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
    """Return a light, readable CJK font suitable for embedded print output.

    macOS' Songti.ttc looks tempting, but PyMuPDF opens the first face in that
    collection as ``Songti SC Black``. That made translated PDFs look like they
    were typeset entirely in extra-bold poster text, so prefer a lighter
    Mincho/Song-style face when available.
    """

    candidates = [
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/Library/Fonts/NotoSerifCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc"),
        Path("/System/Library/Fonts/ヒラギノ明朝 ProN.ttc"),
        Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise PipelineError(
        "找不到可嵌入的中文印刷字体；请安装 Noto Serif CJK。"
    )


def _is_footer_page_number(block: BookBlock, page_height: float) -> bool:
    if not block.bbox:
        return False
    text = block.source_text.strip()
    if not re.fullmatch(r"[ivxlcdmIVXLCDM\d]+", text):
        return False
    return block.bbox[1] > page_height * 0.88


def _looks_like_toc_entry(text: str) -> bool:
    cleaned = " ".join(text.replace("\x00", " ").split())
    if not cleaned:
        return False
    if "TABLE OF CONTENTS" in cleaned.upper() or cleaned == "目录":
        return True
    return bool(
        re.search(r"(?:^|\s)(?:[ivxlcdmIVXLCDM]{1,8}|\d+|[0-9]+[a-z])\s*$", cleaned)
    )


def _toc_pages(by_page: dict[int, list[BookBlock]]) -> set[int]:
    pages: set[int] = set()
    in_toc = False
    for page_number in sorted(by_page):
        blocks = by_page[page_number]
        has_title = any(
            "TABLE OF CONTENTS" in block.source_text.upper()
            for block in blocks
        )
        toc_like = sum(
            _looks_like_toc_entry(block.source_text)
            for block in blocks
        )
        if has_title:
            in_toc = True
        if in_toc and toc_like >= 3:
            pages.add(page_number)
            continue
        if in_toc:
            break
    return pages


def _clean_toc_text(text: str) -> str:
    lines = [
        line.strip()
        for line in text.replace("\x00", " ").splitlines()
        if line.strip()
    ]
    if not lines:
        return ""
    return "  ".join(lines)


def _expanded_rect(
    fitz,
    block: BookBlock,
    page_rect,
    *,
    stretch_right: bool,
):
    rect = fitz.Rect(block.bbox)
    rect.x0 = max(0, rect.x0 - 3.5)
    rect.y0 = max(0, rect.y0 - 2.0)
    rect.x1 = min(page_rect.width, rect.x1 + 3.5)
    rect.y1 = min(page_rect.height, rect.y1 + 3.0)
    if stretch_right and rect.width > 80:
        rect.x1 = max(rect.x1, page_rect.width - 42)
    return rect


def _font_size_for(block: BookBlock) -> float:
    source_size = block.font_size or 9.0
    if source_size >= 30:
        return 15.0
    if source_size >= 15:
        return min(13.5, source_size * 0.70)
    if source_size >= 13:
        return min(12.6, source_size * 0.90)
    if source_size >= 11:
        return min(10.5, source_size * 0.82)
    if source_size <= 7.5:
        return max(5.8, source_size * 0.82)
    return min(8.2, max(6.4, source_size * 0.88))


def _insert_translated_text(
    page,
    rect,
    text: str,
    *,
    font_file: Path,
    font_size: float,
    lineheight: float,
    min_size: float,
) -> bool:
    while font_size >= min_size:
        result = page.insert_textbox(
            rect,
            text,
            fontname="book-cjk",
            fontfile=str(font_file),
            fontsize=font_size,
            lineheight=lineheight,
            color=(0.08, 0.08, 0.08),
            overlay=True,
        )
        if result >= 0:
            return True
        font_size -= 0.35
    return False


def _render_toc_page(
    fitz,
    page,
    page_blocks: list[BookBlock],
    *,
    font_file: Path,
) -> list[str]:
    warnings: list[str] = []
    content_blocks = [
        block
        for block in page_blocks
        if block.bbox and not _is_footer_page_number(block, page.rect.height)
    ]
    if not content_blocks:
        return warnings

    cover = fitz.Rect(content_blocks[0].bbox)
    for block in content_blocks[1:]:
        cover |= fitz.Rect(block.bbox)
    cover.x0 = max(0, cover.x0 - 8)
    cover.x1 = min(page.rect.width, page.rect.width - 40)
    cover.y0 = max(0, cover.y0 - 4)
    cover.y1 = min(page.rect.height, cover.y1 + 4)
    page.draw_rect(cover, color=None, fill=(1, 1, 1), overlay=True)

    for block in content_blocks:
        if not block.translated_text:
            continue
        is_title = "TABLE OF CONTENTS" in block.source_text.upper()
        if is_title:
            text = "目录"
            rect = fitz.Rect(block.bbox)
            rect.x1 = page.rect.width - 40
            font_size = 10.5
            lineheight = 1.25
        else:
            text = _clean_toc_text(block.translated_text)
            if not text:
                continue
            rect = fitz.Rect(block.bbox)
            rect.x0 = max(42, rect.x0 - 2)
            rect.x1 = page.rect.width - 42
            rect.y0 -= 0.8
            rect.y1 = max(rect.y1 + 5.5, rect.y0 + 11)
            font_size = 6.8
            lineheight = 1.08
        ok = _insert_translated_text(
            page,
            rect,
            text,
            font_file=font_file,
            font_size=font_size,
            lineheight=lineheight,
            min_size=5.0,
        )
        if not ok:
            warnings.append(f"目录页块 {block.id} 文字过长")
    return warnings


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


def _clean_outline_text(text: str) -> str:
    cleaned = " ".join(
        text.replace("\x00", " ")
        .replace("·", " ")
        .replace(".", " ")
        .split()
    )
    return re.sub(
        r"\s+(?:[ivxlcdmIVXLCDM]+|\d+[a-zA-Z]?)\s*$",
        "",
        cleaned,
    ).strip()


def _strip_outline_prefix(text: str) -> str:
    return re.sub(
        r"^[ivxlcdmIVXLCDM\d]{1,8}\s+",
        "",
        text,
    ).strip()


def _outline_translation_maps(
    blocks: list[BookBlock],
) -> tuple[dict[str, str], dict[str, str]]:
    by_source: dict[str, str] = {}
    by_roman: dict[str, str] = {}
    roman_pattern = re.compile(r"^([ivxlcdmIVXLCDM]{1,8})\b")
    for block in blocks:
        if not block.translated_text:
            continue
        source = _clean_outline_text(block.source_text)
        translated = _clean_outline_text(block.translated_text)
        if not source or not translated:
            continue
        by_source[source.casefold()] = translated
        stripped_source = _strip_outline_prefix(source)
        if stripped_source and stripped_source != source:
            by_source[stripped_source.casefold()] = translated
        source_roman = roman_pattern.match(source)
        translated_roman = roman_pattern.match(translated)
        roman = source_roman or translated_roman
        if roman and _strip_outline_prefix(translated):
            by_roman[roman.group(1).upper()] = translated
    return by_source, by_roman


def _with_outline_roman(text: str, roman_match) -> str:
    if not roman_match:
        return text
    roman = roman_match.group(1).upper()
    if re.match(r"^[ivxlcdmIVXLCDM\d]{1,8}\b", text):
        return re.sub(
            r"^[ivxlcdmIVXLCDM\d]{1,8}\b",
            roman,
            text,
            count=1,
        )
    return f"{roman} {text}"


def _mapped_outline_text(
    text: str,
    by_source: dict[str, str],
    by_roman: dict[str, str],
) -> str:
    cleaned = _clean_outline_text(text)
    if not cleaned:
        return text.replace("\x00", " ").strip()
    if cleaned.casefold() == "contents":
        return "目录"
    roman = re.match(r"^([ivxlcdmIVXLCDM]{1,8})\b", cleaned)
    candidates = [
        cleaned,
        _strip_outline_prefix(cleaned),
    ]
    for candidate in candidates:
        folded = candidate.casefold()
        if folded in by_source:
            return _with_outline_roman(by_source[folded], roman)
        for source, translated in by_source.items():
            if folded == source or folded.startswith(source) or source.startswith(folded):
                return _with_outline_roman(translated, roman)
    if roman and roman.group(1).upper() in by_roman:
        return _with_outline_roman(by_roman[roman.group(1).upper()], roman)
    return text.replace("\x00", " ").strip()


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
    toc_pages = _toc_pages(by_page)
    warnings: list[str] = []

    for page_index, source_page in enumerate(original):
        page = rendered.new_page(
            width=source_page.rect.width,
            height=source_page.rect.height,
        )
        page.show_pdf_page(page.rect, original, page_index)
        page_number = page_index + 1
        page_blocks = by_page.get(page_number, [])
        if page_number in toc_pages:
            warnings.extend(
                _render_toc_page(
                    fitz,
                    page,
                    page_blocks,
                    font_file=font_file,
                )
            )
            continue

        has_images = bool(source_page.get_images(full=True))
        for block in by_page.get(page_index + 1, []):
            if not block.translated_text or not block.bbox:
                continue
            if _is_footer_page_number(block, source_page.rect.height):
                continue
            rect = _expanded_rect(
                fitz,
                block,
                source_page.rect,
                stretch_right=not has_images and (block.font_size or 0) < 12,
            )
            page.draw_rect(
                rect,
                color=None,
                fill=(1, 1, 1),
                overlay=True,
            )
            text = block.translated_text
            if mode == "bilingual":
                text = f"{block.source_text}\n\n{text}"
            ok = _insert_translated_text(
                page,
                rect,
                text,
                font_file=font_file,
                font_size=_font_size_for(block),
                lineheight=1.32,
                min_size=4.8,
            )
            if not ok:
                warnings.append(
                    f"第 {page_index + 1} 页块 {block.id} 文字过长"
                )

    toc = original.get_toc()
    if toc:
        outline_blocks = [
            block
            for block in blocks
            if block.page in toc_pages
        ] or blocks
        outline_by_source, outline_by_roman = _outline_translation_maps(
            outline_blocks
        )
        rendered.set_toc(
            [
                [
                    level,
                    _mapped_outline_text(
                        title,
                        outline_by_source,
                        outline_by_roman,
                    ),
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
