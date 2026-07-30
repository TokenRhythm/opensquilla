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

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
