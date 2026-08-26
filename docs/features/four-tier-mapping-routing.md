# Four-tier mapping routing

`four_tier_mapping` is an opt-in, isolated
`llm_ensemble.selection_mode` implementing the state machine in
Single-Model-Routing v2. It does not replace or alter `router_dynamic`,
`router_tree_baseline`, static B5, custom B5, or direct routing.

Only the two local classifiers are mocked in this version. Intent and tier
predictions come from request-stable pseudo-random functions; task boundaries,
durable state, context selection, single-model dispatch, settlement, usage and
billing use the real runtime path.

## Enable the mode

```toml
[llm_ensemble]
enabled = true
mode = "single"
selection_mode = "four_tier_mapping"

[llm_ensemble.four_tier_mapping]
mock_seed = 20260826 # optional
default_new_task_tier = "c1"
```

`four_tier_mapping` is the only accepted selection-mode and config-subtree
spelling; development spellings are not compatibility aliases. Disabling the
ensemble may park this single-mode config without deleting it; re-enabling
restores the same mode.

The ladder is immutable:

| Tier | Provider model | Reasoning | Deployment label |
| --- | --- | --- | --- |
| C0 | `openrouter:qwen/qwen3.7-flash` | Thinking (`high`) | `qwen3.7-flash-thinking` |
| C1 | `openrouter:deepseek/deepseek-v4-flash` | `max` | `deepseek-v4-flash-0731` |
| C2 | `openrouter:deepseek/deepseek-v4-pro` | `max` | `deepseek-v4-pro-0813` |
| C3 | `openrouter:z-ai/glm-5.3` | `max` | `glm-5.3` |

All four tier keys, provider/model identities, reasoning levels and deployment
labels are frozen. Config validation rejects overrides, including mutation of
the nested mapping after load. The v1 mock config migrates to v2 by removing
the obsolete process-local state limit and adding the frozen deployment
labels.

The current OpenRouter model IDs for C1 and C2 are aliases rather than dated
IDs. The route records the requested deployment label and
`deployment_version_attested=false`; the label alone is not proof that the
provider served revision 0731 or 0813. Exact revision attestation requires a
provider/catalog identity that exposes it.

## State-machine rules

1. Explicit new-task and regenerate controls are deterministic rules. They do
   not run the intent mock. Recognized command-like text such as `/new`,
   `/redo`, “新建任务” and “重新生成” follows the same rule path.
2. With no active durable task, the request starts a new task.
3. `continue` keeps the current tier and does not run the tier mock.
4. `redo` runs the tier mock but may only keep or upgrade the current tier. A
   predicted downgrade is blocked and becomes a hold.
5. `new_task` applies the task-reset mask and selects from C0–C3 again.
6. Intent classifier error or uncertainty falls back to `continue`. Redo tier
   error or uncertainty holds the current tier. New-task tier error or
   uncertainty uses `default_new_task_tier` (C1 by default).

`continue` and `redo` keep the same task ID and task-scoped generation
context. `new_task` creates a new task ID and removes unrelated prior-task
context from generation. The classifier feature history is separately bounded
to the task-start user message plus the latest three user messages; this limit
does not truncate the model's task-scoped generation transcript.

The runtime persists task state and the task-start input-message anchor. The
router itself owns no process-local session state, so worker restarts and
multi-worker scheduling do not silently create a new task. State transitions
are committed with optimistic version checks, and accepted request identity is
used to prevent duplicate classification/execution.

Every accepted four-tier-mapping input owns its own classification, durable input
anchor and provider execution. `queueMode=collect` is therefore normalized to
an independent `followup`; four-tier-mapping inputs and queued candidates are
never coalesced. Other routing modes retain the existing collect behavior.

Web regenerate is a private, one-shot Gateway control. It is accepted only
after the parent route is committed and settled, the replacement text exactly
matches its durable user anchor, and the prefix fork can be committed
atomically with the child input/task/receipt. The prefix planner keeps an
in-memory parent-to-child message-ID map so a multi-turn redo uses the child's
task-start anchor without adding parent identifiers to transcript
`turn_context`; an ambiguous or missing mapping fails before acceptance. A
first-turn redo keeps the task and tier with zero prior history, and binds the
new child input as its task start. Only a fixed whitelist of Web-owned metadata
crosses into `TurnRunner`; request-controlled provenance cannot forge redo,
and all redo controls are removed from cached envelopes, collected prompts and
promoted follow-ups.

The Web RPC sends the canonical control value
`routingControl.mode="four_tier_mapping"`. Development spellings are rejected
rather than normalized at this boundary.

The two routing config subtrees are snapshotted before the durable acceptance
transaction. A settings write between commit and runtime activation therefore
cannot change the strategy of an already accepted turn.

## Mock feature contract

The production design defines one shared 413-dimensional feature vector for
both classifiers. This implementation does **not** construct a zero/scaffold
vector and does not claim that BGE, TF-IDF, PCA, SVD or LightGBM ran. It records:

- `feature_schema_version=fixed-four-tier-v2-features-mock-v2`;
- `feature_vector_dim=413`;
- `feature_vector_status=mock_not_materialized`;
- the missing mask for context, usage, prior execution and attachment metadata;
- a content-free attachment count plus normalized
  `document/image/audio/video/archive/other` modalities;
- truncation flags for the current request, task history and previous answer.

Prior Provider usage and prior route execution are validated as independent
bundles. Route status may exist when the assistant row has no `turn_usage`; in
that case `missing.execution=false` but `missing.usage=true`. A partial usage
bundle is cleared rather than filling absent token/cache fields with zero.
Attachment metadata follows the same all-or-nothing rule: every attachment must
provide a usable media/type field or the entire count-and-modality feature group
is zeroed and `missing.attachment_metadata=true`. No attachments is a complete
empty bundle with the missing mask unset.
Names, bodies, encoded bytes and storage identifiers never enter classifier
input or the route trace.

Until the frozen tokenizer exists, each classifier text segment uses a
documented 2,040-character proxy for the future 510-token limit. Oversized
segments use deterministic 3:1 head/tail truncation. Task history retains the
task-start message and at most the latest three user messages. The new-task
mask clears old history, previous response/usage and old route state while
retaining the current request, attachment facts and original missing mask.

The random mocks are deterministic for `effective_mock_seed + classifier
snapshot`. Session ID, request ID, input-message ID and explicit control event
are excluded from classifier input, so queue order and identity do not change a
prediction for identical semantic pre-route state. When `mock_seed` is omitted,
a system-random effective seed is created for the router instance and recorded
in the decision trace.

## Dispatch and failure behavior

The selected tier activates exactly one provider/model. The runtime checks the
catalog entry, credential/provider resolution, reasoning support, tool/vision
capabilities and context capacity before generation. The fixed route owns the
model selection; a per-turn model override is not an escape hatch into another
routing path.

Preflight, health admission or provider failure may be retried only within the
same selected deployment according to the provider's normal request policy.
There is no model fallback, no second proposer, no aggregator and no automatic
tier change. A terminal failure is settled and returned as a failure.

## Durable audit and settlement

The decision trace records `mode="four_tier_mapping"`.
`turn.metadata.fixed_four_tier_v2_decision` and the durable decision row carry
the route/task/request/execution identities, classifier source and run status,
probabilities, final state transition, policy/schema versions, selected and
executed deployment, preflight/dispatch outcome, attempts, response identity,
Provider usage, normalized token/cache/cost buckets and terminal status.

Raw prompt/history/answer text is not copied into the route trace. The feature
input audit stores durable input/task-start message references, SHA-256 hashes
for the current request, retained history segments, aggregate task history and
previous answer, normalized attachment modalities, plus the missing/truncation
masks. A classifier that did not
run records `prediction=null`, `probabilities=null`, `confidence=null` and
`run_status=not_run` rather than fabricated values.

State payloads and staged decision traces are schema-checked during
rehydration. Invalid labels, source/run-status conflicts, malformed
probabilities, wrong feature contracts, impossible downgrade/transition state,
bad hashes and inconsistent masks fail closed.

## Current limitation and replacement seam

This is a production-shaped routing and accounting path with mock
classification. It is not yet the trained local-classifier release: no 413
feature artifacts or vectors are emitted for training, and the character proxy
is not equivalent to the frozen BGE tokenizer. The `IntentClassifier` and
`TierClassifier` seams can later be backed by the frozen BGE/TF-IDF/LightGBM
pipeline without changing the routing state machine or selection-mode
boundary.
