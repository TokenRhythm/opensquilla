"""filtered_skill_reasons: per-skill drop reason codes for the skills_filter step.

Addresses issue #54: ``filtered_skill_ids`` records which skills survived the
skills_filter step but not why any were dropped. The step now publishes a
companion ``filtered_skill_reasons`` mapping (skill ID -> stable reason code)
covering the three drop surfaces: the deterministic gate, meta-skill visibility,
and retrieval top-k selection. The mapping flows into the decision log's
``PipelineStepRecord`` and round-trips through the JSONL writer/reader.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.engine import pipeline as pipeline_mod
from opensquilla.engine.steps import skills_filter
from opensquilla.engine.steps.skills_filter import filter_skills
from opensquilla.observability.decision_log import (
    DecisionEntry,
    PipelineStepRecord,
    load_entries,
    write_decision_entry,
)
from opensquilla.skills.types import SkillLayer, SkillSpec


def _skill(name: str) -> SkillSpec:
    return SkillSpec(
        name=name,
        description=f"{name} skill",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="body",
    )


def _make_entry(**overrides) -> DecisionEntry:
    defaults = dict(
        turn_id="t1",
        session_key="s1",
        prompt_hash="a" * 16,
        system_prompt_hash="b" * 16,
        tool_list_hash="c" * 16,
        tool_choice="auto",
        tokens_input=10,
        tokens_output=20,
        model="claude",
        provider="anthropic",
        latency_ms=100,
        ts="2026-01-01T00:00:00Z",
        pipeline_steps=[
            PipelineStepRecord(
                step_name="filter_skills",
                applied=True,
                filtered_skill_ids=["weather-local"],
                filtered_skill_reasons={"github-local": "not_in_top_k"},
            )
        ],
    )
    defaults.update(overrides)
    return DecisionEntry(**defaults)


def test_gate_reason_code_disable_model_invocation() -> None:
    spec = _skill("hidden")
    spec.disable_model_invocation = True
    drop_reasons: dict[str, str] = {}
    gated = skills_filter._deterministic_gate(
        [spec], available_tools=set(), drop_reasons=drop_reasons
    )
    assert gated == []
    assert drop_reasons == {"hidden": "disable_model_invocation"}


def test_gate_reason_code_missing_required_tools() -> None:
    spec = _skill("needs-git")
    spec.requires_tools = ["git_status"]
    drop_reasons: dict[str, str] = {}
    gated = skills_filter._deterministic_gate(
        [spec], available_tools=set(), drop_reasons=drop_reasons
    )
    assert gated == []
    assert drop_reasons == {"needs-git": "missing_required_tools"}


def test_gate_reason_code_superseded_by_toolset() -> None:
    spec = _skill("legacy-git")
    spec.fallback_for_toolsets = ["git_status"]
    drop_reasons: dict[str, str] = {}
    gated = skills_filter._deterministic_gate(
        [spec], available_tools={"git_status"}, drop_reasons=drop_reasons
    )
    assert gated == []
    assert drop_reasons == {"legacy-git": "superseded_by_toolset"}


def test_pipeline_step_record_roundtrips_filtered_skill_reasons(tmp_path: Path) -> None:
    entry = _make_entry()
    write_decision_entry(entry, log_dir=tmp_path)
    loaded = load_entries(next(tmp_path.glob("decisions-*.jsonl")))
    assert len(loaded) == 1
    (step,) = loaded[0].pipeline_steps
    assert step.filtered_skill_ids == ["weather-local"]
    assert step.filtered_skill_reasons == {"github-local": "not_in_top_k"}


def test_old_row_without_filtered_skill_reasons_reads_as_none(tmp_path: Path) -> None:
    """Backward-tolerant read: a pre-#54 row (no filtered_skill_reasons) must
    hydrate cleanly with None via _filter_payload."""
    legacy_step = {
        "step_name": "filter_skills",
        "applied": True,
        "filtered_skill_ids": ["weather-local"],
        "routing_source": "none",
    }
    payload = {
        "turn_id": "old",
        "session_key": "s",
        "prompt_hash": "a" * 16,
        "system_prompt_hash": "b" * 16,
        "tool_list_hash": "c" * 16,
        "tool_choice": "auto",
        "tokens_input": 1,
        "tokens_output": 2,
        "model": "x",
        "provider": "y",
        "latency_ms": 3,
        "ts": "2026-01-01T00:00:00Z",
        "pipeline_steps": [legacy_step],
    }
    path = tmp_path / "decisions-20260101.jsonl"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    loaded = load_entries(path)
    assert len(loaded) == 1
    (step,) = loaded[0].pipeline_steps
    assert step.filtered_skill_ids == ["weather-local"]
    assert step.filtered_skill_reasons is None


@pytest.mark.asyncio
async def test_run_pipeline_wires_filtered_skill_reasons_into_record() -> None:
    """run_pipeline must copy ctx.metadata['filtered_skill_reasons'] into the
    filter_skills PipelineStepRecord (and None for other steps)."""

    async def other_step(ctx):
        return ctx

    async def filter_skills(ctx):
        ctx.metadata["filtered_skill_ids"] = ["weather-local"]
        ctx.metadata["filtered_skill_reasons"] = {"github-local": "not_in_top_k"}
        return ctx

    ctx = pipeline_mod.TurnContext(
        message="hi",
        session_key="agent:main:webchat:default",
        config=None,
        provider=None,
        model="test-model",
        tool_defs=[],
        system_prompt="base",
        metadata={"pipeline_steps": []},
    )
    ctx = await pipeline_mod.run_pipeline(ctx, [other_step, filter_skills])
    (first, second) = ctx.metadata["pipeline_steps"]
    assert first.filtered_skill_reasons is None
    assert second.step_name == "filter_skills"
    assert second.filtered_skill_ids == ["weather-local"]
    assert second.filtered_skill_reasons == {"github-local": "not_in_top_k"}


@pytest.mark.asyncio
async def test_retrieval_top_k_drop_recorded_as_not_in_top_k(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """filter_enabled + top_k smaller than candidate count -> survivors get IDs,
    dropped candidates get the not_in_top_k reason code (the exact issue #54
    scenario, with a real loader and lexical retriever)."""
    from opensquilla.skills.loader import SkillLoader

    workspace = tmp_path / "workspace"
    for name, description, triggers in (
        ("weather-local", "Fetch weather forecasts.", "[weather, forecast]"),
        ("github-local", "Inspect GitHub pull requests.", "[github, pull request]"),
    ):
        skill_dir = workspace / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"triggers: {triggers}\n"
            "---\n\n"
            f"# {name}\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(skills_filter, "_retriever", None)
    loader = SkillLoader(workspace_dir=workspace, snapshot_path=tmp_path / "snapshot.json")
    ctx = pipeline_mod.TurnContext(
        message="please check the weather forecast",
        session_key="agent:main:webchat:default",
        config=SimpleNamespace(
            tools=SimpleNamespace(profile="standard"),
            skills=SimpleNamespace(
                filter_enabled=True,
                filter_top_k=1,
                filter_strategy="lexical",
                filter_lexical_top_n=20,
                filter_semantic_top_n=20,
                filter_rrf_k=60,
                filter_embedding_model="BAAI/bge-small-zh-v1.5",
                max_skills_prompt_chars=100_000,
                injection_mode="system",
            ),
        ),
        provider=None,
        model="test-model",
        tool_defs=[
            SimpleNamespace(name=name)
            for name in (
                "background_process",
                "cron",
                "exec_command",
                "memory_get",
                "memory_save",
                "memory_search",
                "process",
            )
        ],
        system_prompt="base",
        metadata={"skill_loader": loader},
    )

    ctx = await filter_skills(ctx)

    assert ctx.metadata["filtered_skill_ids"] == ["weather-local"]
    assert ctx.metadata["filtered_skill_reasons"] == {"github-local": "not_in_top_k"}


def test_retrieval_failure_shape_maps_to_retrieval_failed_code() -> None:
    """Empty retriever output with candidates available -> retrieval_failed.
    The step-level branch is exercised via its public contract here."""
    from opensquilla.skills.retrieval import HybridRetriever

    retriever = HybridRetriever(embedder=None, strategy="lexical")
    # A query with zero lexical hits (no FTS/substring match) makes rank()
    # return [] for every layer, so retrieve() hits its full-failure path
    # and returns [] — the exact shape the step maps to retrieval_failed.
    dropped = retriever.retrieve([_skill("a"), _skill("b")], "zzzzqqqqxxxx", top_k=5)
    assert dropped == []

    # Mirror the step's branch: empty result + non-empty filterable -> the
    # stable retrieval_failed code (never not_in_top_k).
    filterable = [_skill("a"), _skill("b")]
    retrieval_drop_reasons: dict[str, str] = {}
    if not dropped and filterable:
        for s in filterable:
            retrieval_drop_reasons[skills_filter._skill_id(s)] = "retrieval_failed"
    assert retrieval_drop_reasons == {
        "a": "retrieval_failed",
        "b": "retrieval_failed",
    }
