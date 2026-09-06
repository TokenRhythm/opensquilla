# Meta-Skill Authoring Guide

This guide is for authors and maintainers who write, validate, and review
OpenSquilla MetaSkills. For user-facing guidance, read
[`../features/meta-skill-user-guide.md`](../features/meta-skill-user-guide.md).

## What a MetaSkill Is

A MetaSkill is a `SKILL.md` file with:

- `kind: meta`;
- one or more natural-language `triggers`;
- a `composition:` block that defines a directed acyclic graph of steps.

At runtime, the model may call:

```text
meta_invoke(name="<meta-skill-name>")
```

OpenSquilla then executes the declared composition step by step and returns the
final result to the user. The model chooses the workflow, but the runtime
enforces dependency order, template rendering, risk metadata, recursion guards,
tool gates, pauses, and final text selection.

Operators can disable model-visible MetaSkill behavior globally:

```toml
[meta_skill]
enabled = false
```

When disabled, MetaSkills remain installed for inventory and historical run
inspection, but they are not injected into prompts, `meta_invoke` is not
surfaced to the model, and explicit `meta_invoke` calls are rejected.

## When to Use a MetaSkill

Use a MetaSkill when a task is repeatable and naturally decomposes into a small
workflow, for example:

- classify the user request, then route to the right specialist skill;
- run two independent analysis skills, then merge their outputs;
- search or inspect context, then summarize it into a user-facing answer;
- execute a deterministic CLI-backed skill, then review or persist the result;
- pause for structured user input before continuing.

Do not use a MetaSkill for one-off instructions, open-ended planning that should
remain conversational, or flows that need arbitrary recursion. A MetaSkill
cannot compose another MetaSkill.

## Where to Put a MetaSkill

For local managed skills, create:

```text
~/.opensquilla/skills/<skill-name>/SKILL.md
```

For repository-bundled skills, place the skill under the bundled skills tree:

```text
src/opensquilla/skills/bundled/<skill-name>/SKILL.md
```

Generated proposals are reviewed before installation. After accepting a
proposal, OpenSquilla promotes it into the managed skills directory and refreshes
the live skill loader.

## Basic Authoring Flow

1. Define the task contract: inputs, output, boundaries, false positives, and
   user-confirmation points.
2. Write a conservative `name`, `description`, and `triggers`.
3. Split the workflow into steps such as intake, collect, analyze, draft, audit,
   and deliver.
4. Add `depends_on` whenever a step needs output from earlier steps.
5. Filter all user input and previous step output in templates.
6. Declare risk metadata and capabilities.
7. Run deterministic and soft-activation checks.
8. Inspect proposal and auto-enable audit output before accepting or enabling.

Users can activate a MetaSkill in two ways:

- Soft activation: ask naturally, and let the model choose the right
  `meta_invoke` call.
- Explicit activation: ask for the named MetaSkill when debugging or testing.

## Required Frontmatter

Every MetaSkill should declare:

```yaml
---
name: short-stable-name
kind: meta
description: One sentence that tells the model when this workflow applies.
triggers:
  - short phrase users naturally type
meta_priority: 50
always: false
final_text_mode: auto
metadata:
  opensquilla:
    risk: low
    capabilities: []
composition:
  steps: []
---
```

The fields have these meanings:

- `name`: stable identifier used by `meta_invoke`.
- `kind`: must be `meta`.
- `description`: model-facing description for when to use the workflow.
- `triggers`: phrases used by deterministic and model-assisted activation.
- `meta_priority`: sort key when multiple MetaSkills may match.
- `always`: normally `false`; MetaSkills should not be injected unconditionally.
- `final_text_mode`: how the final answer is derived.
- `metadata.opensquilla.risk`: highest unattended auto-enable risk.
- `metadata.opensquilla.capabilities`: explicit side-effect capabilities.
- `composition.steps`: ordered DAG definition.

## Risk Metadata

Use `metadata.opensquilla.risk` to declare the highest risk level required by
the workflow:

- `low`: read-only reasoning, classification, summarization, or safe local
  inspection.
- `medium`: local file or artifact writes, deterministic document generation, or
  network reads.
- `high`: shell/process control, credential use, network writes, external side
  effects, or direct tool calls that can alter state.

Use `metadata.opensquilla.capabilities` to make side effects explicit. Common
capabilities include:

- `filesystem-write`;
- `artifact-write`;
- `document-export`;
- `network`;
- `network-read`;
- `network-write`;
- `external-side-effect`;
- `credential-use`;
- `process-control`;
- `shell`.

If a referenced sub-skill lacks risk metadata, unattended auto-enable treats the
dependency conservatively. New skills should declare risk and capabilities
instead of relying on legacy compatibility fallbacks.

## Step Types

MetaSkill steps support these execution kinds.

### `agent`

Use `agent` for a normal skill-backed sub-agent turn. This is the best default
for user-facing reasoning and synthesis.

```yaml
- id: summarize
  kind: agent
  skill: summarize
  with:
    text: "{{ outputs.search | truncate(2000) }}"
```

### `llm_chat`

Use `llm_chat` for one bounded LLM generation step with no tool loop. This is
useful for intake normalization, compact drafting, final audit, and lightweight
synthesis.

```yaml
- id: normalize
  kind: llm_chat
  with:
    system: "Extract the request fields. Do not ask a question."
    task: "{{ inputs.user_message | xml_escape | truncate(1000) }}"
```

### `llm_classify`

Use `llm_classify` when the step should return exactly one value from a closed
set. This is useful for routing, triage, and compact decisions.

```yaml
- id: classify
  kind: llm_classify
  output_choices: [BUG, FEATURE, QUESTION]
  with:
    text: "{{ inputs.user_message | xml_escape | truncate(512) }}"
```

### `user_input`

Use `user_input` when the workflow should pause and collect structured data from
the user. The step requires a `clarify:` schema.

```yaml
- id: collect_project
  kind: user_input
  when: "'NEEDS_CLARIFICATION: yes' in outputs.intake"
  clarify:
    mode: form
    intro: "A few fields are needed before this workflow can continue."
    nl_extract: true
    fields:
      - name: topic
        type: string
        required: true
        prompt: "Topic"
        max_chars: 200
```

The supported field types are `string`, `enum`, `int`, and `bool`. Use
`skip_if` or `when` to avoid pausing when intake has enough information.

### `tool_call`

Use `tool_call` only for deterministic direct tool execution. Declare a
`tool_allowlist`, keep arguments narrow, and mark the MetaSkill as high risk when
the tool can change state.

```yaml
- id: persist
  kind: tool_call
  tool: memory_save
  tool_allowlist: [memory_save]
  tool_args:
    text: "{{ outputs.summary | truncate(2000) }}"
```

### `skill_exec`

Use `skill_exec` for a skill with an `entrypoint:` manifest that should run as a
subprocess. This is appropriate for deterministic CLI-backed skills such as
document conversion or report generation.

```yaml
- id: render
  kind: skill_exec
  skill: html-to-pdf
  with:
    html: "{{ outputs.report | truncate(12000) }}"
```

## Step Labels and Progress

Two optional step-level fields drive the WebUI run progress ribbon
(see [`../features/meta-skill-user-guide.md`](../features/meta-skill-user-guide.md)
"Run Progress Ribbon"):

- `label`: a short, human-readable name for the step. The ribbon renders
  this as the chip text. If omitted, the WebUI humanizes the step `id`
  (e.g. `intake` → `Intake`).
- `progress_emits`: whether the step's executor may publish live
  `status_text` updates to the ribbon. Defaults by kind:
  - `agent` / `skill_exec`: `true`
  - `tool_call`: `false`
  - `llm_chat` / `llm_classify` / `user_input`: ignored

Example:

```yaml
composition:
  steps:
    - id: intake
      kind: llm_chat
      label: 意图提取
      with: { ... }
    - id: search
      kind: agent
      skill: web-research
      label: 检索证据
      progress_emits: true
      with: { ... }
```

Keep labels short (2-6 chars in CJK, 1-2 words in English). Long labels
get truncated in the ribbon header.

## Dependencies and Parallelism

Steps without dependencies may run in parallel. A step with `depends_on` waits
for all named steps to finish.

```yaml
composition:
  steps:
    - id: inspect_code
      kind: agent
      skill: code-reviewer
      with:
        request: "{{ inputs.user_message | xml_escape | truncate(512) }}"

    - id: inspect_tests
      kind: agent
      skill: test-engineer
      with:
        request: "{{ inputs.user_message | xml_escape | truncate(512) }}"

    - id: merge
      kind: agent
      skill: summarize
      depends_on: [inspect_code, inspect_tests]
      with:
        text: |
          Code review:
          {{ outputs.inspect_code | truncate(2000) }}

          Test review:
          {{ outputs.inspect_tests | truncate(2000) }}
```

The graph must be acyclic. A step may only depend on step ids declared in the
same composition.

## Routing and Failure Handling

Use `route` when an `agent` or `skill_exec` step should choose a skill based on
previous outputs:

```yaml
- id: classify
  kind: llm_classify
  output_choices: [DOCS, BUG, SECURITY]
  with:
    text: "{{ inputs.user_message | xml_escape | truncate(512) }}"

- id: handle
  kind: agent
  skill: summarize
  depends_on: [classify]
  route:
    - when: "outputs.classify == 'DOCS'"
      to: writer
    - when: "outputs.classify == 'BUG'"
      to: debugger
    - when: "outputs.classify == 'SECURITY'"
      to: security-reviewer
  with:
    request: "{{ inputs.user_message | xml_escape | truncate(512) }}"
```

Use `on_failure` for a single substitute step. The substitute must exist in the
same plan, must not have its own dependencies, and must not have its own
`on_failure`.

## Final Text Modes

Use `final_text_mode` to control the final user-facing result:

- `auto`: default. The orchestrator summarizes step outputs into a concise final
  answer.
- `raw`: return the last non-substitute step output verbatim.
- `step:<step_id>`: return one specific step output verbatim.

Examples:

```yaml
final_text_mode: auto
final_text_mode: raw
final_text_mode: "step:summarize"
```

Use `step:<step_id>` when one step is the intended deliverable. Use `raw` when
the final step already formats a complete report. Use `auto` when the workflow
produces several intermediate outputs that need a compact user-facing summary.

## Template Safety

Templates are Jinja expressions. Treat user input and previous step output as
untrusted:

- For user text, start with `xml_escape` or `slugify`, then bound it with
  `truncate`.
- For `outputs.<step_id>`, always bound or encode with `truncate`, `xml_escape`,
  `slugify`, or `tojson`.
- Do not pass raw `{{ inputs.user_message }}` into a downstream step.
- Do not pass raw `{{ outputs.some_step }}` into another step.
- Keep prompt-shaped strings explicit, short, and task-specific.

Safe examples:

```yaml
query: "{{ inputs.user_message | xml_escape | truncate(512) }}"
text: "{{ outputs.search | truncate(2000) }}"
slug: "{{ inputs.user_message | slugify | truncate(80) }}"
payload: "{{ outputs.plan | tojson }}"
```

Unsafe examples:

```yaml
query: "{{ inputs.user_message }}"
text: "{{ outputs.search }}"
```

## Activation Guidance

MetaSkills are manual-only by default in normal product use. Users launch them
with `/meta <name>`, so trigger and soft-activation checks are needed only when
you are intentionally supporting `meta_skill.auto_trigger = true`.

When maintaining auto-trigger compatibility, write triggers as short phrases
users naturally type:

- Prefer: `summarize recent history`
- Prefer: `review current diff`
- Avoid: `run the internal OpenSquilla DAG composition meta skill`

Use two to five triggers unless a production workflow has a tested reason to use
more. Avoid triggers that collide with explanation questions such as "how does
this meta-skill work?" A user asking about a MetaSkill should not accidentally
run it when auto-trigger compatibility is enabled.

Set `description` to explain when the model should choose the workflow in
auto-trigger mode. Do not hide critical constraints in the body only; the model
primarily sees the frontmatter and injected skill summary when auto-trigger is
enabled.

## Validation Checklist

Before sharing or enabling a MetaSkill:

1. Confirm the frontmatter parses as YAML.
2. Confirm `kind: meta` and `composition.steps` are present.
3. Confirm all `depends_on`, `route.to`, and `on_failure` references point to
   valid steps or skills.
4. Confirm the graph has no cycles.
5. Confirm all user input and step outputs are filtered.
6. Confirm `metadata.opensquilla.risk` and `metadata.opensquilla.capabilities`
   reflect the workflow's true side effects.
7. If supporting `meta_skill.auto_trigger = true`, run deterministic trigger
   checks with `scripts/meta_trigger_accuracy.py`.
8. If supporting `meta_skill.auto_trigger = true`, run model-decision soft
   activation checks with
   `scripts/live_meta_soft_activation_e2e.py --env-file /path/to/.env`.
9. For generated skills, inspect the Web UI proposal detail and its auto-enable
   audit before accepting or enabling.

## Troubleshooting

If the MetaSkill does not appear to run:

- Check that the `SKILL.md` is under a loaded skill directory.
- Refresh or restart the gateway if the skill was added outside the proposal
  accept flow.
- Confirm you ran it with `/meta <name>` on a surface that supports MetaSkill
  runs.
- Confirm `disable-model-invocation` is not set for a MetaSkill you expect the
  model to invoke.
- If you expect natural-language auto-triggering, confirm
  `meta_skill.auto_trigger = true`.
- Confirm the skill has `kind: meta` and a non-empty `composition.steps` list.
- Confirm the user wording matches the triggers or description.

If parsing fails:

- Check duplicate step ids.
- Check unknown `kind` values.
- Check missing `skill` for `agent` or `skill_exec` steps.
- Check missing `output_choices` for `llm_classify`.
- Check missing `clarify.fields` for `user_input`.
- Check missing `tool`, invalid `tool_args`, or mismatched `tool_allowlist` for
  `tool_call`.
- Check cycles and undefined `depends_on` references.

If auto-enable is skipped:

- Inspect the proposal's auto-enable audit in the Web UI.
- Add missing risk metadata to referenced sub-skills.
- Lower the workflow's side effects, or require manual review for medium/high
  risk workflows.

## Test Prompts

At minimum include:

- English positive trigger;
- explicit invocation;
- pasted-history negative case;
- neighboring-domain negative case;
- output-quality judge rubric.

Use realistic user phrasing with a clear subject and goal. Avoid operator-style
phrases that users would not naturally type.

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
