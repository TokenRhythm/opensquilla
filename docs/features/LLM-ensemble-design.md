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

There are five selection modes, dispatched by
`llm_ensemble.selection_mode` in
`build_ensemble_provider_from_config`
(`src/opensquilla/provider/ensemble.py`):

| `selection_mode` | Family | Status |
|------------------|--------|--------|
| `static_openrouter` | Static lineup | Default for fresh configs |
| `static_tokenrhythm` | Static lineup | Supported |
| `custom` | Static lineup (user-authored) | Supported |
| `router_dynamic` | Dynamic Step2 ranking | Supported (config-only) |
| `router_tree_baseline` | Frozen local-tree slot ranking | Supported (config-only benchmark) |

Legacy input values `static_openrouter_b5`, `static_tokenrhythm_b5`, and
`custom_b5` are migrated to the first three canonical values above. New TOML,
RPC responses, and UI writes use only the canonical values.

The first three modes are **static**: the lineup is fixed ahead of the turn,
either from a packaged preset or from an explicit user-authored list. The last
two are **dynamic**: they assemble a lineup per turn, but deliberately use
different algorithms. `router_dynamic` runs the current task-analyzer and
Step2 ranker; `router_tree_baseline` freezes the former slot-template selector
for apples-to-apples benchmark comparisons.

Fresh configs default to `static_openrouter`. The Web UI remains unchanged:
it offers the static families (preset + custom) and its existing
`router_dynamic` compatibility path. `router_tree_baseline` is selected
directly in TOML for benchmark runs; this change adds no Web UI surface.

---

# Part 1 — Static Lineups (current design)

A static lineup is fixed before the turn runs: a set of proposer models plus one
aggregator model, all known ahead of time. No per-turn scoring happens. Two
variants share this shape:

- **Presets** — `static_openrouter` / `static_tokenrhythm`: packaged,
  hard-coded lineups on a single provider.
- **Custom** — `custom`: an explicit user-authored lineup with
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

The shorter `selection_mode` values resolve to these stable internal B5
profile identifiers. Traces and historical artifacts may therefore retain the
profile names above even though new configuration is written as
`static_openrouter` or `static_tokenrhythm`.

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

## 1.2 Custom lineup (`custom`)

Source: `_build_custom_b5_members`, `_custom_b5_candidates`
(`src/opensquilla/provider/ensemble.py`); schema `LlmEnsembleCandidateConfig`
(`src/opensquilla/gateway/config.py`).

`custom` lets an operator author the lineup explicitly via
`llm_ensemble.candidates`. Each candidate row carries:

- **`provider`** / **`model`** — required, non-empty; provider is lower-cased.
- **`role`** — `aggregator` is the only structural value; an empty or omitted
  role means proposer. The Web UI therefore presents only **Proposer** and
  **Aggregator**. Released values `primary`, `contrast`, `fast_check`, and
  `critic` remain accepted and preserved as advisory decision-trace labels,
  but all execute and appear in settings as proposers. Unknown values coerce
  to `""` instead of failing, so a hand-edited config never blocks boot.
- **`enabled`** — disabled rows are kept for read compatibility but never
  counted or run.

Lineup assembly (`_build_custom_b5_members`):

1. Every enabled row whose role is **not** `aggregator` runs as a proposer,
   labeled by its role (or `proposer_N` when unassigned).
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
`selection_mode == "custom"` (presets carry fixed lineups; both dynamic
modes select per turn):

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

## 1.3 Shared fixed-lineup defaults

Both static families set `is_static_b5 = True` in
`build_ensemble_provider_from_config`, which applies the fixed-lineup family
defaults. For the configurable rows below, replacement is **only** applied when
the stored value still equals its legacy default (`_static_default_if_legacy`),
so operator overrides are preserved. Quorum grace is a runtime family policy,
not a public configuration field:

| Parameter | Dynamic-mode configured default | Fixed-lineup default |
|-----------|---------------------------------|----------------------|
| `min_successful_proposers` | 1 | 3 (presets) / `N-1` (custom, "all but one") |
| `proposer_timeout_seconds` | 3600 | 300 |
| `aggregator_timeout_seconds` | 3600 | 480 |
| `shuffle_candidates` | `True` | `False` |
| `quorum_grace_seconds` | 0 (wait for every proposer) | 10 |

`min_successful_proposers` is additionally clamped down to the actual proposer
count. Both the configured and effective values (min-success, timeouts, shuffle)
are recorded in the selection plan for debugging.

The legacy value `0` disables quorum early-exit and waits for every proposer; it
does not mean an immediate, zero-delay cutoff. Fixed lineups instead allow ten
seconds after reaching quorum so a nearly complete final draft can still join
the fusion without waiting for the full proposer timeout.

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
selection_mode = "static_openrouter"   # or static_tokenrhythm / custom
```

Custom lineup:

```toml
[llm_ensemble]
enabled = true
selection_mode = "custom"

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
above). Both share the fixed-lineup runtime defaults, which an operator may
still override explicitly (`min_successful_proposers`,
`proposer_timeout_seconds`, `aggregator_timeout_seconds`, `shuffle_candidates`,
`proposer_tools`, `aggregator_tools`). Proposer and aggregator tool permissions are
independent; both receive the outer tool set only when their corresponding switch is enabled.

---

# Part 2 — `router_tree_baseline` Local-Tree Baseline

> **Status: supported, config-only benchmark mode.** This mode preserves the
> multi-model selection algorithm used by `router_dynamic` before the Step2
> ranker replaced it. It is intentionally versioned and isolated so benchmark
> results do not drift when the production ranker changes.

`router_tree_baseline` is a two-stage local-tree pipeline:

1. The existing SquillaRouter step runs first. Its bundled local predictor
   chooses the tier (`C0`-`C3`) and anchor model and writes the route and
   confidence into turn metadata.
2. The baseline selector consumes that tier, confidence, and anchor. It fills a
   fixed set of proposer roles and independently selects an aggregator from the
   same pool.

The baseline selector does **not** invoke the dedicated remote task analyzer,
build a task profile, apply session intent, or run the current Step2 hard-filter
and rerank pipeline. It also does not run SquillaRouter a second time: the
router decision already present on the turn is its input. For a real local-tree
benchmark, `squilla_router.enabled` must therefore be `true`; without route
metadata the selector remains runnable but falls back to `C1` with confidence
`0`, which is a compatibility fallback rather than the intended benchmark.

Implementation:

- `src/opensquilla/provider/tree_baseline_router.py` — validation, candidate
  normalization, legacy scoring, deterministic selection, and replay trace;
- `src/opensquilla/provider/router_tree_baseline_config.json` — frozen slot
  templates, model priors, every score/penalty coefficient, trace precision,
  algorithm version, and config version;
- `src/opensquilla/provider/ensemble.py` — adapts current config into the
  candidate pool and materializes the selected provider members;
- `src/opensquilla/engine/runtime.py` — dispatches the mode without analyzer
  invocation and stores its compact decision metadata.

### Isolation from `router_dynamic`

The two dynamic modes share only the generic ensemble execution runtime after
their members have been selected. Their selection implementations are kept
separate:

- `router_dynamic` imports only `ranking_router.py` and its two
  `router_dynamic_*.json` snapshots;
- `router_tree_baseline` lazily imports `tree_baseline_router.py` only after
  that exact mode is dispatched, and reads only
  `router_tree_baseline_config.json`;
- neither selector module imports the other;
- runtime catches `DynamicRankingError` and `TreeBaselineSelectionError` in
  separate branches, preserving the existing ranking fallback reason and
  metadata keys unchanged.

This means a missing or malformed tree-baseline module/config cannot affect a
`router_dynamic` turn. The mode names are the configuration boundary:

```toml
[llm_ensemble]
# Choose exactly one:
selection_mode = "router_dynamic"
# selection_mode = "router_tree_baseline"
```

## 2.1 Candidate pool and tier inputs

The routed tier is read in this order:

1. `turn.metadata.routed_tier`;
2. `routing_extra.final_tier`;
3. `routing_extra.base_tier`;
4. frozen default `C1`.

Legacy `T0`-`T3` values are accepted as aliases for `C0`-`C3`. The candidate
pool is stable-order deduplicated by `(provider, model)` and contains:

1. the routed model as the mandatory `anchor`;
2. enabled `llm_ensemble.candidates` rows (a current-config adapter);
3. every `llm_ensemble.model_options` entry;
4. every configured `squilla_router.tiers` model.

Unlike current `router_dynamic`, omitting `model_options` injects the original
eight-model pool frozen in the baseline JSON. Those models were part of the old
selector's default and therefore remain part of this benchmark. A configured
non-empty list replaces that default; an empty list retains the frozen default.
Known models receive the frozen quality, cost/latency, family, vendor,
architecture, and tier priors from the JSON snapshot. Unknown models receive
tier priors and deterministic identity heuristics, so custom pools are still
replayable.

The frozen default entries are OpenRouter model IDs. A run using that default
needs an inherited OpenRouter credential or `OPENROUTER_API_KEY`; otherwise use
a configured non-empty `model_options` list on providers available to the
benchmark environment. Onboarding status includes every effective pool
provider so missing credentials are visible before the run.

## 2.2 Fixed tier templates

The local-tree tier fixes the number and semantic role of proposer slots:

| Tier | Proposer slots | Aggregator slot |
|------|----------------|-----------------|
| `C0` | `anchor`, `cheap_contrast` | `aggregator_fast` |
| `C1` | `anchor`, `balanced_contrast` | `aggregator_balanced` |
| `C2` | `anchor`, `adjacent_tier_check`, `orthogonal_family` | `aggregator_strong` |
| `C3` | `anchor`, `strong_critic`, `orthogonal_family`, `fast_sanity` | `aggregator_strong` |

This produces 2, 2, 3, or 4 proposer calls plus one aggregator call. The anchor
is never reranked; it is the model selected by SquillaRouter. Every remaining
slot is selected greedily from the complete pool. A model may fill more than
one role when the pool is small, but each reuse incurs that slot's configured
duplicate penalty.

## 2.3 Legacy scoring and deterministic selection

For candidate `m` and slot `s`, the frozen score is:

```text
Score(m, s) =
    w_quality(s)  * quality_prior(m)
  + w_affinity(s) * router_affinity(m, routed_tier, confidence)
  + w_diversity(s) * diversity(m, already_selected)
  + w_cost(s)     * cost_latency_prior(m)
  + w_role(s)     * role_match(m, s)
  - selected_penalty(s) * prior_selection_count(m)
```

`role_match` is itself slot-specific. Cheap/fast slots reward lower tiers and
cost/latency; contrast slots reward different family/vendor/provider; adjacent
checks reward a one-tier distance; critic/strong aggregator slots reward high
tier and quality; orthogonal slots emphasize contrast and incremental
diversity. Router affinity penalizes tier distance more strongly as local-router
confidence rises, preserving the old behavior where low confidence relaxed the
tier lock.

There are no scoring magic numbers in Python. All weights, sub-feature weights,
same/different contrast values, diversity increments, tier priors, distance
scales, duplicate penalties, and the original default pool live in
`router_tree_baseline_config.json`. Selection-time loading validates finite ranges,
slot coverage, and weight sums. The trace records the config SHA-256 plus its
algorithm/config versions.

Scores sort by total score, then quality prior, cost/latency prior, and original
pool order. The same inputs and JSON snapshot therefore select the same lineup.
The aggregator is scored only after all proposers are selected, so its diversity
and duplicate terms see the final proposer set.

## 2.4 Runtime behavior and configuration

The mode keeps the dynamic-family runtime defaults: configured quorum `1`,
3,600-second proposer/aggregator timeouts, candidate shuffling enabled, and no
quorum grace. Quorum is clamped to the selected proposer count. Candidate
shuffling only changes the order shown to the aggregator; it does not change
the deterministic model selection.

The selection plan records the complete candidate pool, top scores per slot,
score components/weights, selected `P`/`A`, routed tier/confidence, config hash,
and `uses_remote_task_analyzer=false`. Runtime also exposes a compact
`router_tree_baseline_decision` in turn metadata. An invalid or unavailable
baseline snapshot fails open to the already-routed single provider with reason
`router_tree_baseline_unavailable`.

```toml
[squilla_router]
enabled = true

[llm_ensemble]
enabled = true
selection_mode = "router_tree_baseline"
```

---

# Part 3 — `router_dynamic` Step2 Ranking

> **Status: supported, config-only.** The Web UI still offers the static
> families and migration to `custom`, but existing TOML/RPC configs can use
> `router_dynamic`. Its implementation is the profile-driven Step2 ranking
> pipeline, not the former fixed slot-template algorithm.

`router_dynamic` chooses proposer set `P` and aggregator `A` independently for
every turn. When `ranking_thinking_assignment_enabled` is on, it then assigns
the per-role thinking plan `T` without changing the model-ranking search
space. The implementation is split across:

- `src/opensquilla/provider/ranking_router.py` — context adapters, task-profile
  validation, hard filters, scoring, greedy selection, and trace generation;
- `src/opensquilla/provider/router_dynamic_model_profiles.json` — versioned
  80-model curated OpenRouter registry and static/online profile estimates;
- `src/opensquilla/provider/router_dynamic_ranking_config.json` — the complete
  dynamic-routing parameter set, including limits, fallback/mock defaults,
  hard-filter states, ranking, reranking, proposer count, and session behavior;
- `src/opensquilla/provider/ensemble.py` — deployment availability and member
  construction;
- `src/opensquilla/engine/runtime.py` — analyzer invocation, per-session route
  continuity, usage accounting, and single-provider fail-open behavior.

## 3.1 Per-turn inputs

Before ranking, runtime builds four replaceable inputs:

1. **Request context** — bounded conversation summary/recent turns, tool and
   workspace state, attachment references and actual input modalities, token
   budgets, and the previous route. Caller-supplied context is projected onto
   this fixed schema; unknown fields are dropped and every text/list field is
   bounded before it reaches the analyzer. The trace stores a SHA-256 hash
   rather than raw prompt content.
2. **Task profile** — a dedicated OpenRouter deployment of
   `deepseek/deepseek-v4-pro`, pinned to Together, is called once as the JSON
   classifier. It uses a dedicated OpenRouter request with reasoning disabled
   even when the single-model route selected the same model ID. The result must
   contain capability, domain, and tier distributions that sum to one within
   the configured tolerance, plus cost, latency, context, modality, risk, and
   session-intent constraints. Required numeric fields accept finite JSON
   numbers only; booleans and numeric strings are rejected. Invalid JSON,
   timeout, provider errors, an omitted real input modality, an invalid schema,
   or an unavailable OpenRouter credential falls back to a conservative profile
   derived from SquillaRouter's `c0`-`c3` result. Invalid optional format or
   analyzer-confidence fields are dropped, recorded as normalization warnings,
   and do not discard an otherwise valid task profile.
3. **User profile** — application is controlled by
   `llm_ensemble.ranking_user_profile_enabled` (default `false`). When enabled,
   runtime overlays the hand-editable permission/preference fields at
   `router/profile.json` and history projected from Dream-consolidated routing
   preference lines in `MEMORY.md` onto the `mock_user_profile` baseline. The
   baseline supplies the shape and every default the file does not carry. An
   unreadable or invalid hand-editable file fails open to the baseline; invalid
   memory lines are ignored. When disabled, runtime builds no profile at all
   and ranking receives none.

   Generation of new routing-preference memory from thumbs is separately
   controlled by `llm_ensemble.ranking_user_profile_generation_enabled`
   (default `false`). It accepts only feedback whose persisted decision is an
   executed `router_dynamic` ensemble. General feedback sidecars used by the
   offline self-learning loop are still written when generation is disabled.
   The two switches are independent: operators can collect preferences before
   applying them, or apply an existing/manual profile without collecting more.

   The profile is never sent to the task analyzer; only ranking reads it.
4. **Model registry snapshot** — composed from the routed model, enabled
   `llm_ensemble.candidates`, non-default legacy `model_options`, configured
   SquillaRouter tiers, and the packaged eighty-model curated registry. Unknown
   deployments receive deterministic synthesized priors. Credential presence
   is added as an availability fact before ranking. Malformed profile rows and
   duplicate case-normalized deployment identities are rejected before pool
   composition. Numeric facts used by ranking must also be finite and in range,
   so a negative price, inverted latency bound, or invalid strength cannot be
   silently interpreted as a favorable zero-cost/default score.

The analyzer defaults to a 20-second outer timeout, bounded input/output sizes,
and `temperature=0`; those values and its thinking mode are read from the
ranking JSON. It records token and billed-cost usage in the normal session usage
tracker. A syntactically valid JSON fragment is accepted only after the provider
emits its terminal `DoneEvent`; incomplete streams use the fallback profile.

## 3.2 Hard filters

Every candidate is checked separately for proposer and aggregator roles before
it can be scored. A candidate is rejected for any of these reasons:

- disabled/unhealthy deployment, missing credential, exhausted quota, or rate
  limit;
- unsupported role; when user-profile integration is enabled, user model
  allow/deny policy or a disallowed task-risk level;
- missing any required input modality;
- insufficient context window.

When thinking assignment is enabled, a model with no unified thinking level is
also rejected with `thinking_level_unavailable`. High-risk tasks require at
least `high`; normal tasks keep partially compatible models and resolve an
unsupported target to the nearest advertised level after selection.

Context need includes the expected output, not just the prompt:

```text
C_proposer   = C_input + C_tools + C_candidate
C_aggregator = C_input + C_tools + |P| * C_candidate + C_aggregator_output
```

`C_tools` is derived from the bounded tool state when no larger caller token
estimate is available. Input estimation treats ASCII text with the configured
bytes-per-token ratio and dense non-ASCII scripts with a separate configured
characters-per-token ratio. Candidate text caps use their own conservative
one-token-per-retained-character bound; they do not assume four characters per
token or reduce that bound using the routed anchor's model-specific generation
cap.

Aggregator feasibility is checked once for each prospective proposer count.
The hard-filter result depends on the count and context budget, not on which
candidate fills that slot, so every candidate in a greedy step shares the same
check. The trace records eligible aggregators and filter-reason counts for each
prospective set size, and the final full aggregator ranking still runs after
`P` is fixed.

## 3.3 Base scoring

For each eligible proposer, task match combines capability, domain, and tier
profile expectations. With user-profile integration enabled, it is blended
with the user score, then adjusted by weak session continuity and normalized
cost/latency penalties:

```text
S_match_raw  = 0.45 * capability + 0.25 * domain + 0.30 * tier
S_match      = 0.82 * S_match_raw + 0.18 * proposer_role_fit
S_qual_clean = 0.85 * S_match + 0.15 * S_user
S_qual       = clamp(S_qual_clean + S_session)
S_base       = S_qual - w_cost * cost - w_latency * latency
```

With `ranking_user_profile_enabled = false`, `S_user` is not evaluated,
`S_qual_clean = S_match`, and cost/latency weights come only from task
constraints. This renormalization avoids silently retaining the user-score
weight as a zero-filled penalty.

`S_user` starts at `0.50`; positive/negative model feedback contributes up to
`+/-0.50`, with confidence saturated after 20 observations. Cost is
`(0.30 * input_price + 0.70 * output_price) / 40`, and latency is
`p95_ms / 30000`, both clamped to `[0, 1]`. Their weights come from task
constraints plus user preference. Ties are deterministic: higher score, then
higher quality, then lexical deployment identity. These defaults, including
normalization denominators and blend weights, come from the ranking JSON.

## 3.4 Dynamic proposer count

The effective tier is the rounded expectation of `tier_dist`:

| Effective tier | SquillaRouter tier | `N_min` | `N_max` |
|----------------|--------------------|---------|---------|
| 1 | `c0` | 1 | 1 |
| 2 | `c1` | 1 | 2 |
| 3 | `c2` | 2 | 3 |
| 4 | `c3` | 3 | 5 |

High-risk work raises the target to at least four proposers (maximum five).
Hard cost/latency constraints cap the set at two. User cost/latency preferences
can apply the same cap only when user-profile integration is enabled. When
filters or the candidate pool prevent `N_min`, ranking returns the best
feasible set and records `coverage_shortfall=true` instead of violating a hard
filter.

## 3.5 Greedy proposer selection

This stage is a deterministic feature-based rerank; it does **not** call an
additional LLM or cross-encoder reranker. Ranking keeps the top
`L = max(8, 2*N_max)` base-score rows (bounded by pool size), then applies the
quality floor below. The default margin is `0.28` for low risk, `0.20` for
medium risk, and `0.12` for high risk:

```text
quality_floor = max(S_base_clean in top-L) - risk_margin
```

Candidates below that floor cannot enter `P`. At every step, the selector first
checks once that at least one aggregator could consume the complete prospective
set size. It then recomputes each remaining candidate's marginal gain against
the already selected models:

```text
marginal = 0.55 * quality
         + 0.30 * capability_coverage_gain
         + 0.10 * error_complementarity
         - 0.25 * max_similarity_to_selected
```

`capability_coverage_gain` is the task-profile-weighted positive improvement
over the best selected model for each requested capability. Similarity is
`0.50 * cosine(capability vectors) + 0.50 * lineage`, where lineage is `1.0`
for the same family, `0.50` for only the same vendor, and `0` otherwise. Error
complementarity is `1 - max cosine` over the configured online error-rate
dimensions (`hallucination`, `omission`, `format_error`, `tool_error`, and
`timeout` by default); it is zero before the first selection or when the
candidate has no error signal.

Marginal rows sort by marginal gain, clean base score, quality, then lexical
deployment identity. Once `N_min` is satisfied, selection stops if the best
marginal gain is below the configured threshold (`0.0` by default). The trace
records the top-L floor decision, each selected component and configured number
of alternatives, per-size aggregator feasibility, and structured stop details.

## 3.6 Aggregator selection

After `P` is fixed, all aggregator-eligible models are rescored with the full
aggregator context requirement:

```text
S_agg_qual = 0.78 * task_match + 0.22 * aggregator_role_fit
Score_agg  = S_agg_qual + S_session
           - overlap_bias - w_cost * cost - w_latency * latency
```

Reusing a proposer incurs a self-overlap penalty; sharing its family or vendor
adds a related-model penalty (`0.08` and `0.05` by default). Aggregator rows
sort by final score, quality, then lexical deployment identity. Candidate
drafts are always anonymized. If the chosen aggregator overlaps a proposer,
candidate order is randomized to reduce position/self-preference bias. Each
aggregation trace records both the random seed and the resulting candidate-index
order so the exact aggregator input can be replayed without relying on
process-global RNG state.

## 3.7 Session intent

Session adjustments activate only when analyzer confidence is at least `0.60`
and a previous dynamic route exists:

- `continue` adds at most `+0.10` (scaled by prior quality feedback) to models
  from the previous `P/A`;
- `redo` subtracts `0.10` from the previous `P/A` and shifts `tier_dist` up one
  tier, with at most two consecutive escalations;
- `new_task` clears escalation state.

Runtime keeps only route identities, feedback, escalation level, and (when
enabled) the executed thinking assignment plus its policy version in a bounded
in-memory cache of 1,024 sessions. It never stores prompt or candidate content
there.

## 3.8 Thinking-level assignment

`llm_ensemble.ranking_thinking_assignment_enabled` defaults to `false`.
Disabled mode preserves the existing `P/A` selection, member thinking values,
provider payloads, and the exact legacy audit contract. The current packaged
config/registry are projected to ranking `step2-ranking-v2`, config schema
`step2-ranking-config-v3`, and registry schema `step2-model-registry-v1`;
thinking-only fields and lifecycle events are omitted from traces, runtime
metadata, profile hashes, and decision-completed logs.

Enabled mode runs `thinking-policy-v1` only after the final proposer and
aggregator models are known and after the session adjustment:

1. the session-adjusted effective tier maps to
   `low`, `medium`, `high`, or `highest` by
   `floor(expectation + 0.5)`; the policy version fixes the rounding offset at
   `0.5`;
2. the aggregator moves up one level, capped at `highest`;
3. high-risk work has a `high` floor;
4. low/hard-limit cost or interactive/hard-timeout latency shifts the result
   down exactly once without crossing the risk floor;
5. `redo` adds no second shift because its tier distribution was already
   adjusted by the session policy;
6. an unsupported target resolves to the nearest unified level, breaking ties
   downward for normal tasks and upward for high-risk tasks.

The registry stores `thinking_levels` in the unified ordering and an explicit
`thinking_level_mapping` for each model. The mapped provider-native value
(such as `xhigh` or `max` for unified `highest`) is what reaches the adapter;
the member's thinking-token budget is recomputed from that effective value.
Provider rejection before any visible output triggers an explicit retry at the
nearest remaining supported level. The nearest order is recomputed from the
level that was just rejected, rather than sorted once from the initial level.
High-risk retries never cross below `high`; if no safe level remains, the
request fails explicitly. Requested,
assigned, provider-native, fallback-reason, and policy-version fields are
recorded on execution and usage rows.

## 3.9 Output, fallback, and observability

`_build_router_dynamic_members` returns `router_dynamic/c0` through
`router_dynamic/c3`, `proposer_1...N`, one `aggregator`, and a replay-oriented
selection plan. When the configured quorum is still the legacy value `1`, the
effective quorum becomes the ranking result's `N_min`, clamped to the selected
proposer count. Explicit timeouts remain unchanged.

If config/context preparation fails, or no proposer or aggregator survives hard
filtering, ranking raises `DynamicRankingError`; runtime records
`router_dynamic_ranking_unavailable` and continues with the already selected
single-model provider. Failure to resolve or construct the fixed analyzer itself
uses the deterministic task-profile fallback locally and does not abort ranking.
When thinking assignment is enabled, `thinking_level_unavailable` is a
fail-closed exception to that general ranking fallback, including when other
hard-filter reasons are present. Execution-time rejection likewise stays
inside the managed nearest-level chain and never escapes to an unmanaged
`fallback_single` provider.

The plan/logs include analyzer provenance and usage, task/profile/request
hashes, registry version and exact snapshot hash, per-model profile hashes,
hard-filter reasons, all base scores, `N_min/N_max`, every greedy step, selected
`P/A`, aggregator overlap/bias, coverage shortfall, stop reason, and session
escalation. It also stores the ranking-config schema version, config version,
SHA-256 hash, and complete parameter snapshot. The execution trace additionally
records `candidate_order_seed`, `candidate_display_order`, the conditional
`thinking_assignment`, assignment reasons, unsupported-level fallbacks,
execution fallbacks, and policy versions. Structured lifecycle events are:

- `task_analyzer_started`, `task_analyzer_completed`, or
  `task_analyzer_fallback`;
- `candidate_pool_recorded` and `model_scores_recorded`;
- `proposer_selection_recorded` and `aggregator_selection_recorded`;
- `thinking_assignment_recorded` when the feature is enabled;
- `router_decision_recorded`.

All use the `llm_ensemble.router_dynamic.` event prefix and omit raw user or
candidate content. In addition, every successful ensemble mode emits a unified,
ordered decision stream under `llm_ensemble.routing.*`. One `decision_id`
correlates the local-router input, task-analysis summary and proposer bounds
(dynamic mode), every candidate, every hard-filter and score row (dynamic
mode), every proposer/rerank step, every aggregator score, and the final
selection. The tree baseline records every slot decision and aggregator
candidate score; static and custom modes record each fixed lineup member.
Selection failures end with `decision_failed`; readiness gates such as missing
credentials end with `decision_skipped`. Every event has a monotonically
increasing `sequence`, starting with `decision_started = 0`, and contains no
prompt, candidate answer, or raw user-profile payload.

## 3.10 Configuration surface

```toml
[llm_ensemble]
enabled = true
selection_mode = "router_dynamic"
ranking_user_profile_generation_enabled = false
ranking_user_profile_enabled = false
ranking_thinking_assignment_enabled = false
```

Set `ranking_user_profile_generation_enabled = true` to transcribe thumbs from
dynamic-ranking decisions into preference memory. Set
`ranking_user_profile_enabled = true` to apply the resolved profile during
ranking. Both switches affect only `router_dynamic`; the four fixed/custom/tree
modes neither generate nor consume a user profile.

Set `ranking_thinking_assignment_enabled = true` to enable the independent
post-ranking thinking policy. It affects only `router_dynamic`; leaving it
unset or `false` preserves the legacy execution path.

Operators can extend the deployment pool with `llm_ensemble.candidates`,
non-default `llm_ensemble.model_options`, and `squilla_router.tiers`. All
tunable Step2 behavior lives in
`src/opensquilla/provider/router_dynamic_ranking_config.json`:

| JSON group | Controls |
|------------|----------|
| `validation`, `trace` | sum tolerance, trace precision, and nonzero epsilon |
| `routing_tiers` | `c0`-`c3` mapping and default tier |
| `thinking_assignment` | versioned unified level order, tier base levels, aggregator step, risk floor, and one-step resource downshift |
| `context` | context buckets, request truncation, token estimates, output budgets |
| `task_profile_schema` | accepted task constraints and session intents |
| `task_analyzer` | timeout, input/response limits, output tokens, temperature, thinking |
| `fallback_task_profile` | deterministic analyzer-failure profile and tier risk |
| `mock_user_profile` | baseline profile shape/defaults; when application is enabled, hand-edited permission/preference fields from `router/profile.json` and Dream-projected history from `MEMORY.md` are overlaid at the engine read seam |
| `synthetic_model` | facts and priors for deployments missing a catalog profile |
| `hard_filter` | eligible/unavailable states and default required modalities |
| `exploration` | reserved trace declaration; must remain `false` / propensity `1.0` until exploration is implemented |
| `normalization` | input/output price blend and cost/latency denominators |
| `task_match`, `user_score`, `quality` | base task, role, user, and quality weights |
| `penalties` | task/user cost and latency weights and trade-off adjustments |
| `session` | intent threshold, continuity delta, feedback default, redo ceiling |
| `proposer_count` | tier/risk bounds and cost/latency caps |
| `rerank` | top-L, quality floors, marginal weights, similarity, errors, stop rule |
| `aggregator` | task/role blend and overlap penalties |

Runtime loads one versioned snapshot and passes it through context construction,
task analysis, registry synthesis, and final ranking. The loader validates the
exact key set at every fixed object, config and task distribution tolerances,
normalized weight groups, numeric ranges, protocol-valued maps, and count bounds
before selection.
Unknown, misspelled, missing, or currently inactive settings therefore fail
explicitly instead of appearing to take effect. A malformed config raises
`DynamicRankingError`, so runtime follows the existing single-model fail-open
path. The packaged config is cached; editing it requires a process restart.
The eighty curated OpenRouter profiles remain separately versioned in
`router_dynamic_model_profiles.json`. Catalog facts (model ID/version, context,
modalities, token prices, advertised reasoning/tool support, unified thinking
levels, and provider-native thinking mapping) carry their verification date. The
`is_open_source` flag means that the exact model has publicly downloadable
weights under a published license (including open-weight licenses, not only
OSI-approved licenses); `is_chinese_model` follows the original model
developer, independently of the serving provider. Capability priors, latency,
and error rates are curated routing estimates rather than OpenRouter
measurements. Defaults used to synthesize an unknown deployment are in the
ranking JSON. Protocol enums, probability bounds, and
deterministic tie-break order remain code contracts rather than tuning knobs.
`router_dynamic` retains its 3,600-second proposer/aggregator timeouts and zero
quorum-grace default.

The fixed task analyzer resolves its OpenRouter credential from the active or
primary OpenRouter config, `[llm_profiles.openrouter]`, or
`OPENROUTER_API_KEY`. If none is available, task analysis uses the deterministic
fallback profile; it does not call the routed single-model provider.
