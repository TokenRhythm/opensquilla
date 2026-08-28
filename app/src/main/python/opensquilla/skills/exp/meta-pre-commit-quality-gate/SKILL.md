---
name: meta-pre-commit-quality-gate
description: "Run three quality gates (ruff + mypy + pytest) in parallel over the staged diff, then arbitrate a single BLOCK/APPROVE verdict. Use before committing changes locally when you want a comprehensive pre-commit gate beyond per-file linting — exactly the same gate set CI enforces."
kind: meta
meta_priority: 70
always: false
triggers:
  - "pre-commit quality gate"
  - "pre-commit 质量门"
  - "提交前质量检查"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: high
    capabilities:
      - subprocess
      - filesystem-read
      - filesystem-write
composition:
  steps:
    - id: collect_staged
      kind: skill_exec
      skill: git-diff
      with:
        mode: staged_files
        cwd: "{{ inputs.workspace_dir | default('.') }}"
    - id: run_ruff
      kind: agent
      skill: sub-agent
      depends_on: [collect_staged]
      with:
        task: |
          Run `uv run ruff check <staged .py files>` on the staged Python files
          listed below. Filter for paths ending in `.py` only.

          Staged files:
          ---
          {{ outputs.collect_staged | truncate(800) }}
          ---

          If the staged list is NO_STAGED_FILES or contains no .py files, treat
          as PASS.

          Reply with EXACTLY one line, no preamble:
            PASS: ruff clean
            FAIL: <count> findings — <head of first 5 violation lines>
    - id: run_mypy
      kind: agent
      skill: sub-agent
      depends_on: [collect_staged]
      with:
        task: |
          Run `uv run mypy --show-error-codes` on the staged Python files that
          live under `src/opensquilla/` (skip files outside that tree because
          the project's mypy config only covers the package).

          Staged files:
          ---
          {{ outputs.collect_staged | truncate(800) }}
          ---

          If no `src/opensquilla/**` files are staged, treat as PASS.

          Reply with EXACTLY one line, no preamble:
            PASS: mypy clean (or no src files staged)
            FAIL: <count> errors — <head of first 3 errors>
    - id: run_pytest
      kind: agent
      skill: sub-agent
      depends_on: [collect_staged]
      with:
        task: |
          Identify pytest files affected by these staged files (any
          `tests/test_*.py` that imports or references a same-named module).
          Run `uv run pytest -q -x --tb=short` on the affected subset; if you
          cannot localise, run the default-path suite with the same flags.

          Staged files:
          ---
          {{ outputs.collect_staged | truncate(800) }}
          ---

          Reply with EXACTLY one line, no preamble:
            PASS: <count> tests passed
            FAIL: <first failing test name> — <one-line error>
    - id: arbitrate
      kind: agent
      skill: sub-agent
      depends_on: [run_ruff, run_mypy, run_pytest]
      with:
        task: |
          Three quality gates ran over the staged diff:
            - ruff:   {{ outputs.run_ruff }}
            - mypy:   {{ outputs.run_mypy }}
            - pytest: {{ outputs.run_pytest }}

          Apply the rule STRICTLY (do NOT soften):
            * If ANY of the three starts with "FAIL" → final verdict is BLOCK.
              The BLOCK summary concatenates each failing gate's verbatim text.
            * If ALL three start with "PASS" → final verdict is APPROVE.

          Reply with EXACTLY this structure on the first line, then optionally
          additional lines:
            BLOCK: <one-line summary; list every gate that failed>
            APPROVE: ruff/mypy/pytest all green
    - id: persist
      kind: tool_call
      tool: memory_save
      tool_allowlist: [memory_save]
      depends_on: [arbitrate]
      tool_args:
        path: "memory/pre-commit-gates.md"
        mode: append
        content: |
          === pre-commit quality gate ===
          invocation: {{ inputs.user_message | xml_escape | truncate(200) }}
          staged: {{ outputs.collect_staged | truncate(600) }}
          ruff:   {{ outputs.run_ruff | truncate(200) }}
          mypy:   {{ outputs.run_mypy | truncate(200) }}
          pytest: {{ outputs.run_pytest | truncate(200) }}
          verdict: {{ outputs.arbitrate | truncate(400) }}
---

# Pre-Commit Quality Gate (Meta-Skill)

A **combinator-style** meta-skill that runs three independent quality
gates in parallel over the currently-staged diff, then arbitrates a
single ship-readiness verdict.

## Trigger surface

Fire by saying `pre-commit quality gate` or one of the localized triggers
listed in the frontmatter. The skill is also designed to be invoked from a git
pre-commit hook via the soft path (`meta_invoke`), but hook installation is a
separate manual step.

## Fallback

If the orchestrator fails (sub-Agent error, timeout, etc.), the
caller should manually run, in order: `uv run ruff check src tests`,
`uv run mypy src/opensquilla --show-error-codes`, then
`uv run pytest -q`. The same three commands are the CI quality gate
in `.github/workflows/ci.yml`.
