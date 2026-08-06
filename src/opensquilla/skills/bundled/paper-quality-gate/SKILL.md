---
name: paper-quality-gate
description: "Deterministic pre-compile gate for meta-paper-write. Enforces length/citation verdicts and rejects unsupported empirical-result claims when no user evidence was supplied."
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

# Paper quality gate

Internal deterministic gate used after bounded LaTeX sanitization and
immediately before compilation. Input is a JSON object containing
`paper_contract`, `length_gate`, `citation_gate`, and `manuscript_package`.
The preceding sanitizer may normalize safe punctuation and replace detected
forecast magnitudes with explicit placeholders, but this gate remains an
independent fail-closed audit of the resulting artifact.

The command exits non-zero when an upstream LLM gate blocks, when a required
verdict marker is absent, when the manuscript artifact cannot be read, or when
an evidence-free manuscript presents empirical findings as completed results.
It also rejects concrete English or Chinese predicted outcome numbers (for
example, an anticipated 12% improvement) when evidence is absent. Experimental
setup parameters remain allowed; a predefined metric target such as 50%
accuracy is also allowed when the unknown time-to-target remains nonnumeric.
Explicitly conditional claims and planned significance thresholds are not
treated as observed findings. Unknown outcomes must use `TBD` / `待实验确定`
instead of invented point estimates.

Every figure/table caption is audited independently when evidence is absent.
Categorical outcome claims such as "the proposed method achieves lower cost"
or "所提方法保持最低通信成本" are blocked. Neutral planned placeholders and
explicit hypotheses/future tests remain valid, including captions that state
concrete setup values.

The gate also rejects literal Unicode Greek math glyphs, Unicode en dashes, and
single Latin-style em dashes in the manuscript. Paired Chinese em dashes
(`——`) remain valid native-language punctuation. Authors should use named
LaTeX math macros such as `\(\alpha\)` / `\(\varepsilon\)` and LaTeX `--` /
`---` punctuation, avoiding silent missing-glyph degradation in XeLaTeX.
