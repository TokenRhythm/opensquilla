# WeasyPrint reference

The CSS subset that matters for paged output, common pitfalls, and
platform notes. Full docs: <https://doc.courtbouillon.org/weasyprint/>.

## What WeasyPrint does well

- Static HTML + CSS → high-fidelity PDF
- CSS Paged Media: `@page`, headers/footers, page numbering
- Tables, lists, basic flexbox, grid (with caveats)
- `@font-face` with TTF/OTF/WOFF
- SVG inline and as `<img>`
- Bookmarks / outlines from headings
- Clickable hyperlinks (internal `#anchor` and external)

## What WeasyPrint does NOT do

- Execute JavaScript. Render JS-heavy pages with a headless browser
  first, then feed the resulting static HTML here.
- Streaming output. The full document is built in memory before being
  written.
- Real-time printing. WeasyPrint is for offline rendering, not live
  preview.

## CSS @page essentials

```css
@page {
  size: Letter;          /* or A4, A3, Legal, "8.5in 11in", landscape */
  margin: 1in;
}

@page :first {
  margin-top: 0.5in;     /* tighter top on cover page */
}

@page {
  @top-center { content: "Document Title"; font-size: 9pt; }
  @bottom-right {
    content: "Page " counter(page) " of " counter(pages);
    font-size: 9pt;
  }
}
```

## Page breaks

```css
.section-break    { page-break-before: always; }
.keep-together   { break-inside: avoid; }
h1, h2           { break-after: avoid; }   /* keep heading with next text */
```

`break-*` is the modern syntax; `page-break-*` still works for backward
compatibility with older docs.

## Embedded fonts

System fonts work but are not portable. To guarantee reproducible output,
embed:

```css
@font-face {
  font-family: 'Inter';
  src: url('./fonts/Inter-Regular.woff2') format('woff2');
  font-weight: normal;
}
body { font-family: 'Inter', system-ui, sans-serif; }
```

Paths are relative to the HTML file when rendering from disk; for URL
rendering, paths must resolve to fetchable URLs.

## Common pitfalls

- **`position: fixed` does not work** the way it does in browsers.
  Use `@page` margin boxes for headers/footers instead.
- **Flexbox layouts with intrinsic widths sometimes misalign**. Set
  explicit widths for direct flex children when layout matters.
- **Background colors do not print by default** in some contexts. Add
  `-webkit-print-color-adjust: exact;` if porting from a browser CSS.
- **Cairo + Windows + emoji**: emoji glyphs from system fonts may not
  render correctly on Windows. Use a font with explicit emoji support
  (e.g., Noto Color Emoji) embedded via `@font-face`.
- **Large images** can blow up PDF size. Resize and re-encode (`pillow`)
  before rendering.

## Performance

- Single-document render: 200ms–2s typical, depending on size.
- Cold start (first import): ~1s for WeasyPrint to load native libs.
- Memory: roughly proportional to image content. Documents with hundreds
  of high-resolution images can exceed 1GB peak.

## Platform-specific notes

### macOS

WeasyPrint links against the Pango stack via Homebrew. After
`brew install pango cairo gdk-pixbuf libffi`, WeasyPrint imports cleanly.
On Apple Silicon, ensure Homebrew's `lib/` is on `DYLD_LIBRARY_PATH` if
you see "library not loaded" errors.

### Linux

`apt-get install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b
libfontconfig1` covers Debian/Ubuntu. RHEL/Fedora use `dnf install pango
cairo`. Container builds should add these to the base image.

### Windows

Two paths:

1. GTK runtime: `winget install GTK.GTK3` (preferred, simplest).
2. MSYS2: `pacman -S mingw-w64-x86_64-pango mingw-w64-x86_64-cairo
   mingw-w64-x86_64-gdk-pixbuf2`, then add MSYS2's `mingw64\bin` to
   `PATH`.

WeasyPrint ≥61 added a "lite" path bundling its own native libs on
Windows; check the install guide for the current state before adding the
GTK dependency.
