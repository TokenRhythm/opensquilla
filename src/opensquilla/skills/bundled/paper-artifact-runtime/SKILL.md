---
name: paper-artifact-runtime
description: "Internal cross-platform artifact persistence, assembly, citation-audit, and PDF compilation runtime for meta-paper-write."
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
entrypoint:
  command: python
  args: ["{baseDir}/scripts/run.py"]
  stdin: "{{ with.payload }}"
  parse: text
  timeout: 120
---

# Paper artifact runtime

Internal deterministic runtime for `meta-paper-write`. It accepts one JSON
object on standard input with an `operation` field and performs exactly one of
these operations inside the orchestrator-owned workspace:

- `persist_sections`
- `assemble_manuscript_tex`
- `materialize_manuscript`
- `apply_length_expansion`
- `citation_map`
- `compile_pdf`

The runtime validates the runtime-owned MetaSkill run identifier, rejects
symlinked artifact roots and files, and keeps every artifact under
`paper/<meta_run_id>/`. PDF compilation invokes the managed `xelatex` and
`bibtex` executables with a platform-neutral argument vector, disables TeX
shell escape, applies paranoid Kpathsea file access, and verifies the real PDF
page count and final LaTeX quality log before returning success markers. Length
repair accepts at most one bounded body-only fragment per stable repair id,
rejects commands that could alter document boundaries, citations, or external
inputs, and applies it idempotently inside the run-owned manuscript.
