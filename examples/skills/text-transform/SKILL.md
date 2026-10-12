---
name: text-transform
description: Convert UTF-8 text to upper or lower case with a local Python script.
entrypoint:
  command: python
  args:
    - "{baseDir}/scripts/transform.py"
    - "{{ with.mode }}"
  stdin: "{{ with.text }}"
  parse: text
  timeout: 20
---

# Text transform

Pass `mode: upper` or `mode: lower` and the text through the step's `with` mapping.
The script reads UTF-8 text from standard input and returns transformed text on
standard output. Keeping the script in this Skill package lets the runtime find
it through `{baseDir}`, independently of the current working directory.
