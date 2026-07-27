---
name: pdf-toolkit
description: "Structured `.pdf` operations: extract text/tables, merge pages from multiple PDFs, split a PDF by page ranges, fill PDF form fields, and generate fresh PDFs from JSON. Trigger when the user wants programmatic PDF work without natural-language rewriting — examples: pull tables from a report, combine three PDFs, extract pages 5-12, fill a tax form, or build a new PDF from data. Distinct from `nano-pdf`, which uses an LLM to rewrite a page from a sentence; this skill is deterministic byte-level work via pypdf, pdfplumber, and reportlab."
description_zh: "结构化的 .pdf 操作：提取文本/表格、合并多个PDF的页面、按页码范围拆分PDF、填写PDF表单字段以及从JSON生成新PDF。当用户需要无需自然语言改写的程序化PDF处理时触发，例如从报告提取表格、合并三个PDF、提取第5-12页、填写税表或用数据构建新PDF。区别于用LLM改写页面的 nano-pdf；本技能通过pypdf、pdfplumber和reportlab进行确定性的字节级处理。"
homepage: https://pypdf.readthedocs.io/
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/pdf
  maintained_by: OpenSquilla
metadata:
  {
    "platform":
      {
        "emoji": "📕",
        "requires": { "anyBins": ["python", "python3"] },
        "install":
          [
            {
              "id": "pypdf",
              "kind": "uv",
              "package": "pypdf",
              "label": "Install pypdf (uv pip)",
            },
            {
              "id": "reportlab",
              "kind": "uv",
              "package": "reportlab",
              "label": "Install reportlab (uv pip)",
            },
          ],
      },
  }
---

# pdf-toolkit

Deterministic, structural PDF operations. Use this skill for programmatic
work where you know exactly what you want done. Use the sibling `nano-pdf`
skill instead when the task is "rewrite this page to say X" — `nano-pdf`
applies a natural-language edit; `pdf-toolkit` applies an explicit operation.

## Decide the operation

| Goal | Script |
|---|---|
| Get text or tables out of a PDF | `extract.py` |
| Combine pages from multiple PDFs | `merge.py` |
| Split a PDF by page ranges | `split.py` |
| Fill `/Tx` form fields in a PDF | `form_fill.py` |
| Build a new PDF from data | inline `reportlab` snippet, see Path C below |

---

## Path A: Extract

```bash
python {baseDir}/scripts/extract.py /path/to/doc.pdf --json
```

Output:

```json
{
  "pages": 12,
  "metadata": {"title": "...", "author": "..."},
  "text": [
    {"page": 1, "content": "..."},
    {"page": 2, "content": "..."}
  ],
  "tables": [
    {"page": 3, "rows": [["..."], ["..."]]}
  ]
}
```

Text uses `pdfplumber` (already in default dependencies) which preserves
column layout better than naive PDF text extraction. Tables use
`pdfplumber.extract_tables()` with default settings; for tricky layouts
pass `--tables-strategy lines|text|explicit` to switch detection mode.

For OCR (scanned PDFs), this skill does not include Tesseract — use the
sibling skill that wraps an OCR engine (out of scope here).

---

## Path B: Merge / Split

Merge full files:

```bash
python {baseDir}/scripts/merge.py a.pdf b.pdf c.pdf --out combined.pdf
```

Or merge specific page ranges with the manifest form:

```bash
python {baseDir}/scripts/merge.py manifest.json --out combined.pdf
```

`manifest.json`:

```json
[
  {"file": "a.pdf", "pages": "1-3"},
  {"file": "b.pdf", "pages": "5,7,9-11"},
  {"file": "c.pdf"}
]
```

Page ranges are 1-based, comma-separated, hyphen for ranges. Omit `pages` to
include the whole file. Splits use the same syntax in reverse:

```bash
python {baseDir}/scripts/split.py input.pdf --pages "1-3,7,10-12" --out output_dir/
```

Each range writes one output file: `output_dir/input_001.pdf`,
`output_dir/input_002.pdf`, …

---

## Path C: Form fill

```bash
python {baseDir}/scripts/form_fill.py form.pdf data.json --out filled.pdf
```

`data.json` maps field name → string value:

```json
{
  "applicant_name": "Wei E.",
  "submission_date": "2026-05-06",
  "agreed": "Yes"
}
```

The script discovers fields via `pypdf.PdfReader.get_fields()` and updates
them with `update_page_form_field_values()`. Fields not present in the JSON
are left untouched. Run with `--list-fields` to enumerate the form's fields
without filling.

Caveats:

- `/Btn` checkbox fields take the export value (often `Yes`, `On`, or `1`)
  rather than `true` — inspect with `--list-fields` to discover.
- AcroForm fills only. XFA forms (used by some legal templates) require
  Adobe-specific tooling and are out of scope.
- Some signed PDFs invalidate the signature when fields change. Strip
  signatures explicitly with `--clear-signatures` if that is intended.

---

## Path D: Generate from scratch

Use `reportlab` directly when you need a new PDF:

```python
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import LETTER
from pathlib import Path

c = canvas.Canvas(str(Path("out.pdf")), pagesize=LETTER)
c.setFont("Helvetica-Bold", 18)
c.drawString(72, 720, "Q3 Review")
c.setFont("Helvetica", 11)
c.drawString(72, 696, "Revenue grew 18% year over year.")
c.showPage()
c.save()
```

For tables, headers/footers, and multi-column layouts, switch to
`reportlab.platypus` (`SimpleDocTemplate`, `Paragraph`, `Table`,
`PageBreak`). See [references/reportlab.md](references/reportlab.md).

---

## Boundary with `nano-pdf`

`nano-pdf` (sibling bundled skill) wraps an LLM that takes a page index and
a natural-language instruction. Use it when the change is "fix the typo on
page 1" or "make the title shorter". Use **this** skill when the change is
"merge these three PDFs", "extract the tables", or "fill the form". The two
do not overlap: if you find yourself reaching for `nano-pdf` to do a merge,
switch to `pdf-toolkit`; if you reach here to "rewrite page 5 to be friendlier",
switch back.

---

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| Extracted text is empty | Scanned PDF, no text layer | OCR is out of scope; use a separate OCR skill |
| Garbled characters in extract | PDF uses a custom font encoding | Try `pdfplumber.open(path, laparams={...})` with `char_margin` adjustments |
| Merged PDF is huge | Underlying PDFs include large embedded fonts | Subset fonts via `pypdf` `compress_content_streams()` |
| Form fill silently no-ops | Field name in JSON does not match PDF field name | Run with `--list-fields` first to see exact names |
| Pages out of order after split | Range overlap collapsed unexpectedly | Use disjoint ranges, e.g. `1-3,4-6` not `1-5,3-6` |

---

## Boundaries

- This skill works with text-based and form-based PDFs. Scanned image PDFs
  need OCR before any text path produces results.
- Encrypted PDFs are read-only here. Decryption requires the user-supplied
  password and is out of scope for this skill.
- For PDF-to-image rendering, use a separate skill that wraps Poppler or
  PyMuPDF.
- Digital signature operations (signing, verifying, revoking) are out of
  scope.
