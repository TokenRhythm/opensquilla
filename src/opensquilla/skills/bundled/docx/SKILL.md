---
name: docx
description: "Read, edit, or create Microsoft Word `.docx` files. Trigger this skill whenever the user mentions a Word document, .docx file, contract, report, brief, memo, or asks to extract text, modify an existing doc, generate one from a brief, or audit tracked changes. Three execution paths: text-and-structure extraction, in-place edit-by-run (preserves styles), and create-from-scratch with python-docx. Falls back to OOXML unzip-and-patch for layout work python-docx cannot reach."
description_zh: "读取、编辑或创建Microsoft Word .docx 文件。当用户提到Word文档、.docx文件、合同、报告、简报、备忘录，或要求提取文本、修改现有文档、根据简报生成文档或审查修订记录时触发。支持三种执行路径：文本与结构提取、按run就地编辑（保留样式）、用python-docx从零创建；处理python-docx无法完成的排版时回退到OOXML解压补丁。"
homepage: https://python-docx.readthedocs.io/
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/word-docx
  maintained_by: OpenSquilla
metadata:
  {
    "platform":
      {
        "emoji": "📘",
        "requires": { "anyBins": ["python", "python3"] },
        "install":
          [
            {
              "id": "python-docx",
              "kind": "uv",
              "package": "python-docx",
              "label": "Install python-docx (uv pip)",
            },
          ],
      },
  }
entrypoint:
  command: python {baseDir}/scripts/export_markdown_docx.py
  args:
    - --out
    - "{{ with.output_path }}"
  stdin: "{{ with.markdown }}"
  parse: text
  timeout: 60
---

# docx

Work with Microsoft Word `.docx` files. The format is OOXML — a zip container
holding XML parts (`word/document.xml`, `styles.xml`, `numbering.xml`, headers,
footers, relationships). Treat structure as primary; rendered text is a view.

## Decide the path first

Pick **one** path up front. The right path depends only on what is on disk
before you start.

| You have | Goal | Path |
|---|---|---|
| Existing `.docx` | Read text/structure | A. Inspect |
| Existing `.docx` | Modify content while keeping styles | B. Edit-in-place |
| Nothing or a brief | Build a new doc | C. Create from scratch |

If the user hands you a doc and asks for changes, default to path B and treat
the input as the visual style baseline. Only choose path C when the user says
"start fresh" or there is no input.

---

## Path A: Inspect

Dump structure as JSON for inspection without mutating anything.

```bash
python {baseDir}/scripts/inspect_docx.py /path/to/doc.docx
```

Output schema:

```json
{
  "paragraphs": [{"index": 0, "text": "...", "style": "Heading 1"}, ...],
  "tables": [[["row0,col0", "row0,col1"], ...], ...],
  "sections": 1,
  "has_tracked_changes": false
}
```

Use this whenever you need to see what is in the doc before deciding how to
edit. The output is stable and machine-readable — diff two inspect outputs to
verify a round-trip preserved everything you intended.

---

## Path B: Edit in place

Two sub-strategies; pick by how invasive the edit is.

### B1. Run-level text replacement (preferred)

When the change is "swap this string" or "fill these placeholders": mutate
runs in place. This preserves all theme/style/font settings.

```bash
python {baseDir}/scripts/edit_docx.py input.docx ops.json --out output.docx
```

`ops.json` is a list of operations:

```json
[
  {"op": "replace_run", "para": 0, "run": 0, "text": "Q3 Review"},
  {"op": "replace_text", "find": "{{CLIENT}}", "with": "Acme Corp"}
]
```

Edit at the **run** level, not the paragraph level — replacing whole paragraph
text drops formatting. If a placeholder spans multiple runs (often happens
when the original template applied bold/italic mid-word), the helper script
collapses runs into the first one and clears the rest.

### B2. Structural edits (sections / page layout / numbering)

python-docx exposes paragraphs, tables, and runs but has limited support for
page layout, numbering definitions, and tracked changes. For those, unzip the
`.docx`, patch `word/document.xml` and adjacent parts, and repack:

```bash
mkdir _unpacked && (cd _unpacked && unzip -q ../input.docx)
# edit _unpacked/word/document.xml
(cd _unpacked && zip -q -r ../output.docx . -x "*.DS_Store")
```

Rules when patching XML:

- Use `defusedxml.ElementTree` or `lxml`, not stdlib `xml.etree.ElementTree`.
  ET drops or rewrites namespace prefixes (`w:`, `r:`) in ways Word refuses to
  load.
- Preserve `xml:space="preserve"` on `<w:t>` elements that hold leading or
  trailing whitespace.
- `[Content_Types].xml` must list every part type. Removing a header without
  also removing its override entry yields a "repair" prompt in Word.
- Numbering definitions live in `numbering.xml`; bullet/number changes must
  patch the numbering ID, not just the visible text.

When done, validate by opening in LibreOffice headless before declaring
success — silent failures are common.

---

## Path C: Create from scratch

```bash
python {baseDir}/scripts/create_docx.py spec.json --out out.docx
```

`spec.json` describes content declaratively:

```json
{
  "metadata": {"title": "Q3 Review", "author": "Wei E."},
  "body": [
    {"kind": "heading", "level": 1, "text": "Q3 Review"},
    {"kind": "paragraph", "text": "Revenue +18% YoY."},
    {"kind": "table", "rows": [["Metric", "Value"], ["Revenue", "$2.1M"]]}
  ]
}
```

For programmatic use call python-docx directly:

```python
from docx import Document
doc = Document()
doc.add_heading("Q3 Review", level=1)
doc.add_paragraph("Revenue +18% YoY.")
table = doc.add_table(rows=2, cols=2)
table.rows[0].cells[0].text = "Metric"
doc.save("out.docx")
```

See [references/python_docx.md](references/python_docx.md) for paragraphs,
styles, numbering, tables, headers/footers, and section breaks.

---

## Tracked changes

Tracked changes are stored in `word/document.xml` as `<w:ins>` and `<w:del>`
elements. python-docx does not expose them as first-class objects — the
inspect helper sets `has_tracked_changes: true` when any `w:ins` or `w:del`
element is found, and you must resolve them by patching XML directly. Treat
docs with tracked changes as read-only until reviewers accept or reject the
revisions.

---

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| Word reports "needs repair" | Removed a header part but left override in `[Content_Types].xml` | Strip the override entry too |
| Text replacement drops bold/italic | Replaced `paragraph.text` instead of editing runs | Use `op: replace_run` |
| Numbering restarts unexpectedly | Edited a list item across two `abstractNum` definitions | Patch `numbering.xml`; rebuild numbering IDs |
| Smart-quote characters render as garbage | XML read with stdlib ET dropped namespaces | Switch to `defusedxml` or `lxml` |
| Long string overflows | Cell width is fixed in the template | Either shorten or compute auto-fit before save |

---

## Boundaries

- This skill is for `.docx` (OOXML WordprocessingML). It does **not** handle
  `.doc` (legacy binary) or Google Docs. Convert via LibreOffice or Word
  export first.
- Do not run macro-enabled `.docm` / VBA. The runtime sandbox does not
  execute embedded code, and security scanners flag mixed content.
- For PDF generation from a `.docx`, hand off to LibreOffice headless or a
  separate PDF skill. This skill stops at `.docx`.
