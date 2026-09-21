---
name: knowledge-local-research
description: Coordinate local Knowledge research and source-grounded analytical delivery. Use Knowledge MCP as the only factual source; load the finance, PDF/table, and report companion skills when the task needs them. Not internet research.
---

# Local Knowledge Research coordinator

This is the coordinator for research backed by the local Knowledge service. It
defines the investigation order and the hand-offs between the model, Knowledge
MCP, and the research sidecar. It does not turn Knowledge into an answer
generator: Knowledge returns source-grounded Evidence, and the agent must keep
source statements, its synthesis, and unresolved questions separate.

## Select the companion skills first

Read only the companions required by the request, before drafting:

- `skill_view(name="knowledge-research-finance")` for prices, earnings,
  financial statements, valuation, forecasts, market attribution, or any
  number whose unit, period, or actual/estimate status matters.
- `skill_view(name="knowledge-research-pdf-tables")` for PDF evidence,
  tables, charts, exhibits, footnotes, page inventory, or original table
  crops.
- `skill_view(name="knowledge-research-report")` when the user asks for a
  report, HTML/PDF output, exhibits, provenance, review, finalization, or
  publication.

Load more than one companion when the request crosses domains. If the task is
a short fact lookup, the companions and `mode="deep"` are unnecessary; use
`mode="standard"` only when the user explicitly wants a narrow quick lookup.

## Non-negotiable source boundary

Use Knowledge MCP as the only factual source. Do not use web search, memory,
shell output, generated text, or an unbound file as evidence. The gateway and
research bridge enforce the write and publication gates, but the model remains
responsible for choosing relevant sources and making a qualified judgment.
Consult `TOOLS.md` for exact schemas, cursors, grouping, retries, and recovery.

## Investigation sequence

1. Frame the question, relevant period, competing explanations, and the
   freshness boundary. Start a new `mcp_researchBegin` and preserve its
   `researchId`; resume only unfinished research.
2. Discover iteratively with focused `mcp_search` calls without
   `collectionIds`. Read promising results before expanding. Investigate
   disagreement and gaps, not only documents that support the first thesis.
3. Deepen important files with `mcp_searchByIds` using explicit file or scope
   selections, at most 20 refs per call. A per-call result limit is not a
   limit on how much of an important file may be examined.
4. Read the saved core evidence with `mcp_researchReadEvidence`, following
   continuations. A discovered but unread result is unexamined, not proof that
   the information is unavailable.
5. Before writing, complete the PDF/table work required by the companion
   skill. Do not let a review metadata lookup stand in for source inspection.

Keep a compact map of claims, supporting and opposing evidence, open gaps, and
the time boundary. Continue targeted discovery until another relevant source
could no longer materially change the conclusion; if a gap remains unresolved,
narrow the judgment and state the limit. For a broad corpus with at least 30
discovered source files, use at least 30 independent sources in substantive
claims before finalization. Explicitly read a core set of up to 24 files (at
least 12 for a 30-source bibliography), following continuations. Build 3–5
source-grounded data exhibits from distinct source files when the corpus is
broad, covering the comparisons, metrics, mechanisms, scenarios, or risks that
make the answer concrete. The sidecar enforces these adaptive bibliography,
reading, and exhibit targets; do not pad the bibliography or invent table
cells. Smaller investigations keep the ordinary source-grounded review
requirements without a forced bibliography or exhibit floor.

## Drafting discipline

Use one checkable idea per paragraph:

`claim -> specific support -> interpretation -> boundary`

Explain why sources agree or disagree. Do not infer causality from topical
similarity, turn an old forecast into a current fact, or manufacture a
probability, quote, target, trigger, or numerical decomposition. Add claims in
small complete batches only after the initial scoped reading and core evidence
reading are complete. Use stable `claimKey`/`batchKey` values and exact
`evidenceRefs`; wait for each receipt before the next write.

If a write returns `RESEARCH_PREPARATION_REQUIRED`, perform the missing scoped
reads, evidence continuations, or PDF inventory, then replay the unchanged
batch. Keep successful receipts and never hand-edit generated artifacts.

For a broad report, each exhibit must state the entity, metric, unit, period,
actual/estimate status and source, and explain what the comparison can and
cannot establish. A report with many citations but one generic table is not a
complete deep report; add the missing source-grounded exhibits or narrow the
judgment.

## Review boundary

Before finalization, challenge the strongest conclusion: check entity,
direction, date, horizon, numeric scale, conditions, and the strongest contrary
source. Correct or qualify claims with their current hash and review changed
groups again. A finished draft is not finished research, and a successful tool
call is not semantic verification.

Follow the report companion for artifact and publication rules. Final chat
should contain the answer, material limits, and usable links only; do not expose
internal IDs, private paths, raw provenance, or tool narration.
