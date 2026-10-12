---
name: file-note-create
description: Create a small workspace note from provided facts and a package template.
---

# Create a note

Read `{baseDir}/templates/note.txt` with `read_file`. Fill the template using
only facts in the supplied task. Write the completed note to `note.txt` in the
workspace with `write_file`. Return a short handoff naming `note.txt` and its
content. Do not call external services or write outside the workspace.
