# Providers and Models

OpenSquilla supports multiple LLM providers through one configuration surface.
You can run direct single-model mode or enable SquillaRouter for tiered routing.

Use this page when you need to configure a provider, inspect model support, or
choose between direct model mode and router mode.

## Inspect Providers

List provider metadata from the local install:

```sh
opensquilla providers list
opensquilla providers list --json
```

Show runtime provider diagnostics from the running gateway:

```sh
opensquilla providers status
opensquilla providers status openrouter --json
opensquilla providers status --probe-models
```

`providers list` does not require a running gateway. `providers status` does.

## Configure a Provider

Interactive:

```sh
opensquilla providers configure openrouter
```

Non-interactive onboarding-style configuration:

```sh
export OPENROUTER_API_KEY="sk-..."
opensquilla configure provider --provider openrouter --api-key-env OPENROUTER_API_KEY
```

Direct provider examples:

```sh
opensquilla configure provider --provider openai --model gpt-5.4-mini --api-key-env OPENAI_API_KEY
opensquilla configure provider --provider anthropic --model claude-sonnet-4-5 --api-key-env ANTHROPIC_API_KEY
opensquilla configure provider --provider gemini --model gemini-2.5-flash --api-key-env GEMINI_API_KEY
opensquilla configure provider --provider ollama --model llama3.1
```

Prefer environment-variable references for API keys so secrets are not written
directly into configuration files.

### Endpoint (base URL) resolution

`llm.base_url` resolves **explicit config → derived env var → provider
default**:

- A custom endpoint you saved (Web UI advanced options, `config.set`, or a
  hand-written `base_url` in the TOML) always wins.
- If the config never chose an endpoint — no `base_url`, or the field still
  holds the provider's own default URL — the derived environment variable
  (`OPENAI_BASE_URL`, `OPENROUTER_BASE_URL`, `<PROVIDER>_BASE_URL`) applies.
  This is the lever for pointing a whole fleet at a corporate proxy without
  touching each config file.
- `OPENSQUILLA_LLM_BASE_URL` enters at config-model construction (the
  `OPENSQUILLA_LLM_*` settings layer): it fills `base_url` whenever the TOML
  does not set one, and the resolver then treats it as an explicit value —
  so it beats the provider-derived vars above, while a `base_url` written in
  the TOML still beats it.

API keys follow the same explicit-config-first rule via `api_key` /
`api_key_env`.

## Onboarding-Verified Providers

This build exposes onboarding support for:

- TokenRhythm
- OpenRouter
- OpenAI
- Anthropic
- Ollama
- DeepSeek
- Gemini
- DashScope / Qwen
- Moonshot AI
- Zhipu / Z.AI
- Baidu Qianfan
- Volcengine Ark

The provider registry may contain additional compatible providers for advanced
or self-hosted setups. Use `opensquilla providers list` on your install for the
current catalog.

### OpenAI: `openai` vs `openai_responses`

OpenAI is exposed as two provider ids that share the same `OPENAI_API_KEY` and
base URL (`https://api.openai.com/v1`):

- `openai` — the chat/completions request shape. Use this for standard
  chat-style turns and broad tool compatibility.
- `openai_responses` — the native Responses-API shape (capabilities `chat` and
  `responses`). Use this when you want Responses-API behavior rather than the
  chat/completions surface.

Both read the same key and base URL, so switching between them needs only a
`provider` change.

### Qwen Token Plan: OpenAI and Anthropic protocols

Token Plan is separate from regular DashScope and Bailian Coding Plan. Use its
dedicated `sk-sp-...` key; a standard Model Studio key cannot consume Token
Plan Credits.

OpenSquilla exposes the mainland China service through two verified provider
ids:

- `qwen_token_plan` — OpenAI-compatible Chat Completions at
  `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`.
- `qwen_token_plan_anthropic` — Anthropic Messages at
  `https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic`
  (+ `/v1/messages`), using bearer authentication.

Both read `QWEN_TOKEN_PLAN_API_KEY` and default to
`qwen3.8-max-preview`:

```sh
export QWEN_TOKEN_PLAN_API_KEY="sk-sp-..."
opensquilla configure provider \
  --provider qwen_token_plan \
  --model qwen3.8-max-preview \
  --api-key-env QWEN_TOKEN_PLAN_API_KEY
```

The packaged model catalog is the documented team-plan superset. Personal
plans can use `qwen3.8-max-preview`, `qwen3.7-max`, `qwen3.7-plus`,
`qwen3.6-flash`, `glm-5.2`, and `deepseek-v4-pro`; team plans additionally
include `qwen3.6-plus`, DeepSeek V4 Flash / V3.2, Kimi K2.7 / K2.6 / K2.5,
GLM 5.1 / 5, and MiniMax M2.5. Entitlement is enforced by the service.
Settings and onboarding query the verified OpenAI-compatible `/models`
endpoint so suggestions reflect the current key's entitlement. The Anthropic
profile deliberately uses that same account catalog instead of presenting the
packaged superset as a live result.

The OpenAI-compatible adapter applies the model-family wire contracts required
by this mixed catalog: forced thinking and the 0.6 temperature floor for
Qwen 3.8, DeepSeek V4 effort and reasoning replay, Kimi tool-call reasoning
replay, GLM `tool_stream`, and thinking-mode tool-choice normalization. The
Anthropic adapter preserves signed thinking blocks and clamps Qwen 3.8's
temperature floor. The included inline SquillaRouter preset uses Qwen 3.6
Flash → Qwen 3.7 Plus → Qwen 3.7 Max → Qwen 3.8 Max Preview, with Qwen 3.7
Plus as the image route.

Token Plan also exposes native image generation through OpenSquilla's
`image_generate` tool. This is separate from the router's image route: the
router image model understands image *inputs*, while Wan creates new image
artifacts. Configure either `wan2.7-image` or `wan2.7-image-pro`:

```toml
[image_generation]
enabled = true
primary = "qwen_token_plan/wan2.7-image"
size = "768x768"

[image_generation.providers.qwen_token_plan]
api_key_env = "QWEN_TOKEN_PLAN_API_KEY"
base_url = "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1"
```

The native adapter uses the documented multimodal-generation request shape,
converts OpenSquilla's `WIDTHxHEIGHT` size to the service's `WIDTH*HEIGHT`
form, and securely downloads the temporary signed result URL. When Token Plan
is also the active LLM provider, the image provider can reuse its credential
because both official endpoints have the same HTTPS origin; it does not reuse
the incompatible chat API path.

Token Plan is licensed for supported interactive AI programming and agent
tools. It is not a replacement credential for unattended application
backends or generic batch API workloads; follow the current plan terms for
your subscription.

### Custom OpenAI- and Anthropic-compatible endpoints

Use `custom` for an OpenAI Chat Completions endpoint and
`custom_anthropic` for an Anthropic Messages endpoint. Both require an
explicit model and base URL; API keys are optional:

```toml
[llm]
provider = "custom_anthropic"
model = "vendor-model"
base_url = "https://llm.example.com/anthropic"
api_key_env = "CUSTOM_ANTHROPIC_API_KEY"
```

`custom_anthropic` appends `/v1/messages` and sends a bearer token.
`custom` appends `/chat/completions` to versioned base URLs and reads
`CUSTOM_LLM_API_KEY`.

Unknown custom models keep the conservative 8k context default for upgrade
compatibility. Declare the endpoint's real window under
`[models.custom."<model>"]` or
`[models.custom_anthropic."<model>"]`; this is important for both remote
gateways and local servers with larger windows.

`custom` and `custom_anthropic` default to a zero token-cost estimate. This
does not mean a remote service, GPU, or other infrastructure is free. To
estimate a paid endpoint, set both input and output prices for its exact
provider/model override; cache-read and cache-write prices remain optional:

```toml
[models.custom."vendor-model"]
input_cost_per_mtok = 0.5
output_cost_per_mtok = 2.0
```

If either main price is omitted, the generic custom endpoint remains at the
zero-price default and does not inherit a same-named marketplace model's
price.

The current custom-provider boundary is intentionally explicit:

- protocol selection is available through the two fixed provider ids;
- provider-scoped model overrides, base URL, proxy, and optional bearer key
  are supported;
- arbitrary user-named provider ids and arbitrary request headers are not yet
  part of the persisted provider contract;
- custom Anthropic auth is currently optional bearer only (`x-api-key` and
  custom auth modes are not configurable), and custom protocol choices do not
  yet include OpenAI Responses or native Gemini;
- endpoint-wide `extra_body` / request-transform configuration is not exposed;
- reasoning dialects are never inferred from an untrusted custom host—set
  provider-scoped model metadata only when the endpoint contract is known.

### Volcengine Ark: regular vs coding-plan endpoints

Use `volcengine` for regular Ark chat/completions models. Its default base URL
is the OpenAI-compatible endpoint `https://ark.cn-beijing.volces.com/api/v3`.

Use `volcengine_coding_plan` for Volcengine's OpenAI Responses-compatible coding-plan
subscription surface. Its default base URL is
`https://ark.cn-beijing.volces.com/api/coding/v3`; OpenSquilla appends
`/responses` when it sends the request.

```sh
export VOLCENGINE_API_KEY="..."
opensquilla configure provider --provider volcengine_coding_plan --model <model> --api-key-env VOLCENGINE_API_KEY
```

Use `volcengine_coding_plan_anthropic` for tools or deployments that expect the
Anthropic Messages protocol. Its default base URL is
`https://ark.cn-beijing.volces.com/api/coding`; OpenSquilla appends
`/v1/messages`.

```sh
export VOLCENGINE_API_KEY="..."
opensquilla configure provider --provider volcengine_coding_plan_anthropic --model <model> --api-key-env VOLCENGINE_API_KEY
```

Do not point either coding-plan provider at the regular `/api/v3` URL. That
regular Ark URL does not consume Coding Plan quota.

### Tencent TokenHub: CN, Anthropic-protocol, and international endpoints

Tencent's Hunyuan `hy3` / `hy3-preview` models are served on the TokenHub
platform (the legacy `api.hunyuan.cloud.tencent.com` platform is being
retired and never received `hy3`). Three experimental provider ids map the
documented endpoints:

- `tencent_tokenhub` — OpenAI-compatible chat/completions at
  `https://tokenhub.tencentmaas.com/v1` (mainland; keys from the CN TokenHub
  console, `TENCENT_TOKENHUB_API_KEY`). `hy3` thinking uses
  `reasoning_effort` `low`/`high`, and assistant `reasoning_content` is
  replayed across turns as the hy3 interleaved-thinking contract requires.
- `tencent_tokenhub_anthropic` — the same deployment's Anthropic Messages
  protocol (`https://tokenhub.tencentmaas.com` + `/v1/messages`,
  `x-api-key` auth, same key).
- `tencent_tokenhub_intl` — the international deployment at
  `https://tokenhub-intl.tencentcloudmaas.com/v1`
  (`TENCENT_TOKENHUB_INTL_API_KEY`). It is a separate Tencent Cloud account
  and key system, and its model list currently carries third-party models
  (DeepSeek, GLM, Kimi, MiniMax) but not `hy3`.

```sh
export TENCENT_TOKENHUB_API_KEY="..."
opensquilla configure provider --provider tencent_tokenhub --model hy3 --api-key-env TENCENT_TOKENHUB_API_KEY
```

TokenHub also hosts third-party models behind the same endpoints; OpenSquilla
does not inject thinking payloads for those ids because TokenHub does not
document their dialects on this gateway.

Tencent's Token Plan subscription (the Hy Token Plan carries `hy3` /
`hy3-preview`; the General plan adds `tc-code-latest`, DeepSeek V4, GLM-5.x,
Kimi and MiniMax ids on the same key) is exposed as two more provider ids on
the plan host:

- `tencent_token_plan` — Chat Completions at
  `https://api.lkeap.cloud.tencent.com/plan/v3` (the plan endpoints do not
  offer the Responses API).
- `tencent_token_plan_anthropic` — Anthropic Messages at
  `https://api.lkeap.cloud.tencent.com/plan/anthropic` (+ `/v1/messages`),
  bearer auth.

Both read `TENCENT_TOKEN_PLAN_API_KEY`. Plan keys are dedicated `sk-tp-…`
credentials created on the TokenHub Token Plan console page — they are not
interchangeable with pay-as-you-go TokenHub keys. Note Tencent's plan terms
restrict these keys to interactive AI-tool use and prohibit non-interactive
batch/automation calling; unattended pipelines should use the pay-as-you-go
`tencent_tokenhub` provider instead. The plans are mainland-only products —
the international site offers pay-as-you-go TokenHub only.

## Model Inspection

List models:

```sh
opensquilla models list
```

If runtime-backed model inspection cannot connect, start the gateway:

```sh
opensquilla gateway run
```

For provider metadata that does not require the gateway, use:

```sh
opensquilla providers list
```

### Context-Window Resolution Order

Context budgeting, compaction thresholds, usage pressure reporting, and the
router's capability facts all resolve a model's context window through the
same layers, first match wins:

1. **Per-model override** — `[models.<provider_id>."<model_id>"]`
   `context_window` in your config. Set this for models the catalog does not
   know (direct DashScope/TokenHub ids, self-hosted vLLM declaring its real
   window) or to correct a wrong catalog value. Reported as source
   `override` (`config` in `config.effective`, `model_override` in usage
   context status).
2. **Global override** — `llm.context_window_tokens` (0 = auto). A blunt
   instrument that applies to whatever model is active; the per-model
   override always beats it.
3. **Model catalog** — live OpenRouter data, the vendored models.dev
   snapshot, then packaged corrections.
4. **Default** — a conservative 8,192 for local runtimes (match your actual
   `num_ctx`/server window with an override), 200,000 otherwise.

The Web UI exposes the per-model override under Settings → Chat Model →
Advanced, with an auto-detected / override / effective readout.

## Direct Model vs Router

Direct model mode:

```sh
opensquilla configure router --router disabled
opensquilla configure provider --provider openai --model gpt-5.4-mini --api-key-env OPENAI_API_KEY
```

Router mode:

```sh
opensquilla configure router --router recommended
```

| Mode | Use when |
| --- | --- |
| Direct model | You are testing one exact model, reproducing provider behavior, or auditing provider billing. |
| Router mode | You want normal personal-agent use where cost and task complexity vary by turn. |

For routing details, see
[`features/squilla-router.md`](features/squilla-router.md).

## Pricing and Cost Estimation

OpenSquilla reports real provider-billed cost when a provider returns it, and
estimates cost locally from token usage everywhere else. Every usage row and
by-model breakdown item is labeled so you can tell which kind of number you
are looking at.

### How a Cost Is Estimated

Each priced call is split into four token buckets — fresh input, cache read,
cache write, output — and each bucket is priced at its own rate. The result
carries a `basis` label:

| Basis | Meaning |
| --- | --- |
| `cache_aware` | All buckets present in the call have a known rate; the four-bucket math ran. |
| `cache_blind` | The call used cache tokens but a needed cache rate is unknown, so OpenSquilla fell back to pricing every input token (cache or fresh) at the plain input rate. This is a conservative upper bound, not the real charge — expect it to overstate cost on cache-heavy sessions. |
| `free` | The model or runtime is zero-priced, including generic custom endpoints without complete price overrides. |

### Price Resolution Order

For a given `(model, provider)` pair, OpenSquilla resolves a price through
these layers, first match wins:

1. **Local runtime** — `ollama`, `lm_studio`, `ovms`, `vllm`, and `local` are
   always free, regardless of model id.
2. **Generic custom endpoint** — `custom` and `custom_anthropic` use a
   complete explicit input/output price override when present; otherwise they
   resolve to zero with `priceSource="custom_free"` and stop before catalog,
   live, static, or default cloud pricing.
3. **User override** — `[models.<provider_id>."<model_id>"]` in your config
   (see [`configuration.md`](configuration.md) and `opensquilla.toml.example`).
4. **Model catalog** — the vendored models.dev snapshot, including per-model
   cache-read/cache-write rates where upstream publishes them.
5. **Live OpenRouter endpoint price** — looked up only when the provider is
   `openrouter` or unset (first-party provider ids never query the OpenRouter
   marketplace); falls back to the static table if OpenRouter is unreachable.
6. **Static table** — a built-in pricing table bundled with OpenSquilla.
7. **Default** — `$3` / `$15` per million input/output tokens when nothing
   else matched.

If OpenSquilla is estimating a model at the wrong price, add an override
instead of waiting for a catalog refresh:

```toml
[models.openrouter."z-ai/glm-5.2"]
input_cost_per_mtok = 0.5        # USD per million input tokens
output_cost_per_mtok = 2.0       # USD per million output tokens
cache_read_cost_per_mtok = 0.05  # USD per million cached-prompt-read tokens
cache_write_cost_per_mtok = 0.6  # USD per million cached-prompt-write tokens
```

Quote model ids that contain dots or slashes. For ordinary provider overrides,
all four fields are optional. For `custom` and `custom_anthropic`, set
`input_cost_per_mtok` and `output_cost_per_mtok` together to enable a nonzero
estimate; cache rates remain optional. `config.set`/`patch`/`apply` and
`opensquilla gateway reload` hot-apply these overrides; see
`opensquilla.toml.example` for more examples including self-hosted `vllm` and
`custom` endpoints.

### Cost Provenance (`costSource`)

Every usage row and by-model breakdown item carries a `costSource` (also
exposed dual-cased as `cost_source`):

| `costSource` | Meaning |
| --- | --- |
| `provider_billed` | The full cost came from a real provider-reported bill. |
| `opensquilla_estimate` | No billed cost was available; the figure is a local estimate. |
| `mixed` | The same model had both billed and unbilled calls in the aggregated row — the total is billed cost plus an estimate for the rest, not a pure bill. |
| `unavailable` | No billed cost or positive local estimate is available. |

Rows also carry two additive fields: `estimateBasis` (the `cache_aware` /
`cache_blind` / `free` label above, present only when part of the row was
estimated) and `priceSource` (which resolver layer priced it — `user_override`,
`catalog`, `live_openrouter`, `static_table`, `default`, `local_free`, or
`custom_free`). The Web UI's by-model usage cards show a small source chip for
`costSource` and, when the underlying basis is `cache_blind`, a hint that the
figure is an upper bound rather than the real cache-discounted cost.

### Which Providers Yield Billed vs. Estimated Cost

| Capability | Providers |
| --- | --- |
| Provider-billed cost | `openrouter` only |
| Cache-aware estimate possible | `anthropic`, `deepseek`, `minimax` (Anthropic-shaped), ensemble members |
| Cache-read-aware estimate only (no cache-write rate) | `openai`, `openai_responses`, `azure`, `gemini`, `openai_codex` |
| Cache-blind estimate (falls back to plain input-rate pricing when cache tokens appear) | other OpenAI-compatible provider kinds |
| Zero-priced estimate | local runtimes (`ollama`, `lm_studio`, `ovms`, `vllm`, `local`) and `custom` / `custom_anthropic` without complete input/output price overrides |
| Subscription (no invoice to compare against) | coding-plan/subscription provider kinds — treat any reported figure as an estimate, not a bill |

Use `opensquilla providers status --probe-models` and `opensquilla cost
--by-model` to see which class your configured provider/model falls into for
a given session.

### Turn and Router Budget Gates

Two per-turn agent budgets exist and behave differently:

- `max_turn_billed_cost_usd` gates only on real provider-billed cost. It is
  inert (never trips) on providers or paths that never report billed cost —
  do not rely on it alone outside `openrouter`.
- `max_turn_cost_usd` gates on the same accumulator used everywhere else in
  this section: billed cost when the provider reports it, otherwise the
  cache-aware/cache-blind estimate. It can trip only when a call has billed
  cost or a nonzero local estimate. A generic custom endpoint without complete
  input/output price overrides resolves to zero, so configure both rates before
  relying on this gate for a paid custom endpoint. When it trips, the error
  (`turn_cost_budget_exceeded`) states whether the total was billed, estimated,
  or mixed.

SquillaRouter's session budget gate (`[squilla_router.budget]`, see
[`features/squilla-router.md`](features/squilla-router.md)) logs a
`spend_source` alongside each `router_budget.warn`/`router_budget.cap` event
and in the routing trail:

| `spend_source` | Meaning |
| --- | --- |
| `billed` | Accumulated spend is real provider-billed cost. |
| `estimate` | Accumulated spend is a local estimate for the whole session. |
| `estimate_mixed` | The session mixes billed and estimated cost. |
| `none` | No spend has been recorded yet. |
| `unknown` | Spend could not be determined; the gate suspends rather than acting on a guess. |

Read next: [`usage-and-cost.md`](usage-and-cost.md) for the `opensquilla cost`
CLI and how to read a session's usage rows.

## Provider Troubleshooting

Start with:

```sh
opensquilla doctor
opensquilla providers status
opensquilla diagnostics on
```

Check:

- the API key environment variable is set in the gateway process environment;
- the model id matches the provider;
- the base URL is correct for compatible APIs;
- proxy settings match your network;
- router is disabled when debugging one exact provider/model;
- the gateway was restarted after config changes.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
