---
name: meta-route-demo
description: Classify the requested letter case, run one conditional branch, and select a Skill.
kind: meta
final_text_mode: "step:transform"
composition:
  steps:
    - id: classify
      kind: llm_classify
      output_choices: [UPPER, LOWER]
      with:
        task: "Choose UPPER when the request asks for uppercase; otherwise choose LOWER. Request: {{ inputs.user_message }}"
    - id: upper_branch
      kind: tool_call
      depends_on: [classify]
      when: "outputs.classify == 'UPPER'"
      tool: write_file
      tool_args:
        path: upper-selected.txt
        content: "UPPER"
    - id: lower_branch
      kind: tool_call
      depends_on: [classify]
      when: "outputs.classify == 'LOWER'"
      tool: write_file
      tool_args:
        path: lower-selected.txt
        content: "LOWER"
    - id: transform
      kind: skill_exec
      skill: text-lowercase
      depends_on: [upper_branch, lower_branch]
      route:
        - when: "outputs.classify == 'UPPER'"
          to: text-uppercase
        - when: "outputs.classify == 'LOWER'"
          to: text-lowercase
      with:
        text: "{{ inputs.user_message }}"
---

# Conditional branches and Skill routing

The classifier returns one of two closed-set labels. `when` skips the unmatched
file-writing branch; a skipped step still satisfies downstream dependencies.
The transform waits for both branch states, then `route` selects a Skill name.
Route destinations are Skill names, not DAG step IDs.

Use a model configuration and `--run-mode full`. A fresh workspace contains
either `upper-selected.txt` or `lower-selected.txt`, and the returned text is
transformed by the selected Python Skill. Reusing a workspace can retain old
files, so use separate workspaces when checking branch selection.
