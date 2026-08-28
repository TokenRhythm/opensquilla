---
name: meta-long-running-build-watchdog
description: "[DEPRECATED] Build watchdog — launches arbitrary commands from the user message in tmux and lets sub-agent auto-apply a fix. Disabled pending the E5 bounded sub-agent contract + Jinja sandbox + side-effect ledger (plan §3.1 A1/A8 / §5.3 E4): the launch task interpolates raw user_message into a shell-bound tmux session and the heal step lets sub-agent mutate state with no rollback. Do not re-enable without `metadata.opensquilla.risk: high` + capabilities {shell, tmux, filesystem-write, subprocess} and a saga-style compensation step."
kind: meta
meta_priority: 0
always: false
disable-model-invocation: true
triggers: []
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: high
    capabilities:
      - shell
      - tmux
      - filesystem-write
      - subprocess
composition:
  steps:
    - id: launch
      skill: tmux
      with:
        task: "Start a detached tmux session running the command described in: {{ inputs.user_message | xml_escape | truncate(512) }}"
    - id: inspect
      skill: tmux
      depends_on: [launch]
      with:
        task: "After a short interval, scrape the pane output of the session started above and report any error or warning lines."
    - id: heal
      skill: sub-agent
      depends_on: [inspect]
      with:
        task: "Diagnose the captured logs and propose / apply a fix. Logs: {{ outputs.inspect }}"
    - id: memorize
      kind: tool_call
      tool: memory_save
      tool_allowlist: [memory_save]
      depends_on: [heal]
      tool_args:
        path: "memory/build-watchdog.md"
        mode: append
        content: "{{ outputs.heal }}"
---

# Long-Running Build Watchdog (Meta-Skill)

Watches a long-running command via tmux, lets `sub-agent` diagnose
failures and propose a fix, and records the diagnosis to memory.
Designed for overnight model fine-tunes, CI image builds, or repeated
regression suites that may fail intermittently.

## Fallback

Manually start a tmux session, scrape output, ask the LLM to diagnose,
record the resolution.
