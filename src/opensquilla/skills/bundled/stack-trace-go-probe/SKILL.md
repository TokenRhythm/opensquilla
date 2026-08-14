---
name: stack-trace-go-probe
description: "Internal helper for meta-stack-trace-investigator. Use when a Go panic or stack trace needs Go-specific nil/error checks, go test reproducer guidance, and patch targets."
description_zh: "meta-stack-trace-investigator的内部辅助工具。当Go panic或堆栈跟踪需要Go特定的nil/error检查、go test复现指引和补丁目标时使用。"
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
---

# Stack Trace Go Probe

Return only:

```
LANGUAGE_PROBE: go
CHECKS:
  - <nil pointer / error-return / goroutine boundary check>
  - <package or interface contract check>
REPRODUCER:
  - <minimal go test ./... -run <Name> command or snippet>
PATCH_TARGETS:
  - <nil guard / explicit error handling / interface assertion target>
VERIFY:
  - <go test command>
```

Prefer narrow `go test ./path -run TestName` commands when the trace exposes a
package or symbol. Do not suggest mutating production state.
