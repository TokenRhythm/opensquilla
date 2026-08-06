---
name: paper-source-readiness-gate
description: "Deterministic early source-coverage gate for meta-paper-write. Stops drafting before expensive section generation when the curated, verifiable bibliography cannot meet the numeric citation target."
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

# Paper source readiness gate

Internal deterministic gate used immediately after source curation and before
experiment design, outlining, or section writing. The input JSON contains the
paper contract, paper preferences, structured source pack, and generated
bibliography.

The command exits non-zero when the citation target is not an integer, source
curation reports insufficient coverage, the usable-reference count is below
target, or usable keys are absent from the primary-reference region or the
bibliography. Failure output includes the concrete found/required count.
