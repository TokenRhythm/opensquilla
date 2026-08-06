---
name: short-drama-review-normalizer
description: "Internal deterministic consent gate for meta-short-drama. Normalizes draft review and post-revision confirmation replies, and fails closed before external image/video generation."
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  requires:
    anyBins: ["python", "python3"]
  opensquilla:
    risk: low
    capabilities: []
entrypoint:
  command: python {baseDir}/scripts/normalize.py
  stdin: "{{ with.payload | tojson }}"
  parse: text
  timeout: 30
---

# short-drama-review-normalizer

Internal, deterministic consent boundary used by `meta-short-drama` after its
free-form draft review and before any image/video provider call. Its
`canonical_script_snapshot` phase also freezes the final in-memory script; it
never re-reads the user-editable `script.txt`.

The initial-review phase accepts the verbatim `review` string and emits a
bounded decision block. Explicit approval proceeds without overrides. A
recognizable, short-drama-specific adjustment emits `DECISION: revise` and is
preserved verbatim as `NEW_NOTES`; it never authorizes a provider call. After
the revised script is shown in a second `user_input` preview, the
`media_approval` phase requires a new, standalone explicit approval. Missing,
ambiguous, off-topic, or additional-edit replies hold; explicit cancellation
cancels.

Explicit external-transfer restrictions also hold, even when the same reply
contains an otherwise valid adjustment. This includes negated transfer verbs,
requirements that content remain on-device or local-only, and prohibitions on
an external recipient seeing, accessing, or receiving the content. Ordinary
local edits to a character, scene, shot, lighting, or style are not treated as
privacy restrictions without one of those explicit constraints.

This helper does not call a model or the network. The final `media_approval`
`DECISION` output is the sole authority for downstream paid media conditions;
a language model never gets to promote an edit or unclear reply to consent.
The snapshot phase requires exactly one final `DECISION: proceed|hold|cancel`,
accepts at most 200,000 UTF-8 bytes, and echoes the supplied script without
consulting disk. Only `proceed` can unlock the separate paid-step conditions.
