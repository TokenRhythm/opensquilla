"""Snapshot regression net for ``AgentBootstrapStage`` through ``TurnRunner._run_turn``.

The corpus enumerates every input shape the stage has been observed to
handle and pins the output snapshot. The harness patches the
dependencies the agent-bootstrap slice needs (the five
``_resolve_agent_*`` helpers, the model catalog, the summarizer
provider helper, the memory sync managers + private-memory check, and
the Agent constructor) so the slice runs against deterministic stubs.

It then probes ``_maybe_compact_on_t3_upgrade`` (the line immediately
after the slice in ``_run_turn``) to capture the post-slice locals and
raise a sentinel ``BaseException`` that halts the generator without
touching downstream stages. The raising-stub case (#11) exercises the
propagation path through the runtime's terminal ``except Exception``
handler.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.turn_runner.harness import _TurnRunnerAgentFactoryAdapter
from opensquilla.engine.types import AgentConfig, ErrorEvent
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext

# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubProvider:
    name: str = "stub"
    provider_name: str = ""

    def __post_init__(self):
        if not self.provider_name:
            self.provider_name = self.name


@dataclass
class _StubSelector:
    label: str = "selector"
    overridden_models: list[str] = field(default_factory=list)
    resolve_returns: Any = None
    current_model: str = "claude-sonnet-4.5"

    @property
    def current_config(self):
        return SimpleNamespace(model=self.current_model)

    def clone(self):
        return self

    def override_model(self, model: str) -> None:
        self.overridden_models.append(model)

    def resolve(self):
        return self.resolve_returns or _StubProvider("resolved-after-override")


@dataclass
class _StubModelCatalog:
    max_tokens_value: int = 4096
    context_window_value: int = 200_000
    capabilities_value: Any = None

    def resolve_max_tokens(self, model_id, user_override=0, provider=""):  # noqa: ARG002
        return self.max_tokens_value

    def resolve_context_window(self, model_id, provider=""):  # noqa: ARG002
        return self.context_window_value

    def get_capabilities(self, model_id, provider_name="", base_url=""):  # noqa: ARG002
        return self.capabilities_value


@dataclass
class _StubMemorySyncManager:
    label: str = "syncmgr"
    warmed_keys: list[str] = field(default_factory=list)

    async def warm_session(self, session_key: str) -> None:
        self.warmed_keys.append(session_key)


# ---------------------------------------------------------------------------
# Sentinel for halting the generator after the slice
# ---------------------------------------------------------------------------


class _SliceCapture(BaseException):
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot


def _capture_locals_at_post_slice() -> dict[str, Any]:
    """Read ``_run_turn``'s locals at the boundary right after the slice.

    The probe is hooked onto ``_maybe_compact_on_t3_upgrade`` (the very
    next call site after the agent-bootstrap slice). At entry, the
    caller's frame contains every local the agent-bootstrap boundary must
    populate.
    """

    frame = sys._getframe(2)
    while frame is not None:
        if "agent" in frame.f_locals and "agent_config" in frame.f_locals:
            break
        frame = frame.f_back
    assert frame is not None, "could not locate _run_turn frame"
    locs = frame.f_locals

    agent = locs.get("agent")
    agent_config = locs.get("agent_config")
    sync_manager = locs.get("sync_manager")
    return {
        "outcome": "success",
        "agent_class": type(agent).__name__ if agent is not None else None,
        "agent_config_max_tokens": getattr(agent_config, "max_tokens", None),
        "agent_config_context_window_tokens": getattr(
            agent_config, "context_window_tokens", None
        ),
        "agent_config_max_iterations": getattr(
            agent_config, "max_iterations", None
        ),
        "agent_config_timeout": getattr(agent_config, "timeout", None),
        "agent_config_iteration_timeout": getattr(
            agent_config, "iteration_timeout", None
        ),
        "agent_config_tool_timeout": getattr(agent_config, "tool_timeout", None),
        "agent_config_request_timeout": getattr(
            agent_config, "request_timeout", None
        ),
        "agent_config_max_provider_retries": getattr(
            agent_config, "max_provider_retries", None
        ),
        "agent_config_length_capped_continuations": getattr(
            agent_config, "length_capped_continuations", None
        ),
        "agent_config_system_prompt": getattr(agent_config, "system_prompt", None),
        "agent_config_model_id": getattr(agent_config, "model_id", None),
        "agent_config_cache_mode": getattr(agent_config, "cache_mode", None),
        "agent_config_thinking": getattr(agent_config, "thinking", None),
        "agent_config_tool_result_projection_max_inline_chars": getattr(
            agent_config, "tool_result_projection_max_inline_chars", None
        ),
        "agent_config_tool_result_fresh_diagnostic_policy_enabled": getattr(
            agent_config, "tool_result_fresh_diagnostic_policy_enabled", None
        ),
        "agent_config_tool_result_diagnostic_retrieval_gate_enabled": getattr(
            agent_config, "tool_result_diagnostic_retrieval_gate_enabled", None
        ),
        "agent_config_tool_result_fresh_diagnostic_inline_max_chars": getattr(
            agent_config, "tool_result_fresh_diagnostic_inline_max_chars", None
        ),
        "agent_config_tool_result_dispatch_max_chars": getattr(
            agent_config, "tool_result_dispatch_max_chars", None
        ),
        "agent_config_tool_result_dispatch_turn_max_chars": getattr(
            agent_config, "tool_result_dispatch_turn_max_chars", None
        ),
        "agent_config_tool_result_store_full_trace": getattr(
            agent_config, "tool_result_store_full_trace", None
        ),
        "agent_config_flush_enabled": getattr(
            agent_config, "flush_enabled", None
        ),
        "effective_runtime_timeout": locs.get("effective_runtime_timeout"),
        "effective_max_iterations": locs.get("effective_max_iterations"),
        "effective_iteration_timeout": locs.get("effective_iteration_timeout"),
        "effective_tool_timeout": locs.get("effective_tool_timeout"),
        "effective_agent_request_timeout": locs.get(
            "effective_agent_request_timeout"
        ),
        "effective_max_provider_retries": locs.get(
            "effective_max_provider_retries"
        ),
        "private_memory_allowed": locs.get("private_memory_allowed"),
        "sync_manager_label": getattr(sync_manager, "label", None),
    }


# ---------------------------------------------------------------------------
# Patch helpers
# ---------------------------------------------------------------------------


def _patch_resolver(runner, provider, cloned_selector):
    def _resolve_provider(self):  # noqa: ARG001
        return provider, cloned_selector

    runner._resolve_provider = _resolve_provider.__get__(runner, TurnRunner)


def _patch_builder(runner, tool_defs, tool_handler, metadata):
    metadata_value = dict(metadata or {})

    def _build_tools(self, ctx=None, metadata=None):  # noqa: ARG001
        if metadata is not None:
            metadata.update(metadata_value)
        return list(tool_defs), tool_handler

    runner._build_tools = _build_tools.__get__(runner, TurnRunner)


def _patch_ctx_mutators(runner):
    async def _with_artifact_context(self, ctx, session_key):  # noqa: ARG001
        return replace(ctx, artifact_session_id=session_key.split(":")[-1] or session_key)

    def _with_runtime_write_callbacks(self, ctx, agent_id):  # noqa: ARG001
        return replace(ctx, agent_id=agent_id)

    runner._with_artifact_context = _with_artifact_context.__get__(runner, TurnRunner)
    runner._with_runtime_write_callbacks = (
        _with_runtime_write_callbacks.__get__(runner, TurnRunner)
    )


def _patch_assemble_prompt(runner, base_prompt, prompt_metadata):
    pm_to_emit = dict(prompt_metadata)

    def _assemble_prompt(
        self, agent_id, tool_defs, *, session_key=None, semantic_message=None,
        extra_context=None, prompt_metadata=None, bootstrap_context_mode=None,
        fresh_user_session=False, workspace_dir=None,
    ):  # noqa: ARG001
        if prompt_metadata is not None:
            prompt_metadata.update(pm_to_emit)
        return base_prompt

    runner._assemble_prompt = _assemble_prompt.__get__(runner, TurnRunner)


def _patch_run_pipeline(runner, turn_factory, provider):
    async def _run_pipeline(
        self, message, session_key, in_provider, cloned_selector,
        tool_defs, base_prompt, attachments, *,
        semantic_message=None, ingress_pipeline_steps=None,
        prev_assistant_text=None, prev_assistant_usage=None,
        history_user_texts=None,
        history_has_recent_image=False,
        history_image_turn_count=0,
        vision_sticky_remaining=0,
        turns_since_last_image=None,
        last_image_turn_text=None,
        vision_candidate_turns=0,
        flags_text_override=None,
        tool_context=None,
        normalization_metadata=None,
    ):  # noqa: ARG001
        return turn_factory(), provider

    runner._run_pipeline = _run_pipeline.__get__(runner, TurnRunner)


def _patch_router_context(runner):
    async def _router_previous_assistant_context(
        self, session_key, *, exclude_last_user=False,  # noqa: ARG001, ARG002
        bound_user_message_id=None,  # noqa: ARG001
    ):
        return {}

    runner._router_previous_assistant_context = (
        _router_previous_assistant_context.__get__(runner, TurnRunner)
    )


def _patch_resolve_prompt_config(runner, final_prompt, breakpoints, request_ctx):
    def _resolve_prompt_config(self, turn):  # noqa: ARG001
        return final_prompt, breakpoints, request_ctx

    runner._resolve_prompt_config = _resolve_prompt_config.__get__(runner, TurnRunner)


def _patch_session_id(runner, session_id):
    async def _resolve_session_id_for_log(self, session_key):  # noqa: ARG001, ARG002
        return session_id

    runner._resolve_session_id_for_log = (
        _resolve_session_id_for_log.__get__(runner, TurnRunner)
    )


def _patch_budget_resolvers(runner, case):
    if case.get("max_iterations_raises"):
        def _raise_max_iter(self, session_key, max_iterations):  # noqa: ARG001, ARG002
            raise ValueError("max_iterations must be an integer >= 0")

        runner._resolve_agent_max_iterations = _raise_max_iter.__get__(
            runner, TurnRunner
        )
        return

    runtime_t = case["runtime_timeout"]
    max_iter = case["max_iterations"]
    iter_t = case["iteration_timeout"]
    tool_t = case["tool_timeout"]
    req_t = case["request_timeout"]
    retries = case["max_provider_retries"]

    def _runtime(self, session_key):  # noqa: ARG001, ARG002
        return runtime_t

    def _max_iter(self, session_key, mi):  # noqa: ARG001, ARG002
        return mi if mi is not None else max_iter

    def _iter_t(self, session_key, it):  # noqa: ARG001, ARG002
        return it if it is not None else iter_t

    def _tool_t(self, session_key, tt):  # noqa: ARG001, ARG002
        return tt if tt is not None else tool_t

    def _req_t(self, session_key, rt):  # noqa: ARG001, ARG002
        return rt if rt is not None else req_t

    def _retries(self, session_key, r):  # noqa: ARG001, ARG002
        return r if r is not None else retries

    runner._resolve_agent_runtime_timeout = _runtime.__get__(runner, TurnRunner)
    runner._resolve_agent_max_iterations = _max_iter.__get__(runner, TurnRunner)
    runner._resolve_agent_iteration_timeout = _iter_t.__get__(runner, TurnRunner)
    runner._resolve_agent_tool_timeout = _tool_t.__get__(runner, TurnRunner)
    runner._resolve_agent_request_timeout = _req_t.__get__(runner, TurnRunner)
    runner._resolve_agent_max_provider_retries = _retries.__get__(runner, TurnRunner)


def _patch_thinking(runner, case):
    thinking_value = case["thinking"]

    def _resolve_turn_thinking(self, turn):  # noqa: ARG001, ARG002
        return thinking_value

    runner._resolve_turn_thinking = _resolve_turn_thinking.__get__(runner, TurnRunner)


def _patch_memory_helpers(runner):
    def _resolve_memory_source_dir(self, agent_id):  # noqa: ARG001, ARG002
        return "/tmp/mem"

    def _load_memory_md(self, workspace_dir, max_chars=None):  # noqa: ARG001, ARG002
        return None

    def _load_daily_notes(self, workspace_dir):  # noqa: ARG001, ARG002
        return {}

    runner._resolve_memory_source_dir = _resolve_memory_source_dir.__get__(
        runner, TurnRunner
    )
    runner._load_memory_md = _load_memory_md.__get__(runner, TurnRunner)
    runner._load_daily_notes = _load_daily_notes.__get__(runner, TurnRunner)


def _patch_post_slice_probe(runner):
    """Hook _maybe_compact_on_t3_upgrade (first call past the slice)."""

    async def _probe(
        self, session_key, turn, context_window_tokens,
        *, compaction_provider=None, compaction_model=None,
    ):  # noqa: ARG001, ARG002
        snapshot = _capture_locals_at_post_slice()
        raise _SliceCapture(snapshot)

    runner._maybe_compact_on_t3_upgrade = _probe.__get__(runner, TurnRunner)


def _patch_observability(runner):
    """Silence trace emit and persist so the slice can run end-to-end."""

    def _emit_turn_event(self, *args, **kwargs):  # noqa: ARG001, ARG002
        return None

    async def _persist_turn_error(self, *args, **kwargs):  # noqa: ARG001, ARG002
        return None

    runner._emit_turn_event = _emit_turn_event.__get__(runner, TurnRunner)
    runner._persist_turn_error = _persist_turn_error.__get__(runner, TurnRunner)


def _build_runner(*, model_catalog=None, memory_sync_managers=None) -> TurnRunner:
    return TurnRunner(
        provider_selector=None,
        tool_registry=None,
        session_manager=None,
        skill_loader=None,
        usage_tracker=None,
        config=None,
        memory_sync_managers=memory_sync_managers,
        model_catalog=model_catalog,
        memory_retrievers=None,
        turn_capture_services=None,
        session_flush_service=None,
        session_lock_provider=None,
        diagnostics_state=None,
        turn_hooks=None,
    )


def _make_turn_factory(*, metadata=None, tool_defs=None):
    def _factory():
        return SimpleNamespace(
            message="EFFECTIVE",
            metadata=dict(metadata or {}),
            tool_defs=list(tool_defs or []),
            model="",
        )
    return _factory


# ---------------------------------------------------------------------------
# Corpus — 11 cases per the design.
# ---------------------------------------------------------------------------


_CASE_BASE: dict[str, Any] = dict(
    runtime_timeout=60.0,
    max_iterations=10,
    iteration_timeout=30.0,
    tool_timeout=20.0,
    request_timeout=120.0,
    max_provider_retries=3,
    thinking=False,
    catalog_max_tokens=4096,
    catalog_context_window=200_000,
    catalog_capabilities=None,
    model_catalog_present=True,
    memory_sync_managers=None,
    private_memory_allowed_value=True,
    snapshot_pre_existing=False,
    per_call_timeout=None,
    per_call_max_iterations=None,
    max_iterations_raises=False,
)


def _case(case_id: str, **overrides: Any) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = dict(_CASE_BASE)
    payload.update(overrides)
    return case_id, payload


_CORPUS: list[tuple[str, dict[str, Any]]] = [
    _case("success_all_defaults"),
    _case("per_call_timeout", per_call_timeout=42.0),
    _case("env_var_timeout_proxy", runtime_timeout=99.0),
    _case(
        "session_max_iterations",
        per_call_max_iterations=5,
        max_iterations=5,
    ),
    _case(
        "no_model_catalog",
        model_catalog_present=False,
        catalog_max_tokens=16384,
        catalog_context_window=200_000,
    ),
    _case(
        "model_with_capabilities",
        thinking=True,
        catalog_capabilities=SimpleNamespace(supports_reasoning=True),
    ),
    _case("projection_limit_default"),
    _case("sync_manager_warm"),
    _case("private_memory_disabled", private_memory_allowed_value=False),
    _case("snapshot_already_exists", snapshot_pre_existing=True),
    _case(
        "max_iterations_zero_unbounded",
        per_call_max_iterations=0,
        max_iterations=0,
    ),
]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def _drive(runner, case, monkeypatch):
    # Patch private-memory injection for cases that toggle the boolean
    def _allows(session_key):  # noqa: ARG001
        return case["private_memory_allowed_value"]

    # Two import sites: runtime imports ``allows_private_memory_prompt_injection``
    # at module level, and the adapter lazy-imports inside the function body.
    # Patch both so the runtime path and adapter path see the same value.
    monkeypatch.setattr(
        "opensquilla.engine.runtime.allows_private_memory_prompt_injection",
        _allows,
    )
    monkeypatch.setattr(
        "opensquilla.session.keys.allows_private_memory_prompt_injection",
        _allows,
    )

    # If the case calls for a pre-existing snapshot, seed the dict
    if case["snapshot_pre_existing"]:
        from opensquilla.engine.runtime import MemorySnapshot

        runner._memory_snapshots[("agent:main", "agent:main:s1")] = MemorySnapshot()

    captured = None
    raised = None
    yielded: list[Any] = []
    gen = runner._run_turn(
        message="hi",
        session_key="agent:main:s1",
        agent_id="agent:main",
        model=None,
        attachments=[],
        tool_context=None,
        input_mode="user",
        persist_input=False,
        input_provenance=None,
        history_has_persisted_user=True,
        semantic_message=None,
        timeout=case["per_call_timeout"],
        max_iterations=case["per_call_max_iterations"],
    )
    try:
        async for event in gen:
            yielded.append(event)
    except _SliceCapture as cap:
        captured = cap.snapshot
    except BaseException as exc:  # noqa: BLE001
        raised = type(exc)
    finally:
        await gen.aclose()
    return captured, yielded, raised


def _setup_runner(case: dict[str, Any]) -> TurnRunner:
    catalog = None
    if case["model_catalog_present"]:
        catalog = _StubModelCatalog(
            max_tokens_value=case["catalog_max_tokens"],
            context_window_value=case["catalog_context_window"],
            capabilities_value=case["catalog_capabilities"],
        )

    memory_sync_managers = case["memory_sync_managers"]
    runner = _build_runner(
        model_catalog=catalog,
        memory_sync_managers=memory_sync_managers,
    )
    selector = _StubSelector(
        "sel",
        current_model="claude-sonnet-4.5",
        resolve_returns=_StubProvider("override-resolved"),
    )
    _patch_resolver(runner, _StubProvider("p"), selector)
    _patch_builder(runner, [SimpleNamespace(name="t1")], object(), {"tool_profile": "agent"})
    _patch_ctx_mutators(runner)
    _patch_assemble_prompt(runner, "BASE", {})
    _patch_run_pipeline(
        runner,
        _make_turn_factory(metadata={"tool_profile": "agent"}, tool_defs=[]),
        provider=_StubProvider("post-pipeline"),
    )
    _patch_router_context(runner)
    _patch_resolve_prompt_config(runner, "FINAL", None, None)
    _patch_session_id(runner, "sess-1")
    _patch_budget_resolvers(runner, case)
    _patch_thinking(runner, case)
    _patch_memory_helpers(runner)
    _patch_post_slice_probe(runner)
    _patch_observability(runner)
    return runner


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


_CORPUS_IDS = [c[0] for c in _CORPUS]


@pytest.mark.parametrize("case_id,case", _CORPUS, ids=_CORPUS_IDS)
@pytest.mark.asyncio
async def test_agent_bootstrap_stage_snapshot(
    case_id, case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _setup_runner(case)
    captured, yielded, raised = await _drive(runner, case, monkeypatch)

    if case["max_iterations_raises"]:
        # Exception propagates through the runtime's terminal handler ->
        # ErrorEvent yielded; no probe capture.
        assert captured is None
        assert raised is None
        assert len(yielded) == 1
        assert isinstance(yielded[0], ErrorEvent)
        assert yielded[0].code == "agent_error"
        return

    assert raised is None, f"{case_id} raised: {raised}"
    assert captured is not None, f"{case_id} captured nothing"

    expected_runtime_timeout = (
        float(case["per_call_timeout"])
        if case["per_call_timeout"] is not None
        else case["runtime_timeout"]
    )
    expected_max_iterations = case["max_iterations"]
    expected_snapshot = {
        "outcome": "success",
        "agent_class": "Agent",
        "agent_config_max_tokens": case["catalog_max_tokens"],
        "agent_config_context_window_tokens": case["catalog_context_window"],
        "agent_config_max_iterations": expected_max_iterations,
        "agent_config_timeout": expected_runtime_timeout,
        "agent_config_iteration_timeout": case["iteration_timeout"],
        "agent_config_tool_timeout": case["tool_timeout"],
        "agent_config_request_timeout": case["request_timeout"],
        "agent_config_max_provider_retries": case["max_provider_retries"],
        "agent_config_length_capped_continuations": 3,
        "agent_config_system_prompt": "FINAL",
        "agent_config_model_id": "claude-sonnet-4.5",
        "agent_config_cache_mode": "off",
        "agent_config_thinking": case["thinking"],
        "agent_config_tool_result_projection_max_inline_chars": 60_000,
        "agent_config_tool_result_fresh_diagnostic_policy_enabled": False,
        "agent_config_tool_result_diagnostic_retrieval_gate_enabled": False,
        "agent_config_tool_result_fresh_diagnostic_inline_max_chars": 64_000,
        "agent_config_tool_result_dispatch_max_chars": 0,
        "agent_config_tool_result_dispatch_turn_max_chars": 0,
        "agent_config_tool_result_store_full_trace": False,
        "agent_config_flush_enabled": False,
        "effective_runtime_timeout": expected_runtime_timeout,
        "effective_max_iterations": expected_max_iterations,
        "effective_iteration_timeout": case["iteration_timeout"],
        "effective_tool_timeout": case["tool_timeout"],
        "effective_agent_request_timeout": case["request_timeout"],
        "effective_max_provider_retries": case["max_provider_retries"],
        "private_memory_allowed": case["private_memory_allowed_value"],
        "sync_manager_label": None,
    }
    assert captured == expected_snapshot, (
        f"case={case_id}: snapshot diverged.\n"
        f"  expected={expected_snapshot}\n  actual  ={captured}"
    )


@pytest.mark.asyncio
async def test_memory_snapshot_dict_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_memory_snapshots`` dict mutation lands the expected entry."""

    case = dict(_CORPUS[0][1])
    runner = _setup_runner(case)
    await _drive(runner, case, monkeypatch)
    snapshots = dict(runner._memory_snapshots)
    assert len(snapshots) >= 1
    for snapshot in snapshots.values():
        assert snapshot.memory_md is None
        assert snapshot.daily_notes == {}


@pytest.mark.asyncio
async def test_sync_manager_warm_called(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``memory_sync_managers`` is configured, ``warm_session`` runs once."""

    sync_manager = _StubMemorySyncManager()
    case = dict(_CORPUS[0][1])
    case["memory_sync_managers"] = {"agent:main": sync_manager}
    runner = _setup_runner(case)
    captured, _, _ = await _drive(runner, case, monkeypatch)
    assert sync_manager.warmed_keys == ["agent:main:s1"]
    assert captured is not None
    assert captured["sync_manager_label"] == "syncmgr"


def test_agent_factory_forwards_registry_and_tool_context() -> None:
    """meta_invoke dispatch needs the Agent to retain registry/context wiring."""

    registry = ToolRegistry()
    tool_context = ToolContext(is_owner=True, workspace_dir="/tmp")
    runner = TurnRunner(
        provider_selector=None,
        tool_registry=registry,
    )

    agent = _TurnRunnerAgentFactoryAdapter(runner).build(
        provider=_StubProvider("p"),
        config=AgentConfig(),
        tool_definitions=[],
        tool_handler=None,
        session_key="agent:main:s1",
        turn_call_logger=None,
        memory_sync_manager=None,
        tool_context=tool_context,
    )

    assert agent._tool_registry is registry
    assert agent._tool_context is tool_context
