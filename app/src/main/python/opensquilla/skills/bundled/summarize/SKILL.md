---
name: summarize
description: Summarize, condense, or digest content
description_zh: "对内容进行总结、浓缩或摘要。"
always: false
triggers:
  - summarize
  - condense
  - digest
  - tldr
  - summary
provenance:
  origin: openclaw-derived
  license: MIT
  upstream_url: https://github.com/openclaw/openclaw
  maintained_by: OpenSquilla
---

# Summarize Skill

When the user asks to summarize, condense, or get a digest of content, provide a structured summary.

Format:
1. **Key Points** — 3-5 bullet points of the most important information
2. **Details** — Brief expansion on each key point if needed
3. **Action Items** — Any tasks or follow-ups identified (if applicable)

Keep summaries concise and focused on what matters most to the user.
