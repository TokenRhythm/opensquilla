---
name: text-lowercase
description: Convert supplied UTF-8 text to lowercase using Python.
entrypoint:
  command: python
  args:
    - "{baseDir}/scripts/transform.py"
  stdin: "{{ with.text }}"
  parse: text
  timeout: 20
---

# Lowercase text

Read the bound `text` from standard input and return its lowercase form.
