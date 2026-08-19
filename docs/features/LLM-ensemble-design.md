# LLM Ensemble Design: Static Lineups & Dynamic Router Selection

`llm_ensemble` runs a **B5 fusion** turn: several *proposer* models each draft
an answer, and one *aggregator* model fuses those drafts into the final
response. This document describes how the **set of models** is chosen for a
turn. It does not cover the ensemble runtime mechanics (streaming, timeouts,
quorum, fallback) — only model selection.

## Why an ensemble instead of a single model

Any single model has a fixed set of blind spots: the failure modes of its
training data, its decoding randomness, and its particular biases. Asking that
one model again doesn't remove them — it re-rolls the *same* distribution. An
ensemble attacks the problem from a different angle: draft an answer with
several *different* models, then have an aggregator reconcile them. The wins
over a single-model turn:

- **Error cancellation / higher accuracy.** Independent models rarely make the
  *same* mistake on the same input. When drafts disagree, the aggregator can
  cross-check them and keep the answer the majority supports; when they agree,
  that agreement is real signal that the answer is solid. Idiosyncratic
  one-off errors get outvoted instead of shipped.
- **Coverage through diversity.** Different vendors/families/architectures have
  genuinely different strengths — one is better at code, another at long-form
  reasoning, another at careful instruction-following. A lineup that spans them
  covers more of the input space than any single model's strong suit. This is
  exactly why selection *rewards diversity* (distinct vendor/family/
  architecture) rather than picking the top-N by raw quality.
- **Robustness & availability.** A single model is a single point of failure —
  one timeout, rate-limit, or degraded response fails the whole turn. With a
  quorum of proposers the turn still succeeds as long as *enough* drafts come
  back, and the aggregator simply fuses what arrived.
- **Reduced variance.** Fusing several drafts smooths out per-call sampling
  noise, so repeated runs of the same prompt are more stable and less
  sensitive to an unlucky roll of the dice on any one model.
- **A critic pass, not just a vote.** The aggregator is a model in its own
  right: it can spot a draft that's confidently wrong, prefer the
  better-reasoned answer over the more verbose one, and synthesize the best
  parts of several drafts — a step a single-model turn never gets.

The cost is real — an N-proposer turn spends roughly N+1 model calls and its
latency is bounded by the slowest proposer plus the aggregator. The selection
strategies below exist to spend that budget well: match ensemble size and
composition to how hard the turn actually is, rather than always paying for the
largest lineup. Lower-difficulty turns get a small, cheap lineup; harder turns
get more proposers and stronger critics.

## Selection strategies

There are three selection strategies, dispatched by
`llm_ensemble.selection_mode` in
`build_ensemble_provider_from_config`
(`src/opensquilla/provider/ensemble.py`):

| `selection_mode` | Family | Status |
|------------------|--------|--------|
| `static_openrouter_b5` | Static lineup | Default for fresh configs |
| `static_tokenrhythm_b5` | Static lineup | Supported |
| `custom_b5` | Static lineup (user-authored) | Supported |
| `router_dynamic` | Dynamic selection | Legacy |

The first two families are **static**: the lineup is fixed ahead of the turn,
either from a packaged preset or from an explicit user-authored list. The last
is **dynamic**: the lineup is scored and assembled per turn from the router's
own tier decision.

Fresh configs default to `static_openrouter_b5`. The Web UI offers only the
static families (preset + custom); `router_dynamic` is no longer offered there
and stored configs surface a one-click migration to `custom_b5`. Direct
TOML/RPC configuration keeps working for every mode.

---

# Part 1 — Static Lineups (current design)

A static lineup is fixed before the turn runs: a set of proposer models plus one
aggregator model, all known ahead of time. No per-turn scoring happens. Two
variants share this shape:

- **Presets** — `static_openrouter_b5` / `static_tokenrhythm_b5`: packaged,
  hard-coded lineups on a single provider.
- **Custom** — `custom_b5`: an explicit user-authored lineup with
  role-labelled candidates and a single aggregator.

Both variants belong to the same **fixed-lineup defaults family**
(`is_static_b5` in the builder) and therefore inherit the same runtime defaults
(quorum, timeouts, no shuffle, quorum grace) — see
[Shared fixed-lineup defaults](#shared-fixed-lineup-defaults).

## 1.1 Static presets

Source: `_build_static_b5_members`, `STATIC_B5_PROFILES`
(`src/opensquilla/provider/ensemble.py`).

Each preset is a `StaticB5Profile` — four fixed proposers plus one aggregator,
all bound to a single provider:

| Profile | Provider | Proposers | Aggregator |
|---------|----------|-----------|------------|
| `static_openrouter_b5` | `openrouter` | `deepseek/deepseek-v4-pro`, `z-ai/glm-5.2`, `moonshotai/kimi-k2.7-code`, `qwen/qwen3.7-max` | `z-ai/glm-5.2` |
| `static_tokenrhythm_b5` | `tokenrhythm` | `deepseek-v4-pro`, `glm-5.2`, `kimi-k2.7-code`, `qwen3.7-max` | `glm-5.2` |

The TokenRhythm profile is a mirror of the OpenRouter one: same aggregation
shape and defaults, the same four models, only the provider and the model-id
naming differ (OpenRouter-style `vendor/model` slugs vs. TokenRhythm's bare
names).

`_build_static_b5_members` simply materializes the profile: each proposer model
becomes an `EnsembleMemberConfig` labeled `proposer_1..N`, the aggregator model
becomes one labeled `aggregator`, and the selection plan records the profile
name, proposer/aggregator models, and proposer count. There is nothing to score
— the lineup is the profile.

### Credential gate

`static_b5_credential_available` decides whether the ensemble may run: it
resolves an API key for every member (all four proposers + aggregator) using the
same key-resolution order as the runtime (see
[Member provider resolution](#member-provider-resolution)). A user whose active
provider differs but whose environment carries the profile provider's env key
(e.g. `OPENROUTER_API_KEY`, `TOKENRHYTHM_API_KEY`) is treated as opted in. If any
member cannot resolve a key, the ensemble is skipped rather than posting a turn
upstream with an empty bearer token.

## 1.2 Custom lineup (`custom_b5`)

Source: `_build_custom_b5_members`, `_custom_b5_candidates`
(`src/opensquilla/provider/ensemble.py`); schema `LlmEnsembleCandidateConfig`
(`src/opensquilla/gateway/config.py`).

`custom_b5` lets an operator author the lineup explicitly via
`llm_ensemble.candidates`. Each candidate row carries:

- **`provider`** / **`model`** — required, non-empty; provider is lower-cased.
- **`role`** — exactly `proposer` or `aggregator`. Older advisory proposer
  aliases and unknown non-aggregator values normalize to `proposer`, so an old
  or hand-edited config never blocks boot or leaks internal labels into traces.
- **`enabled`** — disabled rows are kept for read compatibility but never
  counted or run.

Lineup assembly (`_build_custom_b5_members`):

1. Every enabled row whose role is `proposer` runs as a proposer. Internal
   request identity uses `proposer_N`; public traces display only `Proposer`.
2. The single row with role `aggregator` fuses the drafts. Proposer rows dedupe
   on `(provider, model)`; the aggregator row may legitimately reuse a
   proposer's model (a model both drafts and fuses).
3. **Fallback:** if no `aggregator` row exists, the aggregator falls back to the
   currently routed model — the same model the user would have gotten without
   the ensemble — so a proposer-only config still runs instead of erroring at
   turn time. The plan records `aggregator.source` as `candidate_role` or
   `inherited_model` accordingly.

### Lineup bounds & validation

Enforced by `LlmEnsembleConfig._validate_custom_b5_lineup`
(`src/opensquilla/gateway/config.py`), checked **only** when
`selection_mode == "custom_b5"` (presets carry fixed lineups; `router_dynamic`
selects per turn):

- At most **one** enabled candidate may carry role `aggregator`.
- Enabled proposer count must stay within
  `[CUSTOM_B5_MIN_PROPOSERS=2, CUSTOM_B5_MAX_PROPOSERS=6]`.
- Total per-turn calls are capped at `CUSTOM_B5_MAX_TOTAL_CALLS=8`
  (proposers + aggregator).

### Readiness gate

`custom_b5_lineup_ready` returns `(ready, reason)` before wrapping the turn. It
fails closed with a machine-readable reason when the lineup is not runnable:
`no_proposers`, `unknown_provider:<p>`, or `missing_credential:<p>` (a member
whose provider requires a key but resolves none). This mirrors the static-preset
gate — a member with an empty bearer token would post the conversation upstream
unauthenticated, so the wrap is skipped.

## 1.3 Runtime policy and lineup defaults

C3 tier activation has source-aware defaults. When a newly configured C3 tier
activates the shared four-proposer plan and the corresponding fields are
absent, its effective policy is:

| Parameter | C3 tier-local default | Global/custom default |
|-----------|-----------------------|-----------------------|
| `min_successful_proposers` | 1 (one of four) | 1 |
| `target_successful_proposers` | unset (same as minimum) | unset (same as minimum) |
| `proposer_max_retries` | 1 | 0 |
| `all_failed_policy` | `fallback_single` | `fallback_single` |

The C3 defaults fill only missing fields. Explicit global/custom values are
authoritative, including `min_successful_proposers = 4`,
`proposer_max_retries = 0` or `2`, and `all_failed_policy = "error"`. The
`error` policy returns a terminal Ensemble error and does not start the fixed
provider; only `fallback_single` does. Existing global/custom configurations
without an explicit retry field retain the historical zero-retry behavior.

Timeout defaults depend on the lineup family rather than the C3 policy source:

| Lineup family | Proposer total timeout | Aggregator idle timeout |
|---------------|------------------------|-------------------------|
| Packaged static B5 | 120 seconds | 180 seconds |
| Operator-authored `custom_b5` | 300 seconds | 480 seconds |
| Legacy `router_dynamic` | 3600 seconds | 3600 seconds |

Packaged and custom fixed lineups default to deterministic candidate ordering
(`shuffle_candidates = false`). Explicit runtime-policy values and timeout
overrides are recorded alongside their effective values in the selection plan
for diagnostics.

The two timeout fields have intentionally different streaming semantics.
`proposer_timeout_seconds` bounds each proposer's total execution time.
`aggregator_timeout_seconds` is an idle/stall budget between upstream stream
events, so an active aggregation may run longer than that value while a silent
provider is still bounded. Host-generated keep-alive heartbeats do not reset
the aggregator budget. This field is not the turn's total wall-clock deadline:
the outer Agent/TaskRuntime deadline remains authoritative for the complete
turn. A completed aggregator idle budget is terminal for that attempt and is
never retried as another full-budget aggregator request.

`min_successful_proposers` is additionally clamped down to the actual proposer
count. The historical ten-second fixed-lineup quorum-grace value remains in
diagnostics for compatibility, but it no longer cancels or completes proposer
work early; its effective runtime value is zero.

For score-oriented runs, `target_successful_proposers` separates the preferred
draft count from the minimum acceptance floor. For example, `target = 4` and
`min = 3` waits for all four fixed B5 proposers. If one proposer exhausts its
configured retries, the remaining three may still be aggregated; two or fewer
still follow `all_failed_policy`. A target above the fixed four-member lineup,
or above a custom lineup's proposer count, is rejected as a configuration
error.

`proposer_max_retries` is the number of in-place attempts after the initial
request. When non-zero, transient 429/5xx/transport failures, blank completed
outputs, and `length` completions without visible candidate text are retried.
Every attempt receives its own `proposer_timeout_seconds` budget. The accepted
candidate body comes only from the last successful attempt, while trace and
usage accounting retain every started attempt, including zero-usage rows marked
`usage_receipt_missing = true`.

The aggregator already retries bounded transient upstream failures in place.
It also retries a stream that ends before `DoneEvent` when that attempt has
emitted no text, reasoning, or tool delta, reusing the completed proposer
drafts. Once any user-visible delta has been emitted, the attempt is terminal
so a retry cannot duplicate text or tool activity downstream.
When an aggregator exhausts its idle budget without user-visible output,
`fallback_single` makes exactly one request with the original conversation;
it does not retry the timed-out aggregator or rerun proposers. The `error`
policy remains terminal.

Proposers never own an executable tool boundary. By default they receive no
current tool schemas. Setting `proposer_tools = true` exposes those schemas only
as advisory vocabulary: native or textual tool-shaped output is converted into
bounded, untrusted candidate text. The aggregator may use that information, but
must independently issue any real tool call through the normal registry,
permission, approval, and sandbox checks.

## 1.4 Member provider resolution

Every member (static or custom) resolves its concrete `ProviderConfig` through
`_member_provider_config`, which layers member intent over the inherited/routed
provider config:

- **API key** — a member-level `api_key_env` env var if set; else the inherited
  key when the member's provider matches the active provider; else the provider
  registry's env key (e.g. `OPENROUTER_API_KEY`).
- **`base_url`** — member override, else the inherited base URL (same provider)
  or the provider spec's default base URL.
- **`proxy` / `org_id` / `provider_routing`** — inherited only when the member
  shares the active provider; otherwise reset.

This is what lets a static/custom lineup run against a provider the user isn't
actively routing to, as long as that provider's credential is present in the
environment.

## 1.5 Configuration surface

```toml
[llm_ensemble]
enabled = true
selection_mode = "static_openrouter_b5"   # or static_tokenrhythm_b5 / custom_b5
```

Custom lineup:

```toml
[llm_ensemble]
enabled = true
selection_mode = "custom_b5"

[[llm_ensemble.candidates]]
provider = "openrouter"
model = "deepseek/deepseek-v4-pro"

[[llm_ensemble.candidates]]
provider = "openrouter"
model = "z-ai/glm-5.2"

[[llm_ensemble.candidates]]
provider = "openrouter"
model = "z-ai/glm-5.2"
role = "aggregator"
```

Static presets expose no lineup tuning — the models are fixed in code. Custom
lineups are tuned entirely through the `candidates` list (subject to the bounds
above). They share the same runtime mechanism, while packaged static profiles
use 120/180-second proposer/aggregator defaults and custom lineups use 300/480
seconds. Operators may explicitly configure `min_successful_proposers`,
`target_successful_proposers`, `proposer_max_retries`, `all_failed_policy`,
`proposer_timeout_seconds`, `aggregator_timeout_seconds`, and
`shuffle_candidates`.

A resilient score-oriented fixed-lineup posture is:

```toml
[llm_ensemble]
enabled = true
selection_mode = "static_openrouter_b5"
min_successful_proposers = 3
target_successful_proposers = 4
proposer_max_retries = 2
```

Four successful drafts are preferred and three are accepted after retries. If
the lineup cannot reach quorum, or if the aggregator cannot complete, the turn
uses the provider and model configured in `[llm]` as the fixed fallback when
`all_failed_policy = "fallback_single"`. With `all_failed_policy = "error"`,
the same failure is terminal and the fixed provider is not called.

---

# Part 2 — `router_dynamic` Selection (legacy)

> **Status: legacy.** `router_dynamic` remains fully supported for existing
> configs but is no longer offered in the Web UI. Stored `router_dynamic`
> configs surface a one-click migration to `custom_b5`. Direct TOML/RPC
> configuration keeps working as described below.

`router_dynamic` is the dynamic model-selection strategy: instead of a fixed
lineup, it picks proposers and the aggregator **per turn**, driven by
SquillaRouter's tier decision for that turn. Enable it with
`llm_ensemble.selection_mode = "router_dynamic"`.

Source: `src/opensquilla/provider/ensemble.py`
(`_candidate_pool`, `_score_dynamic_candidate`, `_select_dynamic_candidate`,
`_build_router_dynamic_members`).

## 2.1 Why dynamic selection

A fixed proposer/aggregator list can't adapt to the model actually chosen for a
turn, and forces operators to hand-tune which models pair well together at each
router tier. `router_dynamic` instead:

- reuses the model SquillaRouter already picked for the turn as the **anchor**
  proposer, so the ensemble never contradicts the router's own tier decision;
- fills the remaining proposer slots and the aggregator slot by scoring a pool
  of candidate models against a per-tier "slot template";
- penalizes re-selecting a model that's already in the ensemble, so proposers
  stay diverse instead of collapsing onto a few high-quality models.

## 2.2 Inputs

`_build_router_dynamic_members` takes three things:

1. **`inherited_provider_config`** — the provider/model SquillaRouter already
   resolved for this turn (becomes the anchor).
2. **`turn_metadata`** — carries `routed_tier` (`c0`–`c3`), `routing_confidence`
   (0.0–1.0), and `routing_extra` (`final_tier`/`base_tier` fallbacks used if
   `routed_tier` is missing). Defaults to tier `c1` if nothing usable is found.
3. **`config`** — `llm_ensemble.model_options` and `squilla_router.tiers`,
   used to build the candidate pool.

## 2.3 Candidate pool

`_candidate_pool` assembles a deduplicated list of `(provider, model)`
candidates, in this order:

1. **Router anchor** — the inherited provider/model (`source="router_anchor"`).
   This is always `pool[0]` and always becomes the first proposer.
2. **`llm_ensemble.model_options`** — the operator-configured candidate list
   (`source="model_options"`). If a model string contains `/` it's assumed to
   be an OpenRouter-style id and routed via `openrouter`; otherwise it inherits
   the anchor's provider.
3. **`squilla_router.tiers[*].model`** — every model configured for a
   SquillaRouter tier (`source="router_tier:<tier>"`), so tier-specific models
   the operator has wired into the router are eligible even if not listed in
   `model_options`.

Each candidate is annotated with priors from `_DYNAMIC_MODEL_CATALOG` — a
built-in table of ~14 known models with `tier`, `quality` (0–1), `cost_latency`
(0–1, higher = cheaper/faster), `family`, `vendor`, and `architecture`. Models
not in the catalog fall back to tier-average priors (`_tier_quality_prior`,
`_tier_cost_latency_prior`) derived from the model string or tier hint.

## 2.4 Slot templates

Each router tier maps to an ordered list of proposer "slots"
(`_DYNAMIC_TIER_SLOTS`):

| Tier | Slots |
|------|-------|
| `c0` | `anchor`, `cheap_contrast` |
| `c1` | `anchor`, `balanced_contrast` |
| `c2` | `anchor`, `adjacent_tier_check`, `orthogonal_family` |
| `c3` | `anchor`, `strong_critic`, `orthogonal_family`, `fast_sanity` |

Lower tiers (cheap/simple turns) get a small, cost-biased ensemble; higher
tiers (hard turns) get more proposers with slots biased toward quality and
contrast. The `anchor` slot is always filled by the router's own model and is
never scored — it's taken as-is.

Each tier also maps to an aggregator slot (`_DYNAMIC_AGGREGATOR_SLOT`):
`c0→aggregator_fast`, `c1→aggregator_balanced`, `c2`/`c3→aggregator_strong`.

## 2.5 Scoring a candidate for a slot

For every non-anchor slot, every pool candidate is scored and the best one is
selected (`_select_dynamic_candidate` → `_score_dynamic_candidate`):

```
score = weights.quality   * quality_prior
      + weights.affinity  * router_affinity_score
      + weights.diversity * diversity_score
      + weights.cost      * cost_latency_prior
      + weights.role      * role_match_score(slot)
      - duplicate_penalty
```

Each slot has its own weight vector (`_DYNAMIC_SLOT_WEIGHTS`), e.g.
`cheap_contrast` weights `cost` and `role` heavily and `affinity` lightly,
while `strong_critic` weights `quality` and `role` heavily and `cost` almost
not at all.

### Score components

- **`router_affinity_score`** — how close the candidate's tier prior is to the
  turn's `routed_tier`, scaled by `routing_confidence`. Low router confidence
  relaxes tier matching instead of forcing a brittle lock, since a
  low-confidence route is itself uncertain about the right tier.
- **`diversity_score`** — rewards a candidate whose family/vendor/provider/
  tier/architecture aren't already represented among the proposers picked so
  far in this turn (checked incrementally, slot by slot).
- **`role_match_score`** — slot-specific logic (see below), combining tier
  targeting, contrast against the anchor, quality, or cost depending on what
  that slot is supposed to contribute.
- **`duplicate_penalty`** — `_DYNAMIC_SELECTED_PENALTY[slot] * times_already_selected`.
  Selecting the same `(provider, model)` again is allowed but costs
  increasingly more as the same model keeps winning slots.

### Role match by slot

`_role_match_score` differs by slot — this is where each slot's intent is
actually encoded:

- **`cheap_contrast`** — favors tier `c0`/`c1`, contrast with the anchor, and
  cost/latency. A cheap "second opinion."
- **`balanced_contrast`** — favors tier `c1`/`c2`, contrast, and quality.
- **`adjacent_tier_check`** — favors a tier one step above/below the routed
  tier (`adjacent_distance == 1`), plus quality. Checks whether a
  slightly-different-strength model agrees.
- **`orthogonal_family`** — favors contrast and diversity above all — a
  model from a different vendor/family/architecture than the anchor.
- **`strong_critic`** — favors tier `c3` and quality heavily — the strongest
  available model as a critic, used only at higher tiers.
- **`fast_sanity`** — favors tier `c0`/`c1` and cost/latency — a fast,
  cheap sanity check, used only at `c3`.
- **`aggregator_fast` / `aggregator_balanced` / `aggregator_strong`** — each
  balances tier targeting and quality differently; `aggregator_strong`
  weights quality highest and cost lowest, since the aggregator's output is
  the final response.

### Tie-breaking

Candidates are sorted by `(score, quality_prior, cost_latency_prior,
-pool_index)` descending, so ties fall back to higher quality, then higher
cost/latency score, then earlier pool position (closer to the anchor/operator-
configured list) wins.

## 2.6 Selection order

`_build_router_dynamic_members` runs slots in the tier's template order:

1. `anchor` — taken directly, no scoring.
2. Remaining proposer slots, in order — each selection is added to `selected`
   and `selected_counts` before the next slot is scored, so later slots see
   updated diversity/duplicate state.
3. The aggregator slot, scored last, against the same accumulated `selected`
   state as the proposers (so it also gets a duplicate penalty if it repeats
   a proposer's model).

## 2.7 Output

The function returns `(profile_name, proposers, aggregator, selection_plan)`:

- `profile_name` — `"router_dynamic/<tier>"`, e.g. `"router_dynamic/c2"`.
- `proposers` — one `EnsembleMemberConfig` per slot, labeled by slot name
  (`anchor`, `cheap_contrast`, ...).
- `aggregator` — one `EnsembleMemberConfig`, labeled `aggregator`.
- `selection_plan` — a full trace for observability, including the resolved
  tier/confidence, the anchor, the slot template, per-slot score breakdowns
  (`_score_trace`, including the top-3 scored candidates per slot for
  debugging near-misses), the aggregator's score breakdown, the full
  candidate pool, and `duplicate_policy: "selected_penalty"`.

`build_ensemble_provider_from_config` (the public entrypoint) additionally
clamps `min_successful_proposers` down to `len(proposers)` if the configured
value exceeds how many proposer slots the tier's template actually produced
— e.g. configuring `min_successful_proposers=4` at tier `c0` (2 slots) yields
an effective minimum of 2. Both the configured and effective values are
recorded in `selection_plan` for debugging.

For OpenRouter-compatible calls, the local LLM trace emits an additive
`llm.response_headers` event when a valid `x-generation-id` response header
arrives. The event stores only that value in `response_ids`; arbitrary response
headers are never retained. This preserves a physical attempt's billing
identity when a request later errors, is cancelled, or ends without a response
body. It does not change the request payload, prompt, model, tools, retry
policy, or fallback behavior.

## 2.8 Configuration surface

```toml
[llm_ensemble]
enabled = true
selection_mode = "router_dynamic"
```

What operators can tune:

- `llm_ensemble.model_options` — extends the candidate pool beyond the router
  anchor and configured router tiers.
- `llm_ensemble.min_successful_proposers` — desired minimum successful
  proposers (clamped per-turn as described above).
- `llm_ensemble.target_successful_proposers` — preferred successful proposer
  count above the minimum floor (clamped per-turn for dynamic templates).
- `llm_ensemble.proposer_max_retries` — opt-in per-proposer retries; zero keeps
  the historical single-attempt behavior for global/custom activation. When a
  C3 tier activates the plan and this field is absent, its tier-local default
  is one retry.
- `squilla_router.tiers[*].model` — indirectly expands the candidate pool and
  determines which model becomes the anchor for a given tier.

There is no operator control over slot templates, weights, or the model
catalog priors — those are fixed in code. Unlike the static families,
`router_dynamic` keeps the legacy runtime defaults (timeouts 3600s,
`shuffle_candidates=True`, `min_successful_proposers=1`) for global activation.
The source-aware C3 retry default described above still applies when C3 is what
activates the plan.
