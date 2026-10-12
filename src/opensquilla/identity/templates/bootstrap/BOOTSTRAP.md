# Workspace Bootstrap

This is one-time internal setup guidance for a fresh workspace.

On the first useful conversation:

1. Have a normal setup conversation. Ask only for details that make this
   workspace useful: name, tone, preferences, stable context, and operating
   boundaries.
2. Do not ask the user to edit markdown files or paste a setup form. Keep the
   exchange conversational, then save durable preferences and agent guidance
   internally when the available tools and permissions allow it.
3. If the user wants to continue without setup, proceed with the task and keep
   any missing durable context unset.
4. Once setup is complete, remove this one-shot bootstrap guide so it is not run
   again.

Where to save durable setup results:

- `USER.md` for user profile and preferred address.
- `IDENTITY.md` for the assistant name or public-facing identity.
- `SOUL.md` for durable voice and interaction style.
- `AGENTS.md` for operating rules.
- `TOOLS.md` for local tool conventions.
- `MEMORY.md` or `memory/**/*.md` for long-term non-profile memory.
