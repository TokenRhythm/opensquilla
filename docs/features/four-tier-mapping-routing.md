# Four-tier mapping routing

`four_tier_mapping` is an opt-in, isolated
`llm_ensemble.selection_mode` implementing the state machine in
Single-Model-Routing v2. It does not replace or alter `router_dynamic`,
`router_tree_baseline`, static B5, custom B5, or direct routing.

Intent and tier predictions can come from one hash-pinned model set registered
by `routing-training-platform`. The model set owns both classifier heads and
the frozen preprocessing closure. A deterministic random backend remains only
for legacy configuration and state-machine tests. Task boundaries, durable
state, context selection, single-model dispatch, settlement, usage and billing
all use the same runtime path for both backends.

## Enable the mode

```toml
[llm_ensemble]
enabled = true
mode = "single"
selection_mode = "four_tier_mapping"

[llm_ensemble.four_tier_mapping]
default_new_task_tier = "c1"

[llm_ensemble.four_tier_mapping.classifier]
backend = "registered_model"
artifact_root = "/absolute/path/to/routing-artifacts"
metadata_db = "/absolute/path/to/metadata.sqlite3"
model_set_id = "router-lightgbm-example-a1"
# Replace this valid-shape placeholder with the registry's exact hash.
expected_manifest_hash = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
# Explicit candidate opt-in; remove it after the model reaches VALIDATED.
allow_candidate = true
```

The training package must be installed in the same Python environment as
OpenSquilla. `artifact_root`, `metadata_db`, `model_set_id`, and the expected
Manifest Hash are all required, so a typo or mutable/unregistered model fails
before routing. Every new v3 configuration must explicitly include the
`classifier` block; omitting it fails configuration validation instead of
silently selecting a random model. A `CANDIDATE` model is rejected unless
`allow_candidate=true` is explicit. That switch allows the candidate to drive
real provider dispatch; it is an operator opt-in for diagnostic evaluation,
not a release-gate or sandbox boundary. `REJECTED` and `DEPRECATED` are never
accepted. The metadata database is opened read-only, and status plus Manifest
Hash are rechecked before every prediction, including a cached joint
prediction. This evaluator follows the explicitly configured model ID and
hash; changing the registry's active pointer does not silently replace it.

Legacy mock configuration is still available for tests:

```toml
[llm_ensemble.four_tier_mapping.classifier]
backend = "random_mock"
seed = 20260826 # optional
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
the nested mapping after load. Old `mock_seed` v1/v2 configurations migrate to
the v3 `classifier.backend="random_mock"` shape without changing their seed.
An old config file that contained only the four-tier selection mode is migrated
once to an explicit random classifier so the formerly implicit behavior is
visible; newly constructed v3 configs do not receive that compatibility path.

The current OpenRouter model IDs for C1 and C2 are aliases rather than dated
IDs. The route records the requested deployment label and
`deployment_version_attested=false`; the label alone is not proof that the
provider served revision 0731 or 0813. Exact revision attestation requires a
provider/catalog identity that exposes it.

## State-machine rules

1. Explicit new-task and regenerate controls are deterministic rules. They do
   not use the intent head. Recognized command-like text such as `/new`,
   `/redo`, “新建任务” and “重新生成” follows the same rule path.
2. With no active durable task, the request starts a new task.
3. `continue` keeps the current tier and ignores the tier head.
4. `redo` uses the tier head but may only keep or upgrade the current tier. A
   predicted downgrade is blocked and becomes a hold.
5. `new_task` applies the task-reset mask and selects from C0–C3 again.
6. Intent classifier error or uncertainty falls back to `continue`. Redo tier
   error or uncertainty holds the current tier. New-task tier error or
   uncertainty uses `default_new_task_tier` (C1 by default).

`continue` and `redo` keep the same task ID and task-scoped generation
context. `new_task` creates a new task ID and removes unrelated prior-task
context from generation. Classification is separate: it always sees the true
route-before history, including up to four recent user inputs across task
boundaries. A `new_task` decision never rewrites or clears its own model input.

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

## Model input and feature contract

The registered backend passes the canonical `RouterInput` contract into the
training package's own feature runtime. It contains the raw current request,
route-before task anchor, four most recent cross-task user inputs, prior answer,
usage and outcome, active tier, five committed routes from the preceding
30-minute window, context estimates, available tools and sanitized attachment
metadata. It never includes the current prediction, benchmark score, best
model, cost result, current answer or future transcript rows.

If fixed routing is enabled after a conversation has started, the latest
route-before assistant row is still supplied even though it has no fixed-route
binding. A successful answer that asks the user for missing information is
marked `clarification`; failed/cancelled route settlement remains authoritative
as `failure`. For redo, the authenticated parent decision is explicitly the
newest route-history entry and the history is then capped at five.

The same loaded runner extracts one feature view and computes intent and tier
in parallel. Intent probabilities use `new_task/continue/redo`; tier labels are
normalized from `C0..C3` to the state machine's `c0..c3`. Policy gating does
not truncate or renormalize the four-class distribution.

The decision trace records the actual model input schema (`lightgbm_380.v1` or
`bert_text88.v1`), materialized numeric dimension, model-set ID, Manifest Hash,
artifact-closure Hash, runner/environment digests and registry status. The
registered runner uses INT8 BERT sessions for online inference and is closed
when its accepted configuration is replaced, when selection leaves this mode,
or when the service shuts down. Native inference is serialized on a dedicated
worker so it cannot exhaust the event loop's general blocking-work pool.

The legacy random backend continues to record:

- `feature_schema_version=fixed-four-tier-v2-features-mock-v2`;
- `feature_vector_dim=413`;
- `feature_vector_status=mock_not_materialized`.

Prior Provider usage and prior route execution are validated as independent
bundles. Route status may exist when the assistant row has no `turn_usage`; in
that case `missing.execution=false` but `missing.usage=true`. A partial usage
bundle is cleared rather than filling absent token/cache fields with zero.
Attachment metadata follows the same all-or-nothing rule: every attachment must
provide a usable media/type field or the entire count-and-modality feature group
is zeroed and `missing.attachment_metadata=true`. No attachments is a complete
empty bundle with the missing mask unset.
Names, encoded bytes and internal storage paths never enter the canonical
model input or route trace. Raw request/history/answer text is used only for
inference and remains in transcript storage; the trace persists hashes and
bounded metadata, not the text itself. Token truncation is owned by the frozen
training runtime rather than OpenSquilla's legacy character proxy.

The random mocks are deterministic for `effective_mock_seed + classifier
snapshot`. Session ID, request ID, input-message ID and explicit control event
are excluded from classifier input, so queue order and identity do not change a
prediction for identical semantic pre-route state. When `mock_seed` is omitted,
a system-random effective seed is created for the router instance and recorded
in the decision trace.

## Result-blind Benchmark worker

The headless entry point used by `routing-training-platform` is:

```bash
python -m opensquilla.engine.routing.benchmark_worker \
  --request /absolute/path/request.json \
  --output-dir /absolute/path/empty-output-directory
```

It accepts canonical route-before input, the complete production
`four_tier_mapping` config, and the public C0-C3 model mapping. It loads the
hash-pinned registered Router and runs `FixedFourTierV2Router`, but never
constructs an Agent, Provider, TurnRunner, or downstream LLM request. Its only
decision output is the selected tier/deployment for each `item_id`.

The formal v1 scope is `independent`: every input must have
`active_route_tier=null`, so the production no-active-task rule resolves intent
to `new_task` and only the tier head runs. This evaluates new-task tier
selection, confidence/margin fallback, policy configuration, and the frozen
C0-C3 mapping. It does not evaluate intent gating, `continue`, `redo`, or
cross-turn continuity. In particular, it does not cover production session
persistence, redo escalation, history keep/reset effects across turns,
Provider/cache continuity, downstream dispatch, or real LLM failure behavior.
The optional `episode` mode starts each synthetic episode from empty state and
is diagnostic only; it is not an attestation of a captured or counterfactual
live session.

Structured extension fields are scanned for result-derived data before the
Router is constructed. `previous_usage` is limited to the flat, complete
prior-turn token/latency and route-execution bundles; unknown, partial, nested,
negative, or boolean counter values are rejected. The result matrix is not an
input to this process.

`attestation.json` is written last and is the bundle commit marker. It binds
the input, semantic path-free config, model pool, decisions, policy, Router
artifacts/runtime, and OpenSquilla source closure. A sanitized same-UID local
subprocess reduces accidental exposure but is not a security isolation or a
release-grade attestation.

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

## Evaluation status

The result-blind worker executes the trained Router together with the
production state-machine rules for the declared replay scope. System replay is
currently diagnostic and is deliberately rejected by the release gate. A
future formal gate requires a trusted isolation boundary (for example a
separate UID or container), externally verifiable execution attestation, and,
for multi-turn claims, an approved state-before/episode input protocol.
