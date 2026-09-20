---
name: knowledge-research-report
description: Produce, review, finalize, and publish source-grounded Knowledge research reports with HTML, PDF, tables, and provenance. Load with knowledge-local-research when the user requests a report or artifacts.
---

# Report and publication companion

Use this companion only after `knowledge-local-research` has framed the question
and the required finance/PDF companions have been loaded. The sidecar renders
the report and bibliography from verified research state; the agent must not
hand-author a parallel bibliography or HTML report.

## Claims and exhibits

Write one checkable argument per paragraph:

`claim -> specific support -> interpretation -> boundary`

Complete initial scoped reading, core evidence reading, and required PDF/table
inspection before calling `mcp_researchAddClaims`. Submit supported paragraphs
in small complete batches, normally one or two claims. Use stable
`claimKey`/`batchKey` values and exact `evidenceRefs`; wait for the receipt
before the next write. An identical retry reuses the batch key. A revision keeps
the claim key, supplies the current hash, and uses a new batch key.

Add useful exhibits through `mcp_researchAddTable` in the section whose argument
they support. Captions must identify the comparison, period/units, analytical
significance, and any limit. The sidecar requires usable complete table text and
the original PDF crop; it does not treat crop availability as proof of visual
inspection. The final report shows per-source reading coverage and a coverage
overview with the measured-source count, overall coverage, median coverage, and
low-coverage count. These are indexed-text projection measurements, not proof
of model comprehension or a target to inflate.

## Review and finalization

Create a fresh `mcp_researchNavigate(view="review")` and follow all pending
pages. Compare the actual wording of each claim/source group with its bound
evidence, especially entity, direction, date, horizon, numeric scale, and
conditions. Correct or qualify claims using the current hash and review changed
groups again. Resolve `needs_review` through correction or justified
qualification; never remove integrity checks or change mode to make a draft
pass.

Call `mcp_researchFinalize` only after review. Core conclusions without
evidence and broken artifacts block publication; secondary gaps and optional
missing tables can remain as stated limitations.

## Exact delivery contract

Publish exactly the finalized manifest's three artifacts in one tool-call batch,
each with `bundle="none"`:

- `report.html`: self-contained prose, citations, full parsed tables, and
  original crops.
- `report.pdf`: A4 prose and citations with original crops for table exhibits.
- `provenance.json`: machine evidence, internal IDs, bindings, and hashes.

Omit `bundle_root` or use `null`; an empty string is invalid. If delivery is
partial, publish every remaining manifest file together. Count only successful
receipts. The generated bibliography includes only sources actually used and
deduplicates them; coverage is not comprehension and is never a target to
inflate.

Chat, HTML, and PDF contain no internal refs, private paths, raw provenance, or
tool narration. Final chat gives usable links and material limits. Keep all
receipts and never hand-edit generated reports.
