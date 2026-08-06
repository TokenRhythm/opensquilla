---
name: paper-delivery-summary
description: "Deterministic final delivery summary for meta-paper-write. Reports only verified PDF compilation fields and the exact citation-map SUMMARY statistics."
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  requires:
    anyBins: ["python", "python3"]
entrypoint:
  command: python {baseDir}/scripts/render.py
  stdin: "{{ with.payload }}"
  parse: text
  timeout: 30
---

# Paper delivery summary

Internal deterministic delivery formatter for `meta-paper-write`. It accepts
the paper contract, the runtime language instruction, `compile_pdf` output,
and `citation_map` output as JSON. It fails closed unless the PDF markers and
the complete, internally consistent citation `SUMMARY` are machine-readable.

The formatter never calls an LLM and never infers page or citation counts from
prose. Chinese and English delivery text is selected from the confirmed paper
language contract, cross-checked against the runtime language instruction.
