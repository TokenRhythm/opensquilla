---
name: file-note-review
description: Join a formatted note and a fact-check file using a package guide.
---

# Review a note

Read `{baseDir}/references/review-guide.txt`, `formatted.txt`, and `checks.txt`
with `read_file`. Use the supplied formatter handoff, both files, and the
original task to review the note. Preserve all
supported facts and remove unsupported claims. Write the reviewed text to
`reviewed.txt` in the same workspace with `write_file`. Return a short summary
naming `reviewed.txt`. Do not call external services or write outside the
workspace.
