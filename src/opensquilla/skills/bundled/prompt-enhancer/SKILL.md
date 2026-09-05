---
name: prompt-enhancer
description: Enhance a rough user instruction into a structured prompt with role, task type, checks, deliverables, and anti-patterns
description_zh: "将粗糙的用户指令增强为结构化提示词，包含角色、任务类型识别、预检清单、交付要求与反模式。"
always: false
triggers:
  - enhance prompt
  - improve prompt
  - structure prompt
  - 优化提示词
  - 增强提示
  - 增强提示词
  - 结构化指令
  - 改写提示词
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  upstream_url: https://github.com/opensquilla/opensquilla
  maintained_by: OpenSquilla
---

# Prompt Enhancer Skill

When the user asks to enhance, improve, or structure a prompt (or a rough instruction they want to turn into a better prompt), rewrite it into a structured prompt following the framework below. Return only the enhanced prompt — no commentary, no preamble.

## Framework

1. **Identify the task type** — one of: analysis, design, implementation, review, writing, translation, query, orchestration.
   - Translation: conversion between languages.
   - Query: find a factual answer; no new content generated.
   - Review/audit: judge existing output.
   - Analysis: break down data, facts, or phenomena.
   - Design: produce a plan, architecture, or proposal (no implementation).
   - Writing: produce human-readable prose.
   - Orchestration: coordinate multi-step, cross-session work.
   - Implementation: land a concrete artifact (code/file/config). (Default fallback.)

2. **Enhance the body** — strip leading imperatives ("please", "帮我", "请") so the objective reads cleanly; keep the user's original intent verbatim.

3. **Add a role** (only when the input implies one — never fabricate a role):
   - Code/programming → senior software engineer
   - Copywriting/marketing → copywriter and marketing strategist
   - Translation → professional translator and localisation specialist
   - Summarize/condense → rigorous analyst
   - Plan/architecture → senior solutions architect
   - Data/analytics → senior data analyst

4. **Add task-type scaffolding** — checks, deliverables, and anti-patterns:

### Analysis
- Checks: Is the data/source trustworthy and are definitions consistent? Does every conclusion have a data point behind it?
- Deliverables: comparison table or conclusion with an evidence chain.
- Avoid: listing data without drawing conclusions; concluding from a single data point.

### Design
- Checks: Is the constraint list (cost/time/tech/compatibility) complete? Were alternatives researched?
- Deliverables: at least 2 candidate options with a comparison table; a clear recommendation and a fallback path.
- Avoid: offering a single option with no fallback; adjectives where metrics are needed.

### Implementation
- Checks: What is the target environment and its dependencies? How will you verify it works?
- Deliverables: a runnable artifact (code/file/config); a verification step or self-test.
- Avoid: handing over a snippet without saying how to run it; claiming done without verifying.

### Review
- Checks: Is the review standard/checklist explicit? What is in scope?
- Deliverables: an issue list (location + severity + fix suggestion); a pass/fail verdict.
- Avoid: reporting problems without suggesting fixes; spreading evenly without prioritising.

### Writing
- Checks: Who is the audience and what is the format? What is the length limit and tone?
- Deliverables: a well-structured draft tailored to the audience and ready to ship.
- Avoid: padding with filler; inventing sources or data.

### Translation
- Checks: Source and target languages? Domain terminology glossary? Formal or casual register?
- Deliverables: the translation with a terminology glossary (if applicable); explicit markers where meaning is uncertain.
- Avoid: word-for-word translation; inconsistent terminology.

### Query
- Checks: Do the preconditions hold (time/place/definition)? Is the source reliable?
- Deliverables: a direct answer with its source.
- Avoid: padding with "according to search results…"; fabricating a source.

### Orchestration
- Checks: What is the current state, dependencies, and blockers? Which independently deliverable steps can the task be split into?
- Deliverables: a visible step list with dependencies; a suggested next action.
- Avoid: relying on memory instead of written records; letting the goal drift.

5. **Add universal output requirements**:
   - Give the final answer directly; do not restate the task.
   - Use clear structure with paragraphs or lists where helpful.
   - If information is missing, ask one precise question instead of guessing.

6. **Match the user's language** — produce the enhanced prompt in the same language as the user's input (Chinese input → Chinese scaffolding; English input → English scaffolding).

Never auto-send: this skill only rewrites the prompt text; the user decides whether to send.
