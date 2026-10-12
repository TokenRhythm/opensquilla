# MetaSkill benchmark reference

Run every command below from the **repository root**, not from `docs/`.
For the shorter prepare/check/run/evaluate walkthrough, start with the
[README](../README.md).

This path starts from the public **AgentSkillOS Bench** tasks and executes only
MetaSkill. It prepares the paper's 20 designated tasks and complete Skill
packages, renders their frozen dependency graphs, runs **three independent
trials per task (60 task runs per model)**, and evaluates the resulting files
and execution traces. Each trial reuses the same frozen graph; it does not
reuse another trial's outputs or regenerate the plan. A task run can make
multiple model calls.

The repository contains source code, source fingerprints, frozen plans, and a
deployment template. Original experiment logs, transcripts, scores, generated
files, and comparison-method runners are not included. Tasks, task input assets,
upstream evaluators, and public Skills are downloaded into an ignored local cache.

## Prepare the original tasks and Skills

```text
uv sync --locked --extra benchmark --python 3.12
uv run --locked --extra benchmark python scripts/run_benchmark.py prepare --cache workspace/benchmark/cache
```

The source manifest pins [AgentSkillOS](https://github.com/ynulihao/AgentSkillOS/tree/c3cfae10ea34a13973c461273ad9c5cc4aa907fe)
and the designated third-party Skills to specific revisions and verifies each
selected file. It copies full Skill packages, including scripts and references.
Text fingerprints normalize CRLF/LF. The small `sympy` Skill is a bundled local
reconstruction with disclosed provenance; it has no claimed upstream revision.
See [benchmark input provenance](../benchmark_data/PROVENANCE.md).

If the small Manim source archive cannot be downloaded, preparation fetches
the selected raw files from the same pinned revision. All content fingerprints
are still checked; a content mismatch fails preparation.

Preparation stages only prompt-referenced input files. In particular, the
hypothesis-testing task `data_computation_task5` uses numbers embedded in its
prompt; the unrelated upstream `task_data` directory with the same name is not
treated as its input. Evaluation rules remain outside the agent's workspace.

On Windows, use a short checkout path such as `C:\src\metaskill-paper`; some
third-party tools still impose path-length limits. In WSL, clone into the Linux
filesystem, for example `~/metaskill-paper`, and create the virtual environment
there. Install system tools inside the Linux distribution, too.

## Configure the model

Copy `configs/benchmark.example.toml` to `configs/benchmark.local.toml`.
This is the benchmark config; the generic runtime examples use a separate
`configs/local.toml`.

PowerShell:

```powershell
Copy-Item configs/benchmark.example.toml configs/benchmark.local.toml
```

Linux/macOS:

```sh
cp configs/benchmark.example.toml configs/benchmark.local.toml
```

The template selects OpenRouter with this `[model]` block:

```toml
[model]
provider = "openrouter"
name = "z-ai/glm-5.2"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
```

Confirm the model is available to your account, or replace `name` with the
exact ID from [OpenRouter's model list](https://openrouter.ai/models).
Set an API key from OpenRouter in the same shell used to launch the benchmark:

```powershell
$env:OPENROUTER_API_KEY = "your-openrouter-api-key"
```

```sh
export OPENROUTER_API_KEY="your-openrouter-api-key"
```

For TokenRhythm, replace **all four** fields in
`configs/benchmark.local.toml`:

```toml
[model]
provider = "tokenrhythm"
name = "YOUR_TOKENRHYTHM_MODEL_ID"
base_url = "https://tokenrhythm.studio/v1"
api_key_env = "TOKENRHYTHM_API_KEY"
```

Copy the exact model ID from your [TokenRhythm console](https://tokenrhythm.studio).
Set an API key from TokenRhythm:

```powershell
$env:TOKENRHYTHM_API_KEY = "your-tokenrhythm-api-key"
```

```sh
export TOKENRHYTHM_API_KEY="your-tokenrhythm-api-key"
```

`provider` selects the runtime adapter, `name` is the supplier's model ID,
and `base_url` is its API endpoint. `api_key_env` names the environment variable
containing that supplier's key; it is not a key issued by MetaSkill. The model
ID and key must belong to the selected endpoint. Setting a key variable alone
does not select a supplier.

Use a separate local config and output directory for each model. The generic
`single-task` and `full-run` directories below each hold one model's batch;
choose new directories when comparing models. `--repeats 3` means three runs
per task **per model**, not three runs divided between models.

### Deployment budgets and runtime adaptations

The template specifies explicit standalone budgets, including 30 Agent
iterations and four ready-node slots. The batch deadline defaults to 5,400
seconds for a whole DAG; `run --timeout` changes that deadline. The runtime's
`timeout_seconds` setting instead limits an individual Agent's total and
iteration budget, while `request_timeout` limits a model HTTP request.

These settings, the frozen plan hashes, source hashes, Python version, and
platform are recorded in your local batch. The standalone runner also freezes
a local `deployment.toml` for the batch, so editing your source config during
a run cannot silently change later trials. The standalone host retains native
tool security grading; it does not recreate the original Gateway bypass profile
or a historical live model catalog. Changing deployment, model version, or tools
can change results.

The release includes an [error-propagation fix](../benchmark_data/PROVENANCE.md#runtime-adaptations):
a terminal Agent error fails its MetaSkill step even when native completion
events retain partial text and usage. Generated files can still receive a score
after an execution failure; inspect the run status alongside the score.

## Install task-specific tools

Install the programs needed by the **selected tasks** before running. The locked
`benchmark` extra provides common scientific Python libraries and document
evaluators; it does not install operating-system programs or external accounts.
For the first `data_computation_task5` run, the XLSX Skill needs LibreOffice's
`soffice` command on PATH; the other task families below are not prerequisites
for that single-task run.

| Skill family | Additional setup |
| --- | --- |
| Scientific plots / data | Scientific Python dependencies are in `--extra benchmark`. |
| LaTeX posters and slides | TeX Live or MiKTeX, relevant poster/beamer packages and fonts; `pdflatex` or `lualatex` on PATH. The scientific-slides `validate_presentation.py` helper specifically invokes `pdflatex`; install it to use that helper even if you compile with LuaLaTeX. |
| DOCX / PPTX | Node.js and LibreOffice's `soffice` command on PATH; DOCX also needs Pandoc. Both Skills use Poppler's `pdftoppm` for visual checks. Skill scripts may require `docx`, `pptxgenjs`, `playwright`, `sharp`, `react`, `react-dom`, and `react-icons`. |
| XLSX | LibreOffice's `soffice` command on PATH for spreadsheet recalculation. |
| Web / browser | Node.js, npm, and Playwright Chromium; on Windows install Git Bash for the upstream `server.sh`. That script installs its per-run dependencies before starting the browser server. |
| Motion / video | FFmpeg and ImageMagick on PATH; install Manim Community and its rendering/LaTeX dependencies for the Manim Skill. |
| Image generation | Original image scripts use `OPENROUTER_API_KEY`, even when the main reasoning model uses TokenRhythm. Their image-model access is separate from `[vision]` image analysis. |
| Firecrawl | `FIRECRAWL_API_KEY` for that Skill's API; its Python SDK is optional when using direct HTTP. |

Configure `[vision]` for image analysis and `[search]` for web search when needed;
the selected model must support the requested capability. These runtime settings
do not supply credentials to external image-generation or Firecrawl scripts.

### Poppler for DOCX/PPTX visual checks

Install Poppler and verify `pdftoppm` in the same shell that will launch the
benchmark. On Windows, download and extract a
[Poppler Windows build](https://github.com/oschwartz10612/poppler-windows/releases),
then add the directory containing `pdftoppm.exe` to PATH. For example, after
extracting it under `C:\tools\poppler` (adjust this path to your installation):

```powershell
$env:PATH = "C:\tools\poppler\Library\bin;" + $env:PATH
Get-Command pdftoppm
pdftoppm -v
```

On Ubuntu/Debian, including Ubuntu in WSL, install
[`poppler-utils`](https://packages.ubuntu.com/noble/poppler-utils):

```sh
sudo apt-get update
sudo apt-get install poppler-utils
command -v pdftoppm
pdftoppm -v
```

Other Linux distributions provide the same command through their Poppler
utilities package. This dependency applies to the DOCX/PPTX visual-check
workflow; it is not required by the basic MetaSkill quick start or every PDF
operation.

### Node packages and shared Playwright browsers

For Node-based document scripts, install packages under an ignored local tools
directory and expose it to Node's resolver.

PowerShell:

```powershell
npm install --prefix workspace/benchmark/node-tools docx pptxgenjs playwright sharp react react-dom react-icons
$env:NODE_PATH = (Resolve-Path workspace/benchmark/node-tools/node_modules).Path
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location).Path "workspace/benchmark/playwright-browsers"
& workspace/benchmark/node-tools/node_modules/.bin/playwright.cmd install chromium
```

Linux/macOS:

```sh
npm install --prefix workspace/benchmark/node-tools docx pptxgenjs playwright sharp react react-dom react-icons
export NODE_PATH="$(pwd)/workspace/benchmark/node-tools/node_modules"
export PLAYWRIGHT_BROWSERS_PATH="$(pwd)/workspace/benchmark/playwright-browsers"
workspace/benchmark/node-tools/node_modules/.bin/playwright install chromium
```

Keep `PLAYWRIGHT_BROWSERS_PATH` set when running the benchmark, so isolated
attempts can find these shared browser binaries. Newly started browser instances
use attempt-local profiles. The pinned browser server uses fixed ports 9222 and
9223; run browser tasks on a dedicated machine or session with those ports free.

The pinned `dev-browser` snapshot has a `bun.lock` but omits `package.json`.
For each writable run copy, the runner reconstructs that manifest from the
lock's own workspace dependencies and supplies ESM/start declarations. It
records this adapter change locally; the frozen source cache stays untouched.
Its server script then runs `npm install`. Other frozen Skill source files and
the graph are preserved. POSIX copies receive executable permissions for shell
scripts. A browser run may still need OS browser libraries; use Playwright's
installation instructions for your platform.

## Check the selected environment

Check only the first task, without calling a model:

```text
uv run --locked --extra benchmark python scripts/run_benchmark.py check --prepared workspace/benchmark/cache/prepared.json --config configs/benchmark.local.toml --task-id data_computation_task5
```

Before running all 20 tasks, check the full environment:

```text
uv run --locked --extra benchmark python scripts/run_benchmark.py check --prepared workspace/benchmark/cache/prepared.json --config configs/benchmark.local.toml
```

The check verifies prepared input fingerprints, model configuration, evaluator
imports, and common task executables / API-key variable presence. It reports
missing requirements by task. It cannot validate external service permissions,
every font or TeX package, or browser availability merely from an installed
executable. Fix reported dependencies before running the selected tasks.

`--allow-missing-tools` is an explicit override for an intentionally limited
environment; such runs may fail or score lower and should not be presented as
equivalent environments.

## Run independent trials

Start with three trials of one task:

```text
uv run --locked --extra benchmark python scripts/run_benchmark.py run --prepared workspace/benchmark/cache/prepared.json --config configs/benchmark.local.toml --task-id data_computation_task5 --output workspace/benchmark/single-task --repeats 3
```

After checking the full environment, use a **separate output directory** for
three rounds of all 20 tasks:

```text
uv run --locked --extra benchmark python scripts/run_benchmark.py run --prepared workspace/benchmark/cache/prepared.json --config configs/benchmark.local.toml --output workspace/benchmark/full-run --repeats 3
```

The full batch executes all 20 tasks in each of three rounds, for 60 task runs.
Both commands make paid model requests using your configured account. Each
attempt gets a fresh workspace, full writable Skill copies, runtime state, and
input assets. External tools use an attempt-local home, cache, temporary
directory, and LibreOffice profile; the runner does not change the parent
shell's environment. Nodes inside a DAG may run concurrently; task attempts
run sequentially. Skills and runtime state move outside the artifact workspace
before evaluation so their fixture files cannot accidentally satisfy an output
check. Custom scripts still run with host access.

### Resume and retry a batch

Resume an interrupted full batch with the same settings and `--resume`:

```text
uv run --locked --extra benchmark python scripts/run_benchmark.py run --prepared workspace/benchmark/cache/prepared.json --config configs/benchmark.local.toml --output workspace/benchmark/full-run --repeats 3 --resume
```

Existing trials are skipped, including failures. To retry failed or interrupted
trials, add both `--resume --retry-failed`:

```text
uv run --locked --extra benchmark python scripts/run_benchmark.py run --prepared workspace/benchmark/cache/prepared.json --config configs/benchmark.local.toml --output workspace/benchmark/full-run --repeats 3 --resume --retry-failed
```

New attempts are retained separately. The summary uses the latest attempt per
trial and also reports all attempts. No best-of-three selection or silent retry
discards failures. A changed config, task selection, prepared-input fingerprint,
repeat count, or deadline requires a new output directory. A stale `.batch.lock`
must only be removed after confirming the recorded process and its child runtime
have stopped.

For a single-task batch, keep the same `--task-id` and `single-task` output
directory when adding those resume flags.

## Read and repeat the evaluation

Each attempt is evaluated automatically. Re-evaluate the single-task outputs
without calling a model:

```text
uv run --locked --extra benchmark python scripts/run_benchmark.py evaluate --output workspace/benchmark/single-task
```

For the full batch, use its corresponding directory:

```text
uv run --locked --extra benchmark python scripts/run_benchmark.py evaluate --output workspace/benchmark/full-run
```

Your local output directory contains:

- `summary.json`: overall, per-task, and per-round statistics, score mean /
  standard deviation / range, expected versus completed trial counts, failures,
  unscored runs, and conformance counts.
- `trials.csv`: one row per task / trial using its latest attempt.
- `attempts.json`: every attempt, including failures and retries.
- `runs/<task>/trial-XX/attempt-XX/workspace/`: that attempt's generated files.
- Each attempt's `run.json`, `runtime-state/benchmark-trace.json`, local logs,
  and retained Skill copies for diagnosis. These stay ignored by Git.

For the single-task command above, check that the summary expects three trials
and records all three as completed, then inspect each trial's execution status
and conformance before interpreting scores. For the full batch, the expected
trial count is 60. A score alone does not establish successful execution.

### Objective score, execution conformance, and usage

The **official objective score** measures the upstream task's structural
checks: required files, types, parseability, and task-specific assertions. A
failed model run can still have partial files scored. Evaluator errors and
missing scores remain unknown; they are never converted to a zero score.

The separate **execution conformance** audit checks completion of canonical
nodes, predecessor finish-before-start ordering, populated predecessor text
bindings, and observed branch overlap. A binding is counted as populated when
the referenced node is recorded and its corresponding rendered input field is
non-empty; this metric does not compare the bound text with the predecessor's
output or establish its semantic correctness. Missing or clipped traces are
marked unknown. Dependency outputs are truncated to 6,000 characters as in the
frozen renderer. Runtime provider token/cost receipts are local trace fields;
absent receipts remain unknown, and calls made by external Skill programs are
outside that accounting.

This evaluates new MetaSkill runs. It does not reproduce the paper's blinded
pairwise quality preferences against other methods or promise the same scores
from changing hosted models. Keep `workspace/`, local configs, and generated
results out of source commits.

## Platform scope

The CLI and automated suite have been validated with Python 3.12 on Windows
and Ubuntu 24.04 under WSL 2. POSIX lifecycle tests exercise real process groups,
including batch timeout, repeated SIGTERM, and native background and command
process cleanup. These checks cover runtime and evaluator integration; each
task still needs its external programs and services. macOS commands are
provided but have not been validated on a Mac.
