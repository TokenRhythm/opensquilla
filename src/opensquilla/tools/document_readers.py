"""Bounded basic document extraction shared by host and sandbox file readers.

Office containers are read directly: basic text access never depends on a skill,
Office installation, or generated code. No macros or external links execute.
"""

from __future__ import annotations

import csv
import email.policy
import io
import json
import posixpath
import zipfile
from collections.abc import Generator
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from opensquilla.tools.types import SafeToolError

DOCUMENT_EXTENSIONS = frozenset(
    {".docx", ".pptx", ".eml", ".mbox", ".msg", ".pdf", ".xlsx", ".csv", ".tsv"}
)
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_CHARS = 40_000
MAX_UNITS = 200
MAX_PDF_PAGES = 10
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _check_input(path: Path) -> None:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise SafeToolError("Document exceeds the 64 MiB basic-reader input limit")


def _package(path: Path) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
        entries = archive.infolist()
        if len(entries) > 10_000 or sum(item.file_size for item in entries) > MAX_PACKAGE_BYTES:
            archive.close()
            raise SafeToolError("Office document exceeds the bounded package parsing limit")
        if any(item.flag_bits & 1 for item in entries):
            archive.close()
            raise SafeToolError(
                "Encrypted Office documents cannot be read; provide an unlocked copy"
            )
        return archive
    except zipfile.BadZipFile as exc:
        with path.open("rb") as stream:
            encrypted = stream.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        detail = "encrypted or legacy Office document" if encrypted else "corrupt Office document"
        raise SafeToolError(f"Cannot read {detail}: {path.name}") from exc


def _member(archive: zipfile.ZipFile, name: str) -> bytes:
    entry = archive.getinfo(name)
    if entry.file_size > MAX_XML_BYTES:
        raise SafeToolError("Office XML member exceeds the 16 MiB parsing limit")
    raw = archive.read(entry)
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise SafeToolError("Office XML entity declarations are not supported")
    return raw


def _docx_units(path: Path) -> Generator[tuple[int, str, dict[str, Any]], None, None]:
    with _package(path) as archive:
        raw = _member(archive, "word/document.xml")
        index = 0
        for _, element in ET.iterparse(io.BytesIO(raw), events=("end",)):
            if element.tag != f"{_W}p":
                continue
            index += 1
            text = "".join(
                node.text or ""
                if node.tag == f"{_W}t"
                else "\t"
                if node.tag == f"{_W}tab"
                else "\n"
                for node in element.iter()
                if node.tag in {f"{_W}t", f"{_W}tab", f"{_W}br"}
            )
            yield index, text, {}
            element.clear()


def _pptx_units(
    path: Path, offset: int, limit: int
) -> Generator[tuple[int, str, dict[str, Any]], None, None]:
    with _package(path) as archive:
        manifest = ET.fromstring(_member(archive, "ppt/presentation.xml"))
        relations = ET.fromstring(_member(archive, "ppt/_rels/presentation.xml.rels"))
        targets = {
            node.get("Id"): node.get("Target", "")
            for node in relations
            if node.get("TargetMode") != "External"
        }
        slides = manifest.findall(f".//{_P}sldId")
        for index, slide in enumerate(slides, 1):
            if index < offset:
                continue
            if index >= offset + limit:
                yield index, "", {"total_units": len(slides)}
                break
            target = targets.get(slide.get(f"{_R}id"))
            if not target:
                raise SafeToolError("Corrupt PPTX: slide relationship is missing")
            member = (
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join("ppt", target))
            )
            if not member.startswith("ppt/slides/"):
                raise SafeToolError("Corrupt PPTX: invalid slide relationship")
            root = ET.fromstring(_member(archive, member))
            paragraphs = [
                "".join(node.text or "" for node in paragraph.iter(f"{_A}t"))
                for paragraph in root.iter(f"{_A}p")
            ]
            yield index, "\n".join(paragraphs), {"total_units": len(slides)}


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "br", "div", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _message_text(raw: bytes) -> str:
    message = BytesParser(policy=email.policy.default).parsebytes(raw)
    if message.get_content_type() in {"application/pkcs7-mime", "multipart/encrypted"}:
        raise SafeToolError("Encrypted email cannot be read; provide decrypted message content")
    headers = "\n".join(
        f"{name}: {message[name]}" for name in ("From", "To", "Date", "Subject") if message[name]
    )
    parts = [
        part
        for part in message.walk()
        if not part.is_multipart() and part.get_content_disposition() != "attachment"
    ]
    body_parts = [part for part in parts if part.get_content_type() == "text/plain"]
    if not body_parts:
        body_parts = [part for part in parts if part.get_content_type() == "text/html"]
    bodies = []
    for part in body_parts:
        text = part.get_content()
        if not isinstance(text, str):
            continue
        if part.get_content_type() == "text/html":
            parser = _HTMLText()
            parser.feed(text)
            text = "".join(parser.parts)
        bodies.append(text)
    return headers + "\n\n" + "\n".join(bodies)


def _email_units(path: Path) -> Generator[tuple[int, str, dict[str, Any]], None, None]:
    if path.suffix.lower() == ".msg":
        if path.stat().st_size > MAX_XML_BYTES:
            raise SafeToolError("Outlook message exceeds the 16 MiB parsing limit")
        try:
            import extract_msg
        except ImportError as exc:
            raise SafeToolError(
                "Outlook MSG reading requires the optional opensquilla[msg] dependency"
            ) from exc
        message = None
        try:
            message = extract_msg.openMsg(str(path))
            if "smime" in str(getattr(message, "classType", "")).lower():
                raise SafeToolError(
                    "Encrypted or signed Outlook message needs a decrypted EML copy"
                )
            headers = "\n".join(
                f"{label}: {getattr(message, attribute, '') or ''}"
                for label, attribute in (("From", "sender"), ("To", "to"), ("Subject", "subject"))
            )
            yield 1, headers + "\n\n" + str(getattr(message, "body", "") or ""), {"total_units": 1}
        except SafeToolError:
            raise
        except Exception as exc:
            raise SafeToolError("Corrupt or unsupported Outlook message") from exc
        finally:
            if message is not None:
                message.close()
        return
    if path.suffix.lower() == ".eml":
        if path.stat().st_size > MAX_XML_BYTES:
            raise SafeToolError("Email message exceeds the 16 MiB parsing limit")
        yield 1, _message_text(path.read_bytes()), {"total_units": 1}
        return
    current = bytearray()
    index = 0
    with path.open("rb") as stream:
        for line in stream:
            if line.startswith(b"From ") and current:
                index += 1
                yield index, _message_text(bytes(current)), {}
                current.clear()
            if not line.startswith(b"From "):
                current.extend(line)
            if len(current) > MAX_XML_BYTES:
                raise SafeToolError("Email message exceeds the 16 MiB parsing limit")
    if current:
        yield index + 1, _message_text(bytes(current)), {}


def _spreadsheet_units(
    path: Path,
    offset: int,
    sheet: str | int | None,
) -> Generator[tuple[int, str, dict[str, Any]], None, None]:
    from opensquilla.tools.builtin import filesystem as fs

    if path.suffix.lower() in {".csv", ".tsv"}:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, delimiter="\t" if path.suffix.lower() == ".tsv" else ",")
            for index, row in enumerate(reader, 1):
                if index >= offset:
                    yield index, json.dumps(row, ensure_ascii=False), {"sheet": path.name}
        return
    with _package(path) as archive:
        names = set(archive.namelist())
        manifest = ET.fromstring(_member(archive, "xl/workbook.xml"))
        # Reuse the established spreadsheet cell and relationship interpretation.
        relationships = fs._read_xlsx_workbook_relationships(archive, names)
        shared = fs._read_xlsx_shared_strings(archive, names)
        sheets = manifest.findall(f".//{{{fs._XLSX_MAIN_NS}}}sheet")
        requested = sheet if sheet is not None else 1
        if any(len(node.get("name", "")) > 256 for node in sheets):
            raise SafeToolError("Workbook sheet name exceeds the basic-reader limit")
        chosen = fs._select_spreadsheet_sheets(
            [(node.get("name") or f"Sheet{i}", []) for i, node in enumerate(sheets, 1)],
            requested,
        )[0][0]
        node = next(node for node in sheets if node.get("name") == chosen)
        target = relationships.get(node.get(f"{_R}id", ""), "")
        member = fs._normalize_xlsx_target(target)
        raw = _member(archive, member)
        for _, element in ET.iterparse(io.BytesIO(raw), events=("end",)):
            if element.tag != f"{{{fs._XLSX_MAIN_NS}}}row":
                continue
            index = int(element.get("r", "1"))
            if index >= offset:
                wrapper = ET.Element("worksheet")
                wrapper.append(element)
                rows = fs._read_xlsx_worksheet(ET.tostring(wrapper), shared)
                yield (
                    index,
                    json.dumps(rows[0] if rows else [], ensure_ascii=False),
                    {
                        "sheet": chosen,
                        "sheets": [item.get("name") for item in sheets[:32]],
                        "sheets_truncated": len(sheets) > 32,
                    },
                )
            element.clear()


def _pdf_units(
    path: Path, offset: int, limit: int
) -> Generator[tuple[int, str, dict[str, Any]], None, None]:
    try:
        import pdfplumber
    except ImportError as exc:
        raise SafeToolError("PDF reading requires the installed pdfplumber dependency") from exc
    try:
        with pdfplumber.open(str(path)) as document:
            total = len(document.pages)
            for index in range(offset - 1, min(total, offset - 1 + limit + 1)):
                page = document.pages[index]
                # The extra unit is only a continuation sentinel: never extract its content.
                if index == offset - 1 + limit:
                    yield index + 1, "", {"total_units": total}
                    break
                text = page.extract_text() or ""
                has_images = bool(page.images)
                yield (
                    index + 1,
                    text,
                    {
                        "total_units": total,
                        "textless": not bool(text.strip()),
                        "visual_content": has_images,
                    },
                )
                page.close()
    except SafeToolError:
        raise
    except Exception as exc:
        name = type(exc).__name__.lower()
        if "password" in name or "encrypt" in name or "password" in str(exc).lower():
            raise SafeToolError("PDF is password-protected; provide an unlocked copy") from exc
        raise SafeToolError(f"Corrupt or unreadable PDF: {path.name}") from exc


def read_document(
    path: Path,
    *,
    offset: int | None = None,
    limit: int | None = None,
    character_offset: int = 0,
    sheet: str | int | None = None,
) -> dict[str, Any] | None:
    ext = path.suffix.lower()
    if ext not in DOCUMENT_EXTENSIONS:
        return None
    _check_input(path)
    start = max(1, offset or 1)
    count = min(
        max(1, limit or (10 if ext == ".pdf" else 100)),
        MAX_PDF_PAGES if ext == ".pdf" else MAX_UNITS,
    )
    if character_offset < 0:
        raise SafeToolError("character_offset must be non-negative")
    unit = {
        ".docx": "paragraph",
        ".pptx": "slide",
        ".pdf": "page",
        ".eml": "message",
        ".mbox": "message",
        ".msg": "message",
    }.get(ext, "row")
    if ext == ".docx":
        source = _docx_units(path)
    elif ext == ".pptx":
        source = _pptx_units(path, start, count)
    elif ext == ".pdf":
        source = _pdf_units(path, start, count)
    elif ext in {".eml", ".mbox", ".msg"}:
        source = _email_units(path)
    else:
        source = _spreadsheet_units(path, start, sheet)
    result: dict[str, Any] = {
        "path": str(path),
        "format": ext[1:],
        "unit": unit,
        "offset": start,
        "units": [],
        "truncated": False,
        "next_offset": None,
        "next_character_offset": 0,
    }
    remaining = MAX_OUTPUT_CHARS
    try:
        for index, content, metadata in source:
            if index < start:
                continue
            if len(result["units"]) >= count or remaining == 0:
                result.update(truncated=True, next_offset=index)
                break
            for header in ("sheets", "sheets_truncated"):
                if header in metadata:
                    result.setdefault(header, metadata.pop(header))
            consumed = character_offset if index == start else 0
            text = content[consumed : consumed + remaining]
            result["units"].append({"index": index, "text": text, **metadata})
            remaining -= len(text)
            if consumed + len(text) < len(content):
                result.update(
                    truncated=True, next_offset=index, next_character_offset=consumed + len(text)
                )
                break
        result["range"] = [item["index"] for item in result["units"]]
        if ext == ".pdf":
            result["textless_pages"] = [
                item["index"] for item in result["units"] if item.get("textless")
            ]
            result["visual_pages"] = [
                item["index"] for item in result["units"] if item.get("visual_content")
            ]
            if result["textless_pages"] or result["visual_pages"]:
                result["note"] = (
                    "Text extraction may omit scanned text, images, or diagrams. "
                    "Use pdf(path, pages=..., render=true) to load selected pages for vision."
                )
        return result
    except (
        ET.ParseError,
        KeyError,
        zipfile.BadZipFile,
        UnicodeError,
        ValueError,
        csv.Error,
    ) as exc:
        raise SafeToolError(
            f"Corrupt or unsupported {ext[1:].upper()} document: {path.name}"
        ) from exc
    finally:
        source.close()


def complete_document_read(
    result: dict[str, Any],
    *,
    offset: int | None,
    limit: int | None,
    character_offset: int = 0,
    sheet: str | int | None = None,
) -> bool:
    """Preserve fresh-read requirements when a format applies implicit output limits."""
    return bool(
        not result["truncated"]
        and limit is None
        and (offset is None or offset <= 1)
        and character_offset == 0
        and sheet is None
        and not result.get("sheets_truncated")
        and len(result.get("sheets", [])) <= 1
    )


def read_pdf_request(
    path: Path, *, pages: str | None = None, render: bool = False
) -> dict[str, Any]:
    """Read/render selected PDF pages inside the same executor as file reads."""
    import base64

    from opensquilla.tools.builtin.media import _parse_page_range, _render_pdf_page_png

    _check_input(path)
    try:
        import pdfplumber
    except ImportError as exc:
        raise SafeToolError("PDF reading requires the installed pdfplumber dependency") from exc
    try:
        with pdfplumber.open(str(path)) as document:
            total = len(document.pages)
            selected = _parse_page_range(pages, total) if pages else list(range(min(total, 10)))
            if len(selected) > MAX_PDF_PAGES:
                raise SafeToolError("Read or render at most 10 PDF pages per call")
            chunks = []
            textless = []
            visual = []
            images: list[dict[str, str]] = []
            remaining = MAX_OUTPUT_CHARS
            actual = []
            next_page = None
            next_character_offset = 0
            render_remainder = selected[4:] if render else []
            if render:
                selected = selected[:4]
            for index in selected:
                if remaining == 0:
                    next_page = index + 1
                    break
                page = document.pages[index]
                text = page.extract_text() or ""
                if not text.strip():
                    textless.append(index + 1)
                if page.images:
                    visual.append(index + 1)
                actual.append(index + 1)
                chunks.append(text[:remaining])
                if len(text) > remaining:
                    next_page = index + 1
                    next_character_offset = remaining
                remaining -= len(chunks[-1])
                page.close()
                if render:
                    payload = _render_pdf_page_png(path, index + 1)
                    if len(payload) > 10 * 1024 * 1024:
                        raise SafeToolError("Rendered PDF page exceeds the image byte limit")
                    images.append({"mime": "image/png", "data": base64.b64encode(payload).decode()})
                if next_character_offset:
                    break
            if next_page is None and render_remainder:
                next_page = render_remainder[0] + 1
            if next_page is None and not pages and actual and actual[-1] < total:
                next_page = actual[-1] + 1
            receipt = {
                "path": str(path),
                "pages": ",".join(map(str, actual)),
                "range": actual,
                "total_pages": total,
                "text": "\n\n".join(chunks),
                "textless_pages": textless,
                "visual_pages": visual,
                "truncated": next_page is not None,
                "next_offset": next_page,
                "next_character_offset": next_character_offset,
                "note": (
                    "Page images loaded for model input; they have not yet been analyzed."
                    if render
                    else "Text extraction can omit scanned text, images, and diagrams. "
                    "Use render=true for selected pages; use read_file continuation fields "
                    "when a single page's text is truncated."
                ),
            }
            return {"message": json.dumps(receipt), "images": images}
    except SafeToolError:
        raise
    except Exception as exc:
        detail = (type(exc).__name__ + str(exc)).lower()
        if "password" in detail or "encrypt" in detail:
            raise SafeToolError("PDF is password-protected; provide an unlocked copy") from exc
        raise SafeToolError(f"Corrupt or unreadable PDF: {path.name}") from exc
