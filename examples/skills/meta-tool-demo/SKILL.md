---
name: meta-tool-demo
description: Write, read, and copy supplied text through native tools without a model.
kind: meta
final_text_mode: "step:copy"
composition:
  steps:
    - id: write
      kind: tool_call
      tool: write_file
      tool_args:
        path: source.txt
        content: "{{ inputs.user_message }}"
    - id: read
      kind: tool_call
      tool: read_file
      depends_on: [write]
      tool_args:
        path: source.txt
    - id: copy
      kind: tool_call
      tool: write_file
      depends_on: [read]
      tool_args:
        path: handoff.txt
        content: "{{ outputs.read }}"
---

# Native tool chain

Run trusted file tools with `--run-mode full`. All paths are relative to the
selected workspace. The first file contains the supplied text; `handoff.txt`
contains the native read tool's output, including its formatting. The final
answer is the write tool's status, not a model-generated response.
