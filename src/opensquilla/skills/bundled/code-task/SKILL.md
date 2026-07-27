---
name: code-task
description: "PREFERRED way to change code in a REAL repository: fix a GitHub issue, fix a bug, add/implement a function or feature, or make any edit to a project the user names by a filesystem path (e.g. /tmp/foo, ~/code/bar) or a git URL. Clones the repo, runs an OpenSquilla agent on the host to make the change on a task branch, then independently VERIFIES it with a red→green→regression test loop and reports a structured result. STRONGLY prefer this over hand-editing the user's files yourself in this session: editing files directly skips the isolation and the runner-verified red→green proof, so it is not equivalent. Use it whenever the request names a real on-disk repo/path or a repo URL and asks to fix/add/implement/change code. Examples: 'fix issue 412 in github.com/acme/widgets', '给 /tmp/calc 加个 average 函数', '帮我改一下 ~/proj 里的 X', 'implement CSV export in my project'. Docker-free host execution; treat the target repo as TRUSTED. GitHub issue mode needs the `gh` CLI. For self-contained, TESTABLE code from scratch when NO repo is named (e.g. 'write a function that maps A-Z to pitches'), use `--verification-mode scratch` with no --repo: it scaffolds a throwaway project, writes the code plus a test, and verifies it green. Only truly trivial one-liners or conceptual/non-testable questions are answered inline."
description_zh: "在真实代码仓库中修改代码的首选方式：修复GitHub issue、修复bug、添加/实现函数或功能，或对用户以文件路径或git URL指定的项目做任何改动。它克隆仓库、在任务分支上运行OpenSquilla代理进行修改，再通过红→绿→回归测试循环独立验证并返回结构化结果，优于在本会话中手动改文件。当请求指定了真实的本地仓库/路径或仓库URL并要求修复/添加/实现/更改代码时使用；若未指定仓库、需从零编写可测试代码，则用 --verification-mode scratch。仅极简单的一行改动或概念性/不可测试的问题才直接内联回答。"
triggers:
  - "code-task"
  - "fix the issue"
  - "fix issue"
  - "fix the bug"
  - "implement this feature"
  - "add a function"
  - "add a feature"
  - "change the code"
  - "解决 issue"
  - "给项目加功能"
  - "给项目加"
  - "加个函数"
  - "加一个函数"
  - "改一下代码"
  - "修复仓库"
  - "修复 bug"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  maintained_by: OpenSquilla
metadata:
  {
    "opensquilla":
      {
        "requires_tools": ["background_process", "exec_command", "process"],
      },
    "platform":
      {
        "emoji": "🛠️",
        "requires": { "bins": ["git"], "env": [] },
        "install": [],
      },
  }
---

# code-task

Solve a real-repository coding task end to end: clone the repo to a
disposable working directory, run an OpenSquilla agent to make the change on
a task branch, then **independently verify** it with a red→green→regression
loop. Host mode (no Docker) in v1.

## Use this — do not hand-edit the repo yourself

When the user asks to fix/add/implement/change code in a repository they
name by path or URL, route it through `opensquilla code-task solve` — even
if the change looks small enough to do by hand. Editing the files yourself
in this session is **not equivalent**: it skips the disposable clone, the
task branch, and (most importantly) the runner-verified red→green→regression
proof, so neither you nor the user gets evidence the change actually works.
Answer inline (no code-task) ONLY for truly trivial one-liners, pseudocode, or
conceptual / non-deterministic questions. For self-contained TESTABLE code from
scratch with no repo named, use `code-task solve --task "..." --verification-mode
scratch` (no --repo): it writes the code plus a test and verifies it green-only
(no red/regression -- there is nothing pre-existing to regress). When a real repo
is named, prefer code-task red-green.

## Translating the user's request

The user speaks naturally ("fix issue 412 in github.com/acme/widgets",
"add CSV BOM support to my project at ~/code/foo"). Map that to the command:

```
opensquilla code-task solve --repo <url-or-path> ( --issue N | --task "<text>" | --task-file <path> ) [--yes]
```

> Invocation: do NOT assume a bare `opensquilla` (or bare `python`) is on PATH —
> the gateway commonly runs from an absolute interpreter path. When coding mode
> is active it injects the EXACT, resolved, PATH-independent command: use ONLY
> that. Otherwise invoke via an ABSOLUTE interpreter, e.g.
> `/abs/path/python -P -m opensquilla.cli.main code-task solve ...`. Never
> `pip install` OpenSquilla or run an installer to "get" the command; if it
> cannot be run, stop and report the environment is broken.

- **A GitHub issue** → `--issue N` (needs `gh`; see below).
- **A short request in the message** → `--task "<their request>"`.
- **A long spec, or pasted from Jira/GitLab/内网** → save it to a file and
  use `--task-file <path>`.
- Pass `--repo` for changes to an EXISTING repo. Omit it for
  `--verification-mode scratch` (from-scratch code) and for a from-scratch
  `--verification-mode build` (a brand-new app) — both scaffold their own repo.
  If the user is already in a local checkout, use that path; otherwise the URL.
- Pass `--yes` to skip the interactive trusted-host confirmation (you are
  acting on the user's behalf), but only after the safety check below.

## Before you run — two checks

1. **Trusted repo**: code-task runs an agent on the host that may install
   dependencies and execute the repo's code. It is NOT a sandbox. Only run
   it against repositories the user trusts. If the repo's provenance is
   unclear, ask first.
2. **Enough information**: you must be able to state the expected behavior
   change ("what is wrong/missing now, what should be true after"). If the
   request is too vague to write an acceptance test for, ask the user to
   clarify BEFORE running — do not burn a run on a guess.
   - **Build-from-scratch (`--verification-mode build`)** has no acceptance
     test. Decide by whether you know WHAT THE APP SHOULD DO, not just its kind.
     If the request is only a broad app type/goal with no concrete features,
     target user, or scope (e.g. "make me an English-learning app", "a drawing
     app"), ask 1-2 focused questions (core features/screens and who it's for),
     then STOP — do not run code-task until answered. If it already names
     concrete features, scope, or target users, do NOT ask — build it with
     sensible defaults and state your assumptions. Never ask about
     platform/framework/styling. At most 2 questions; never interrogate.

## GitHub issue mode needs `gh`

`--issue` shells out to the GitHub CLI (`gh`). If `gh` is missing or not
authenticated, tell the user to `gh auth login`, or fall back: have them
paste the issue text and use `--task` / `--task-file` instead. The issue
body AND comments are pulled in (comments often hold the repro steps).

## While it runs — watch the run dir, not the source repo

code-task clones the `--repo` source into an isolated run directory and does
all its work there. The **source repo stays empty until a run finishes and
VERIFIES**, at which point (build mode, local source) the change is committed
back. Therefore:

- Do NOT judge progress by the source repo's contents, and do NOT conclude the
  run is "stuck" because the source still looks empty — that is expected.
- A run takes several minutes. Let it finish: `process(action="wait")` on the
  background session. Do NOT kill it, do NOT "clean and retry", and do NOT
  launch the same task again while one is still running.
- The run prints its run directory on startup and writes a live
  `<run_dir>/status.json` (phase = preparing → agent_running → collecting_change
  → verifying → completed). Watch that if you want progress.
- Decide success only from the returned result `state` and
  `build.installer_path` (which points into the run dir, not the source).

## Reading the result

`--json` prints a result object; key fields:

- `state`: `verified` (acceptance test went red→green, no regressions),
  `already_satisfied` (the behavior already held on the base commit),
  `not_testable` (work done but not expressible as a test),
  `environment_blocked` (could not build/test the repo),
  `invalid_acceptance_test` (agent produced no valid verification manifest),
  `failed` (acceptance not green or a regression appeared).
- `branch`, `commits`, `files_changed`, `diffstat`, `patch_path`.
- `acceptance`: each test with `before`→`after` (e.g. `fail`→`pass`).
- `regression`: existing-suite result and `new_failures`.
- `assumptions`: **surface these to the user** — a wrong assumption means a
  wrong fix.
- `usage`: cost / tokens (aggregated across internal retry attempts).
- `attempts` / `max_attempts` / `retry_exhausted`: code-task RETRIES internally
  when its own verification fails -- it re-runs the agent on the SAME prepared
  repo (no re-clone, no re-explore) with the concrete failure fed back, up to
  `max_attempts`. A returned result is FINAL across those internal attempts.
- `relaunch_recommended` is always `false` and `final_failure_reason` explains a
  failure: do NOT re-launch the same task yourself on a `failed` result -- the
  internal retries are already exhausted. Surface the failure to the user.

## What to tell the user

1. Up front: cloning + dependency install + the agent loop can take several
   minutes; you'll report when done.
2. After: report `state`, what changed (diffstat), the acceptance red→green
   evidence, any `assumptions`, the cost, and where the branch/diff lives.
3. On `failed` / `environment_blocked`: quote `error` / `final_failure_reason`
   and point at the `agent_stdout.log`. Do NOT relaunch the same task yourself --
   code-task already retried internally (see `retry_exhausted`).

## Constraints

- Runs on the gateway host — git, the toolchain, and disk all come from
  there. Works the same from TUI, Web UI, or any channel.
- v1 is host-only and always clones fresh (no `--in-place`). For untrusted
  repositories, a Docker-isolated backend is planned but not in v1.

## Verification modes

`code-task solve` defaults to `--verification-mode red-green`: the agent writes acceptance tests, the runner proves red on the base and green on the change, then runs regression.

For building an app or UI **from scratch** (e.g. an Electron + Vite + React desktop app) there is no red->green test loop. Use `--verification-mode build`: the runner owns a fixed checklist (`npm ci` -> `npm run build` -> `npx electron-builder` for the HOST OS, with the target pinned — `--mac dmg` -> `.dmg`, `--win nsis` -> `.exe`, `--linux AppImage` -> `.AppImage`) and `state=verified` means the app actually builds and packages into an installer. Each OS only builds its own installer, so run on each OS (or a CI matrix) to collect all three. The result carries `verification_kind=build` and `build.installer_path(s)`. Preview/launch is intentionally out of scope (no GUI is run).

For self-contained, testable code when the user has not named a repo, use
`--verification-mode scratch` with `--task` or `--task-file` and no `--repo`.
The runner creates an empty git repo, asks the agent to write code plus pytest
coverage, and independently reruns the declared acceptance command. This mode is
green-only and returns `verification_kind=scratch`.
