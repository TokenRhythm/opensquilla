---
name: sympy
description: "Perform reproducible mathematical and statistical calculations from structured JSON data. Use for descriptive statistics, one-sided or two-sided independent-sample t-tests, chi-square independence tests, and one-way ANOVA. The entrypoint returns machine-readable JSON so later skills such as xlsx can render the results. Never estimate p-values in prose when this calculator is available."
description_zh: "根据结构化 JSON 数据执行可复现的数学与统计计算。适用于描述统计、单侧或双侧独立样本 t 检验、卡方独立性检验和单因素方差分析。入口程序返回机器可读 JSON，供 xlsx 等后续 Skill 生成文件。"
provenance:
  origin: opensquilla-lp4fm-experiment
  license: Apache-2.0
  maintained_by: OpenSquilla LP4FM experiment
metadata:
  requires:
    anyBins: ["python", "python3"]
  install:
    - id: scipy
      kind: uv
      package: scipy
      label: Install scipy
    - id: sympy
      kind: uv
      package: sympy
      label: Install sympy
entrypoint:
  command: python {baseDir}/scripts/hypothesis_tests.py
  stdin: "{{ with.input_json | default(with.task, true) }}"
  parse: json
  timeout: 60
---

# sympy statistical calculator

Use this Skill when a task requires exact, auditable statistical calculations.
Pass one JSON object through `with.input_json`, or pass a natural-language
hypothesis-testing request through `with.task`. Do not place task-specific
datasets or expected answers in this Skill.

## Supported inputs

Independent-sample t-test:

```json
{"test":"t_test","group_a":[1,2,3],"group_b":[4,5,6],"alternative":"less","equal_var":true}
```

`alternative` is `two-sided`, `less`, or `greater`. It describes the
alternative hypothesis for `group_a` relative to `group_b`.

Chi-square independence test:

```json
{"test":"chi_square","table":[[10,20],[15,25]],"correction":false}
```

One-way ANOVA:

```json
{"test":"anova","groups":[[1,2,3],[4,5,6],[7,8,9]]}
```

## Output

The command prints one JSON object containing the test statistic, p-value,
degrees of freedom where applicable, group means, and a conventional effect
size. A non-zero exit means the input was invalid or a dependency was missing.

## Experimental use

For AgentSkillOS experiments, preserve the runtime trace showing this Skill's
entrypoint invocation. Library installation alone does not count as Skill use.
