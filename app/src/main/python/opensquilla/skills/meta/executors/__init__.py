"""Per-step executors for MetaOrchestrator.

Each module exports a free async function (or async generator) that runs
one ``MetaStep`` of a specific ``kind``. Executors take their dependencies
(agent_runner, llm_chat, tool_invoker, skill_loader, workspace_dir) as
explicit arguments so they remain independently testable and don't import
the orchestrator facade.
"""

from __future__ import annotations

from opensquilla.skills.meta.executors.user_input import run_user_input_step  # noqa: F401
