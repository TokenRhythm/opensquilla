from __future__ import annotations

import io
import zipfile

import pytest

from opensquilla.office_format import (
    OFFICE_MIMES,
    OfficeFormatError,
    office_format,
    validate_office_bytes,
)


def office_package(format: str = "docx", text: str = "Original") -> bytes:
    family, main, root = {
        "docx": ("wordprocessingml", "word/document.xml", "document"),
        "pptx": ("presentationml", "ppt/presentation.xml", "presentation"),
        "xlsx": ("spreadsheetml", "xl/workbook.xml", "workbook"),
    }[format]
    content_family = {"docx": "document", "pptx": "presentation", "xlsx": "sheet"}[format]
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            (
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" '
                'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                f'<Override PartName="/{main}" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                f'{family}.{content_family}.main+xml"/></Types>'
            ),
        )
        archive.writestr(
            "_rels/.rels",
            (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/officeDocument" '
                f'Target="{main}"/></Relationships>'
            ),
        )
        if format == "docx":
            archive.writestr(
                main,
                (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
                    '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>'
                ),
            )
        else:
            archive.writestr(
                main, f'<{root} xmlns="http://schemas.openxmlformats.org/{family}/2006/main"/>'
            )
    deterministic = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(target.getvalue())) as source:
        with zipfile.ZipFile(deterministic, "w") as output:
            for info in source.infolist():
                info.date_time = (2020, 1, 1, 0, 0, 0)
                output.writestr(info, source.read(info.filename))
    return deterministic.getvalue()


@pytest.mark.parametrize("format", OFFICE_MIMES)
def test_modern_office_opc(format):
    validate_office_bytes(office_package(format), format)
    assert office_format(f"Report.{format.upper()}", OFFICE_MIMES[format]) == format
    with pytest.raises(OfficeFormatError):
        validate_office_bytes(b"not an Office package", format)


def test_office_parser_is_bound_to_real_package_type():
    with pytest.raises(OfficeFormatError, match="content_type_mismatch"):
        validate_office_bytes(office_package("docx"), "pptx")
    assert office_format("legacy.doc") is None
    assert office_format("macro.docm") is None
    assert office_format("wrong.docx", OFFICE_MIMES["xlsx"]) is None


def test_office_rejects_unsafe_zip_paths_and_doctype():
    content = io.BytesIO(office_package())
    with zipfile.ZipFile(content, "a") as archive:
        archive.writestr("../escaped.xml", "unsafe")
    with pytest.raises(OfficeFormatError):
        validate_office_bytes(content.getvalue(), "docx")
    content = io.BytesIO(office_package())
    output = io.BytesIO()
    with zipfile.ZipFile(content) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "word/document.xml":
                data = b'<!DOCTYPE document [<!ENTITY x "unsafe">]>' + data
            target.writestr(name, data)
    with pytest.raises(OfficeFormatError, match="unsafe_xml_declaration"):
        validate_office_bytes(output.getvalue(), "docx")
