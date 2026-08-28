# reportlab reference

Authoring PDFs from data. Two surfaces: the low-level `canvas` API for
pixel-perfect placement, and the high-level `platypus` flowables for
auto-paginating documents. Full docs:
<https://www.reportlab.com/docs/reportlab-userguide.pdf>.

## Canvas (low-level)

```python
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import LETTER, A4

c = canvas.Canvas("out.pdf", pagesize=LETTER)
c.setFont("Helvetica-Bold", 18)
c.drawString(72, 720, "Title")        # x, y in points; origin = bottom-left
c.setFont("Helvetica", 11)
c.drawString(72, 696, "Body line.")
c.showPage()                          # finish current page
c.save()                              # finalize file
```

Key fonts available without registration: `Helvetica`, `Helvetica-Bold`,
`Helvetica-Oblique`, `Helvetica-BoldOblique`, `Times-Roman`, `Times-Bold`,
`Times-Italic`, `Times-BoldItalic`, `Courier` (regular/bold/italic),
`Symbol`, `ZapfDingbats`.

For custom fonts:

```python
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
pdfmetrics.registerFont(TTFont("Inter", "Inter-Regular.ttf"))
c.setFont("Inter", 11)
```

## Platypus (high-level)

```python
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib import colors

styles = getSampleStyleSheet()
story = [
    Paragraph("Q3 Review", styles["Title"]),
    Spacer(1, 0.2 * inch),
    Paragraph("Revenue grew <b>18%</b> YoY.", styles["BodyText"]),
    PageBreak(),
    Table([["Region", "Revenue"], ["NA", "$1.2M"]], hAlign="LEFT"),
]

doc = SimpleDocTemplate("out.pdf", pagesize=LETTER)
doc.build(story)
```

Paragraphs accept a subset of HTML: `<b>`, `<i>`, `<u>`, `<font>`, `<br/>`,
`<a href="...">`. Style sheets carry font, leading, alignment, color,
spacing.

## Tables

```python
data = [["Metric", "Q1", "Q2", "Q3"],
        ["Revenue", "$1.0M", "$1.1M", "$1.2M"]]
t = Table(data, colWidths=[1.5 * inch, inch, inch, inch])
t.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
    ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
    ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
    ("ALIGN",      (1, 1), (-1, -1), "RIGHT"),
    ("GRID",       (0, 0), (-1, -1), 0.25, colors.grey),
]))
```

Coordinates inside tables are `(col, row)` and accept negative indices.

## Page templates and footers

```python
from reportlab.platypus.doctemplate import PageTemplate
from reportlab.platypus.frames import Frame

def on_page(canvas_obj, doc):
    canvas_obj.saveState()
    canvas_obj.setFont("Helvetica", 8)
    canvas_obj.drawString(inch, 0.5 * inch, f"Page {doc.page}")
    canvas_obj.restoreState()

doc = SimpleDocTemplate("out.pdf", pagesize=LETTER)
frame = Frame(inch, inch, 6.5 * inch, 9 * inch)
template = PageTemplate(id="main", frames=[frame], onPage=on_page)
doc.addPageTemplates([template])
```

## Pitfalls

- Coordinates are in points (1pt = 1/72 inch), origin at the bottom-left.
  Mixing this with image-tooling (top-left origin) is a common bug.
- `Paragraph` text containing literal `<` or `>` must be escaped to
  `&lt;` / `&gt;`, otherwise reportlab parses them as malformed tags.
- Custom fonts require both registration and embedding. Copy the .ttf into
  the project rather than referencing a system path.
- `colWidths=[1.5*inch, ...]` is essential for stable layout; without it
  reportlab auto-sizes based on widest cell, which depends on font metrics.
