"""Goal objective routing stays ephemeral and out of model/log surfaces."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps import skills_filter
from opensquilla.gateway.config import GatewayConfig
from opensquilla.skills.types import SkillLayer, SkillSpec


@pytest.mark.asyncio
async def test_skill_retrieval_uses_goal_hint_but_logs_only_a_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objective = "Use the private migration codename to select the database skill."
    captured_queries: list[str] = []
    captured_logs: list[dict[str, object]] = []

    class _Retriever:
        def retrieve(
            self,
            skills: list[SkillSpec],
            query: str,
            *,
            top_k: int,
        ) -> list[SkillSpec]:
            captured_queries.append(query)
            return skills[:top_k]

    monkeypatch.setattr(skills_filter, "_get_retriever", lambda _config: _Retriever())
    monkeypatch.setattr(
        skills_filter.log,
        "debug",
        lambda _event, **fields: captured_logs.append(fields),
    )
    config = GatewayConfig()
    config.skills.filter_enabled = True
    skill = SkillSpec(
        name="database",
        description="Inspect database state",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="Use database tools.",
        path=Path("/synthetic/database/SKILL.md"),
    )
    ctx = TurnContext(
        message="Continue working on the active Goal.",
        session_key="agent:main:goal-routing",
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        raw_message="Continue working on the active Goal.",
        routing_hint=objective,
        skill_catalog=SimpleNamespace(generation=1, skills=(skill,)),
    )

    result = await skills_filter.filter_skills(ctx)

    assert captured_queries == [objective]
    assert captured_logs[-1]["query_preview"] == "[goal objective]"
    assert objective not in repr(result.metadata)
    assert objective not in str(result.system_prompt)


@pytest.mark.asyncio
async def test_skill_filter_log_does_not_leak_user_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_logs: list[dict[str, object]] = []
    message = "Use the private migration codename UNIQUE_MARKER to pick the database skill."

    class _Retriever:
        def retrieve(
            self,
            skills: list[SkillSpec],
            query: str,
            *,
            top_k: int,
        ) -> list[SkillSpec]:
            return skills[:top_k]

    monkeypatch.setattr(skills_filter, "_get_retriever", lambda _config: _Retriever())
    monkeypatch.setattr(
        skills_filter.log,
        "debug",
        lambda _event, **fields: captured_logs.append(fields),
    )
    config = GatewayConfig()
    config.skills.filter_enabled = True
    skill = SkillSpec(
        name="database",
        description="Inspect database state",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="Use database tools.",
        path=Path("/synthetic/database/SKILL.md"),
    )
    ctx = TurnContext(
        message=message,
        session_key="agent:main:log-redaction",
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        raw_message=message,
        skill_catalog=SimpleNamespace(generation=1, skills=(skill,)),
    )

    result = await skills_filter.filter_skills(ctx)

    # The debug log must not carry recoverable user prompt text (issue #1208):
    # query_preview is now a non-recoverable fingerprint.
    preview = captured_logs[-1]["query_preview"]
    assert isinstance(preview, str)
    assert preview.startswith("sha256:")
    assert "UNIQUE_MARKER" not in repr(captured_logs)
    assert message not in repr(captured_logs)
    assert result.metadata.get("skills_rendered_count") is not None
