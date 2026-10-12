---
name: text-merge
description: Combine the two upstream transformations into one text response.
entrypoint:
  command: python
  args:
    - "{baseDir}/scripts/merge.py"
  stdin: "{{ with | tojson }}"
  parse: text
  timeout: 20
---

# Merge text

Pass `upper` and `lower` through the step's `with` mapping. The entrypoint receives
a JSON object on standard input, so quotes, newlines, and non-ASCII text retain
their meaning across process boundaries.
