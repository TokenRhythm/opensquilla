---
name: paper-citation-integrity-gate
description: "Deterministic citation-count and provenance gate for meta-paper-write. Checks the citation_map summary against the numeric paper citation target without trusting an LLM verdict."
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

# Paper citation integrity gate

Internal deterministic gate used after `citation_map`. It parses the requested
integer citation target and the map's machine-readable `SUMMARY`, then blocks
when distinct cited keys are below target or any cited entry is invalid or
weak. Unused bibliography entries are reported as a warning, not a blocker.
