# OpenSquilla MetaSkill

MetaSkill composes Skill packages into executable dependency graphs in OpenSquilla. Each Skill keeps its natural-language instructions, scripts, and resources; the runtime controls execution order, parallel branches, and predecessor text bindings. Skills exchange generated files through a shared workspace.

This independent paper branch of OpenSquilla provides the standalone MetaSkill runtime and a MetaSkill-only runner for the paper's 20 designated AgentSkillOS Bench tasks. It preserves the research implementation separately from ongoing development on the main branch.

- **Try MetaSkill:** start with the [no-key example](#quick-start), then [run a complete Skill graph](#run-a-complete-skill-graph).
- **Run the benchmark:** follow [one original task for three trials](#run-one-original-task-three-times), then [all 20 tasks](#run-all-20-tasks).

## Install

Install [Git](https://git-scm.com/downloads) and [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```text
git clone --branch paper/metaskill-lp4fm-2026 https://github.com/TokenRhythm/opensquilla.git
cd opensquilla
uv sync --locked --python 3.12
```

Use a short Windows checkout path such as `C:\src\opensquilla`. In WSL, clone into the Linux filesystem, for example `~/opensquilla`, and install uv and task tools inside Linux. Keep all commands below in the repository root.

Windows and Ubuntu 24.04 under WSL 2 have been validated with Python 3.12. macOS commands are provided but have not been tested on a Mac. This checkout exposes CLI runtime libraries; Desktop, Web UI, and Gateway application entry points are outside its scope.

## Quick start

Run a synthetic Python Skill graph without an API key:

```text
uv run --locked python scripts/run_metaskill.py --name meta-text-demo --input "Hello MetaSkill" --workspace workspace/demo
```

Two steps transform text in parallel; a third combines their named outputs:

```text
UPPER: HELLO METASKILL
lower: hello metaskill
```

The examples use synthetic inputs. See [all execution examples](docs/runtime.md) for native file tools, text-only model calls, and conditional routing.

## Use a model

Choose **OpenRouter** or **TokenRhythm**, and copy the demo configuration.

PowerShell:

```powershell
Copy-Item configs/example.toml configs/local.toml
```

Linux/macOS:

```sh
cp configs/example.toml configs/local.toml
```

Edit `configs/local.toml`. Replace its `[model]` block with **one** of the following, using a model ID available to your account. The complete Skill examples need a model with tool calling.

**OpenRouter** — choose an ID from the [model list](https://openrouter.ai/models):

```toml
[model]
provider = "openrouter"
name = "YOUR_OPENROUTER_MODEL_ID"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
```

**TokenRhythm** — use the ID issued in your [console](https://tokenrhythm.studio):

```toml
[model]
provider = "tokenrhythm"
name = "YOUR_TOKENRHYTHM_MODEL_ID"
base_url = "https://tokenrhythm.studio/v1"
api_key_env = "TOKENRHYTHM_API_KEY"
```

`provider` selects the adapter; `name` and `base_url` identify the supplier's model and endpoint. `api_key_env` names the environment variable containing that supplier's key. It does not select a supplier or contain a key itself.

Set only your selected supplier's key, in the shell used to run MetaSkill:

| Supplier | PowerShell | Linux/macOS |
| --- | --- | --- |
| OpenRouter | `$env:OPENROUTER_API_KEY = "your-openrouter-api-key"` | `export OPENROUTER_API_KEY="your-openrouter-api-key"` |
| TokenRhythm | `$env:TOKENRHYTHM_API_KEY = "your-tokenrhythm-api-key"` | `export TOKENRHYTHM_API_KEY="your-tokenrhythm-api-key"` |

The four model fields and the key must refer to the same supplier. Model calls use your provider account.

## Run a complete Skill graph

After configuring the model, run this example with native file tools:

```text
uv run --locked python scripts/run_metaskill.py --name meta-file-agent-demo --input "The library opens at nine." --workspace workspace/file-agent --config configs/local.toml --run-mode full --trace-json workspace/file-agent/trace.json
```

`create` loads a package template and writes `note.txt`. `format` and `check` can then run in parallel, writing `formatted.txt` and `checks.txt`. `review` waits for both, receives only `outputs.format` as a text binding, reads both files, and writes `reviewed.txt`. The trace records step states, timing, inputs, and outputs.

Dependencies determine when a node runs; bindings determine which predecessor text it receives. Files are shared separately through the workspace. See [the example graph](examples/skills/meta-file-agent-demo/SKILL.md).

`--run-mode full` lets trusted Skills use your operating-system access. The default `safe` mode needs a usable platform sandbox; `skill_exec` scripts run as your user independently of that native-tool sandbox. Keep generated files and traces under the ignored `workspace/` directory.

There are five execution types: `agent`, `skill_exec`, `tool_call`, `llm_chat`, and `llm_classify`. These are runtime components. **The paper's benchmark graphs use `agent` nodes to execute complete Skill packages.** The [runtime guide](docs/runtime.md) covers all five types, tools, permissions, and configuration options.

## Run one original task three times

This workflow starts from public **AgentSkillOS Bench** inputs and runs MetaSkill only. It reports official objective checks and execution conformance. It evaluates new runs; it does not reproduce the paper's blinded pairwise quality preferences against other methods or guarantee the same scores from changing hosted models.

Start with `data_computation_task5`, which produces statistical-test JSON and a spreadsheet from datasets embedded in the original prompt. **One task × three independent trials = three task runs per model.** Every trial uses the same frozen graph with a fresh workspace and no outputs from earlier trials.

### 1. Prepare the inputs

```text
uv sync --locked --extra benchmark --python 3.12
uv run --locked --extra benchmark python scripts/run_benchmark.py prepare --cache workspace/benchmark/cache
```

Preparation downloads and verifies the pinned public tasks, evaluators, and complete Skill packages for all 20 tasks. The commands below select only one task to execute. Frozen plans and source fingerprints are included; original experiment logs, transcripts, scores, generated files, and comparison-method runners are not. See [input provenance](benchmark_data/PROVENANCE.md) and [preparation details](docs/benchmark.md).

### 2. Configure this benchmark run

Use the benchmark template, which supplies the benchmark budgets and tool settings.

PowerShell:

```powershell
Copy-Item configs/benchmark.example.toml configs/benchmark.local.toml
```

Linux/macOS:

```sh
cp configs/benchmark.example.toml configs/benchmark.local.toml
```

Edit **`configs/benchmark.local.toml`**. Set its four `[model]` fields using the [OpenRouter or TokenRhythm configuration above](#use-a-model), and set the matching key in this shell. Keep the template's `[runtime]` and `[tools]` sections. Its initial model selection is OpenRouter `z-ai/glm-5.2`; confirm availability or replace it with your chosen deployment.

The example graph uses `configs/local.toml`; benchmark commands use `configs/benchmark.local.toml`. Use a separate benchmark config and output directory for each model. The template enables `full` tool access for trusted benchmark Skills, and each task run may make multiple paid model calls.

### 3. Install this task's tool and check readiness

The benchmark extra provides its scientific Python libraries. This introductory task also needs **LibreOffice**, with `soffice` available in the same shell.

On Windows, [install LibreOffice](https://www.libreoffice.org/installation-instructions/), then add its program directory if needed. Adjust the path if you used a different installation location:

```powershell
$env:PATH = "C:\Program Files\LibreOffice\program;" + $env:PATH
Get-Command soffice
```

On Ubuntu/WSL, install [LibreOffice Calc](https://packages.ubuntu.com/noble/libreoffice-calc):

```sh
sudo apt-get update
sudo apt-get install libreoffice-calc
command -v soffice
```

Other platforms should install LibreOffice through their usual installer or package manager and expose `soffice` on PATH. Now check **only this task**, without calling a model:

```text
uv run --locked --extra benchmark python scripts/run_benchmark.py check --prepared workspace/benchmark/cache/prepared.json --config configs/benchmark.local.toml --task-id data_computation_task5
```

Continue when `errors` and `missing_tools` are both empty. This check verifies configuration and local requirements, not external account permissions. Other tasks have [additional dependencies](docs/benchmark.md#install-task-specific-tools).

### 4. Run three trials and evaluate the same directory

```text
uv run --locked --extra benchmark python scripts/run_benchmark.py run --prepared workspace/benchmark/cache/prepared.json --config configs/benchmark.local.toml --task-id data_computation_task5 --output workspace/benchmark/single-task --repeats 3
```

Every attempt is evaluated automatically. To repeat evaluation without model calls, use the **same output directory**:

```text
uv run --locked --extra benchmark python scripts/run_benchmark.py evaluate --output workspace/benchmark/single-task
```

Open `workspace/benchmark/single-task/summary.json` and `trials.csv`. For a complete, successful three-trial execution, check:

| Check | Expected value |
| --- | --- |
| `coverage.expected_trials`, `coverage.recorded_trials`, and `coverage.completed_trials` | All `3` |
| `coverage.missing_trials` | `[]` |
| Three rows in `trials.csv` | Trials `1`, `2`, `3`, each with `status=completed`, `execution_status=completed`, and `exit_code=0` |
| Each row's `objective_status` | `completed` (evaluation ran successfully) |
| Each row's `conformance_status` | `valid`; investigate `invalid` or `unknown` |

Then inspect `objective_score` and the summary's mean, standard deviation, and range. Execution success does not guarantee a full score. The official score checks artifact structure and task assertions; it is not the paper's comparative quality ranking. A populated text binding means its predecessor record exists and the rendered field is non-empty, not that its content has been verified as correct.

Each attempt's files are under `runs/<task>/trial-XX/attempt-XX/workspace/`. Failures and retries remain recorded. See [evaluation and recovery details](docs/benchmark.md) before retrying; keep all local results out of source commits.

## Run all 20 tasks

After the single-task workflow succeeds, install the [dependencies for the full task set](docs/benchmark.md#install-task-specific-tools) and run the full preflight:

```text
uv run --locked --extra benchmark python scripts/run_benchmark.py check --prepared workspace/benchmark/cache/prepared.json --config configs/benchmark.local.toml
```

Resolve missing requirements, then use a **new output directory** for the full batch:

```text
uv run --locked --extra benchmark python scripts/run_benchmark.py run --prepared workspace/benchmark/cache/prepared.json --config configs/benchmark.local.toml --output workspace/benchmark/full-run --repeats 3
uv run --locked --extra benchmark python scripts/run_benchmark.py evaluate --output workspace/benchmark/full-run
```

**20 tasks × three trials = 60 task runs per model.** For two models, run a separate 60-run batch for each, with its own config and output directory. These counts refer to task runs, not individual API calls. Ready nodes within a graph may run concurrently; task attempts run sequentially.

Add `--resume` to the same run command to continue an interrupted batch. Add both `--resume --retry-failed` to retain earlier failures and create new attempts. Changing task selection, model configuration, repeat count, or deadline requires a new output directory. See the [benchmark guide](docs/benchmark.md) for deployment fingerprints, recovery, and result interpretation.

## Add a Skill

1. Put each Skill's `SKILL.md`, scripts, and resources in its own directory.
2. Start a MetaSkill from [the script graph](examples/skills/meta-text-demo/SKILL.md) or [the complete Skill graph](examples/skills/meta-file-agent-demo/SKILL.md).
3. Use `depends_on` for ordering and `{{ outputs.step_id }}` for predecessor text. Include `{{ inputs.user_message }}` wherever a node needs the original request.
4. Pass the parent directory to `--skills-dir` and the MetaSkill's frontmatter name to `--name`.

For a trusted custom graph that uses native tools:

```text
uv run --locked python scripts/run_metaskill.py --skills-dir path/to/skills --name your-meta-skill --input "Your task" --workspace workspace/custom --config configs/local.toml --run-mode full
```

See [runtime options and examples](docs/runtime.md) for named inputs, timeouts, concurrency, tool selection, and traces.

## Documentation and checks

- [Runtime guide](docs/runtime.md): five execution types, native tools, custom inputs, and permissions.
- [Benchmark guide](docs/benchmark.md): external dependencies, frozen deployment, recovery, and evaluation details.
- [Provenance](benchmark_data/PROVENANCE.md): pinned sources, frozen plans, and runtime adaptations.

Run the automated suite without external model calls:

```text
uv run --locked --extra benchmark python -m unittest discover -s tests -v
```

Tests use temporary inputs and a local HTTP/SSE model simulator. They cover the five execution types, native tools and Agent loops, resources, parallel branches, fan-in, bindings, routing, file handoffs, and error handling. Windows and WSL runtime/evaluator checks do not establish that all 20 tasks succeed with every model or external service.

## License

Apache-2.0. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for retained attribution.
