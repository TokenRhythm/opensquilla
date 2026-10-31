"""Exercise the PDF dependency at the bundled toolkit boundary, using synthetic files."""

from __future__ import annotations

import runpy
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src/opensquilla/skills/bundled/pdf-toolkit/scripts"


def test_pdf_toolkit_dependency_floor_includes_parser_security_fixes() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "pypdf>=6.21.4" in project["project"]["dependencies"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = [p["version"] for p in lock["package"] if p["name"] == "pypdf"]
    assert versions
    assert all(tuple(map(int, version.split("."))) >= (6, 21, 4) for version in versions)


def test_pdf_toolkit_extract_merge_split_and_form_fill(tmp_path: Path) -> None:
    source = tmp_path / "form.pdf"
    document = canvas.Canvas(str(source))
    document.drawString(72, 720, "Synthetic PDF compatibility fixture")
    document.acroForm.textfield(name="example", x=72, y=650, width=200, height=24)
    document.showPage()
    document.save()

    extract = runpy.run_path(str(SCRIPTS / "extract.py"))["extract"]
    merge = runpy.run_path(str(SCRIPTS / "merge.py"))["merge"]
    split = runpy.run_path(str(SCRIPTS / "split.py"))["split"]
    form = runpy.run_path(str(SCRIPTS / "form_fill.py"))
    assert "Synthetic PDF" in extract(source, None)["text"][0]["content"]
    merged = tmp_path / "merged.pdf"
    assert merge([{"file": str(source)}, {"file": str(source)}], merged) == 2
    assert len(PdfReader(merged).pages) == 2
    parts = split(merged, "2", tmp_path / "split")
    assert len(parts) == 1
    assert len(PdfReader(parts[0]).pages) == 1
    filled = tmp_path / "filled.pdf"
    assert form["fill"](source, {"example": "synthetic value"}, filled) == 1
    assert form["list_fields"](filled)["example"]["value"] == "synthetic value"


@pytest.mark.parametrize("trailer", ["tr", "% truncated comment"])
def test_pdf_truncated_xref_trailer_terminates(trailer: str) -> None:
    # Isolate malformed parser input so a future dependency regression fails
    # within a fixed deadline instead of hanging the public test worker.
    result = subprocess.run(
        [sys.executable, "-c", """
import io
import sys
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
document = io.BytesIO()
writer = PdfWriter()
writer.add_blank_page(width=100, height=100)
writer.write(document)
reader = PdfReader(document)
assert len(reader.pages) == 1
broken = io.BytesIO(b'ref\\n0 1\\n0000000000 65535 f \\n' + sys.argv[1].encode())
try:
    reader._read_standard_xref_table(broken)
except PdfReadError:
    print('rejected malformed xref')
else:
    raise AssertionError('malformed xref accepted')
""", trailer],
        capture_output=True, text=True, check=True, timeout=10,
    )
    assert result.stdout.strip() == "rejected malformed xref"
