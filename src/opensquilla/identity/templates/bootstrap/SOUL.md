# SOUL.md

Use this file for the agent's durable voice, tone, and interaction style.

Good content:

- preferred brevity or level of detail
- directness, warmth, humor, or formality
- boundaries for how the agent should sound in private or public contexts
- collaboration style that should persist across sessions

Do not store user profile facts, task history, or tool inventories here. Use
`USER.md` for the user, `MEMORY.md` or `memory/**/*.md` for memory, and `TOOLS.md`
for tool notes.

Keep this concise. Higher-priority system, developer, runtime, safety, and user
instructions always override this file.
