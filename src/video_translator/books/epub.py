"""EPUB extraction and rendering while preserving package assets."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from urllib.parse import unquote

from ..errors import PipelineError
from .models import BookBlock, BookOutputMode


BLOCK_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "blockquote",
    "figcaption",
    "td",
    "th",
}


def _lxml():
    try:
        from lxml import etree
    except ImportError as exc:
        raise PipelineError(
            "EPUB 翻译需要 lxml；请重新运行 bootstrap。"
        ) from exc
    return etree


def _safe_extract(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise PipelineError("EPUB 包含不安全的文件路径。")
            archive.extract(member, destination)


def _local_name(element) -> str:
    return str(element.tag).split("}")[-1].lower()


def _text(element) -> str:
    return " ".join("".join(element.itertext()).split())


def _content_documents(root: Path) -> list[Path]:
    etree = _lxml()
    container = root / "META-INF" / "container.xml"
    if not container.is_file():
        raise PipelineError("EPUB 缺少 META-INF/container.xml。")
    tree = etree.parse(str(container))
    rootfiles = tree.xpath(
        "//*[local-name()='rootfile']/@full-path"
    )
    if not rootfiles:
        raise PipelineError("EPUB 找不到 OPF 根文件。")
    opf_path = root / unquote(rootfiles[0])
    opf = etree.parse(str(opf_path))
    manifest_items = {
        item.get("id"): (
            item.get("href"),
            item.get("media-type", ""),
            item.get("properties", ""),
        )
        for item in opf.xpath("//*[local-name()='manifest']/*")
    }
    ordered_ids = [
        item.get("idref")
        for item in opf.xpath("//*[local-name()='spine']/*")
    ]
    for item_id, (_, _, properties) in manifest_items.items():
        if "nav" in properties.split() and item_id not in ordered_ids:
            ordered_ids.append(item_id)
    documents = []
    for item_id in ordered_ids:
        href, media_type, _ = manifest_items.get(
            item_id,
            (None, "", ""),
        )
        if not href or media_type not in {
            "application/xhtml+xml",
            "text/html",
        }:
            continue
        path = (opf_path.parent / unquote(href.split("#")[0])).resolve()
        if root.resolve() not in path.parents or not path.is_file():
            continue
        if path not in documents:
            documents.append(path)
    return documents


def extract_epub(
    source: Path,
    workspace: Path,
) -> tuple[list[BookBlock], dict]:
    extracted = workspace / "epub-extracted"
    if extracted.exists():
        shutil.rmtree(extracted)
    _safe_extract(source, extracted)
    etree = _lxml()
    parser = etree.XMLParser(recover=True, remove_blank_text=False)
    blocks: list[BookBlock] = []
    order = 0
    for document in _content_documents(extracted):
        tree = etree.parse(str(document), parser)
        candidates = tree.xpath(
            "//*[local-name()='nav']//*[local-name()='a']"
            " | //*[local-name()='p' or local-name()='h1'"
            " or local-name()='h2' or local-name()='h3'"
            " or local-name()='h4' or local-name()='h5'"
            " or local-name()='h6' or local-name()='li'"
            " or local-name()='blockquote'"
            " or local-name()='figcaption'"
            " or local-name()='td' or local-name()='th']"
        )
        seen: set[int] = set()
        for element in candidates:
            if id(element) in seen:
                continue
            seen.add(id(element))
            tag = _local_name(element)
            in_navigation = bool(
                element.xpath("ancestor::*[local-name()='nav']")
            )
            if tag != "a" and in_navigation:
                continue
            if tag != "a" and any(
                _local_name(descendant) in BLOCK_TAGS
                for descendant in element.iterdescendants()
            ):
                continue
            text = _text(element)
            if not text:
                continue
            block_id = f"epub-{order:07d}"
            element.set("data-vt-id", block_id)
            blocks.append(
                BookBlock(
                    id=block_id,
                    order=order,
                    source_text=text,
                    kind="toc" if in_navigation else "paragraph",
                    file=str(document.relative_to(extracted)),
                    tag=tag,
                )
            )
            order += 1
        tree.write(
            str(document),
            encoding="utf-8",
            xml_declaration=True,
        )
    if not blocks:
        raise PipelineError("EPUB 没有抽取到可翻译文本。")
    return blocks, {
        "extracted_directory": str(extracted),
        "document_count": len(_content_documents(extracted)),
    }


def _replace_text(element, text: str) -> None:
    element.text = None
    for child in list(element):
        if _local_name(child) == "img":
            child.tail = None
            continue
        element.remove(child)
    element.text = text


def render_epub(
    source_workspace: Path,
    blocks: list[BookBlock],
    output: Path,
    *,
    mode: BookOutputMode,
) -> Path:
    etree = _lxml()
    rendered = source_workspace.parent / "epub-rendered"
    if rendered.exists():
        shutil.rmtree(rendered)
    shutil.copytree(source_workspace, rendered)
    by_id = {block.id: block for block in blocks}
    parser = etree.XMLParser(recover=True, remove_blank_text=False)
    for document in _content_documents(rendered):
        tree = etree.parse(str(document), parser)
        for element in tree.xpath("//*[@data-vt-id]"):
            block_id = element.get("data-vt-id")
            block = by_id.get(block_id)
            element.attrib.pop("data-vt-id", None)
            if not block or not block.translated_text:
                continue
            if block.kind == "toc" or mode == "translated_only":
                _replace_text(element, block.translated_text)
                element.set(
                    "style",
                    (
                        element.get("style", "")
                        + ";font-family:'Songti SC','STSong',serif;"
                    ),
                )
                continue
            namespace = (
                element.tag.split("}")[0] + "}"
                if "}" in str(element.tag)
                else ""
            )
            translation = etree.Element(f"{namespace}p")
            translation.set("class", "vt-translation")
            translation.set(
                "style",
                "font-family:'Songti SC','STSong',serif;"
                "line-height:1.75;margin-top:.35em;margin-bottom:1em;",
            )
            translation.text = block.translated_text
            element.addnext(translation)
        tree.write(
            str(document),
            encoding="utf-8",
            xml_declaration=True,
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w") as archive:
        mimetype = rendered / "mimetype"
        if mimetype.is_file():
            archive.write(
                mimetype,
                "mimetype",
                compress_type=zipfile.ZIP_STORED,
            )
        for path in sorted(rendered.rglob("*")):
            if not path.is_file() or path == mimetype:
                continue
            archive.write(
                path,
                str(path.relative_to(rendered)),
                compress_type=zipfile.ZIP_DEFLATED,
            )
    return output

