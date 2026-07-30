---
name: stack-trace-generic-probe
description: "Internal helper for meta-stack-trace-investigator. Use when a stack trace language is unknown and the workflow needs language-neutral failure-contract checks, reproducer guidance, and patch targets."
description_zh: "meta-stack-trace-investigator的内部辅助工具。当堆栈跟踪的语言未知，且工作流需要语言中立的失败契约检查、复现指引和补丁目标时使用。"
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
---

# Stack Trace Generic Probe

Return only:

```
LANGUAGE_PROBE: generic
CHECKS:
  - <schema/contract check>
  - <boundary check>
REPRODUCER:
  - <minimal language-neutral reproduction shape>
PATCH_TARGETS:
  - <defensive parsing / null handling / error propagation target>
VERIFY:
  - <safe command or manual check>
```

Base every item on the parsed trace supplied by the caller. Do not invent
files or dependencies that are not present in the request.
