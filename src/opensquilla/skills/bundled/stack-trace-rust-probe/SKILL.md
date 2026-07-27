---
name: stack-trace-rust-probe
description: "Internal helper for meta-stack-trace-investigator. Use when a Rust panic or backtrace needs Rust-specific Result/Option checks, cargo test guidance, and patch targets."
description_zh: "meta-stack-trace-investigator的内部辅助工具。当Rust panic或回溯需要Rust特定的Result/Option检查、cargo test指引和补丁目标时使用。"
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
---

# Stack Trace Rust Probe

Return only:

```
LANGUAGE_PROBE: rust
CHECKS:
  - <panic/unwrap/expect/Option/Result handling check>
  - <trait/lifetime/thread boundary check if relevant>
REPRODUCER:
  - <minimal cargo test command or snippet>
PATCH_TARGETS:
  - <replace unwrap/expect / map_err / explicit Result propagation target>
VERIFY:
  - <cargo test command>
```

Prefer narrow `cargo test <name>` commands when the trace exposes a symbol.
Do not recommend unsafe broad rewrites.
