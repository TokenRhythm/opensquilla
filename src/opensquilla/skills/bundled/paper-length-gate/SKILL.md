---
name: paper-length-gate
description: "Deterministic artifact-backed manuscript readiness gate for meta-paper-write. Validates the workspace LaTeX artifact before compilation while leaving final page-count enforcement to compile_pdf."
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  requires:
    anyBins: ["python", "python3"]
entrypoint:
  command: python {baseDir}/scripts/audit.py
  stdin: "{{ with.payload }}"
  parse: text
  timeout: 30
---

# Paper length gate

Internal deterministic pre-compile readiness check. The JSON input contains a
paper contract and the compact manuscript manifest. For full manuscripts the
manifest must point to a non-empty `.tex` file inside the active workspace.
Compact and repair packages retain an inline-LaTeX compatibility path.

This gate checks document structure, citation presence, and a language-aware
minimum body scale that grows with `TARGET_PAGES`. Its report-only preflight is
used solely to drive one bounded authoring repair; a second fail-closed run must
pass before compilation. It does **not** claim that the requested number of
pages was produced. `compile_pdf` remains authoritative and counts pages from
the compiled PDF with `pypdf`.
