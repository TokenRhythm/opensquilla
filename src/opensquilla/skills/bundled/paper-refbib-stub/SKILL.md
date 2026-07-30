---
name: paper-refbib-stub
description: "Convert multi-search-engine JSON to a minimal BibTeX file (@misc{} entries). Demo-only."
description_zh: "将multi-search-engine的JSON转换为最小化的BibTeX文件（@misc{}条目）。仅供演示。"
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  {
    "platform": {
      "emoji": "📚",
      "requires": { "anyBins": ["python", "python3"] }
    }
  }
entrypoint:
  command: python {baseDir}/scripts/json_to_bib.py
  args:
    - --out
    - "paper/references.bib"
  stdin: "{{ outputs.search_papers }}"
  parse: text
  timeout: 10
---

# paper-refbib-stub

Reads a `multi-search-engine` JSON document on stdin and emits a BibTeX file
of `@misc{}` entries keyed `ref1`, `ref2`, ... Caller wires the upstream
search output via `entrypoint.stdin`.
