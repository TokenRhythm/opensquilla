---
name: paper-refbib-stub
description: "Convert normalized multi-search-engine results to minimal BibTeX, preserving real DOI, author, publication year, arXiv ID, source URL, and provenance metadata when available. Unknown years are omitted and duplicate DOI records collapse to one entry."
description_zh: "将规范化的 multi-search-engine 结果转换为最小 BibTeX；在可用时保留真实 DOI、作者、出版年份、arXiv ID、来源 URL 和溯源元数据。未知年份不写入，重复 DOI 合并为一条。"
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

The converter consumes optional normalized `doi`, `authors`,
`corporate_authors`, and `year` fields, while retaining URL-based DOI/arXiv
detection for older producers. Corporate authors are protected with nested
BibTeX braces so institution names are not rearranged as personal names.
It never invents a publication year: missing or malformed years are omitted.
DOIs are normalized case-insensitively, and repeated DOI records emit only
the first entry so citation keys stay unique and deterministic.

All search-provided text fields are treated as untrusted plain text. Control
characters, backticks, embedded BibTeX entry fragments, backslashes, and
unbalanced braces are neutralized before field emission; structural URL
characters are percent-encoded without discarding Unicode locators. This keeps
truncated search snippets from corrupting the generated BibTeX database.
