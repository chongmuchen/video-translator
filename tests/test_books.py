import base64
import zipfile
from pathlib import Path

import pymupdf

from video_translator.books.epub import extract_epub, render_epub
from video_translator.books.models import BookStatus
from video_translator.books.pdf import extract_pdf, render_pdf
from video_translator.books.pipeline import BookTranslationPipeline
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
