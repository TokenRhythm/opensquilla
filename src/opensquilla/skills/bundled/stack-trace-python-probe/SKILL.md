---
name: stack-trace-python-probe
description: "Internal helper for meta-stack-trace-investigator. Use when a Python traceback needs Python-specific root-cause checks, pytest reproducer guidance, and defensive patch targets."
description_zh: "meta-stack-trace-investigator的内部辅助工具。当Python回溯需要Python特定的根因检查、pytest复现指引和防御性补丁目标时使用。"
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
---

# Stack Trace Python Probe

Return only:

```
LANGUAGE_PROBE: python
CHECKS:
  - <exception contract or missing-key/None-handling check>
  - <import/module/package boundary check if relevant>
REPRODUCER:
  - <minimal pytest or python -c reproduction command/snippet>
PATCH_TARGETS:
  - <guard clause / TypedDict / pydantic/schema validation / exception wrapping target>
VERIFY:
  - <targeted pytest command or python syntax/import check>
```

Prefer `pytest -k <symbol>` and `python -m pytest <path>` shapes. Do not
recommend destructive commands.
