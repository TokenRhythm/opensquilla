"""Tests for the read-only ``meta.list`` RPC handler."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import opensquilla.gateway.rpc_meta_runs as meta_rpc
from opensquilla.gateway.rpc.registry import RpcContext
from opensquilla.gateway.rpc_meta_runs import _handle_meta_list
from opensquilla.skills.meta.readiness import MetaSkillReadiness
from opensquilla.skills.types import (
    SkillLayer,
    SkillPlatformMeta,
    SkillRequires,
    SkillSpec,
)


def _make_spec(
    name: str,
    *,
    kind: str = "skill",
    description: str = "",
    disable_model_invocation: bool = False,
) -> SkillSpec:
    return SkillSpec(
        name=name,
        description=description,
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="",
        kind=kind,
        disable_model_invocation=disable_model_invocation,
    )


class _StubLoader:
    """Minimal skill loader exposing ``load_all`` like the real loader."""

    def __init__(self, specs: list[SkillSpec]) -> None:
        self._specs = specs

    def load_all(self) -> list[SkillSpec]:
        return list(self._specs)


def test_meta_list_returns_only_invokable_meta_skills() -> None:
    loader = _StubLoader(
        [
            _make_spec("beta-meta", kind="meta", description="Beta meta-skill"),
            _make_spec("alpha-meta", kind="meta", description="Alpha meta-skill"),
            _make_spec("plain-skill", kind="skill", description="Not a meta-skill"),
            _make_spec(
                "hidden-meta",
                kind="meta",
                description="Disabled meta-skill",
                disable_model_invocation=True,
            ),
        ]
    )
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    payload = asyncio.run(_handle_meta_list(None, ctx))

    assert "disabled" not in payload
    assert payload["skills"] == [
        {
            "name": "alpha-meta",
            "description": "Alpha meta-skill",
            "ready": True,
            "status": "ready",
            "missing_bins": [],
            "missing_env": [],
            "missing_env_any": [],
            "missing_skills": [],
            "missing_capabilities": [],
            "missing_provider_capabilities": [],
            "reasons": [],
            "setup_actions": [],
            "manual_setup_actions": [],
        },
        {
            "name": "beta-meta",
            "description": "Beta meta-skill",
            "ready": True,
            "status": "ready",
            "missing_bins": [],
            "missing_env": [],
            "missing_env_any": [],
            "missing_skills": [],
            "missing_capabilities": [],
            "missing_provider_capabilities": [],
            "reasons": [],
            "setup_actions": [],
            "manual_setup_actions": [],
        },
    ]


def test_meta_list_disabled_when_master_gate_off() -> None:
    loader = _StubLoader(
        [_make_spec("alpha-meta", kind="meta", description="Alpha meta-skill")]
    )
    ctx = RpcContext(
        conn_id="test",
        skill_loader=loader,
        config={"meta_skill": {"enabled": False}},
    )

    payload = asyncio.run(_handle_meta_list(None, ctx))

    assert payload == {"skills": [], "disabled": True}


def test_meta_list_uses_passive_readiness(monkeypatch) -> None:
    calls: list[bool] = []

    def assess(spec, *, skill_index, ctx=None, verify_capabilities=True, config=None):
        del spec, skill_index, ctx, config
        calls.append(verify_capabilities)
        return MetaSkillReadiness(ready=True)

    monkeypatch.setattr(meta_rpc, "assess_meta_skill_readiness", assess)
    loader = _StubLoader([_make_spec("safe-meta", kind="meta")])

    payload = asyncio.run(
        _handle_meta_list(None, RpcContext(conn_id="test", skill_loader=loader))
    )

    assert payload["skills"][0]["name"] == "safe-meta"
    assert calls == [False]


def test_meta_list_does_not_expose_active_openrouter_config_to_untrusted_meta(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    spec = _make_spec("media-meta", kind="meta")
    spec.metadata = SkillPlatformMeta(
        requires=SkillRequires(env_any=["OPENROUTER_API_KEY"])
    )
    config = SimpleNamespace(
        meta_skill=SimpleNamespace(enabled=True),
        llm=SimpleNamespace(
            provider="openrouter",
            api_key="synthetic-openrouter-config-key",
            api_key_env="",
        ),
    )

    payload = asyncio.run(
        _handle_meta_list(
            None,
            RpcContext(conn_id="test", config=config, skill_loader=_StubLoader([spec])),
        )
    )

    assert payload["skills"][0]["ready"] is False
    assert payload["skills"][0]["missing_env_any"] == [["OPENROUTER_API_KEY"]]
    assert payload["skills"][0]["missing_provider_capabilities"] == []
    assert payload["skills"][0]["manual_setup_actions"] == []
    assert "synthetic-openrouter-config-key" not in repr(payload)
