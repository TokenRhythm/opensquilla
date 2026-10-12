---
name: text-uppercase
description: Convert supplied UTF-8 text to uppercase using Python.
entrypoint:
  command: python
  args:
    - "{baseDir}/scripts/transform.py"
  stdin: "{{ with.text }}"
  parse: text
  timeout: 20
---

# Uppercase text

Read the bound `text` from standard input and return its uppercase form.
