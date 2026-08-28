# python-pptx cheatsheet

Public API quick reference for the python-pptx library. See the upstream
docs at https://python-pptx.readthedocs.io for the full surface.

python-pptx is MIT-licensed upstream.

## Setup

```python
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.dml.color import RGBColor
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
```

`Inches`, `Pt`, `Emu` are unit helpers. PowerPoint stores everything as EMU
(914,400 EMU per inch). Always pass these helpers, never raw numbers.

## Presentation lifecycle

```python
prs = Presentation()                        # blank deck (4:3 default)
prs = Presentation("template.pptx")          # open existing

prs.slide_width  = Inches(13.333)            # 16:9 wide
prs.slide_height = Inches(7.5)

prs.save("out.pptx")
```

`Presentation()` accepts a path, an open binary file, or `None`.

## Slides and layouts

```python
layout = prs.slide_layouts[0]   # 0 = title, 1 = title+content, 5 = title only
slide  = prs.slides.add_slide(layout)
slide.shapes.title.text = "Hello"
slide.placeholders[1].text = "Subtitle"

for ph in slide.placeholders:
    print(ph.placeholder_format.idx, ph.name)
```

Layout indices depend on the master. For a default deck `slide_layouts` are
roughly: 0 Title, 1 Title+Content, 2 Section Header, 3 Two Content,
4 Comparison, 5 Title Only, 6 Blank, 7 Content w/ Caption, 8 Picture w/ Caption.

There is no public API to reorder slides. To reorder, use the unpack/repack
XML flow described in `SKILL.md` Path B2.

## Text

```python
tf = shape.text_frame                          # only when shape.has_text_frame
tf.text = "First paragraph"                    # replaces all paragraphs
p2 = tf.add_paragraph()
p2.text = "Second paragraph"

for para in tf.paragraphs:
    para.alignment = PP_ALIGN.LEFT
    for run in para.runs:
        run.font.name = "Calibri"
        run.font.size = Pt(16)
        run.font.bold = True
        run.font.color.rgb = RGBColor(0x1F, 0x29, 0x37)

tf.word_wrap = True
tf.auto_size = None                            # MSO_AUTO_SIZE.NONE
```

To preserve in-line formatting on edits, mutate **`run.text`**, not
`paragraph.text`. Setting `paragraph.text` collapses the paragraph into a
single un-styled run.

## Shapes

```python
shapes = slide.shapes
left, top, width, height = Inches(1), Inches(1), Inches(4), Inches(2)

# Text box
box = shapes.add_textbox(left, top, width, height)
box.text_frame.text = "Note"

# Auto-shape (rect, oval, line, callout, ...)
rect = shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
rect.fill.solid()
rect.fill.fore_color.rgb = RGBColor(0x10, 0xB9, 0x81)
rect.line.color.rgb     = RGBColor(0x06, 0x95, 0x4F)
rect.line.width         = Pt(1.5)

# Picture
pic = shapes.add_picture("logo.png", left, top, width, height)

# Connector / line
shapes.add_connector(1, Inches(1), Inches(3), Inches(5), Inches(3))
```

Common `MSO_SHAPE` values: `RECTANGLE`, `ROUNDED_RECTANGLE`, `OVAL`,
`RIGHT_ARROW`, `CHEVRON`, `DOWN_ARROW_CALLOUT`, `STAR_5_POINT`.

## Tables

```python
rows, cols = 3, 4
table_shape = shapes.add_table(rows, cols, Inches(0.5), Inches(2),
                               Inches(9), Inches(3))
table = table_shape.table
table.columns[0].width = Inches(2)
for r in range(rows):
    for c in range(cols):
        cell = table.cell(r, c)
        cell.text = f"r{r}c{c}"
        cell.fill.solid()
        cell.fill.fore_color.rgb = RGBColor(0xF1, 0xF5, 0xF9)
table.cell(0, 0).merge(table.cell(0, 1))
```

## Charts

```python
data = CategoryChartData()
data.categories = ["Q1", "Q2", "Q3", "Q4"]
data.add_series("Revenue", (4500, 5500, 6200, 7100))
chart_shape = shapes.add_chart(
    XL_CHART_TYPE.COLUMN_CLUSTERED,
    Inches(1), Inches(1.5), Inches(8), Inches(4),
    data,
)
chart = chart_shape.chart
chart.has_title = True
chart.chart_title.text_frame.text = "Quarterly Revenue"
```

`XL_CHART_TYPE` covers `LINE`, `LINE_MARKERS`, `BAR_CLUSTERED`,
`COLUMN_CLUSTERED`, `PIE`, `DOUGHNUT`, `XY_SCATTER`, `RADAR`.

## Speaker notes

```python
slide.notes_slide.notes_text_frame.text = "Mention the YoY methodology change."
```

## Reading existing content

```python
for i, slide in enumerate(prs.slides, 1):
    for shape in slide.shapes:
        if shape.has_text_frame:
            for para in shape.text_frame.paragraphs:
                line = "".join(run.text for run in para.runs)
                if line.strip():
                    print(f"slide {i}: {line}")
```

For images, `shape.image.blob` returns raw bytes; check `shape.shape_type`
against `MSO_SHAPE_TYPE.PICTURE`.

## Limits to remember

- No public API to reorder slides → use unpack/repack.
- No public API to clone a slide cleanly → community recipes copy XML and
  rels manually; consider PptxGenJS for from-scratch work instead.
- No animation API → animations from a template deck are preserved on
  text-only edits but cannot be authored programmatically.
- `add_picture` with SVG fails on older versions; convert SVG → PNG first
  (`cairosvg`, `rsvg-convert`).
