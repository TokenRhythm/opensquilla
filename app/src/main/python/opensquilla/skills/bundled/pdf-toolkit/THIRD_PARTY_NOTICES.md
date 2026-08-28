# Third-party notices for `pdf-toolkit` skill

Source: ClawHub `pdf` (<https://clawhub.ai/pdf>, MIT-0).

## Runtime dependencies

This skill requires:

- `pypdf` (BSD-3-Clause license,
  <https://github.com/py-pdf/pypdf>) for structural reads and writes
- `pdfplumber` (MIT license, <https://github.com/jsvine/pdfplumber>) for
  text and table extraction (already in OpenSquilla default dependencies)
- `reportlab` (BSD-3-Clause license,
  <https://www.reportlab.com/>) for PDF generation

## Scope vs `nano-pdf`

This skill ships alongside the existing `nano-pdf` bundled skill but does
not replace it. `nano-pdf` wraps a natural-language LLM rewriter; this skill
wraps deterministic structural operations. Trigger words and descriptions
were chosen to keep the two from competing in skill retrieval.

## License

The ClawHub source is MIT-0. The OpenSquilla project license is Apache-2.0.
The runtime dependencies carry their own permissive licenses (BSD-3-Clause and
MIT respectively).
