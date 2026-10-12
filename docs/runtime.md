# MetaSkill runtime reference

Run every command below from the repository root. Install the checkout and
configure `configs/local.toml` using the [README](../README.md) before running
model examples. The selected provider, model ID, endpoint, and API-key
environment variable must belong to the same supplier.

This entry point uses OpenSquilla's Agent, provider adapters, native tool
registry, policy dispatcher, and tool handlers. It packages runtime libraries
for `scripts/run_metaskill.py`; Desktop, Web UI, and Gateway application entry
points are outside this release's scope.

## Step types and examples

There are five step types. `agent` runs a complete Skill package through the
Agent tool loop. The other types provide script execution, direct tools, and
model calls without that loop. These are execution components, not five
benchmark methods. The paper's frozen MetaSkill benchmark graphs use `agent`
nodes to invoke complete Skills.

All supplied examples use synthetic inputs.

| Step type | Example | What it demonstrates | Model needed |
| --- | --- | --- | --- |
| `skill_exec` | [meta-text-demo](../examples/skills/meta-text-demo/SKILL.md) | Two Python Skills run independently, then merge named outputs | No |
| `tool_call` | [meta-tool-demo](../examples/skills/meta-tool-demo/SKILL.md) | Native write/read/write with explicit text binding | No |
| `agent` | [meta-writing-demo](../examples/skills/meta-writing-demo/SKILL.md) | Complete Skill resources and sequential text handoff | Yes, with tool calling |
| `agent` | [meta-file-agent-demo](../examples/skills/meta-file-agent-demo/SKILL.md) | Complete Skill packages, parallel branches, fan-in, selected text binding, and shared files | Yes, with tool calling |
| `llm_chat` | [meta-chat-demo](../examples/skills/meta-chat-demo/SKILL.md) | Two text-only calls with an upstream draft binding | Yes |
| `llm_classify` | [meta-route-demo](../examples/skills/meta-route-demo/SKILL.md) | Closed labels, conditional `when` branches, and Skill selection through `route` | Yes |

### Python Skills: parallel execution and join

```text
uv run --locked python scripts/run_metaskill.py --name meta-text-demo --input "Hello MetaSkill" --workspace workspace/demo
```

Two steps transform text in parallel; a third combines their named outputs.
This command needs no API key. Expected output:

```text
UPPER: HELLO METASKILL
lower: hello metaskill
```

`skill_exec` scripts execute as your operating-system user, independently of
the native tool sandbox. Run only scripts you trust.

### Native tools: file and text handoffs

```text
uv run --locked python scripts/run_metaskill.py --name meta-tool-demo --input "Hello MetaSkill" --workspace workspace/tools --run-mode full
```

This creates `workspace/tools/source.txt` and `handoff.txt`. The latter contains
the native read tool's formatted output. The command prints the last write
tool's status. No model is needed; `full` grants tools your operating-system
access. See [permissions](#permissions-and-native-tools).

### Agent Skills: resource loading and sequential handoff

```text
uv run --locked python scripts/run_metaskill.py --name meta-writing-demo --input "Write a short announcement for a local reading group." --workspace workspace/writing --config configs/local.toml --run-mode full
```

The two Agent steps load their own package resources and pass the draft to the
editor. Output wording depends on the model. The model must support tool
calling, and requests use your provider account.

### Agent Skills: parallel branches, fan-in, and shared files

```text
uv run --locked python scripts/run_metaskill.py --name meta-file-agent-demo --input "The library opens at nine." --workspace workspace/file-agent --config configs/local.toml --run-mode full --trace-json workspace/file-agent/trace.json
```

`create` reads a package template and writes `note.txt`. `format` and `check`
both depend on `create` and may run concurrently; they write `formatted.txt`
and `checks.txt`. `review` waits for both branches, binds only `outputs.format`
as text, reads both files, and writes `reviewed.txt`.

Dependencies determine when a node runs; bindings determine which predecessor
text it receives. A dependency does not automatically inject that predecessor's
text. Files travel through the shared workspace separately. The examples retain
the complete Skill packages, including their templates and references.

### Direct model calls: text-only handoff

```text
uv run --locked python scripts/run_metaskill.py --name meta-chat-demo --input "The library opens at nine." --workspace workspace/chat --config configs/local.toml
```

The command returns a polished sentence. Each successful step normally makes
one model call and exposes no tools. This does not run a complete Skill through
the Agent loop.

### Classification, conditional branches, and Skill routing

```text
uv run --locked python scripts/run_metaskill.py --name meta-route-demo --input "Use uppercase: Hello MetaSkill" --workspace workspace/route-upper --config configs/local.toml --run-mode full
uv run --locked python scripts/run_metaskill.py --name meta-route-demo --input "Use lowercase: Hello MetaSkill" --workspace workspace/route-lower --config configs/local.toml --run-mode full
```

The classifier chooses `UPPER` or `LOWER`. The matching branch writes a marker
file; the other branch is skipped. The final Python Skill returns the
transformed input. Use separate workspaces so files from earlier runs cannot
obscure branch selection.

`route.to` names a Skill, rather than another step. A malformed classification
can trigger repair or an Agent fallback and additional model calls. The
permission mode is separate from the classification and routing behavior.

## Permissions and native tools

`safe` is the default permission mode for native tools and requires a usable
platform sandbox. The tool examples above explicitly select `full`, which runs
tools with your operating-system access. Run only Skills you trust. These
permission modes are separate from the five step types; `skill_exec` scripts
execute as your user independently of the native tool sandbox.

List the native tools available to this entry point:

```text
uv run --locked python scripts/run_metaskill.py --list-tools
```

These include file operations, `exec_command`, `execute_code`, background
processes, PDF and image handling, web access, and Git. Tool names and argument
schemas come from the native registry. Use `[tools].allow` or `[tools].deny` in
your local TOML configuration to choose a narrower set.

Install external programs required by your Skills separately, such as Node.js,
a browser, FFmpeg, or LibreOffice. They are not bundled here. Configure
`[vision]` for image analysis and `[search]` for web search when needed; the
selected model and provider must support the requested capability. Image
analysis configuration does not grant an external Skill's image-generation
service access.

## Custom Skills and inputs

1. Put each Skill in a directory with a `SKILL.md` and any required scripts or resources.
2. Start with [meta-text-demo](../examples/skills/meta-text-demo/SKILL.md) or [meta-writing-demo](../examples/skills/meta-writing-demo/SKILL.md).
3. Set `depends_on` for ordering and `{{ outputs.step_id }}` for upstream text bindings.
4. Pass the parent directory to `--skills-dir` and the meta Skill's frontmatter name to `--name`.

For a trusted custom Skill that uses native tools:

```text
uv run --locked python scripts/run_metaskill.py --skills-dir path/to/skills --name your-meta-skill --input "Your task" --workspace workspace/custom --config configs/local.toml --run-mode full
```

Choose `safe` instead only with a usable platform sandbox and suitable
permissions for that Skill. A graph containing only direct `llm_chat` calls
does not need `full`. A graph containing only `skill_exec` steps does not need
a model configuration, but its scripts still run as your user.

`--input` supplies task text, not a file path. Use `--input-file path/to/task.txt`
to read a UTF-8 task file instead; these options are mutually exclusive.

`--inputs-json path/to/inputs.json` supplies an object with additional named
inputs, which templates can access as `{{ inputs.your_field }}`. `user_message`
comes from `--input` or `--input-file`, `workspace_dir` comes from `--workspace`,
and `meta_run_id` is runtime-owned; these fields cannot be overridden through
the JSON file. Include `{{ inputs.user_message }}` in a node's `with` arguments
when that invocation needs the original task.

Duplicate or missing node IDs, undefined dependencies, and dependency cycles
are rejected before execution. A reference to an unavailable template variable
fails rendering. Independent ready nodes may run concurrently; a fan-in node
waits for all declared predecessors.

## Runtime budgets and final output

Use the `[runtime]` table in the explicit TOML configuration:

| Setting | Scope |
| --- | --- |
| `max_parallelism` | Maximum number of dependency-ready nodes running concurrently |
| `timeout_seconds` | Each Agent's total and iteration budget; not a whole-DAG deadline |
| `request_timeout` | Each model HTTP request, including `llm_chat` and `llm_classify`; default 120 seconds |
| `max_iterations` | Each Agent's iteration limit |
| `max_tokens`, `context_window_tokens` | Native model output and context budgets |
| `temperature`, `top_p`, `thinking`, `thinking_budget_tokens` | Explicit sampling and thinking settings, subject to provider and model support |

All supplied examples set `final_text_mode: "step:<id>"` to return a selected
step's output. The default `auto` mode can make an extra model call to summarize
results; `raw` returns the last output. Supplying a language preference can also
trigger a model call to repair the final answer's language.

Provider errors and empty `llm_chat` responses fail the run with a nonzero exit
status. A terminal Agent error fails its composed step even if a later native
completion event retains partial text and usage for accounting. Partial files
may remain in the workspace; they do not prove successful completion.

## Local execution traces

Add `--trace-json workspace/your-run/trace.json` to a command using
`--workspace workspace/your-run`. The trace path must be inside that workspace.
It exports the native execution ledger with the plan, step times and states,
rendered inputs, and outputs. Missing persistence or incomplete terminal records
cause export to fail rather than manufacture a successful trace.

Trace fields may be truncated by the native writer. An explicit positive
`--max-trace-field-bytes` value, used together with `--trace-json`, enables a
larger secret-redacted projection of rendered inputs within that limit. The
backing database retains native clipping and redaction behavior; an expanded
projection that exceeds the limit is marked truncated.

When tracing is enabled, native provider-call receipts are also retained.
Missing usage or cost receipts remain unknown, not zero. Calls made by external
Skill programs or task-specific paid APIs are outside that accounting.

These records contain the current run's content and should stay local. The
backing database and runtime caches are inside the workspace's
`.metaskill-state/` directory. Use ignored `workspace/` paths for runs, keep
credentials in environment variables, and keep local configs and generated
files out of source commits. Custom Skills control where they write generated
files; choosing a workspace is not a filesystem confinement guarantee.

## Standalone scope and platform validation

Only the explicit configuration file and Skill directory are loaded.
Interactive clarification, application sessions, and memory persistence need
an application host; the standalone entry point does not expose them.

The CLI and automated suite have been validated with Python 3.12 on Windows
and Ubuntu 24.04 under WSL 2. POSIX lifecycle tests exercise real process groups,
including batch timeout, repeated SIGTERM, and native background and command
process cleanup. These checks cover runtime and evaluator integration; each
task still needs its external programs and services. macOS commands are
provided but have not been validated on a Mac.

See the [README](../README.md) for installation and the MetaSkill-only benchmark
entry point, and [benchmark provenance](../benchmark_data/PROVENANCE.md) for
frozen-input sources and disclosed runtime adaptations.
