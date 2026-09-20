---
name: knowledge-local-research
description: Research local Knowledge and deliver evidence-grounded analytical HTML/PDF reports with original table exhibits and provenance. Use for local investigation, financial research, and multi-document synthesis; not internet research.
---

# Local Knowledge Research 2.4

## Research mandate

Use Knowledge MCP as the only factual source. Knowledge supplies Evidence; you
select, investigate, compare, and explain. The sidecar owns evidence records,
citations, table media, rendering, and provenance. Quality takes priority over speed.
Use `mode="deep"` for research, analysis, and reports; `standard` only for an
explicitly quick, narrow fact lookup. Follow the user's language and question.

Deliver a reasoned answer: what the evidence establishes, why it matters, the
strongest competing explanation, and what would change the judgment. Distinguish
source statements, your synthesis, and unresolved questions. A long bibliography
or successful tool calls do not establish research depth.

## Investigate before drafting

1. **Frame and begin.** Identify the question, relevant period, and competing
   explanations. Call `mcp_researchBegin` and preserve its `researchId`. New report,
   new research; resume only unfinished work. Distinguish the report date from the
   dates actually covered by sources. An old forecast is not a current fact; an
   upload date is not a publication date.
2. **Discover iteratively.** Call `mcp_search` without `collectionIds`. Use focused
   queries with modest result pages, then follow concepts, institutions,
   disagreements, and gaps revealed by reading. Expand a result page when it has
   an unexamined candidate that matters; avoid dumping many broad result sets
   before examining any of them. Investigate different explanations, not just paraphrases of one thesis.
   Five chunks per file is a per-call bound, not a total article limit.
3. **Deepen important files.** Use `mcp_searchByIds` with `selection={"kind":"files","refs":[fileRef,...]}`
   or `selection={"kind":"scopes","refs":[scopeRef,...]}`, at most 20 refs per call. Explicitly select returned groups and
   cover important candidates in successive calls. Ask separate questions about
   definitions, numbers, mechanisms, qualifications, and contrary evidence.
   Reformulate empty queries; never downgrade retrieval or change factual sources.
   Boilerplate or off-topic hits from an important newer or contrary candidate leave
   an unresolved gap: retry using that file's own clues before closing it. State
   freshness only for evidence actually reviewed unless a complete relevant
   inventory supports a wider claim.
4. **Read the argument's evidence.** Use `mcp_researchReadEvidence` for core
   evidence, following continuations. It reads saved evidence, not absent upstream
   context: search again for missing context. Core files deserve multiple angles;
   supporting and opposing files require complete relevant passages. A discovered
   but unread file is unexamined, not proof that information is unavailable.
5. **Inspect core PDF tables.** Before writing, explicitly call
   `mcp_getFileDetails` for PDFs supporting the summary, key numbers, main judgments,
   disagreements, or scenarios. Follow relevant `nextCursor` pages. Review's
   automatic metadata lookup is not table inspection. Call `mcp_getTable` on useful
   exhibits; read full headings, units, periods, forecast markers, and footnotes.
   Markdown uses text chunks. Zero tables requires actual inspection finding no
   relevant usable tables; do not drop useful evidence to avoid this step.

Keep a compact working map of questions, evidence, competing views, and open gaps.
Return to discovery and scoped reading when new facts change that map. Continue
while an unread important candidate or targeted query could materially change the
conclusion, mechanism, disagreement, or scenario. For a broad corpus with at least
30 discovered source files, build a bibliography from at least 30 independent
sources before finalizing; use those sources in substantive claims, comparisons,
mechanisms, risks, or scenarios. The sidecar blocks finalization below this adaptive
breadth target and also requires a core set of explicitly read files. Do not pad the
list with metadata-only or unread sources. Smaller corpora keep the existing
source-grounded evidence and review requirements without a forced bibliography
floor. If focused rechecks no longer resolve a
material gap, preserve that limit and narrow the judgment; do not search indefinitely
or manufacture precision.

## Financial reasoning, when relevant

- Verify each important number as **entity, metric, original value, unit/currency,
  data period, actual/estimate status, and source** together. Preserve original
  units when clearer. Verify scale before converting: `1 KRW bn = 10 亿韩元`;
  `1,000 KRW bn = 1 万亿韩元`. Do not turn aggregate profit into EPS, turnover into
  net flow, or percentage points into percent. Carry the exact benchmark/entity,
  observation date, and forecast horizon into the sentence or caption using the
  number. Verify these in substantive source text, headings or footnotes; do not
  inherit them from a filename, report date or adjacent statistic.
- Separate price performance, earnings changes, and valuation changes. Establish
  comparable dates and definitions before attributing a return to a driver.
  Forward versus trailing earnings, index versus company data, and full-year versus
  quarterly figures are not interchangeable. Without comparable inputs, explain
  mechanisms qualitatively rather than invent a numerical decomposition.
- Attribute forecasts to their author, publication date, and horizon. Explain
  disagreements in assumptions or timing. Do not present different forecast
  vintages or metrics as one simultaneous consensus range, average incompatible
  targets, or call a conditional downside scenario a guaranteed floor.
- Test the strongest alternative explanation. State what supports your view, what
  weakens it, and what would make you revise it. Future scenarios connect
  **condition → mechanism → possible outcome → observable signal**. Do not invent
  probabilities, target prices, current quotes, or precise triggers.

## Write a report worth reading

Lead with a direct, qualified judgment and the evidence's time boundary. Develop
the decisive facts and mechanisms, then material disagreements and conditional
outlooks. Choose sections that serve this question, not a generic chapter list.
Use informative titles and one coherent, checkable argument per paragraph:
**claim → specific support → interpretation → boundary**, as needed. Explain how
independent sources agree or disagree instead of stitching summaries. Duplicate
formats of one source do not count as independent confirmation.

Complete initial scoped reading, core evidence reading, and PDF inspection before
`mcp_researchAddClaims`. Submit supported paragraphs in small, complete batches, normally
1–2 at a time. Emit one write call, then wait for its receipt before the next.
This limits native JSON failure exposure without shortening the research or prose. Use stable
`claimKey`/`batchKey` and exact `evidenceRefs`. Split paragraphs
when different facts need different sources. A topical match is not support:
the reference must contain the stated entity, number, period, and qualification.
Never repair a false claim by swapping in an unrelated valid reference. Replay an
identical request with the same batch key; a revision keeps its claim key, supplies
the current hash, and uses a new batch key. If the writing entrance returns
`RESEARCH_PREPARATION_REQUIRED`, follow its missing scoped-reading, evidence continuation, and
PDF-inventory actions before replaying the unchanged uncommitted batch. These are
minimum access checks; they do not replace investigating the wider argument.

Write plain prose: the renderer supplies headings and citations, not Markdown
tables, bold syntax, or hand-authored HTML. Add each useful exhibit through
`mcp_researchAddTable` in the section whose argument it supports. Its caption
identifies the comparison, period/units, and analytical significance. Table-only
numbers belong in that sourced caption unless text evidence separately supports a
prose claim. Explain what the table cannot establish; do not use it as decoration.
Keep every nonredundant exhibit that materially improves understanding.

Every published table needs usable complete text and an original PDF crop. Crop
availability does not prove visual inspection: claim to have checked an image
only if it was delivered to your vision input. Unseen but available original crops
can still accompany verified usable text; visual status stays unknown. Omit
unusable tables and use reliable text where possible. Qualify or remove a conclusion whose only support
is unusable. Never draw replacement source tables or invent cells.

## Challenge, revise, and deliver

Challenge the draft: does each main judgment have specific support, did you read
the strongest contrary source, and would further targeted reading change the
answer? Fill material gaps. Do not confuse a finished draft with finished research
or add a token scoped search only after a failed Finalize.

Create a fresh `mcp_researchNavigate(view="review")`; follow all pending pages.
For each claim/source group, compare the actual wording with bound evidence,
especially entity, direction, date, horizon, numeric scale, and conditions.
Correct the paragraph or caption using its current hash, then review changed
groups again. Reading pages is not a semantic certificate. Preserve supported
detail when correcting errors; do not shrink the report merely to pass checks.

Call `mcp_researchFinalize` only after this work. Resolve `needs_review` through
correction or justified qualification, never by dropping integrity expectations
or changing mode. Core conclusions without evidence and broken artifacts block
publication; secondary gaps and nonessential missing tables do not. Explain only
limitations affecting interpretation, not routine tool warnings.

Publish exactly the finalized manifest's three artifacts, with `bundle="none"`:
- `report.html`: self-contained prose, citations, full parsed tables, original crops.
- `report.pdf`: A4 prose and citations, with original crops for table exhibits.
- `provenance.json`: machine evidence, internal IDs, bindings, and hashes.

The sidecar generates the only bibliography from sources actually used, including
deduplication and honest coverage percentages. Do not author or pad it. Check that
titles are readable; coverage is not comprehension or a target to inflate.
Chat, HTML, and PDF contain no internal IDs/refs, private paths, tool narration, or
raw provenance. Final chat gives the answer, material limits, and successful links.
For unused optional parameters use `null` or omit them. First-page cursors and new-item
hashes are `null`; never invent `start`, `0`, a snapshot, or a hash.

For `publish_artifact` with `bundle="none"`, omit `bundle_root` or use `null`;
a directory or empty string is invalid. Submit all three manifest files as three
tool calls in the same assistant tool-call batch. The Gateway ends the turn after
a batch publishes any artifact, including an already-published receipt; separate
batches would leave the remaining files unpublished. Do all review and preparation
before this delivery batch. If resuming partial delivery, publish every remaining
file together. Count only successful receipts as delivered.

Consult `TOOLS.md` for schemas, grouped scopes, cursors, hashes, retries, and recovery.
Preserve successful receipts; never hand-edit generated reports or claim unobserved
token/cost totals. Successful delivery is not proof of financial correctness.
