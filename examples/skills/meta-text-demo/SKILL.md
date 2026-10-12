---
name: meta-text-demo
description: Transform the same input in two independent steps, then combine their outputs.
kind: meta
final_text_mode: "step:merge"
composition:
  steps:
    - id: upper
      kind: skill_exec
      skill: text-transform
      with:
        mode: upper
        text: "{{ inputs.user_message }}"
    - id: lower
      kind: skill_exec
      skill: text-transform
      with:
        mode: lower
        text: "{{ inputs.user_message }}"
    - id: merge
      kind: skill_exec
      skill: text-merge
      depends_on: [upper, lower]
      with:
        upper: "{{ outputs.upper }}"
        lower: "{{ outputs.lower }}"
---

# Parallel text transformation

`upper` and `lower` can execute concurrently. `merge` waits for both and receives
their text outputs through explicit bindings. Each step loads its original Skill
package, including its Python script. No model or API key is needed.

`final_text_mode: "step:merge"` returns the merge output directly, without a
separate model call to summarize it.
