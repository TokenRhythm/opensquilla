# PptxGenJS cheatsheet

Public API quick reference for the PptxGenJS library. See the upstream docs
at https://gitbrent.github.io/PptxGenJS/ for the full surface.

PptxGenJS is MIT-licensed upstream.

## Install

Create a fresh per-run OS temporary directory outside the target repository.
For example, create it from the available Python runtime:

```python
from pathlib import Path
import tempfile

work_dir = Path(tempfile.mkdtemp(prefix="opensquilla-pptxgenjs-"))
```

Use `work_dir` as the working directory for `npm install --ignore-scripts
--no-save pptxgenjs` and `node build_deck.js`. Keep the authoring script in
this disposable directory so Node resolves the local `node_modules/pptxgenjs`.
Do not use `npm install -g`: global packages are not a reliable `require()`
search path. Do not use a fixed path or a shell-specific directory-creation
command. Write the final `.pptx` into the active workspace, then remove the
temporary directory.

## Boilerplate

```javascript
const pptxgen = require("pptxgenjs");
const pres = new pptxgen();

pres.layout = "LAYOUT_WIDE";   // 13.3" × 7.5". Other: LAYOUT_16x9, LAYOUT_4x3
pres.title  = "Q3 Review";
pres.author = "ops@example.com";

const slide = pres.addSlide();
slide.background = { color: "0F172A" };

await pres.writeFile({ fileName: "out.pptx" });
```

Coordinates are in inches by default. `pres.layout` controls the slide
canvas; pass a custom `{ width, height, name }` to define a non-standard size.

## Text

```javascript
slide.addText("Hello", {
  x: 0.5, y: 0.5, w: 6, h: 1,
  fontSize: 36, bold: true, color: "FFFFFF",
  fontFace: "Calibri", align: "left", valign: "middle",
  margin: 0,            // 0 when aligning text to shape edges
  charSpacing: 4,       // letter spacing in points
});

// Multi-paragraph: pass an array of objects
slide.addText(
  [
    { text: "Bold line", options: { bold: true, breakLine: true } },
    { text: "Plain second line",       options: { breakLine: true } },
    { text: "Third line" },
  ],
  { x: 0.5, y: 2, w: 8, h: 3, fontSize: 16 },
);
```

`breakLine: true` separates lines inside a single text box. The last item
does not need it.

## Bullets

```javascript
slide.addText(
  [
    { text: "First",  options: { bullet: true, breakLine: true } },
    { text: "Second", options: { bullet: true, breakLine: true } },
    { text: "Third",  options: { bullet: true } },
  ],
  { x: 1, y: 1, w: 8, h: 3 },
);

// Numbered:
options: { bullet: { type: "number" } }

// Indented sub-bullet:
options: { bullet: true, indentLevel: 1 }
```

Never type unicode `•` into the string — it stacks with the bullet renderer
and produces double bullets.

## Shapes

```javascript
slide.addShape(pres.shapes.RECTANGLE, {
  x: 1, y: 1, w: 4, h: 1,
  fill:   { color: "1F2937" },
  line:   { color: "FFFFFF", width: 1 },
});

slide.addShape(pres.shapes.OVAL, { x: 6, y: 1, w: 1.5, h: 1.5,
  fill: { color: "10B981" } });

slide.addShape(pres.shapes.LINE, {
  x: 1, y: 4, w: 6, h: 0,
  line: { color: "94A3B8", width: 1.5, dashType: "dash" },
});
```

Shape catalog: `RECTANGLE`, `ROUNDED_RECTANGLE`, `OVAL`, `LINE`,
`RIGHT_TRIANGLE`, `RIGHT_ARROW`, `CHEVRON`, `STAR_5`, `CALLOUT_1`.

## Images

```javascript
// From file path
slide.addImage({ path: "logo.png", x: 1, y: 1, w: 2, h: 1 });

// From base64 (no file I/O)
slide.addImage({
  data: "image/png;base64,iVBORw0KGgoAAA...",
  x: 1, y: 1, w: 2, h: 1,
});

// Sizing modes (preserve aspect)
slide.addImage({
  path: "hero.jpg", x: 0, y: 0,
  sizing: { type: "cover", w: 13.3, h: 7.5 },   // cover | contain | crop
});

// Optional flags
slide.addImage({
  path: "logo.png", x: 1, y: 1, w: 2, h: 2,
  rotate: 15, rounding: true, transparency: 30,
  altText: "Company logo",
  hyperlink: { url: "https://example.com" },
});
```

## Tables

```javascript
slide.addTable(
  [
    ["Region", "Q1", "Q2", "Q3"],
    ["NA",     "22", "26", "31"],
    ["EU",     "14", "17", "22"],
  ],
  {
    x: 0.5, y: 2, w: 9,
    colW: [3, 2, 2, 2],
    fill:   { color: "F8FAFC" },
    border: { pt: 0.5, color: "CBD5E1" },
    fontSize: 14,
  },
);

// Per-cell styling and merge
slide.addTable(
  [[
    { text: "Header", options: { fill: { color: "1F2937" }, color: "FFFFFF",
                                  bold: true, colspan: 2 } },
  ]],
  { x: 0.5, y: 1, w: 9 },
);
```

## Charts

```javascript
slide.addChart(
  pres.charts.BAR,
  [{ name: "Revenue", labels: ["Q1","Q2","Q3","Q4"], values: [45,55,62,71] }],
  {
    x: 0.5, y: 1, w: 9, h: 4, barDir: "col",
    chartColors: ["10B981", "34D399", "6EE7B7"],
    catAxisLabelColor: "64748B",
    valAxisLabelColor: "64748B",
    valGridLine: { color: "E2E8F0", size: 0.5 },
    catGridLine: { style: "none" },
    showValue: true, dataLabelPosition: "outEnd",
    showLegend: false,
  },
);
```

Chart types: `BAR`, `LINE`, `PIE`, `DOUGHNUT`, `SCATTER`, `BUBBLE`,
`AREA`, `RADAR`.

## Slide masters

```javascript
pres.defineSlideMaster({
  title: "TITLE_SLIDE",
  background: { color: "0F172A" },
  objects: [
    { placeholder: { options: { name: "title", type: "title",
        x: 1, y: 2.5, w: 11, h: 1.5,
        fontSize: 44, bold: true, color: "FFFFFF" } } },
  ],
});

const t = pres.addSlide({ masterName: "TITLE_SLIDE" });
t.addText("Q3 Review", { placeholder: "title" });
```

## Pitfalls

- **Hex colors must be 6 chars, no `#` prefix.** `"FF0000"` ✓ — `"#FF0000"`
  silently corrupts the output.
- **Never encode alpha into the hex string** (`"FF000080"` corrupts the
  file). Use `transparency: 0–100` (shape/image) or `opacity: 0.0–1.0`
  (shadow).
- **Do not reuse an options object across multiple `add*` calls.** PptxGenJS
  mutates options in place (e.g. converting shadow values to EMU). Build a
  fresh object per call, or use a factory function.
- **`bullet: true` is exclusive with unicode `•` in the text** — using both
  produces double bullets.
- **`lineSpacing` on a bulleted list creates oversized gaps.** Use
  `paraSpaceAfter` instead.
- **Each presentation needs its own `new pptxgen()` instance.** Reusing the
  same `pres` between exports leaks state.
- **Gradient fills are not natively supported.** Render the gradient as a
  PNG in advance and use it as a slide background image.
- **Rounded rectangle accent bars don't cover the rounded corners** — when
  you draw a small rectangle on the side of a `ROUNDED_RECTANGLE`, use a
  plain `RECTANGLE` for the card too, or draw the accent first and the
  rounded shape on top.
