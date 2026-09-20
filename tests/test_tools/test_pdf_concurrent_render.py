"""Real native PDF rendering must survive simultaneous file-tool requests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_concurrent_documents_render_without_crashing_gateway(tmp_path: Path) -> None:
    from reportlab.pdfgen.canvas import Canvas

    # Independent documents still share PDFium's native font/cache state.
    for index in range(4):
        canvas = Canvas(str(tmp_path / f"synthetic-{index}.pdf"))
        canvas.drawString(60, 600, f"Synthetic document {index}")
        canvas.save()
    script = """
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import io, sys
from PIL import Image
from opensquilla.tools.builtin.media import _render_pdf_page_png
paths = sorted(Path(sys.argv[1]).glob('*.pdf')) * 4
def render(path):
    payload = _render_pdf_page_png(path, 1)
    with Image.open(io.BytesIO(payload)) as image:
        image.verify()
    return len(payload)
with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(render, paths))
assert len(results) == 16 and all(size > 100 for size in results)
print('16 concurrent PDF page requests verified')
"""
    # A native library regression must fail this subprocess, not kill pytest.
    source = str(Path(__file__).resolve().parents[2] / "src")
    env = {**os.environ, "PYTHONPATH": source}
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "16 concurrent PDF page requests verified" in completed.stdout
