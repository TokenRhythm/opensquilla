"""Meta-skill creator library.

Importing this package registers `meta_skill_assemble`,
`meta_skill_fill_slots`, `meta_skill_lint_run`, `meta_skill_smoke_run`,
`meta_skill_runtime_e2e_run`, and `meta_skill_persist_proposal` as tools
in the default ToolRegistry.
The orchestrator's `tool_invoker` picks them up automatically.
"""

# Side-effect: registers tools via @tool decorators in proposer.py
from opensquilla.skills.creator import proposer  # noqa: F401
from opensquilla.skills.creator.proposer import (  # noqa: F401
    meta_skill_assemble,
    meta_skill_fill_slots,
    meta_skill_lint_run,
    meta_skill_persist_proposal,
    meta_skill_runtime_e2e_run,
    meta_skill_smoke_run,
    simulate_meta_resolution,
)
