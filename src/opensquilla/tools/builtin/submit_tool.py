"""Registration of the ``submit`` tool.

The tool exists in the registry so it appears in the LLM's tool catalogue and
so policy checks apply via the standard dispatcher preflight. Its handler body
is a routing guard: the actual behaviour happens inside
``Agent._run_one_streaming``'s dispatch loop, which intercepts
``tc.tool_name == 'submit'`` before the standard ``_execute_tool`` path and
returns a synthetic review/confirmation result. If this handler ever fires,
something is misconfigured.

Visibility: ``exposed_by_default=False``. ``submit`` is surfaced per-run via
``ToolContext.surfaced_tools`` only when the ``submit_review`` lever is enabled
(see ``engine/runtime.py``); with the lever off the tool is absent from the
catalogue and the interception branch is dead, so behaviour is unchanged.
"""

from __future__ import annotations

from opensquilla.tools.registry import tool


@tool(
    name="submit",
    description=(
        "Declare your work complete and submit the current workspace changes as "
        "your final answer. You may be shown a review of your changes and asked "
        "to call submit again to confirm."
    ),
    params={
        "summary": {
            "type": "string",
            "description": "Optional short summary of the changes you are submitting.",
        },
    },
    required=[],
    exposed_by_default=False,
)
async def submit(summary: str | None = None) -> str:  # noqa: ARG001 — unused in guard
    raise RuntimeError(
        "submit must be intercepted by Agent._run_one_streaming's dispatch loop "
        "before reaching the registry handler. This RuntimeError indicates a "
        "configuration bug — the dispatch loop did not detect the submit "
        "tool_name in time.",
    )
