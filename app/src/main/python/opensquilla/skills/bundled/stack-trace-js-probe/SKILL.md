---
name: stack-trace-js-probe
description: "Internal helper for meta-stack-trace-investigator. Use when a JavaScript or TypeScript stack trace needs npm/node/tsc-specific checks, reproduction guidance, and patch targets."
description_zh: "meta-stack-trace-investigator的内部辅助工具。当JavaScript或TypeScript堆栈跟踪需要npm/node/tsc特定的检查、复现指引和补丁目标时使用。"
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
---

# Stack Trace JS Probe

Return only:

```
LANGUAGE_PROBE: javascript-typescript
CHECKS:
  - <async boundary / undefined property / JSON parsing / module-resolution check>
  - <TypeScript type-contract check if .ts/.tsx appears>
REPRODUCER:
  - <minimal node/npm/vitest/jest/ts-node reproduction command or snippet>
PATCH_TARGETS:
  - <optional chaining / schema validation / discriminated union / error wrapping target>
VERIFY:
  - <npm test/vitest/jest/tsc command>
```

Pick JavaScript or TypeScript commands from the trace context. If the context
does not identify a package manager, use generic `npm test -- <pattern>` or
`npx tsc --noEmit` as examples.
