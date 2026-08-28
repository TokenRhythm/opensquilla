---
name: awesome-webpage-research
description: "Single-pass mini research for AwesomeWebpageMetaSkill: produce a short cited topic brief from one bounded web-search round. Not a general deep-research replacement."
description_zh: "为AwesomeWebpageMetaSkill提供单轮迷你调研：通过一轮有限的网页搜索生成带引用的简短主题简报。并非通用的深度研究替代方案。"
homepage: ""
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: medium
    capabilities: [network-read]
    requires:
      bins: []
      env: []
---

# Awesome Webpage Research (mini)

Lightweight, single-pass topic research used only by `AwesomeWebpageMetaSkill`.
Produce a concise topic brief with a short citation list so the page planner has
factual anchors. This is **not** a replacement for the bundled `deep-research`
skill; do not invoke it for general literature reviews or multi-round
investigations.

## Inputs

The caller supplies:

- `question`: the topic, audience, language, style, and webpage context to
  research.

## Protocol

Single round. Three steps. No iteration, no plan.json, no state file.

1. **Identify 3-5 focused sub-questions** that the page planner needs answered
   to ground page sections. Cover what the topic is, why it matters, key facts,
   common misconceptions, and at most one stat or date anchor. Do not exceed 5
   sub-questions.
2. **Run one bounded web-search round**: at most one search query per
   sub-question. Use `web-search` for every language. Prefer recent, reputable
   sources. Stop at one usable source per sub-question; do not chase broader
   coverage and do not run a second round.
3. **Compile a single brief** (target ~300-500 words) containing:
   - one paragraph topic summary
   - one paragraph "key facts" with inline `[1]`-style citation tags
   - a `Sources` list of 3-5 entries formatted as `[n] Title — URL`
   - a final `Page anchors:` line listing 3-5 short phrases that can become
     section headings or callouts on the webpage

## Rules

- Do not iterate. One search round, one synthesis pass. Return as soon as the
  brief is written.
- Do not fetch full articles. Use search snippets; perform at most one quick
  fetch per sub-question if a snippet is unusable.
- Do not invent citations. Every `[n]` must map to a source in the `Sources`
  list. If a source cannot be cited, drop the claim instead of fabricating one.
- Do not produce a multi-page literature review. Cap the brief at ~500 words.
- Do not search for images, audio, or video media here; media acquisition is
  handled by other meta-skill steps.
- Do not call `deep-research`, `summarize`, or any meta-skill from this step.
- If the search round returns nothing usable, prepend a single line
  `RESEARCH_THIN` to the brief and continue with a question-only summary
  without inventing citations or sources.

## Output

Return only the brief text. No methodology notes, no per-source commentary,
no JSON wrapper. The caller will truncate this output before feeding it to
the page planner.
