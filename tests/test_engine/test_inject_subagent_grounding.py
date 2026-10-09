"""inject_subagent_grounding pipeline step survives compaction.

Idempotently re-injects the subagent system-prompt grounding for any
recognized subagent session key. No-op for main sessions and when the
grounding text is already present.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from opensquilla.engine.steps.inject_subagent_grounding import (
    _SUBAGENT_GROUNDING,
    inject_subagent_grounding,
)
from opensquilla.engine.subagent_delegation import SUBAGENT_EXECUTION_PROMPT


@dataclass
class _MiniContext:
    session_key: str
    system_prompt: object
    metadata: dict = field(default_factory=dict)


def _ctx(session_key: str, system_prompt: object) -> _MiniContext:
    return _MiniContext(session_key=session_key, system_prompt=system_prompt)


@pytest.mark.asyncio
async def test_main_session_is_a_noop() -> None:
    ctx = _ctx("agent:main:main", "You are a helpful assistant.")
    out = await inject_subagent_grounding(ctx)
    assert out.system_prompt == "You are a helpful assistant."
    assert out.metadata.get("inject_subagent_grounding__applied") is False


@pytest.mark.asyncio
async def test_subagent_session_re_injects_when_missing() -> None:
    ctx = _ctx("agent:main:subagent:abcd1234", "Be helpful.")
    out = await inject_subagent_grounding(ctx)
    assert _SUBAGENT_GROUNDING in out.system_prompt
    assert "Be helpful." in out.system_prompt
    assert out.metadata.get("inject_subagent_grounding__applied") is True


@pytest.mark.asyncio
async def test_single_agent_child_keeps_full_result_contract_after_compaction() -> None:
    ctx = _ctx("agent:main:subagent:single", "Be helpful.")
    ctx.metadata["single_agent_mode"] = True

    out = await inject_subagent_grounding(ctx)

    assert "full final answer" in out.system_prompt
    assert "short summary" in out.system_prompt
    assert SUBAGENT_EXECUTION_PROMPT not in out.system_prompt


@pytest.mark.asyncio
async def test_bare_subagent_session_key_re_injects_when_missing() -> None:
    ctx = _ctx("subagent:abcd1234", "Be helpful.")
    out = await inject_subagent_grounding(ctx)
    assert _SUBAGENT_GROUNDING in out.system_prompt
    assert "Be helpful." in out.system_prompt
    assert out.metadata.get("inject_subagent_grounding__applied") is True


def test_fallback_grounding_matches_spawn_prompt() -> None:
    assert _SUBAGENT_GROUNDING == SUBAGENT_EXECUTION_PROMPT


def test_subagent_grounding_defines_minimum_task_contract() -> None:
    assert _SUBAGENT_GROUNDING.startswith("Your role: complete the assigned task")
    assert "Meet every assigned acceptance criterion" in _SUBAGENT_GROUNDING
    assert "add useful substeps to the shared task list" in _SUBAGENT_GROUNDING
    assert "do not repeat completed work" in _SUBAGENT_GROUNDING
    assert "Return only a concise outcome to the parent" in _SUBAGENT_GROUNDING
    assert '"status":"completed|failed"' in _SUBAGENT_GROUNDING
    assert "incomplete items" not in _SUBAGENT_GROUNDING


@pytest.mark.asyncio
async def test_subagent_session_idempotent_when_present() -> None:
    existing = f"{_SUBAGENT_GROUNDING}\n\nDo the task."
    ctx = _ctx("agent:main:subagent:abcd1234", existing)
    out = await inject_subagent_grounding(ctx)
    assert out.system_prompt == existing
    assert out.metadata.get("inject_subagent_grounding__applied") is False


@pytest.mark.asyncio
async def test_tuple_system_prompt_preserves_cacheable_prefix() -> None:
    ctx = _ctx("agent:main:subagent:abcd1234", ("STATIC PREFIX", "Dynamic part."))
    out = await inject_subagent_grounding(ctx)
    cacheable, dynamic = out.system_prompt
    # Cacheable prefix is untouched so prompt-cache hit is preserved.
    assert cacheable == "STATIC PREFIX"
    assert _SUBAGENT_GROUNDING in dynamic
    assert "Dynamic part." in dynamic


@pytest.mark.asyncio
async def test_empty_system_prompt_gets_grounding() -> None:
    ctx = _ctx("agent:main:subagent:abcd1234", "")
    out = await inject_subagent_grounding(ctx)
    assert out.system_prompt == _SUBAGENT_GROUNDING
