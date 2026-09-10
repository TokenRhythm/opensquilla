# Experiment-only diagnostic event retirement

This notice replaces PR #1615's original inactive-work optimization scope.
The retirement is introduced by commit `15417f0f4` against the engine shared by
`main@09b21762a` and the original PR base. It removes producers and their exclusive
preparation, not just execution when no output path is configured.

## Retired outputs

| Selector in the runtime events file | Retired output |
| --- | --- |
| `feature=runtime_recovery` | `reasoning_prefill_recovery`, `reasoning_continuation_recovery`, `post_tool_empty_recovery`, `repeated_tool_call_recovery`, `source_loop_recovery` |
| `name=final_diff_contract.observed` | Final-diff experiment observations |
| `name=final_diff_salvage.*` | `applied`, `check_failed`, `apply_failed`, `time_budget_exhausted`, `vetoed_lost`, `vetoed_instrumentation` |
| `name=focused_verification.classified` | Experiment-only success/failure/unknown classification |

These outputs are absent even with `runtime_events_path` configured. No empty
or replacement events are emitted. Historical files are not deleted or rewritten.

## Preserved behavior and configuration

Public fields, environment parsing, defaults and Parent/Child inheritance are
unchanged. `final_diff_contract_mode=log` remains accepted as an inactive value
for this mechanism; `warn_model` still checks and can warn once. Error termination
no longer performs the final-diff observation, but salvage still runs at its
existing lifecycle point. Recovery messages, retries, tool history, actual
verification decisions, mutation receipts and candidate/veto/apply behavior remain.

The generic event sink, `runtime_observer`, watchdog decision events, Provider
events and independent turn-call logs remain. Observer evidence still feeds
source-loop recovery and must not be treated as disposable serialization.
The previous PR's independent watchdog/repeat-call/derived-state lazy-work
changes are withdrawn; the unused failure-summary cache deletion remains.

## Experiment migration

Historical runs, manifests, decisions, scores, prompts and archives remain bound
to their pinned engines. Do not revise their delivery rules retroactively.
The audited frozen adapter and scoring chain collect patches independently of
these events; this does not certify every live adapter or guarantee equal scores.

New main experiments must declare these diagnostics **retired/not provided**,
not zero firings. Some historical salvage delivery gates and recovery/final-diff
attribution analyses require the retired records and cannot be reused unchanged.
Use existing request/tool results, final patches and behavior tests only where
they actually prove delivery. Otherwise use the frozen engine or design a future
experiment with an appropriate evidence contract; missing events do not imply
successful delivery or that a mechanism did not run. No new runner or adapter
compatibility layer is introduced by this change.

Reverting the PR's merge restores the outputs. No configuration, user-file or
historical experiment data migration needs to be reversed.
