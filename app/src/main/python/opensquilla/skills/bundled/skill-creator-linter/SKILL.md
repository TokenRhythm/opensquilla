---
name: skill-creator-linter
description: "Internal tool (not user-invocable). Called by meta-skill-creator as a DAG step (kind: agent) to lint a candidate meta-skill SKILL.md against G1 (parse + reference check + xml_escape grep + structural lint) and G2 (scheduler dry-run with stub executors). Deterministic, sub-second, no LLM. Returns JSON diagnostics."
description_zh: "内部工具（不可由用户直接调用）。由meta-skill-creator作为DAG步骤（kind: agent）调用，针对G1（解析+引用检查+xml_escape grep+结构lint）和G2（用桩执行器进行调度器空跑）对候选元技能SKILL.md进行lint。确定性、亚秒级、无LLM，返回JSON诊断。"
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  requires:
    anyBins: ["python", "python3"]
entrypoint:
  command: python {baseDir}/scripts/lint.py
  args:
    - --skill-md-stdin
    - --gates
    - "G1,G2"
  stdin: "{{ with.skill_md }}"
  parse: json
  timeout: 30
---

# Skill Creator Linter

Validates a candidate meta-skill SKILL.md before it gets registered.

## G1 rules

| Rule | Check |
|---|---|
| G1.1 | `parse_meta_plan` does not raise |
| G1.2 | Every `step.skill` for kind=agent/skill_exec exists in the main catalog |
| G1.3 | Template variables resolve at render time (covered by G1.1) |
| G1.4 | `on_failure` parser rules (covered by G1.1) |
| G1.5 | step `kind:` consistency (covered by G1.1) |
| G1.6 | Grep: every `{{ inputs.user_message | xml_escape` usage must have `xml_escape` (or `slugify`) as the first filter. Violations are reported as G1 errors (block linter). |

## G2 dry-run

Replace step executors with stubs yielding `_StepDone(text="<stub:id>")`. Run scheduler; pass if no exception and topology terminates.

## Usage

```
uv run python {baseDir}/scripts/lint.py --skill-md path/to/SKILL.md --gates G1,G2
uv run python {baseDir}/scripts/lint.py --skill-md-stdin --gates G1,G2 < SKILL.md
```

## Output

JSON to stdout: `{"G1": {"passed": bool, "diagnostics": [...]}, "G2": {...}}`.

## Fallback

Manually inspect SKILL.md and run `parse_meta_plan` in a Python REPL.
