---
name: meta-writing-demo
description: Draft and edit a short announcement using two complete Agent Skill packages.
kind: meta
final_text_mode: "step:edit"
composition:
  steps:
    - id: draft
      kind: agent
      skill: announcement-draft
      with:
        task: "{{ inputs.user_message }}"
    - id: edit
      kind: agent
      skill: announcement-edit
      depends_on: [draft]
      with:
        task: "Edit the draft according to the original request: {{ inputs.user_message }}"
        draft: "{{ outputs.draft }}"
---

# A small model-backed composition

This example needs the model configuration described in the repository README.
`draft` uses the complete `announcement-draft` Skill; `edit` uses the separate
`announcement-edit` Skill and receives the draft through `outputs.draft`.

The Skills retain their own natural-language instructions and package resources.
`final_text_mode: "step:edit"` returns the editor's response directly.
