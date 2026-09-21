# Knowledge Research Tool Notes

Use the installed `knowledge-local-research` Skill for the workflow and recovery
rules. These notes explain tool outputs, not additional research quotas.

- Returned `scopeRefs`, `fileRefs`, `evidenceRefs`, and `tableRef` are research-local
  lookup handles; copy namespaces intact. They are not credentials, bibliographic
  facts, or proof of reading. Use the exposed schema and exact returned mappings.
- `mcp_search` interleaves files, at most five chunks per file.
  `mcp_searchByIds` ranks within the selected files; a grouping response performs
  no search. Neither results nor scope membership prove full-file reading.
- `mcp_researchNavigate` cursors resume a fixed snapshot, not a refreshed directory.
  `view="report"` returns keys/hashes and table mappings without paragraph prose.
  Use it for current CAS hashes of items absent from pending review.
  Fresh `view="review"` (no cursor) returns pending groups only, using item and
  source hashes: new, unfinished, or changed items and items whose sources changed.
  Before the first fresh review, metadata preparation is automatic but best-effort.
  Metadata warnings mark incomplete attempts, not source verification; explicitly
  retry `mcp_getFileDetails` if needed. Unknown metadata alone implies neither bad
  OCR nor a need for whole-report rereading.
  Completed unchanged groups carry forward; changed attribution can reopen affected
  groups. Follow every pending page and fragment, then use a fresh view after edits,
  not the old snapshot or a whole-report reread.
- Review groups supply claim text with `claimKey`, `claimHash`, and `evidenceRefs`;
  exact bound evidence with `evidenceRef`, title, locator, and `forClaimItem` linkage;
  and table text with caption, page, and `tableHash`. Projection covers submitted
  content and bound excerpts, not confirmed model reading or semantic
  verification. No pending groups means no pending projection, not adequate research.
- `mcp_researchReadEvidence` reads saved canonical evidence with continuations,
  not new upstream context. `mcp_getFileDetails` inventories detected tables;
  inventory completeness does not establish PDF extraction completeness.
  `mcp_getTable` preserves available table text and original crop metadata;
  materializing a crop does not mean the model saw it. HTML retains original
  screenshots and full tables. PDF retains research prose and citations, but tables
  use only original screenshots, not redrawn HTML tables. Both are self-contained.
  Select tables by analytical contribution, not a representative-image limit or quota.
- Progress: `discoveredFileCount` means discovery. Search/scoped call counts are
  committed ledger calls; successful counts describe verified receipts, not useful
  findings. Grouping, cache replays, and unrecorded uncertain calls are excluded.
  `searchByIdsSelectedFileCount` counts selected files, not files read.
  `completeEvidenceProjectionCount` and `filesWithCompleteEvidenceProjectionCount`
  count prepared evidence ranges, not whole-file reading or comprehension.
  `modelDelivery="unknown"` must not be reinterpreted as successful reading.
- Finalize's `needs_review` response is the lightweight evaluation stage: it lists
  actionable gate failures, sets `optimizationRequired=true`, and gives the next
  step to optimize, run `researchNavigate(view="review")` again, and retry
  finalize. No manifest is produced until the response is clear. `finalized`
  permits manifest publication but does not certify source semantics.
  Optional expected-item lists cover submitted items only.
- Tool receipts cannot observe model failures before execution or complete provider
  token/cost accounting. Do not fill missing usage with zero or call it a billed total.

Scoped search uses one required object: `selection={"kind":"files","refs":[fileRef,...]}`
or `selection={"kind":"scopes","refs":[scopeRef,...]}`. Copy returned references; do not
supply separate fileRefs/scopeRefs arguments. An oversized scope returns groups to select.
Unused optional fields may be omitted or null. Use null for first-page cursors and
new-item hashes; continuation cursors and revision hashes must be exact returned values.
