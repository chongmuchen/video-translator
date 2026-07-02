"""Fixed-layout PDF extraction and translated overlay rendering."""

from __future__ import annotations

from collections import Counter, defaultdict
import re
from pathlib import Path

from ..errors import PipelineError
from .models import BookBlock, BookOutputMode


PAPER_LEFT_RIGHT_MODES = {"paper_reference", "paper_reflow"}
PAPER_TRANSLATED_ONLY_MODES = {
    "paper_translated_reflow",
    "paper_translated_reference",
}
PAPER_STACKED_MODES = {"paper_bilingual_stacked"}
PAPER_OUTPUT_MODES = (
    PAPER_LEFT_RIGHT_MODES
    | PAPER_TRANSLATED_ONLY_MODES
    | PAPER_STACKED_MODES
)
PAPER_PAGE_WIDTH = 595.0
PAPER_PAGE_HEIGHT = 842.0
PAPER_MARGIN_X = 44.0
PAPER_MARGIN_TOP = 88.0
PAPER_MARGIN_BOTTOM = 48.0
PAPER_GUTTER = 24.0

_SECTION_RE = re.compile(
    r"^(?:"
    r"abstract|keywords?|introduction|related\s+work|background|"
    r"methods?|methodology|experiments?|evaluation|results?|discussion|"
    r"conclusions?|references?|acknowledg(?:e)?ments?|appendix|"
    r"\d+(?:\.\d+)*\s+[\w(]|[ivxlcdm]+\.\s+[\w(]"
    r")",
    re.IGNORECASE,
)
_REFERENCE_RE = re.compile(r"^(?:references?|bibliography)\b", re.IGNORECASE)


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


def _paper_clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\x00", " ")).strip()


def _paper_repeated_headers(blocks: list[BookBlock]) -> set[str]:
    pages_by_text: defaultdict[str, set[int]] = defaultdict(set)
    for block in blocks:
        text = _paper_clean(block.source_text)
        if 0 < len(text) <= 90 and block.page:
            pages_by_text[text.casefold()].add(block.page)
    return {
        text
        for text, pages in pages_by_text.items()
        if len(pages) >= 3
    }


def _paper_is_noise(
    block: BookBlock,
    *,
    page_heights: dict[int, float],
    repeated_headers: set[str],
) -> bool:
    text = _paper_clean(block.source_text)
    if not text:
        return True
    if text.casefold() in repeated_headers:
        return True
    if block.page and block.page in page_heights:
        if _is_footer_page_number(block, page_heights[block.page]):
            return True
    if re.fullmatch(r"[-–—•·\s]+", text):
        return True
    if len(text) <= 3 and re.fullmatch(r"[ivxlcdmIVXLCDM\d]+", text):
        return True
    return False


def _paper_is_heading(block: BookBlock, *, first_meaningful: bool) -> bool:
    text = _paper_clean(block.source_text)
    if not text:
        return False
    if first_meaningful:
        return True
    if block.font_size and block.font_size >= 13:
        return len(text) <= 220
    if len(text) <= 140 and _SECTION_RE.match(text):
        return True
    return False


def _paper_block_style(
    block: BookBlock,
    *,
    first_meaningful: bool,
) -> tuple[str, float, float]:
    if _paper_is_heading(block, first_meaningful=first_meaningful):
        if first_meaningful:
            return "title", 13.6, 1.35
        return "heading", 10.8, 1.32
    if _REFERENCE_RE.match(_paper_clean(block.source_text)):
        return "heading", 10.8, 1.32
    if block.kind == "toc":
        return "small", 7.8, 1.28
    return "body", 8.8, 1.36


def _paper_title(blocks: list[BookBlock], source: Path) -> str:
    candidates = [
        block
        for block in blocks
        if block.page in {None, 1, 2} and _paper_clean(block.source_text)
    ]
    if candidates:
        by_size = sorted(
            candidates,
            key=lambda block: (
                block.font_size or 0,
                -len(_paper_clean(block.source_text)),
            ),
            reverse=True,
        )
        title = _paper_clean(by_size[0].source_text)
        if 4 <= len(title) <= 180:
            return title
    return source.stem


def _paper_visual_w(text: str, font_size: float) -> float:
    width = 0.0
    for char in text:
        code = ord(char)
        if char.isspace():
            width += font_size * 0.32
        elif (
            0x2E80 <= code <= 0x9FFF
            or 0xAC00 <= code <= 0xD7AF
            or 0x3040 <= code <= 0x30FF
        ):
            width += font_size * 0.96
        elif char in "ilI.,;:'!|[](){}":
            width += font_size * 0.30
        elif char in "MW@#%&":
            width += font_size * 0.82
        else:
            width += font_size * 0.54
    return width


def _paper_tokens(text: str) -> list[str]:
    return re.findall(
        r"\s+|[A-Za-z0-9][A-Za-z0-9_./:%+,\-–—]*|.",
        text,
        flags=re.DOTALL,
    )


def _paper_wrap(text: str, *, width: float, font_size: float) -> list[str]:
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").split("\n"):
        if not paragraph.strip():
            if lines and lines[-1] != "":
                lines.append("")
            continue
        current = ""
        for token in _paper_tokens(paragraph.strip()):
            if token.isspace():
                token = " "
            candidate = token if not current or token == " " else current + token
            if token == " ":
                if current and not current.endswith(" "):
                    current += " "
                continue
            if _paper_visual_w(candidate, font_size) <= width:
                current = candidate
                continue
            if current:
                lines.append(current.rstrip())
                current = ""
            if _paper_visual_w(token, font_size) <= width:
                current = token.lstrip()
                continue
            partial = ""
            for char in token:
                char_candidate = partial + char
                if (
                    partial
                    and _paper_visual_w(char_candidate, font_size) > width
                ):
                    lines.append(partial)
                    partial = char
                else:
                    partial = char_candidate
            current = partial
        if current.strip():
            lines.append(current.rstrip())
    return lines or [""]


def _paper_draw_lines(
    page,
    *,
    x: float,
    y: float,
    lines: list[str],
    font_file: Path,
    font_size: float,
    line_height: float,
    color: tuple[float, float, float],
) -> None:
    for line in lines:
        if line:
            page.insert_text(
                (x, y),
                line,
                fontname="paper-cjk",
                fontfile=str(font_file),
                fontsize=font_size,
                color=color,
                overlay=True,
            )
        y += line_height


def _paper_truncate(text: str, limit: int) -> str:
    cleaned = _paper_clean(text)
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 1]}…"


class _PaperRenderer:
    def __init__(
        self,
        fitz,
        *,
        source: Path,
        title: str,
        mode: str,
        font_file: Path,
    ) -> None:
        self.fitz = fitz
        self.source = source
        self.title = title
        self.mode = mode
        self.font_file = font_file
        self.document = fitz.open()
        self.page = None
        self.y = PAPER_MARGIN_TOP
        self.outline: list[list[int | str]] = []
        self.last_source_page: int | None = None

    @property
    def bottom(self) -> float:
        return PAPER_PAGE_HEIGHT - PAPER_MARGIN_BOTTOM

    @property
    def column_width(self) -> float:
        return (PAPER_PAGE_WIDTH - PAPER_MARGIN_X * 2 - PAPER_GUTTER) / 2

    @property
    def right_x(self) -> float:
        return PAPER_MARGIN_X + self.column_width + PAPER_GUTTER

    def _text(
        self,
        position: tuple[float, float],
        text: str,
        *,
        size: float,
        color: tuple[float, float, float] = (0.13, 0.13, 0.13),
    ) -> None:
        assert self.page is not None
        self.page.insert_text(
            position,
            text,
            fontname="paper-cjk",
            fontfile=str(self.font_file),
            fontsize=size,
            color=color,
            overlay=True,
        )

    def new_page(self) -> None:
        self.page = self.document.new_page(
            width=PAPER_PAGE_WIDTH,
            height=PAPER_PAGE_HEIGHT,
        )
        self.y = PAPER_MARGIN_TOP
        page_no = self.document.page_count
        mode_label = (
            "论文排版 · 参考原文左右对照"
            if self.mode == "paper_reference"
            else "论文排版 · 新文章式重排左右对照"
        )
        self._text(
            (PAPER_MARGIN_X, 28),
            _paper_truncate(self.title, 48),
            size=8.0,
            color=(0.34, 0.34, 0.34),
        )
        self._text(
            (PAPER_MARGIN_X, 44),
            mode_label,
            size=7.3,
            color=(0.50, 0.50, 0.50),
        )
        self._text(
            (PAPER_MARGIN_X, self.bottom + 23),
            f"{self.source.name} · {page_no}",
            size=7.0,
            color=(0.55, 0.55, 0.55),
        )
        assert self.page is not None
        self.page.draw_line(
            (PAPER_MARGIN_X, 50),
            (PAPER_PAGE_WIDTH - PAPER_MARGIN_X, 50),
            color=(0.86, 0.86, 0.86),
            width=0.55,
        )
        self.page.draw_line(
            (
                PAPER_MARGIN_X + self.column_width + PAPER_GUTTER / 2,
                PAPER_MARGIN_TOP - 8,
            ),
            (
                PAPER_MARGIN_X + self.column_width + PAPER_GUTTER / 2,
                self.bottom,
            ),
            color=(0.88, 0.88, 0.88),
            width=0.35,
        )
        self._text(
            (PAPER_MARGIN_X, PAPER_MARGIN_TOP - 16),
            "原文 Original",
            size=7.3,
            color=(0.45, 0.45, 0.45),
        )
        self._text(
            (self.right_x, PAPER_MARGIN_TOP - 16),
            "译文 Chinese",
            size=7.3,
            color=(0.45, 0.45, 0.45),
        )

    def ensure(self, height: float) -> None:
        if self.page is None:
            self.new_page()
        if self.y + height > self.bottom:
            self.new_page()

    def source_page_marker(self, page_number: int) -> None:
        if self.mode != "paper_reference":
            return
        if self.last_source_page == page_number:
            return
        self.last_source_page = page_number
        height = 20.0
        self.ensure(height)
        assert self.page is not None
        self.page.draw_rect(
            self.fitz.Rect(
                PAPER_MARGIN_X,
                self.y - 10,
                PAPER_PAGE_WIDTH - PAPER_MARGIN_X,
                self.y + 7,
            ),
            color=None,
            fill=(0.955, 0.965, 0.985),
            overlay=True,
        )
        self._text(
            (PAPER_MARGIN_X + 6, self.y + 2),
            f"原 PDF 第 {page_number} 页",
            size=7.5,
            color=(0.25, 0.30, 0.45),
        )
        self.y += height

    def add_outline(self, block: BookBlock, *, first_meaningful: bool) -> None:
        if not _paper_is_heading(block, first_meaningful=first_meaningful):
            return
        title = block.translated_text or block.source_text
        level = 1 if first_meaningful else 2
        self.outline.append(
            [level, _paper_truncate(title, 80), self.document.page_count]
        )

    def add_block(
        self,
        block: BookBlock,
        *,
        first_meaningful: bool,
    ) -> None:
        if block.page:
            self.source_page_marker(block.page)
        style, font_size, line_scale = _paper_block_style(
            block,
            first_meaningful=first_meaningful,
        )
        gap_before = {
            "title": 9.0,
            "heading": 13.0,
            "small": 5.0,
            "body": 7.0,
        }[style]
        if self.page is None:
            self.new_page()
        self.y += gap_before

        line_height = font_size * line_scale
        source_lines = _paper_wrap(
            block.source_text,
            width=self.column_width,
            font_size=font_size,
        )
        translated_lines = _paper_wrap(
            block.translated_text or "【原文不清】",
            width=self.column_width,
            font_size=font_size,
        )
        text_color = (
            (0.05, 0.05, 0.05)
            if style in {"title", "heading"}
            else (0.14, 0.14, 0.14)
        )
        source_color = (0.20, 0.20, 0.20)
        continuation = False

        while source_lines or translated_lines:
            if self.page is None:
                self.new_page()
            available_lines = max(
                2,
                int((self.bottom - self.y - 12) / line_height),
            )
            if available_lines <= 2:
                self.new_page()
                available_lines = max(
                    2,
                    int((self.bottom - self.y - 12) / line_height),
                )
            take = min(
                max(len(source_lines), len(translated_lines)),
                available_lines,
            )
            src_chunk = source_lines[:take]
            dst_chunk = translated_lines[:take]
            source_lines = source_lines[take:]
            translated_lines = translated_lines[take:]
            row_lines = max(len(src_chunk), len(dst_chunk), 1)
            row_height = row_lines * line_height + 12
            self.ensure(row_height)
            assert self.page is not None
            row_top = self.y - 5
            if style in {"title", "heading"}:
                self.page.draw_rect(
                    self.fitz.Rect(
                        PAPER_MARGIN_X - 5,
                        row_top,
                        PAPER_PAGE_WIDTH - PAPER_MARGIN_X + 5,
                        self.y + row_height - 4,
                    ),
                    color=None,
                    fill=(0.985, 0.985, 0.975),
                    overlay=True,
                )
            else:
                self.page.draw_line(
                    (PAPER_MARGIN_X, row_top),
                    (PAPER_PAGE_WIDTH - PAPER_MARGIN_X, row_top),
                    color=(0.93, 0.93, 0.93),
                    width=0.25,
                )
            if continuation:
                self._text(
                    (self.right_x, self.y - 2),
                    "（续）",
                    size=6.5,
                    color=(0.55, 0.55, 0.55),
                )
            _paper_draw_lines(
                self.page,
                x=PAPER_MARGIN_X,
                y=self.y + line_height - 2,
                lines=src_chunk,
                font_file=self.font_file,
                font_size=font_size,
                line_height=line_height,
                color=source_color,
            )
            _paper_draw_lines(
                self.page,
                x=self.right_x,
                y=self.y + line_height - 2,
                lines=dst_chunk,
                font_file=self.font_file,
                font_size=font_size,
                line_height=line_height,
                color=text_color,
            )
            self.add_outline(
                block,
                first_meaningful=first_meaningful and not continuation,
            )
            self.y += row_height
            continuation = True

    def save(self, output: Path) -> None:
        if self.outline:
            deduped: list[list[int | str]] = []
            seen = Counter()
            for level, title, page in self.outline:
                key = (title, page)
                seen[key] += 1
                if seen[key] == 1:
                    deduped.append([level, title, page])
            self.document.set_toc(deduped)
        output.parent.mkdir(parents=True, exist_ok=True)
        self.document.save(output, garbage=4, deflate=True)
        self.document.close()


def _paper_mode_label(mode: str) -> str:
    labels = {
        "paper_reference": "论文排版 · 参考原文左右对照",
        "paper_reflow": "论文排版 · 新文章式重排左右对照",
        "paper_translated_reflow": "论文排版 · 纯译文重排（推荐）",
        "paper_translated_reference": "论文排版 · 按原页序纯译文",
        "paper_bilingual_stacked": "论文排版 · 上下对照重排",
    }
    return labels.get(mode, "论文排版")


def _paper_sort_columns(
    blocks: list[BookBlock],
    *,
    page_width: float,
) -> list[BookBlock]:
    if len(blocks) < 4:
        return sorted(
            blocks,
            key=lambda block: (
                block.bbox[1] if block.bbox else 0,
                block.bbox[0] if block.bbox else 0,
                block.order,
            ),
        )
    left = [
        block
        for block in blocks
        if block.bbox and (block.bbox[0] + block.bbox[2]) / 2 <= page_width / 2
    ]
    right = [
        block
        for block in blocks
        if block.bbox and (block.bbox[0] + block.bbox[2]) / 2 > page_width / 2
    ]
    if len(left) >= 2 and len(right) >= 2:
        return sorted(left, key=lambda item: (item.bbox[1], item.bbox[0])) + sorted(
            right,
            key=lambda item: (item.bbox[1], item.bbox[0]),
        )
    return sorted(
        blocks,
        key=lambda block: (
            block.bbox[1] if block.bbox else 0,
            block.bbox[0] if block.bbox else 0,
            block.order,
        ),
    )


def _paper_page_reading_order(
    blocks: list[BookBlock],
    *,
    page_width: float,
) -> list[BookBlock]:
    with_box = [block for block in blocks if block.bbox]
    without_box = [block for block in blocks if not block.bbox]
    if not with_box:
        return sorted(blocks, key=lambda block: block.order)

    wide: list[BookBlock] = []
    narrow: list[BookBlock] = []
    for block in with_box:
        assert block.bbox is not None
        width = block.bbox[2] - block.bbox[0]
        spans_page = block.bbox[0] < page_width * 0.18 and block.bbox[2] > page_width * 0.82
        if width >= page_width * 0.62 or spans_page:
            wide.append(block)
        else:
            narrow.append(block)

    # Academic papers such as "Attention Is All You Need" usually start with
    # full-width title/author/abstract blocks and then switch to two columns.
    # Treat full-width blocks as reading-order anchors; within each region,
    # read the left column top-to-bottom before the right column.
    ordered: list[BookBlock] = []
    remaining = sorted(narrow, key=lambda block: (block.bbox[1], block.bbox[0]))
    for full in sorted(wide, key=lambda block: (block.bbox[1], block.bbox[0])):
        assert full.bbox is not None
        before = [
            block
            for block in remaining
            if block.bbox and block.bbox[1] < full.bbox[1]
        ]
        if before:
            ordered.extend(
                _paper_sort_columns(before, page_width=page_width)
            )
            before_ids = {id(block) for block in before}
            remaining = [
                block for block in remaining if id(block) not in before_ids
            ]
        ordered.append(full)
    if remaining:
        ordered.extend(_paper_sort_columns(remaining, page_width=page_width))
    ordered.extend(sorted(without_box, key=lambda block: block.order))
    return ordered


def _paper_ordered_blocks(
    blocks: list[BookBlock],
    *,
    page_widths: dict[int, float],
) -> list[BookBlock]:
    unpaged = [block for block in blocks if not block.page]
    pages: defaultdict[int, list[BookBlock]] = defaultdict(list)
    for block in blocks:
        if block.page:
            pages[block.page].append(block)
    ordered = sorted(unpaged, key=lambda block: block.order)
    for page_number in sorted(pages):
        ordered.extend(
            _paper_page_reading_order(
                pages[page_number],
                page_width=page_widths.get(page_number, PAPER_PAGE_WIDTH),
            )
        )
    return ordered


class _ReadablePaperRenderer:
    def __init__(
        self,
        fitz,
        *,
        source: Path,
        title: str,
        mode: str,
        font_file: Path,
    ) -> None:
        self.fitz = fitz
        self.source = source
        self.title = title
        self.mode = mode
        self.font_file = font_file
        self.document = fitz.open()
        self.page = None
        self.y = 76.0
        self.outline: list[list[int | str]] = []
        self.last_source_page: int | None = None

    @property
    def margin_x(self) -> float:
        return 58.0

    @property
    def content_width(self) -> float:
        return PAPER_PAGE_WIDTH - self.margin_x * 2

    @property
    def bottom(self) -> float:
        return PAPER_PAGE_HEIGHT - 54.0

    def _text(
        self,
        position: tuple[float, float],
        text: str,
        *,
        size: float,
        color: tuple[float, float, float] = (0.13, 0.13, 0.13),
    ) -> None:
        assert self.page is not None
        self.page.insert_text(
            position,
            text,
            fontname="paper-readable-cjk",
            fontfile=str(self.font_file),
            fontsize=size,
            color=color,
            overlay=True,
        )

    def new_page(self) -> None:
        self.page = self.document.new_page(
            width=PAPER_PAGE_WIDTH,
            height=PAPER_PAGE_HEIGHT,
        )
        self.y = 76.0
        page_no = self.document.page_count
        self._text(
            (self.margin_x, 30),
            _paper_truncate(self.title, 56),
            size=8.0,
            color=(0.34, 0.34, 0.34),
        )
        self._text(
            (self.margin_x, 47),
            _paper_mode_label(self.mode),
            size=7.3,
            color=(0.50, 0.50, 0.50),
        )
        self._text(
            (self.margin_x, self.bottom + 29),
            f"{self.source.name} · {page_no}",
            size=7.0,
            color=(0.55, 0.55, 0.55),
        )
        assert self.page is not None
        self.page.draw_line(
            (self.margin_x, 58),
            (PAPER_PAGE_WIDTH - self.margin_x, 58),
            color=(0.86, 0.86, 0.86),
            width=0.55,
        )

    def ensure(self, height: float) -> None:
        if self.page is None:
            self.new_page()
        if self.y + height > self.bottom:
            self.new_page()

    def source_page_marker(self, page_number: int) -> None:
        if self.mode != "paper_translated_reference":
            return
        if self.last_source_page == page_number:
            return
        self.last_source_page = page_number
        height = 22.0
        self.ensure(height)
        assert self.page is not None
        self.page.draw_rect(
            self.fitz.Rect(
                self.margin_x,
                self.y - 10,
                PAPER_PAGE_WIDTH - self.margin_x,
                self.y + 8,
            ),
            color=None,
            fill=(0.955, 0.965, 0.985),
            overlay=True,
        )
        self._text(
            (self.margin_x + 7, self.y + 3),
            f"原 PDF 第 {page_number} 页",
            size=7.8,
            color=(0.25, 0.30, 0.45),
        )
        self.y += height

    def add_outline(self, block: BookBlock, *, first_meaningful: bool) -> None:
        if not _paper_is_heading(block, first_meaningful=first_meaningful):
            return
        title = block.translated_text or block.source_text
        level = 1 if first_meaningful else 2
        self.outline.append(
            [level, _paper_truncate(title, 80), self.document.page_count]
        )

    def _draw_wrapped(
        self,
        text: str,
        *,
        font_size: float,
        line_scale: float,
        color: tuple[float, float, float],
        background: bool = False,
        indent: float = 0.0,
    ) -> None:
        line_height = font_size * line_scale
        lines = _paper_wrap(
            text,
            width=self.content_width - indent,
            font_size=font_size,
        )
        while lines:
            self.ensure(line_height * 2)
            available = max(
                1,
                int((self.bottom - self.y - 8) / line_height),
            )
            take = min(len(lines), available)
            chunk = lines[:take]
            lines = lines[take:]
            height = len(chunk) * line_height + 8
            self.ensure(height)
            assert self.page is not None
            if background:
                self.page.draw_rect(
                    self.fitz.Rect(
                        self.margin_x - 5,
                        self.y - 7,
                        PAPER_PAGE_WIDTH - self.margin_x + 5,
                        self.y + height - 5,
                    ),
                    color=None,
                    fill=(0.985, 0.985, 0.975),
                    overlay=True,
                )
            _paper_draw_lines(
                self.page,
                x=self.margin_x + indent,
                y=self.y + line_height - 2,
                lines=chunk,
                font_file=self.font_file,
                font_size=font_size,
                line_height=line_height,
                color=color,
            )
            self.y += height
            if lines:
                self.new_page()

    def add_block(
        self,
        block: BookBlock,
        *,
        first_meaningful: bool,
    ) -> None:
        if block.page:
            self.source_page_marker(block.page)
        style, _, _ = _paper_block_style(
            block,
            first_meaningful=first_meaningful,
        )
        translated = block.translated_text or "【原文不清】"
        is_heading = style in {"title", "heading"}
        gap_before = {
            "title": 18.0,
            "heading": 15.0,
            "small": 7.0,
            "body": 10.0,
        }[style]
        if self.page is None:
            self.new_page()
        self.y += gap_before
        self.add_outline(block, first_meaningful=first_meaningful)

        if self.mode == "paper_bilingual_stacked":
            self._draw_wrapped(
                block.source_text,
                font_size=7.7 if not is_heading else 8.8,
                line_scale=1.35,
                color=(0.45, 0.45, 0.45),
                background=is_heading,
            )
            self.y += 2.0

        font_size = {
            "title": 17.0,
            "heading": 13.6,
            "small": 9.0,
            "body": 10.8,
        }[style]
        self._draw_wrapped(
            translated,
            font_size=font_size,
            line_scale=1.48 if style == "body" else 1.36,
            color=(0.08, 0.08, 0.08),
            background=is_heading and self.mode != "paper_bilingual_stacked",
        )

    def save(self, output: Path) -> None:
        if self.outline:
            deduped: list[list[int | str]] = []
            seen = Counter()
            for level, title, page in self.outline:
                key = (title, page)
                seen[key] += 1
                if seen[key] == 1:
                    deduped.append([level, title, page])
            self.document.set_toc(deduped)
        output.parent.mkdir(parents=True, exist_ok=True)
        self.document.save(output, garbage=4, deflate=True)
        self.document.close()


def _paper_blocks_for_render(
    blocks: list[BookBlock],
    *,
    mode: str,
    page_heights: dict[int, float],
    page_widths: dict[int, float],
) -> list[BookBlock]:
    repeated = _paper_repeated_headers(blocks)
    ordered = _paper_ordered_blocks(blocks, page_widths=page_widths)
    selected = [
        block
        for block in ordered
        if not _paper_is_noise(
            block,
            page_heights=page_heights,
            repeated_headers=repeated,
        )
    ]
    if mode not in {
        "paper_reflow",
        "paper_translated_reflow",
        "paper_bilingual_stacked",
    }:
        return selected

    # Reflow-style modes intentionally drop table-of-contents fragments and
    # page furniture, then typeset the remaining paper as a continuous article.
    # They do not ask the model to summarize or invent structure; the original
    # scholarly order is preserved so citations and equations remain traceable.
    return [
        block
        for block in selected
        if block.kind != "toc"
        and not _looks_like_toc_entry(block.source_text)
    ]


def render_paper_pdf(
    source: Path,
    blocks: list[BookBlock],
    output: Path,
    *,
    mode: BookOutputMode,
) -> tuple[Path, list[str]]:
    fitz = _fitz()
    original = fitz.open(source)
    try:
        page_heights = {
            index + 1: float(page.rect.height)
            for index, page in enumerate(original)
        }
        page_widths = {
            index + 1: float(page.rect.width)
            for index, page in enumerate(original)
        }
        image_pages = [
            index + 1
            for index, page in enumerate(original)
            if page.get_images(full=True)
        ]
    finally:
        original.close()

    font_file = printing_font()
    selected = _paper_blocks_for_render(
        blocks,
        mode=str(mode),
        page_heights=page_heights,
        page_widths=page_widths,
    )
    if not selected:
        raise PipelineError("没有可用于论文排版的文本块。")

    title = _paper_title(selected, source)
    if mode in PAPER_LEFT_RIGHT_MODES:
        renderer = _PaperRenderer(
            fitz,
            source=source,
            title=title,
            mode=str(mode),
            font_file=font_file,
        )
    else:
        renderer = _ReadablePaperRenderer(
            fitz,
            source=source,
            title=title,
            mode=str(mode),
            font_file=font_file,
        )
    first = True
    for block in selected:
        renderer.add_block(block, first_meaningful=first)
        first = False
    renderer.save(output)

    warnings: list[str] = []
    if image_pages:
        warnings.append(
            "论文排版阅读版当前主要重排文字；原 PDF 第 "
            + ", ".join(str(page) for page in image_pages[:12])
            + (" 等页面" if len(image_pages) > 12 else " 页面")
            + " 含图片/图表，最终版请对照原 PDF 校样。"
        )
    if mode in {"paper_reflow", "paper_translated_reflow", "paper_bilingual_stacked"}:
        warnings.append(
            "新文章式重排会改变原 PDF 页码和分页；"
            "引用页码请以原 PDF 为准。"
        )
    elif mode == "paper_translated_reference":
        warnings.append(
            "按原页序纯译文排版保留原 PDF 页序标记，"
            "但输出页数不等同于原 PDF 页数。"
        )
    else:
        warnings.append(
            "参考原文排版保留原文页序标记，"
            "但输出页数不等同于原 PDF 页数。"
        )
    return output, warnings


def render_pdf(
    source: Path,
    blocks: list[BookBlock],
    output: Path,
    *,
    mode: BookOutputMode,
) -> tuple[Path, list[str]]:
    if mode in PAPER_OUTPUT_MODES:
        return render_paper_pdf(source, blocks, output, mode=mode)

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
