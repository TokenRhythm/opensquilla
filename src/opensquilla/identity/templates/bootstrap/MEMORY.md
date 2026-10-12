# MEMORY.md

Use this file for curated durable non-profile facts, preferences, decisions, and
constraints that are safe to include in private agent context.

Examples:

- durable user preferences that are not identity/profile fields
- project decisions or recurring constraints
- facts the user explicitly asks the agent to remember
- stable context that should survive across sessions

User identity and profile fields belong in `USER.md`.
Agent name, tone, and persona belong in IDENTITY.md or SOUL.md.
Daily/session notes belong in `memory/**/*.md`.

Do not store secrets, credentials, private keys, or ordinary one-off deliverables
here.
