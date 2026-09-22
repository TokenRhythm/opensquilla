# SquillaRouter

SquillaRouter is OpenSquilla's local model-routing layer. It helps the agent
choose an appropriate model tier for each turn so routine work does not always
run on the most expensive model.

Use this page when you want to enable routing, understand what it changes, or
decide whether a fixed provider/model is better for a specific run.

## Why Use It

SquillaRouter is useful when you want:

- lower cost for simple chat, edits, summaries, and routine tool work;
- stronger models reserved for hard reasoning, recovery, and long tasks;
- one OpenSquilla workflow that can route across provider profiles;
- local routing decisions without sending prompts to a separate external
  classifier just to choose the model.

It is not required. OpenSquilla can also run in direct single-model mode.

## Enable Routing

Recommended first-run setup:

```sh
opensquilla onboard --router recommended
```

Reconfigure an existing install:

```sh
opensquilla configure router --router recommended
```

Use the OpenRouter mixed defaults:

```sh
opensquilla configure router --router openrouter-mix
```

OpenRouter and TokenRhythm use the following recommended single-model tiers:

| Tier | OpenRouter | TokenRhythm |
| --- | --- | --- |
| C0 | `qwen/qwen3.7-flash` | `qwen3.7-flash` |
| C1 | `deepseek/deepseek-v4-flash-0731` | `deepseek-flash` |
| C2 | `deepseek/deepseek-v4-pro-0813` | `deepseek-v4-pro-0813` |
| C3 | `z-ai/glm-5.3` | `glm-5.3` |

The TokenRhythm ladder leaves tier `thinking_level` unset. Direct
requests without an explicit thinking setting preserve the provider default;
Router auto-thinking can still choose a per-turn level (normally `low` on C1).
Fresh and managed (`preset_binding = "follow_primary"`) configurations receive
this ladder. Upgrades also refresh recognized previous OpenRouter and
TokenRhythm default ladders saved inline, including historical saves without a
managed binding. Edited model ladders and mixed-provider ladders remain
authoritative. Upgrading keeps the ladder's provider and the Router's enabled
state; it does not switch TokenRhythm routes to OpenRouter or vice versa.
OpenRouter retains `high` as its packaged tier thinking level.

Settings offers a recommended-tier reset for single-provider OpenRouter and
TokenRhythm ladders. The button names and restores the ladder's own provider,
even when the primary provider differs. Other providers and mixed-provider
ladders do not show this shortcut. Restoring tiers keeps the current usage mode.

C3 can optionally use the shared multi-model fusion plan configured under
`llm_ensemble`. Enabling fusion on C3 makes it use that plan instead of its
single-model route. If the plan falls back to a single model, it uses the global
provider/model configured under `[llm]`.

For a newly configured C3 fusion tier, the tier-local runtime-policy defaults are one
successful proposer out of the four-member lineup, one retry after each
proposer's initial attempt, and `all_failed_policy = "fallback_single"`. These
defaults fill only fields that the operator has not set. Explicit
`min_successful_proposers`, `proposer_max_retries`, and `all_failed_policy`
values remain authoritative for both global/custom Ensemble use and C3. In
particular, `all_failed_policy = "error"` is a valid terminal policy and does
not start the fixed fallback. A global/custom configuration with no explicit
retry field keeps the historical zero-retry default.

Packaged static B5 lineups use a 120-second total budget per proposer and a
180-second aggregator idle budget. Operator-authored `custom_b5` lineups use
300 and 480 seconds respectively unless explicitly configured otherwise.

Image routing considers only the configured C0–C3 single-model deployments;
C3 fusion is excluded. Image input capability is resolved automatically from
the shared provider model catalog, including API-provided deployment metadata
and offline catalog fallback. The web and desktop clients do not expose a
manual image-capability switch. Legacy tier `supports_image` values remain
readable but do not override the deployment's capability, and `image_model`
is retained for compatibility without creating an executable fifth route.

When capability is unknown, the configured model may receive a native image
request. A recognized image-input rejection can advance through the remaining
configured tiers before falling back to explicit not-analyzed/analysis-failed
text markers. Direct retries only its configured model with markers; Ensemble
uses text markers for all members. These retries stop after visible output or
tool effects. Projection never deletes the canonical attachment, so switching
back to a vision-capable model can recover earlier images after history
compression.

Disable routing and use the configured provider/model directly:

```sh
opensquilla configure router --router disabled
```

## Inspect Provider Support

Check the provider catalog available in your install:

```sh
opensquilla providers list
```

If the gateway is running, inspect runtime provider health:

```sh
opensquilla providers status
```

Router-supported profiles depend on the installed OpenSquilla version,
optional dependencies, and configured provider credentials. Common profiles
include OpenRouter, OpenAI, DeepSeek, Gemini, DashScope, Moonshot, Volcengine,
Zhipu, and compatible provider tiers exposed by the local catalog.

## What the Router Can Affect

Depending on configuration, SquillaRouter may influence:

- selected model tier;
- direct model fallback;
- reasoning level;
- response policy;
- image-capable model selection;
- cache-continuity safeguards for recent higher-tier turns.

The exact decision is available through runtime metadata and diagnostics
surfaces. Turn on diagnostics when you need to understand why a turn was routed
to a particular model:

```sh
opensquilla diagnostics on
```

## Terminal Router HUD

Interactive terminal chat can surface routing decisions through a TUI Router HUD
when router metadata is present and the selected backend supports the structured
UI/plugin surface. In the current implementation, the OpenTUI footer is
the primary terminal display for this HUD. The HUD is display-only: it consumes
the same turn metadata and does not change model selection.

The HUD can show the selected tier, selected model, baseline model, route
source, confidence, estimated savings, fallback state, thinking mode, prompt
policy, whether routing was applied, and rollout phase.

Full routing is shown as an active route. Observe-only routing is shown as an
observe decision, which means OpenSquilla recorded what the router would have
chosen while keeping the configured baseline behavior. Fallback decisions use a
warning style so provider or policy recovery is visible during the turn.

## Recommended Operating Modes

| Goal | Suggested mode |
| --- | --- |
| General personal-agent use | `recommended` |
| Multi-provider cost optimization through OpenRouter | `openrouter-mix` |
| Provider evaluation, billing audit, or reproducible benchmark run | `disabled` |
| Debugging one provider-specific behavior | `disabled` |

For routine use, start with `recommended`. Disable routing only when the model
choice itself is the thing you are testing.

## Example Requests

Good router-friendly requests describe the outcome, not the tier:

```text
Summarize this long issue thread and list the decision points.
```

```text
Review my current diff and point out the highest-risk changes.
```

Avoid asking the router to behave like a manual model picker unless you are
debugging:

```text
Use exactly this one model for every turn.
```

For exact-model work, configure direct routing instead.

## Troubleshooting

If routing does not appear to work:

1. Confirm the router is enabled:

   ```sh
   opensquilla config get router.enabled
   opensquilla config get llm.provider
   ```

2. Check provider readiness:

   ```sh
   opensquilla providers status
   opensquilla doctor
   ```

3. If SquillaRouter optional dependencies are missing, OpenSquilla can still run
   with direct single-model routing. On Windows, ONNX Runtime may require the
   Visual C++ Redistributable. On macOS terminal installs, LightGBM may require
   `libomp` from Homebrew:

   ```sh
   brew install libomp
   opensquilla gateway restart
   ```

4. If you need deterministic model behavior for a run, disable routing:

   ```sh
   opensquilla configure router --router disabled
   ```

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
