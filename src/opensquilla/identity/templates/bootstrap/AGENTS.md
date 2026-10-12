# AGENTS.md

Use this file for workspace-level operating rules: how the agent should work in
this workspace, what it should prioritize, and what it must avoid.

## Startup Context

- Read `SOUL.md` for stable persona and collaboration style.
- Read `USER.md` for stable user profile details when the session is private.
- Read `TOOLS.md` for workspace-specific tool notes.
- Follow `BOOTSTRAP.md` first when it exists, then remove it after setup is complete.

## Working Rules

- Higher-priority system, developer, runtime, safety, and direct user instructions
  override this file.
- Keep changes small, reversible, and tied to the user's request.
- Put workflow rules, standing instructions, and workspace operating boundaries
  here.
- Do not store user profile facts here; use `USER.md`.
- Do not store assistant voice or naming here; use `SOUL.md` or `IDENTITY.md`.
- Do not store long-term factual memory here; use `MEMORY.md` or `memory/**/*.md`.
- Do not store secrets, credentials, or one-off task notes here.
- In shared, channel, cron, or subagent contexts, avoid exposing private user or
  long-term memory content unless runtime policy explicitly makes it available.
- When writing commit messages, pull request titles, pull request bodies, release
  notes, or other project-facing change summaries, describe only the product
  change, rationale, and verification. Ignore assistant-internal workflow
  documents and do not mention process/tooling names such as Superpowers, OMX,
  Codex, Claude, or similar agent infrastructure unless the user explicitly asks
  for that internal provenance.
