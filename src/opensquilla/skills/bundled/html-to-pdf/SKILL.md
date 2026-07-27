---
name: html-to-pdf
description: "Render HTML (with CSS) to a PDF file. Trigger when the user wants to export a styled report, invoice, label, or any HTML/Jinja-rendered page to PDF. Uses WeasyPrint, which supports a meaningful subset of CSS Paged Media (page size, margins, headers/footers, page-break-before/after). Optional dependency — install via `pip install opensquilla[document-extras]` or `uv add weasyprint` because WeasyPrint pulls in native libraries (Pango, Cairo, fontconfig) that need OS-level packages."
description_zh: "将HTML（含CSS）渲染为PDF文件。当用户想把带样式的报告、发票、标签或任何HTML/Jinja渲染页面导出为PDF时触发。使用WeasyPrint，支持CSS分页媒体的常用子集（页面尺寸、页边距、页眉页脚、分页控制）。可选依赖，需通过 pip install opensquilla[document-extras] 或 uv add weasyprint 安装，因其依赖Pango、Cairo、fontconfig等原生库。"
homepage: https://weasyprint.org/
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/generate-pdf
  maintained_by: OpenSquilla
metadata:
  {
    "platform":
      {
        "emoji": "📄",
        "requires": { "anyBins": ["python", "python3"] },
        "install":
          [
            {
              "id": "weasyprint",
              "kind": "uv",
              "package": "weasyprint",
              "label": "Install WeasyPrint (uv pip)",
            },
          ],
      },
  }
---

# html-to-pdf

Render HTML + CSS to PDF using WeasyPrint. Best for static report exports
where the source already exists in HTML form (templates, dashboards,
invoices). For programmatic PDF assembly from data structures, use the
`pdf-toolkit` skill's reportlab path instead.

## Delivery rule

First, use the available tool list for this session to choose the delivery path.

If `write_file`, `edit_file`, `apply_patch`, or `execute_code` is available:

- Build the `.pdf`, `.html`, or requested file in the active workspace using the
  workflow below.
- Call `publish_artifact` for the final file before your final reply when that
  tool is available.
- The examples and workflow steps later in this document apply.

If none of those file-authoring tools are available:

- Do not attempt to generate, save, or modify the final file.
- Do not paste the full HTML/CSS source into chat as a substitute for delivering
  the file.
- Ignore the Quick start and Workflow sections below; they do not apply when
  file authoring is unavailable.
- Reply plainly: explain that the current session cannot create files, and
  offer to publish an existing file by path, describe the document contents in
  text, or continue in a file-authoring surface such as the OpenSquilla Web UI.

In all cases, do not paste full file source as the deliverable. Source code is
appropriate only when the user explicitly asks for code.

## Use cases

- HTML/Jinja template + content → styled PDF report
- Markdown rendered to HTML → printable PDF
- Email content → archival PDF
- Generated dashboards (HTML + screenshots) → shareable PDF

## Limitations

- Source data is structured (JSON, dataframe) with no HTML — use
  `pdf-toolkit` (reportlab) directly instead.
- Source PDF needs editing — use `pdf-toolkit` (pypdf path).
- Need pixel-perfect Word-style document layout — use the `docx` skill.
- Need dynamic JavaScript-driven content — WeasyPrint does not execute
  JS; pre-render with a headless browser first.

## Quick start

```bash
python {baseDir}/scripts/render.py --html report.html --out report.pdf
python {baseDir}/scripts/render.py --html invoice.html --out invoice.pdf --page-size A4
```

The script accepts a local file path, a `file://` URL, or an `http(s)://`
URL. CSS is loaded relative to the HTML location for local paths; for
URLs, the same fetch rules apply (network resources are loaded with
WeasyPrint's default fetcher).

## CSS Paged Media support

WeasyPrint implements the parts of CSS that matter for paged output:

- `@page` rule with `size`, `margin`, `@top-center`, `@bottom-right` boxes
- Page breaks: `page-break-before`, `page-break-after`, `break-inside: avoid`
- Counters: `counter(page)`, `counter(pages)`
- `prince-` properties: WeasyPrint supports many but not all PrinceXML
  extensions

Example header/footer setup:

```css
@page {
  size: Letter;
  margin: 1in;
  @top-center { content: "Q3 Review — Confidential"; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages); }
}
```

## Cross-platform install hints

WeasyPrint is pure Python but depends on native libraries. The OpenSquilla
install spec only triggers `pip install weasyprint`; the OS packages must
be installed separately.

### macOS

```bash
brew install pango cairo gdk-pixbuf libffi
```

### Debian/Ubuntu

```bash
sudo apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 \
    libharfbuzz0b libfontconfig1
```

### Windows

The simplest path is the GTK runtime via winget:

```powershell
winget install --id GTK.GTK3
```

Or use MSYS2's `mingw-w64-x86_64-pango` package and ensure its `bin/`
directory is on `PATH`. WeasyPrint ≥61 ships an alternate "lite" path that
bundles its own native libs on Windows; check WeasyPrint's installation
docs for the current state.

If the `render.py` script raises `OSError: cannot load library`, the
native libs are not on the search path — the user must install them per
the platform instructions above.

## Boundaries

- Does not execute JavaScript. Pre-render dynamic content with a headless
  browser first, then feed the resulting HTML to this skill.
- Does not support every CSS feature — flexbox and grid have known
  limitations in paged contexts. Test layout before relying on either.
- Font availability is OS-dependent. To guarantee reproducibility, embed
  fonts via `@font-face` with absolute paths or data URIs.
- For high-volume PDF generation (hundreds of documents per minute),
  prefer a service-grade renderer (PrinceXML, browser-based pipelines).
  WeasyPrint is the right tool for tens to a few hundred PDFs per run.
