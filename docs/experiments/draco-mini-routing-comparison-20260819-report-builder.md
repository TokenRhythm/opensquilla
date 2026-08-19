# Failure-aware DRACO final report

This builder is offline-only. It imports validation, causal resume-selection,
summary, and accounting helpers from the frozen OpenSquilla checkout; reads
sealed artifacts; and writes `EXPERIMENT_RESULTS.md`. It never invokes a runner
entry point, provider, Judge, model, or network API.

## Primary scientific contract

The frozen experiment has 60 observed group×task rows and 57 scored rows. The
only admissible unscored outcomes are these three native failures:

- `B2/f004b46b-c0e7-4e86-a072-c7491328d538`
- `B4/f004b46b-c0e7-4e86-a072-c7491328d538`
- `S4/f004b46b-c0e7-4e86-a072-c7491328d538`

The frozen accounting/error policy is:

| Arm | Attempt terminal pattern | Actual requests | Unknown | Recorded LLM$ |
|---|---|---:|---:|---:|
| B2 | strict quorum counts `1,2,2`; aggregation never starts | 12 | 1 | 0.534564759 |
| B4 | same large-input/no-visible-response error three times | 35 | 0 | 6.865969500000003 |
| S4 | large-input/no-visible-response, then two empty responses | 25 | 0 | 0.20786312800000004 |

The stock resume-state reason arrays are also exact, ordered allowlists. B4 and
S4 require generation reasons `generation_error, empty_final_text`; B2 requires
those plus the audited aggregator/quorum terminal reasons ending in
`insufficient_b2_configured_quorum`. All three require Judge reasons
`judge_incomplete, judge_errors, missing_quality_total`; B4/S4 cost reasons are
empty, while B2 requires the two missing-actual-aggregator identity backfills
plus `cost_metadata_incomplete`. Audit and fatal-policy reasons must be empty.
An extra or reordered reason aborts the report.

Each must independently prove the exact frozen fingerprint, prior attempts 0,
budget used/limit/count 3/3/3, remaining budget 0, attempt ordinals 1/2/3,
unique attempt IDs,
selected generation failure, empty final text, missing Judge and quality, zero
selected requests/cost, exact terminal error sequence, and the frozen actual
request/unknown/cost accounting. Any different key, error, budget shape,
fingerprint, Judge state, score, or accounting value is fatal.

The report presents all of the following without conflating them:

- per-arm observed, scored, and completion counts;
- scored-only AvgQ over completed Judge rows;
- failure-adjusted AvgQ, where an execution failure has operational utility
  `U=0` and the per-arm denominator remains 10;
- primary task-paired ΔU and bootstrap CI at n=10;
- a diagnostic common complete-case analysis and ranking where f004 is removed
  from every arm, so every comparison uses the same nine tasks;
- selected LLM spend, which excludes failed generation attempts;
- actual LLM spend, which retains every attempt from all three failures; and
- a quality matrix whose three failed cells say `EXEC_FAIL`, never a fabricated
  Judge score.

No fresh replacement is required or used by the primary command. A legacy B2
replacement interface remains parsed as a fail-closed compatibility path for
explicitly labelled post-hoc sensitivity and never changes the 57-row primary
score set.

## Required inputs

All inputs must be immutable terminal copies:

1. `--repo-root`: the exact frozen checkout that produced the artifacts.
2. `--input`: the exact ordered ten-row DRACO Mini JSONL.
3. `--expected-manifest`: the frozen compatibility manifest; its raw SHA must
   match one of the validated causal primary manifests. Its stamped
   `experiment-config.effective.json` and `experiment-config.resolution.json`
   sibling artifacts are also required for the authenticated B2 validator
   correction below.
4. Repeated `--results-jsonl`: the initial result followed by every genuine
   causal resume wave in order. Do not concatenate files and do not use
   last-row-wins.
5. `--output`: the final Markdown path, unless `--validate-only` is used.

The original dirty experiment worktree was retired after completion. Its base
commit, binary Git patch, override, and launcher are preserved under
`/home/codex/draco-runs/draco-mini-routing-comparison-20260819/source-snapshot/`.
Reconstruct that frozen checkout before using it as `--repo-root`; do not point
the validator at a later, merely similar working tree.

Example primary generation, with no replacement arguments:

```bash
PY=/home/codex/code/opensquilla-agentic-routing/.venv/bin/python
REPORTER=/ABS/PATH/generate_draco_direct_results.py

"$PY" "$REPORTER" \
  --repo-root /home/codex/code/opensquilla-draco-mini-routing-comparison-20260819 \
  --input /home/codex/code/opensquilla/data/draco/mini.jsonl \
  --expected-manifest /ABS/PATH/draco_run_INITIAL.manifest.json \
  --results-jsonl /ABS/PATH/draco_ensemble_INITIAL.jsonl \
  --results-jsonl /ABS/PATH/draco_ensemble_REAL_RESUME_2.jsonl \
  --output /ABS/PATH/EXPERIMENT_RESULTS.md
```

Omit nonexistent resume waves; include every real one in causal order. Replace
`--output` with `--validate-only` for a non-writing final check.

## Multi-wave recovery rules

The builder validates each result/trace/checkpoint/manifest seal, terminal
status, task universe, groups, fingerprints, and manifest hashes. It then calls
the same frozen resume loader over the same causally ordered paths at each
closed validator stage. It does not sum per-wave summaries or choose the last
JSONL row manually.

The final selected state must contain exactly three `regenerate` actions at the
allowlisted keys. Every other pair must be either `complete`, or the narrowly
admissible cost-only `metadata_only` state described below. A `judge_only`,
`audit_only`, additional `regenerate`, missing row, duplicate row, drifted
fingerprint, incomplete Judge, missing final text, or nonfinite quality aborts
report generation.

## B2 current-classifier defect

The frozen compatibility builder intentionally stores B2
`contract.experiment_config` as the compact object `{"sha256": ...}`. The
current resume classifier instead tries to read `ensemble` and `routing`
directly from that compact object. Uncorrected, all ten B2 rows acquire the
false reason `missing_expected_b2_ensemble_contract`: nine valid scored rows
become `regenerate`, while the real f004 failure gets that false reason instead
of the contract-backed `insufficient_b2_configured_quorum`.

The report builder resolves only the stamped effective/resolution artifacts
declared by `--expected-manifest`, rejects symlinks or basename drift, verifies
their canonical hash against both manifest benchmark-alignment records, and
reproduces the runner's exact compatibility projection (remove only inactive
`router_dynamic_ranking_override`, legacy `ensemble.proposer_backup_count`, and
scheduling-only runner/Judge concurrency). That projection hash must exactly
equal the compact B2 contract pin. Only then is a deep copy of the B2 validator
contract hydrated in memory and the stock loader rerun.

The allowed delta is closed: selected key/source/line/row SHA cannot change;
non-B2 states must be identical; the nine successful B2 rows must move from the
single false reason to exact `complete`; and B2/f004 must stay `regenerate` with
the false reason replaced by `insufficient_b2_configured_quorum`. Extra reasons,
surface/Judge/quality/fingerprint drift, a different config projection, or any
selection change aborts the report.

## G1 current-classifier defect

The current resume classifier omits the manifest-authenticated
`task_analyzer_execution_contract` in both `ensemble_call_core_reasons` and
several lifecycle calls to `g1_registry_contract_reasons`. This can falsely
classify otherwise complete G1 rows as generation-invalid.

The builder first proves that each affected G1 row has exactly that one false
generation reason and that every declared task-analyzer contract whole-equals
the manifest contract. It then temporarily wraps both validator functions,
injecting the expected contract only when the canonical G1 registry hash is an
exact match. Both functions are restored in `finally`. The patched classifier
must have no remaining generation reason, and selected source path, line, and
row SHA must remain unchanged. This offline validator-domain correction is
disclosed in the report and does not modify the worktree or result rows.

## Manifest-bound arm definitions

The Markdown arm-definition table is generated only after top-level
`group_specs`, compatibility-contract `group_spec`, and every selected row's
`provider_spec` whole-equal one another. B2's four proposer slots and OpenRouter
GLM 5.2 aggregator are additionally derived from the authenticated effective
config. S4 is accepted only with the exact frozen tier mapping `c0`
qwen/qwen3-8b, `c1` deepseek/deepseek-v4-flash, `c2` qwen/qwen3.7-plus, and
`c3` deepseek/deepseek-v4-pro. Thus the prose definitions are evidence-bound,
not free-standing labels.

## Relaxed cost-metadata audit

A surface-complete row may be admitted as `metadata_only` only when:

- `generation_valid=true` and `judge_complete=true`;
- generation, Judge, audit, and fatal-policy reason lists are empty;
- the cost reason list is exactly `['cost_metadata_incomplete']`; and
- row error, final text, attempt evidence, Judge, quality, and fingerprint pass
  the same strict gates as a `complete` row.

The recomputed row account must have top-level
`actual_llm_cost_complete=false`, `actual_llm_total.cost_complete=false`, and a
positive unknown-request count. The runner's LLM account schema does not carry
a `recorded_cost_is_lower_bound` field; the builder derives and reports the
lower-bound label from those real completeness fields. Unknown requests are
retained, never filled with zero, and every admitted pair is listed. All other
actions or reason values remain fatal. This exception allows the two known G1
cost-only rows to remain scored without pretending their cost coverage is
complete.

## Optional legacy B2 post-hoc sensitivity

The three replacement options are all-or-none:

- `--incident-replacement-results-jsonl`
- `--incident-only-group-task-keys`
- either `--write-incident-receipt` or `--incident-replacement-spec`

This path accepts only the frozen no-history B2 targeted protocol: resume-runner
manifest groups `B2,G1`, the original ordered ten-task universe, exactly one
scheduled/result row at B2/f004, zero G1 rows, prior attempts 0, fresh ordinals,
complete Judge/quality, and exact raw SHA bindings for only-keys, expected
manifest, runner, manifests, results, and canonical rows. Receipt creation uses
`O_CREAT|O_EXCL` mode 0600. The replacement is never passed as a causal
`--results-jsonl`, never overwrites the primary failure, and appears only in a
post-hoc sensitivity section.

The current primary rows do not serialize the legacy `physical_attempt_id`
field required by that receipt validator. Consequently the compatibility path
will reject these artifacts rather than weaken identity evidence; it is not an
available recovery mechanism for this report.

For backward receipt compatibility, that optional schema still contains the
legacy literal `analysis_policy.confirmatory=exclude_incident_cell`. The field
authorizes only the old replacement sensitivity contract; it does not select,
name, or alter the current primary failure-aware n=10 analysis.

The current 57/60 primary report should invoke none of these options.

## Local verification

The intended Python compatibility floor is 3.9, so the project Ruff invocation
sets that target explicitly instead of rewriting `timezone.utc` to the
Python-3.11-only `datetime.UTC`:

Run these commands from the repository root:

```bash
SCRIPT=scripts/experiments/generate_draco_direct_results.py
RUFF=.venv/bin/ruff
PYTHON=.venv/bin/python

"$RUFF" format --check --config pyproject.toml "$SCRIPT"
"$RUFF" check --config pyproject.toml --target-version py39 "$SCRIPT"
"$PYTHON" -m py_compile "$SCRIPT"
"$PYTHON" "$SCRIPT" --self-test
```

The self-test covers the exact three-key positive path; authenticated B2 config
projection/hydration and its reason-only delta; manifest-bound arm definitions;
57 scored rows; n=10 failure-aware and n=9 common-task paired analyses;
`EXEC_FAIL` rendering; selected-versus-actual spend semantics; admissible
cost-only metadata; and negative mutations for config/selection/surface drift,
budget, error sequence, Judge, quality, extra failure, request accounting,
non-cost metadata reasons, false cost completeness, S4 roster drift, and
receipt overwrite.
