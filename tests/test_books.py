import base64
import subprocess
import zipfile
from pathlib import Path

import pymupdf

from video_translator.errors import PipelineError
from video_translator.books.epub import extract_epub, render_epub
from video_translator.books.models import BookBlock, BookStatus
from video_translator.books.pdf import extract_pdf, render_pdf
from video_translator.books.pipeline import BookTranslationPipeline
from video_translator.books import professional_pdf
from video_translator.books.professional_pdf import render_professional_pdf
from video_translator.books.store import BookStore
from video_translator.settings import Settings


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0"
    "lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def make_epub(path: Path) -> None:
    files = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": b"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
      media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""",
        "OEBPS/content.opf": b"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="chapter" href="chapter.xhtml"
      media-type="application/xhtml+xml"/>
    <item id="nav" href="nav.xhtml"
      media-type="application/xhtml+xml" properties="nav"/>
    <item id="image" href="image.png" media-type="image/png"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>""",
        "OEBPS/chapter.xhtml": b"""<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
  <h1>Chapter One</h1>
  <p>Hello world.</p>
  <img src="image.png" alt="sample"/>
</body></html>""",
        "OEBPS/nav.xhtml": b"""<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
  <nav><ol><li><a href="chapter.xhtml">Chapter One</a></li></ol></nav>
</body></html>""",
        "OEBPS/image.png": ONE_PIXEL_PNG,
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "mimetype",
            files.pop("mimetype"),
            compress_type=zipfile.ZIP_STORED,
        )
        for name, content in files.items():
            archive.writestr(name, content)


def test_epub_preserves_images_links_and_interleaves_translation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.epub"
    make_epub(source)
    blocks, metadata = extract_epub(source, tmp_path / "workspace")
    for block in blocks:
        block.translated_text = {
            "Chapter One": "第一章",
            "Hello world.": "你好，世界。",
        }[block.source_text]
    output = tmp_path / "translated.epub"
    render_epub(
        Path(metadata["extracted_directory"]),
        blocks,
        output,
        mode="bilingual",
    )

    with zipfile.ZipFile(output) as archive:
        chapter = archive.read("OEBPS/chapter.xhtml").decode()
        nav = archive.read("OEBPS/nav.xhtml").decode()
        image = archive.read("OEBPS/image.png")
        assert archive.infolist()[0].filename == "mimetype"
        assert archive.infolist()[0].compress_type == zipfile.ZIP_STORED
    assert "Hello world." in chapter
    assert "你好，世界。" in chapter
    assert 'href="chapter.xhtml"' in nav
    assert "第一章" in nav
    assert image == ONE_PIXEL_PNG


def test_pdf_preserves_page_count_and_updates_outline(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.pdf"
    document = pymupdf.open()
    page = document.new_page(width=400, height=500)
    page.insert_text((50, 80), "Chapter One", fontsize=18)
    page.insert_text((50, 130), "Hello world.", fontsize=11)
    page.insert_image(
        pymupdf.Rect(50, 170, 100, 220),
        stream=ONE_PIXEL_PNG,
    )
    document.set_toc([[1, "Chapter One", 1]])
    document.save(source)
    document.close()

    blocks, metadata = extract_pdf(source)
    for block in blocks:
        block.translated_text = (
            "第一章"
            if "Chapter One" in block.source_text
            else "你好，世界。"
        )
    output = tmp_path / "translated.pdf"
    _, warnings = render_pdf(
        source,
        blocks,
        output,
        mode="translated_only",
    )
    translated = pymupdf.open(output)

    assert metadata["page_count"] == 1
    assert translated.page_count == 1
    assert translated.get_toc()[0][1] == "第一章"
    assert not warnings
    translated.close()


def test_pdf_paper_layout_modes_generate_bilingual_reading_pdf(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    document = pymupdf.open()
    page = document.new_page(width=420, height=560)
    page.insert_text((50, 80), "A Practical Paper Title", fontsize=19)
    page.insert_text((50, 130), "Abstract", fontsize=14)
    page.insert_text(
        (50, 165),
        "This paper studies a small but useful translation workflow.",
        fontsize=10,
    )
    page.insert_text((50, 220), "1 Introduction", fontsize=13)
    page.insert_text(
        (50, 255),
        "The system keeps intermediate blocks so layout can be retried.",
        fontsize=10,
    )
    document.save(source)
    document.close()

    blocks, _ = extract_pdf(source)
    for block in blocks:
        block.translated_text = {
            "A Practical Paper Title": "一篇实用论文标题",
            "Abstract": "摘要",
            "This paper studies a small but useful translation workflow.": (
                "本文研究一个小而实用的翻译工作流。"
            ),
            "1 Introduction": "1 引言",
            "The system keeps intermediate blocks so layout can be retried.": (
                "系统保留中间文本块，因此可以重复尝试排版。"
            ),
        }[block.source_text]

    for mode in (
        "paper_reference",
        "paper_reflow",
        "paper_translated_reflow",
        "paper_translated_reference",
        "paper_bilingual_stacked",
    ):
        output = tmp_path / f"{mode}.pdf"
        _, warnings = render_pdf(source, blocks, output, mode=mode)
        rendered = pymupdf.open(output)
        text = "\n".join(page.get_text() for page in rendered).replace(
            "\xa0",
            " ",
        )

        assert rendered.page_count >= 1
        assert "A Practical Paper Title" in text
        if mode in {"paper_reference", "paper_reflow"}:
            assert "Original" in text
        else:
            assert "一篇实用论文标题" in text
        assert warnings
        if mode in {
            "paper_reflow",
            "paper_translated_reflow",
            "paper_bilingual_stacked",
        }:
            assert any("改变原 PDF 页码" in warning for warning in warnings)
        rendered.close()


def test_book_pipeline_keeps_blocks_for_rerender(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.epub"
    make_epub(source)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        translator_provider="passthrough",
    )
    store = BookStore(settings)
    pipeline = BookTranslationPipeline(settings, store)

    manifest = pipeline.run(
        source,
        output_mode="translated_only",
        target_language="简体中文",
        glossary={},
    )

    assert manifest.status == BookStatus.rendered
    assert Path(manifest.blocks_path).is_file()
    assert Path(manifest.output_path).is_file()
    assert all(
        block.translated_text == block.source_text
        for block in store.read_blocks(manifest)
    )


def test_book_pipeline_professional_pdf_skips_internal_translation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "paper.pdf"
    document = pymupdf.open()
    page = document.new_page(width=300, height=300)
    page.insert_text((50, 80), "Formula y = x + 1", fontsize=12)
    document.save(source)
    document.close()

    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        translator_provider="passthrough",
    )
    store = BookStore(settings)
    pipeline = BookTranslationPipeline(settings, store)

    def fake_render(
        source,
        selected_output,
        *,
        mode,
        settings,
        job_dir,
        outputs_dir,
        title,
        book_id,
        target_language,
    ):
        selected_output.write_bytes(b"%PDF-1.4 professional mono")
        sibling = outputs_dir / f"{title}-zh-pdf2zh_bing_dual-{book_id[:8]}.pdf"
        sibling.write_bytes(b"%PDF-1.4 professional dual")
        return (
            selected_output,
            ["professional"],
            {
                "pdf2zh_bing_mono": str(selected_output),
                "pdf2zh_bing_dual": str(sibling),
            },
            str(job_dir / "professional.log"),
        )

    monkeypatch.setattr(
        "video_translator.books.pipeline.render_professional_pdf",
        fake_render,
    )

    manifest = pipeline.run(
        source,
        output_mode="pdf2zh_bing_mono",
        target_language="简体中文",
        glossary={},
    )

    assert manifest.status == BookStatus.rendered
    assert Path(manifest.output_path).is_file()
    assert "extract" in manifest.completed_steps
    assert "translate" in manifest.completed_steps
    assert "render" in manifest.completed_steps
    assert manifest.blocks_path is None
    assert "pdf2zh_bing_dual" in manifest.metadata["rendered_outputs"]


def test_professional_pdf_timeout_decodes_bytes_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4")
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        translator_provider="passthrough",
    )
    job_dir = tmp_path / "job"
    outputs_dir = tmp_path / "outputs"
    job_dir.mkdir()
    outputs_dir.mkdir()
    monkeypatch.setattr(
        professional_pdf,
        "_uv_binary",
        lambda settings: "/usr/bin/uv",
    )

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=1,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    monkeypatch.setattr(professional_pdf.subprocess, "run", fake_run)

    try:
        render_professional_pdf(
            source,
            outputs_dir / "translated.pdf",
            mode="pdf2zh_bing_dual",
            settings=settings,
            job_dir=job_dir,
            outputs_dir=outputs_dir,
            title="Paper",
            book_id="1234567890abcdef",
            target_language="简体中文",
        )
    except PipelineError as exc:
        assert "专业 PDF 引擎超时" in str(exc)
    else:
        raise AssertionError("expected PipelineError")

    log_text = (job_dir / "professional-pdf-pdf2zh_bing_dual.log").read_text(
        encoding="utf-8"
    )
    assert "partial stdout" in log_text
    assert "partial stderr" in log_text


def test_professional_pdf_pins_compatible_tencent_tmt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4")
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        translator_provider="passthrough",
    )
    job_dir = tmp_path / "job"
    outputs_dir = tmp_path / "outputs"
    job_dir.mkdir()
    outputs_dir.mkdir()
    monkeypatch.setattr(
        professional_pdf,
        "_uv_binary",
        lambda settings: "/usr/bin/uv",
    )
    captured: list[str] = []

    def fake_run(command, **kwargs):
        captured.extend(command)
        run_dir = Path(command[command.index("-o") + 1])
        (run_dir / "paper-dual.pdf").write_bytes(b"%PDF-1.4 dual")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(professional_pdf.subprocess, "run", fake_run)

    render_professional_pdf(
        source,
        outputs_dir / "translated.pdf",
        mode="pdf2zh_bing_dual",
        settings=settings,
        job_dir=job_dir,
        outputs_dir=outputs_dir,
        title="Paper",
        book_id="1234567890abcdef",
        target_language="简体中文",
    )

    with_index = captured.index("--with")
    assert captured[with_index + 1] == (
        "tencentcloud-sdk-python-tmt==3.1.121"
    )
    assert captured[with_index + 2] == "pdf2zh"
    thread_index = captured.index("-t")
    assert captured[thread_index + 1] == "4"


def test_book_extract_auto_ocr_when_pdf_has_no_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4 scanned")
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        translator_provider="passthrough",
    )
    store = BookStore(settings)
    pipeline = BookTranslationPipeline(settings, store)
    manifest = pipeline.import_book(source, output_mode="translated_only")
    calls = {"extract": 0}

    def fake_extract_pdf(path: Path):
        calls["extract"] += 1
        if calls["extract"] == 1:
            raise PipelineError("PDF 没有可提取文本；扫描版 PDF 需要 OCR")
        return (
            [
                BookBlock(
                    id="ocr-1",
                    order=0,
                    source_text="Scanned text",
                    page=1,
                    bbox=(10, 10, 100, 40),
                )
            ],
            {"page_count": 1},
        )

    def fake_ocr(source, job_dir, *, settings, languages, force):
        ocr_pdf = job_dir / "ocr" / "source-ocr.pdf"
        sidecar = job_dir / "ocr" / "source-ocr.txt"
        log = job_dir / "ocr" / "ocrmypdf.log"
        ocr_pdf.parent.mkdir(parents=True, exist_ok=True)
        ocr_pdf.write_bytes(b"%PDF-1.4 ocr")
        sidecar.write_text("Scanned text", encoding="utf-8")
        log.write_text("ok", encoding="utf-8")
        return type(
            "OcrResult",
            (),
            {
                "pdf_path": ocr_pdf,
                "sidecar_path": sidecar,
                "log_path": log,
            },
        )()

    monkeypatch.setattr(
        "video_translator.books.pipeline.extract_pdf",
        fake_extract_pdf,
    )
    monkeypatch.setattr(
        "video_translator.books.pipeline.run_ocrmypdf",
        fake_ocr,
    )

    manifest = pipeline.extract(
        manifest,
        ocr_mode="auto",
        ocr_languages="eng",
    )

    assert calls["extract"] == 2
    assert manifest.status == BookStatus.extracted
    assert manifest.metadata["ocr_used"] is True
    assert manifest.metadata["ocr_reason"] == "empty_text_fallback"
    assert Path(manifest.metadata["ocr_source_path"]).is_file()
    assert store.read_blocks(manifest)[0].source_text == "Scanned text"
