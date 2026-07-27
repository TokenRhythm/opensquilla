---
name: swe-bench
description: "Run SWE-bench instances with an OpenSquilla agent inside the official Docker images. Trigger when the user wants to run/solve/evaluate a SWE-bench instance (e.g. 'run django__django-16429', 'test OpenSquilla on SWE-bench', '跑一道 SWE-bench 题'), benchmark the agent on SWE-bench_Verified or SWE-bench_Multilingual, or check whether a generated patch resolves an instance. Optional dependency — install via `pip install opensquilla[swebench]`; also needs the docker CLI and an OPENROUTER_API_KEY."
description_zh: "在官方Docker镜像中用OpenSquilla代理运行SWE-bench实例。当用户想运行/求解/评测SWE-bench实例（如'run django__django-16429'、'在SWE-bench上测试OpenSquilla'、'跑一道 SWE-bench 题'）、在SWE-bench_Verified或SWE-bench_Multilingual上做基准测试，或检查生成的补丁是否解决某实例时触发。可选依赖，需通过 pip install opensquilla[swebench] 安装，还需docker CLI和OPENROUTER_API_KEY。"
triggers:
  - "swe-bench"
  - "swebench"
  - "SWE-bench"
  - "跑一道题"
  - "解一道 SWE"
  - "benchmark instance"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  maintained_by: OpenSquilla
metadata:
  {
    "platform":
      {
        "emoji": "🧪",
        "requires": { "env": ["OPENROUTER_API_KEY"] },
        "install":
          [
            {
              "id": "swebench-extra",
              "kind": "uv",
              "package": "opensquilla[swebench]",
              "label": "Install SWE-bench extras (uv pip)",
            },
          ],
      },
  }
---

# swe-bench

Run a SWE-bench instance end-to-end: ensure the instance's Docker image is
available (pull from Docker Hub if missing), start a container, run an
OpenSquilla agent against the issue, collect the patch, and optionally run
the official evaluation.

## Prerequisites — guide the user, don't dead-end

SWE-bench mode runs the official evaluation images, so it needs the **Docker
CLI**. Docker is NOT a hard gate on this skill: if it is missing, the
`opensquilla swebench` command prints the exact install command for the
user's OS and exits. When that happens, relay the install guidance to the
user ("SWE-bench needs Docker — install it with `...`, then I can run this")
instead of saying the task is impossible. Also mention that solving a
**real-repository** coding task (not a benchmark instance) does NOT need
Docker — that is what the `code-task` skill is for.

## Commands

Solve one instance (auto-pulls the image when missing):

```
opensquilla swebench solve <instance_id> --dataset verified --json
```

- `--dataset` accepts `verified`, `multilingual`, or a full HuggingFace
  dataset name.
- Add `--evaluate` to run the official harness afterwards and report
  whether the patch actually resolves the instance (`resolved` in the
  JSON output).
- Add `--model <model>` / `--thinking <level>` to pin a model; leave them
  off to let squilla_router decide.
- `--timeout <seconds>` defaults to 1200.

Pre-fetch an image only:

```
opensquilla swebench pull <instance_id>
```

Evaluate an existing predictions file:

```
opensquilla swebench eval <predictions.jsonl> --dataset verified
```

## Reading the result

`solve --json` prints one JSON object: `state` (`patch_collected` is
success), `patch_path`, `artifact_dir`, `resolved` (true/false, or null
when `--evaluate` was not used), `duration_seconds`, `usage`
(cost/tokens), `error`.

The full artifact trail (prompt, agent log, transcript, usage, patch)
lives under `artifact_dir`.

## What to tell the user

1. Before starting: if the image is not local yet, warn that the first
   run pulls 1-3 GB and can take a few minutes; a solve typically takes
   10-30 minutes depending on the instance and timeout.
2. After finishing: report `state`, whether the patch is non-empty,
   `resolved` when evaluation ran, the cost from `usage`, and the
   `patch_path` so the user can inspect the diff.
3. On `failed`/`timeout`: quote the `error` field and point at
   `<artifact_dir>/agent_stdout.log` for the full trace.

## Constraints

- Images are x86_64; on ARM hosts only locally pre-built images work.
- The run happens on this machine (the gateway host) — docker must be
  reachable from here, and disk usage grows with each pulled image.
- Long runs block the command; for batches beyond a handful of
  instances, suggest the user run the CLI directly in tmux instead of
  going through chat.
