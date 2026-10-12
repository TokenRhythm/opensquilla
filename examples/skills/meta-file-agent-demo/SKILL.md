---
name: meta-file-agent-demo
description: Create a note, process it with two parallel Agent Skills, and join their files.
kind: meta
final_text_mode: "step:review"
composition:
  steps:
    - id: create
      kind: agent
      skill: file-note-create
      with:
        task: "{{ inputs.user_message }}"
    - id: format
      kind: agent
      skill: file-note-format
      depends_on: [create]
      with:
        task: "Format the note using only the supplied request: {{ inputs.user_message }}"
        handoff: "{{ outputs.create }}"
    - id: check
      kind: agent
      skill: file-note-check
      depends_on: [create]
      with:
        task: "Check the note against the supplied request: {{ inputs.user_message }}"
        handoff: "{{ outputs.create }}"
    - id: review
      kind: agent
      skill: file-note-review
      depends_on: [format, check]
      with:
        task: "Review the note using only the supplied request: {{ inputs.user_message }}"
        handoff: "{{ outputs.format }}"
---

# Agent tools and file handoff

All four steps use the full Agent tool loop and their own complete Skill
packages. The creator reads its package template and writes `note.txt`.
`format` and `check` then run independently, read that file, and write separate
`formatted.txt` and `checks.txt` files. The reviewer waits for both branches,
receives only the formatter's response through an explicit text binding, and
reads both files before writing `reviewed.txt`.

Dependencies order execution; text bindings select the predecessor text passed
to a child; the shared workspace carries files separately. A dependency does
not automatically inject every predecessor's response. Use a configured model
and `--run-mode full`.
