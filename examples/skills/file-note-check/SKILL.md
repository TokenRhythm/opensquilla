---
name: file-note-check
description: Check a shared note against supplied facts using a package checklist.
---

# Check a note

Read `{baseDir}/references/check-guide.txt` and `note.txt` with `read_file`.
Compare the note with the supplied task. Write a short fact-check to
`checks.txt` with `write_file`; name unsupported claims if any. Return a short
handoff naming `checks.txt`. Do not change `note.txt` or contact external
services. Use only the workspace for generated files.
