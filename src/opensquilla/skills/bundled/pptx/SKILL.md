---
name: pptx
description: "Read, edit, or create PowerPoint .pptx files. Trigger this skill whenever the user mentions a deck, slides, slide deck, presentation, or a `.pptx` filename — whether the goal is to extract text, modify an existing deck, build one from scratch, or prepare slides for review. Three execution paths are supported: text extraction (always available), template editing (unzip → patch slide XML → repack), and creation from scratch (python-pptx for Python or PptxGenJS for Node)."
description_zh: "读取、编辑或创建PowerPoint .pptx 文件。当用户提到演示文稿、幻灯片、slide deck、presentation 或 .pptx 文件名时触发——无论目标是提取文本、修改现有演示、从零构建还是准备评审幻灯片。支持三种执行路径：文本提取（始终可用）、模板编辑（解压→修补幻灯片XML→重新打包）以及从零创建（Python用python-pptx，Node用PptxGenJS）。"
homepage: https://python-pptx.readthedocs.io/
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/ivangdavila/powerpoint-pptx
  maintained_by: OpenSquilla
metadata:
  {
    "platform":
      {
        "emoji": "📊",
        "requires": { "anyBins": ["python", "python3"] },
        "install":
          [
            {
              "id": "python-pptx",
              "kind": "uv",
              "package": "python-pptx",
              "label": "Install python-pptx (uv pip)",
            },
            {
              "id": "pptxgenjs",
              "kind": "npm",
              "package": "pptxgenjs",
              "bins": ["node"],
              "label": "Install PptxGenJS (npm, optional — only for from-scratch JS path)",
            },
            {
              "id": "libreoffice-darwin",
              "kind": "brew",
              "os": ["darwin"],
              "formula": "libreoffice",
              "bins": ["soffice"],
              "label": "Install LibreOffice for visual QA (brew)",
            },
            {
              "id": "poppler-darwin",
              "kind": "brew",
              "os": ["darwin"],
              "formula": "poppler",
              "bins": ["pdftoppm"],
              "label": "Install Poppler (pdftoppm) for slide-image QA (brew)",
            },
          ],
      },
  }
---

# pptx

Work with PowerPoint `.pptx` decks. The pptx file format is OOXML — a zip
container holding XML descriptions of slides, layouts, masters, and media.

## Delivery rule

First, use the available tool list for this session to choose the delivery path.

If `write_file`, `edit_file`, `apply_patch`, or `execute_code` is available:

- Build the `.pptx` in the active workspace using the paths below.
- Call `publish_artifact` for the final `.pptx` before your final reply when
  that tool is available.
- The code examples later in this document apply.

If only `create_pptx` is available:

- Use it only for a basic text-only deck from slide titles, body text, and
  bullets.
- Do not use it for illustrated, image-heavy, chart-heavy, template-based, or
  visually designed decks. It does not support images, icons, charts, custom
  layouts, or visual QA.
- If the user asked for those visual features, explain that full visual deck
  authoring is unavailable in this session instead of calling `create_pptx` as
  though it satisfies the request.

If none of those file-authoring tools are available:

- Do not attempt to generate, save, or modify the `.pptx`.
- Do not paste OOXML, Python, JavaScript, HTML, or other source as a substitute
  for sending the deck.
- Ignore the Path B, Path C, and Visual QA sections below; they do not apply
  when file authoring is unavailable.
- Reply plainly: explain that the current session cannot create files, and
  offer to publish an existing `.pptx` by path, describe the slide contents in
  text, or continue in a file-authoring surface such as the OpenSquilla Web UI.

In all cases, do not paste full file source as the deliverable. Source code is
appropriate only when the user explicitly asks for code.

## Decide the path first

Pick **one** of three paths up front; do not mix them. The right path depends
only on what is on disk before you start.

| You have | Goal | Path |
|---|---|---|
| Existing `.pptx` | Read text only | A. Read |
| Existing `.pptx` | Modify content while keeping the design | B. Edit-in-place |
| Nothing, or a brief | Build a new deck | C. Create from scratch |

If the user hands you a deck and asks for changes, default to path B and treat
the input deck as the visual style baseline. Only fall back to path C when the
user explicitly says "start fresh" or there is no input deck.

---

## Path A: Read text from a `.pptx`

Use the helper script. It walks slides via the python-pptx public API and
prints text grouped by slide. This is always available because python-pptx is
the only hard dependency.

```bash
python {baseDir}/scripts/extract_text.py /path/to/deck.pptx
python {baseDir}/scripts/extract_text.py /path/to/deck.pptx --json
```

For programmatic use, call python-pptx directly:

```python
from pptx import Presentation
prs = Presentation("deck.pptx")
for i, slide in enumerate(prs.slides, 1):
    for shape in slide.shapes:
        if shape.has_text_frame:
            for para in shape.text_frame.paragraphs:
                print(i, "".join(run.text for run in para.runs))
```

If `markitdown` is available, it gives a faster Markdown rendering:

```bash
python -m markitdown deck.pptx
```

---

## Path B: Edit an existing deck

Two sub-strategies; pick by how invasive the edit is.

### B1. Text-only edits (preferred)

When the change is "swap this string for that one" or "fill these placeholders":
use python-pptx to mutate runs in place. This preserves all theme/master/font
settings.

```python
from pptx import Presentation
prs = Presentation("input.pptx")
slide = prs.slides[0]
for shape in slide.shapes:
    if not shape.has_text_frame:
        continue
    for para in shape.text_frame.paragraphs:
        for run in para.runs:
            if run.text == "{{TITLE}}":
                run.text = "Q3 Review"
prs.save("output.pptx")
```

Edit at the `run` level, not the `paragraph` level — replacing whole paragraph
text drops formatting. If a placeholder spans multiple runs (often happens
when the original template had partial bold/italic), concatenate the runs into
the first run and clear the others.

### B2. Structural edits (add/remove/reorder slides, change layouts)

python-pptx's slide-level mutation is limited (no public reorder API). For
structural work, unzip the `.pptx`, patch `ppt/presentation.xml` and the
slide files, and repack:

```bash
mkdir _unpacked && (cd _unpacked && unzip -q ../input.pptx)
# edit _unpacked/ppt/presentation.xml (sldIdLst order)
# edit _unpacked/ppt/slides/slideN.xml content
(cd _unpacked && zip -q -r ../output.pptx . -x "*.DS_Store")
```

Rules when patching slide XML:

- Use `defusedxml.minidom` or `lxml`, not stdlib `xml.etree.ElementTree`. ET
  drops or rewrites namespace prefixes (`a:`, `p:`, `r:`) in ways PowerPoint
  refuses to load.
- After deleting slides, remove their `<p:sldId>` from
  `ppt/presentation.xml`'s `<p:sldIdLst>` AND remove the matching relationship
  in `ppt/_rels/presentation.xml.rels`. Skipping either yields a "repair"
  prompt in PowerPoint.
- Update `[Content_Types].xml` if you change the slide count.
- If the deck has speaker notes, each `slideN.xml` has a paired
  `notesSlideN.xml` referenced by `slideN.xml.rels`. Delete or move both
  together.

When done, validate by **opening the output in LibreOffice headless** (next
section) before declaring success. An invalid deck silently fails to render
in some places but loads fine in others.

---

## Path C: Create from scratch

Use **python-pptx** when the runtime is Python-first (more readable, simpler
deps). Use **PptxGenJS** when you need richer layout primitives (charts,
tables with merged cells, gradients) or are already in a Node toolchain.

### C1. python-pptx quick recipe

```python
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor

prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)  # 16:9 wide

title_layout = prs.slide_layouts[0]
slide = prs.slides.add_slide(title_layout)
slide.shapes.title.text = "Q3 Review"
slide.placeholders[1].text = "Wei E."

content_layout = prs.slide_layouts[1]
slide = prs.slides.add_slide(content_layout)
slide.shapes.title.text = "Highlights"
tf = slide.placeholders[1].text_frame
tf.text = "Revenue +18% YoY"
for line in ("Churn down to 4.1%", "Two new enterprise logos"):
    p = tf.add_paragraph()
    p.text = line

prs.save("out.pptx")
```

See [references/python_pptx.md](references/python_pptx.md) for shapes, tables,
images, charts, and color/font helpers.

### C2. PptxGenJS quick recipe

```javascript
const pptxgen = require("pptxgenjs");
const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE"; // 13.3" × 7.5"

const slide = pres.addSlide();
slide.background = { color: "0F172A" };
slide.addText("Q3 Review", {
  x: 0.5, y: 0.6, w: 9, h: 1.2,
  fontSize: 44, bold: true, color: "FFFFFF", margin: 0,
});

await pres.writeFile({ fileName: "out.pptx" });
```

See [references/pptxgenjs.md](references/pptxgenjs.md) for the full surface.

### Design checklist (apply to both C1 and C2)

Plain bullets on white look generated. Apply each item before declaring done:

- Pick a content-specific palette (one dominant color ~60% of weight, one
  support, one accent). Avoid generic blue.
- Title slides and section dividers in dark; content slides in light.
- Every slide carries one meaningful visual element: a topic-specific image,
  chart, diagram, data graphic, or recognizable icon. Emoji, colored boxes,
  and decorative lines do not satisfy this requirement. No slide is title +
  bullets only.
- Use varied layouts across slides — two-column, half-bleed image, stat
  grid, quote slide. Repeating one layout for ten pages is the strongest
  "AI-generated" tell.
- Header font ≥ 36 pt; body 14–16 pt. Keep ≥ 0.5" margin from slide edges.
- Left-align body paragraphs; center only titles.
- Do **not** add a thin colored line under every title — it is a strong
  visual marker for AI-generated decks.

---

## Visual QA (when render tools are available)

For paths B and C, when both `soffice` (LibreOffice) and `pdftoppm` (poppler)
are available and the renderer runs, render the entire deck to images and
inspect every page before `publish_artifact`.

```bash
bash {baseDir}/scripts/render_thumbs.sh out.pptx
# emits out-01.jpg, out-02.jpg, ... in cwd, plus out.pdf
```

Do not pass `--range` for final QA. Count the emitted `out-*.jpg` files and
inspect each one. A timeout, conversion error, missing thumbnail, or a review
of only a subset of the pages means visual QA is incomplete. If inspection
finds a defect, fix it, render the entire deck again, and re-inspect every
page. If the full inspection finds no defect, publish without inventing an
unnecessary edit just to force a rerender.

The script needs `soffice` (LibreOffice) and `pdftoppm` (poppler) on PATH. If
either is missing, the script tells you what to install for the host OS.

Inspection prompt for a fresh-eyes pass (use a sub-agent or re-read with a
different model than the one that generated the deck):

```
Inspect each slide image. Assume there are issues; find them.
For each slide list:
  - Overlapping text/shapes
  - Text overflow or cut off at margins
  - Decorative lines positioned for one-line titles when title wrapped to two
  - Low-contrast text (light on light or dark on dark)
  - Inconsistent spacing across analogous slides
  - Leftover placeholder text ("Lorem", "TODO", "{{...}}", "xxxx")
  - Misaligned columns or icons
Report all findings. When you found a defect, do not declare clean until its
fix-and-reverify cycle has passed across every rendered page.
```

If either render tool is unavailable, visual QA is unavailable. A B1 text-only
edit may still be published after the file saves and the intended text is
verified; state that visual QA was unavailable. For B2 or C work without a
renderer, complete the available structural checks but do not claim that visual
QA passed.

---

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `.pptx` opens with "PowerPoint found a problem" | Deleted slide left orphaned `<p:sldId>` or rel | Use the unpack/repack flow; clean rels and Content_Types |
| Output has corrupt colors / file refuses to open (PptxGenJS) | Used `"#FF0000"` (with `#`) or `"FF000080"` (8-char alpha) | Use `"FF0000"` and pass alpha via `transparency` or `opacity` |
| Bullets appear doubled (PptxGenJS) | Wrote unicode `•` in the text string AND used `bullet: true` | Drop the unicode glyph; use `bullet: true` only |
| Smart-quote characters render as garbage after editing XML | XML was read with stdlib `xml.etree.ElementTree` | Switch to `defusedxml.minidom` or `lxml`; preserve `xml:space="preserve"` on `<a:t>` |
| Second shape inherits weird shadow values (PptxGenJS) | Re-used the same `shadow` options object across two shapes — the library mutates it in place | Build a fresh object per call |
| Long edited string doesn't wrap | python-pptx does not auto-fit; the original textbox width is fixed | Either shorten the text or compute line breaks; consider `enable_auto_size` after measurement |
| Visual QA looks wrong on Windows but right on macOS | Fonts on the system differ — soffice falls back silently | Pin fonts referenced by the template, or render QA on the same OS as the consumer |

---

## Troubleshooting

- **"python-pptx not installed"**: run `uv pip install python-pptx` (or
  `pip install python-pptx`). The skill declares this under
  `metadata.platform.install`, so the eligibility report surfaces an
  install hint automatically when the binary is present but the module is
  missing.
- **"command not found: soffice"**: on macOS `brew install libreoffice`; on
  Debian/Ubuntu `sudo apt-get install -y libreoffice`; on Windows install
  LibreOffice from libreoffice.org and add `program/` to PATH. Visual QA is
  optional — paths A and B1 work without it.
- **"command not found: pdftoppm"**: macOS `brew install poppler`;
  Debian/Ubuntu `sudo apt-get install -y poppler-utils`; Windows ships it
  inside the LibreOffice install or via `pdftoppm` from poppler-windows
  releases.
- **PptxGenJS path fails with `MODULE_NOT_FOUND`**: do not use a global npm
  install; Node does not resolve global packages from `require()`. Prefer the
  Python path. If the JS path is necessary, create a disposable local project
  outside the user's repository. Create a fresh per-run OS temporary directory
  with `tempfile.mkdtemp(prefix="opensquilla-pptxgenjs-")`, use it as the
  working directory for `npm install --ignore-scripts --no-save pptxgenjs` and
  `node build_deck.js`, and keep the authoring script beside its
  `node_modules` directory. Write the final `.pptx` into the active workspace
  and remove the disposable directory after publishing it. Do not use a fixed
  path or a shell-specific directory-creation command.

---

## Boundaries

- This skill is for `.pptx` (Office Open XML PresentationML). It is **not**
  for `.ppt` (legacy binary), Google Slides, or Keynote files. Convert those
  to `.pptx` first (LibreOffice or Keynote export).
- Do not embed external macro logic (`.pptm` / VBA). Bundled Codex sandboxes
  do not execute the embedded code, and security scanners flag mixed
  content.
- Source and runtime dependency notices are recorded in
  `THIRD_PARTY_NOTICES.md`.
