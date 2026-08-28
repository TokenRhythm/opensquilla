# python-docx reference

Concise patterns for authoring and editing `.docx` via python-docx. For the
full API see <https://python-docx.readthedocs.io/>.

## Document object

```python
from docx import Document
doc = Document()                    # empty
doc = Document("template.docx")    # open existing as template
doc.save("out.docx")
```

`Document(None)` loads an empty default template that ships with python-docx.

## Paragraphs and runs

A paragraph is a list of runs. Each run carries its own font, bold, italic,
size, color. Editing `paragraph.text` collapses everything into a single run
and **drops formatting** — always edit at the run level when style matters.

```python
para = doc.add_paragraph("Hello ")
run = para.add_run("world")
run.bold = True
run.italic = True
```

## Headings

`add_heading(text, level)` applies the corresponding `Heading N` style. Level
0 is the document title.

## Styles

Apply named styles by string. Common: `"Normal"`, `"Heading 1"`, `"Heading 2"`,
`"Title"`, `"Quote"`, `"List Bullet"`, `"List Number"`, `"Code"` (if defined
in the template).

```python
doc.add_paragraph("Note", style="Quote")
```

## Tables

```python
table = doc.add_table(rows=2, cols=3)
table.style = "Light Grid Accent 1"
table.rows[0].cells[0].text = "Header"
table.rows[1].cells[2].text = "Value"
```

Cells contain paragraphs, not text. Setting `cell.text` replaces the
paragraph contents but keeps the cell-level formatting.

## Sections

Page size, orientation, margins, and headers/footers live on `doc.sections`.

```python
section = doc.sections[0]
section.page_height = Inches(11)
section.page_width = Inches(8.5)
section.left_margin = Inches(1)
```

## Numbering and bullets

python-docx does not expose numbering definitions directly. The high-level
APIs (`add_paragraph(style="List Bullet")`) work for simple cases. For
multi-level numbering with restart behavior, fall back to OOXML editing of
`numbering.xml`.

## Headers and footers

```python
section = doc.sections[0]
header = section.header
header.paragraphs[0].text = "Confidential"
```

`header.is_linked_to_previous` controls whether the header repeats from the
prior section.

## Tracked changes

python-docx does not expose `<w:ins>` / `<w:del>` as first-class objects.
Detection is XML-level:

```python
xml = doc.element.body.xml
has_tracked = "<w:ins" in xml or "<w:del" in xml
```

To accept or reject changes, walk the XML directly with `lxml` and either
unwrap the elements (accept) or remove them entirely (reject).

## Pitfalls

- Stylesheet inheritance means new docs created with `Document()` may not
  have the same styles as a template you opened. Open the template, copy the
  parts you need, save as a new file.
- Tables auto-resize columns based on content; use `table.autofit = False`
  and set column widths explicitly when layout matters.
- `add_picture(path, width=Inches(2))` returns the picture inline; for
  floating placement, drop to OOXML.
