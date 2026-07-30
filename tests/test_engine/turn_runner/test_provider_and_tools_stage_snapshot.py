"""Snapshot regression net for ``ProviderAndToolsStage`` through ``TurnRunner._run_turn``.

The corpus enumerates every input shape the stage has been observed to
handle and pins the output snapshot. The harness patches
``_resolve_provider`` and ``_build_tools`` on the runner so the slice
runs against deterministic stubs without real registries or selectors.
It then probes ``_assemble_prompt`` (the line immediately after the
slice) to capture the post-slice locals and raise a sentinel
``BaseException`` that halts the generator without touching any
downstream stage. Raising-stub cases (#9, #10) exercise the propagation
path through the runtime's terminal ``except Exception`` handler.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import ErrorEvent
from opensquilla.tools.types import CallerKind, ToolContext


@dataclass
class _StubProvider:
    name: str = "stub"


@dataclass
class _StubSelector:
    label: str = "selector"


class _SliceCapture(BaseException):
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot


@dataclass
class _ProbeState:
    emitted_turn_errors: list[dict[str, Any]] = field(default_factory=list)
    persist_error_calls: list[tuple[str, Any]] = field(default_factory=list)


def _make_assemble_prompt_probe():
    def _probe(
        self, agent_id, tool_defs, *, session_key, semantic_message,
        extra_context, prompt_metadata, bootstrap_context_mode,
        fresh_user_session=False, workspace_dir=None,
    ):  # noqa: ARG001
        # Walk up the stack to find the ``_run_turn`` frame. Under the
        # In the staged runtime, ``_assemble_prompt`` is called from inside
        # the ``PromptAssemblerStage`` adapter rather than directly from
        # ``_run_turn``, so frame 1 is the adapter; we have to climb to
        # the frame that carries the ``_run_turn`` locals of interest.
        frame = sys._getframe(1)
        while frame is not None:
            if (
                "provider" in frame.f_locals
                and "cloned_selector" in frame.f_locals
                and "tool_handler" in frame.f_locals
                and "tool_metadata" in frame.f_locals
            ):
                break
            frame = frame.f_back
        if frame is None:
            raise RuntimeError("probe could not locate _run_turn frame")
        locs = frame.f_locals
        provider = locs.get("provider")
        cloned_selector = locs.get("cloned_selector")
        tool_handler = locs.get("tool_handler")
        tool_context = locs.get("tool_context")
        tool_metadata = locs.get("tool_metadata") or {}
        snapshot = {
            "outcome": "success",
            "provider_name": getattr(provider, "name", None),
            "cloned_selector_label": getattr(cloned_selector, "label", None),
            "tool_defs_repr": repr(tool_defs),
            "tool_handler_present": tool_handler is not None,
            "ctx_artifact_session_id": (
                tool_context.artifact_session_id if tool_context is not None else None
            ),
            "ctx_caller_kind": (
                tool_context.caller_kind.value if tool_context is not None else None
            ),
            "tool_metadata_items": tuple(sorted(tool_metadata.items())),
        }
        raise _SliceCapture(snapshot)

    return _probe


def _patch_resolver(runner, provider, cloned_selector, raises=None):
    def _resolve_provider(self):  # noqa: ARG001
        if raises is not None:
            raise raises("equivalence resolver boom")
        return provider, cloned_selector

    runner._resolve_provider = _resolve_provider.__get__(runner, TurnRunner)


def _patch_builder(runner, tool_defs, tool_handler, metadata, raises=None):
    captured = dict(metadata) if metadata is not None else {"tool_profile": "agent"}

    def _build_tools(self, ctx=None, metadata=None):  # noqa: ARG001
        if raises is not None:
            raise raises("equivalence builder boom")
        if metadata is not None:
            metadata.update(captured)
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


def _patch_observability(runner, state):
    original_emit = runner._emit_turn_event

    def _emit_turn_event(self, name, trace_context, **kwargs):  # noqa: ARG001
        if name == "turn_error":
            state.emitted_turn_errors.append(dict(kwargs))
        original_emit(name, trace_context, **kwargs)

    runner._emit_turn_event = _emit_turn_event.__get__(runner, TurnRunner)

    async def _persist_turn_error(self, session_key, event):  # noqa: ARG001
        state.persist_error_calls.append((session_key, event))

    runner._persist_turn_error = _persist_turn_error.__get__(runner, TurnRunner)


def _build_runner() -> TurnRunner:
    return TurnRunner(
        provider_selector=None,
        tool_registry=None,
        session_manager=None,
        skill_loader=None,
        usage_tracker=None,
        config=None,
        memory_sync_managers=None,
        model_catalog=None,
        memory_retrievers=None,
        turn_capture_services=None,
        session_flush_service=None,
        session_lock_provider=None,
        diagnostics_state=None,
        turn_hooks=None,
    )


def _ctx(kind: CallerKind = CallerKind.AGENT, **extra: Any) -> ToolContext:
    return ToolContext(caller_kind=kind, agent_id="agent:main", **extra)


# Corpus: tuples of (case_id, kwargs) for parametrize. Each case configures
# resolver/builder behavior and the expected outcome shape.
_HANDLER = object()
_DEFS_5 = [{"name": f"t{i}"} for i in range(1, 6)]
_DEFS_3 = [{"name": f"t{i}"} for i in range(1, 4)]
_DEFS_2 = [{"name": "t1"}, {"name": "t2"}]
_DEFS_1 = [{"name": "t1"}]


_CORPUS: list[tuple[str, dict[str, Any]]] = [
    (
        "success_no_tool_ctx",
        dict(
            session_key="agent:main:s1", tool_context=None,
            provider=_StubProvider("p1"), cloned_selector=_StubSelector("sel1"),
            tool_defs=_DEFS_5, tool_handler=_HANDLER,
            metadata={"tool_profile": "agent"},
            expected={
                "outcome": "success", "provider_name": "p1",
                "cloned_selector_label": "sel1",
                "tool_defs_repr": repr(_DEFS_5), "tool_handler_present": True,
                "ctx_artifact_session_id": None, "ctx_caller_kind": None,
                "tool_metadata_items": (("tool_profile", "agent"),),
            },
        ),
    ),
    (
        "success_default_ctx",
        dict(
            session_key="agent:main:s2", tool_context=_ctx(),
            provider=_StubProvider("p2"), cloned_selector=_StubSelector("sel2"),
            tool_defs=_DEFS_2, tool_handler=_HANDLER,
            metadata={"tool_profile": "agent"},
            expected={
                "outcome": "success", "provider_name": "p2",
                "cloned_selector_label": "sel2",
                "tool_defs_repr": repr(_DEFS_2), "tool_handler_present": True,
                "ctx_artifact_session_id": "s2", "ctx_caller_kind": "agent",
                "tool_metadata_items": (("tool_profile", "agent"),),
            },
        ),
    ),
    (
        "success_subagent_ctx",
        dict(
            session_key="agent:main:s3",
            tool_context=_ctx(CallerKind.SUBAGENT),
            provider=_StubProvider("p3"), cloned_selector=_StubSelector("sel3"),
            tool_defs=_DEFS_1, tool_handler=_HANDLER,
            metadata={"tool_profile": "subagent"},
            expected={
                "outcome": "success", "provider_name": "p3",
                "cloned_selector_label": "sel3",
                "tool_defs_repr": repr(_DEFS_1), "tool_handler_present": True,
                "ctx_artifact_session_id": "s3", "ctx_caller_kind": "subagent",
                "tool_metadata_items": (("tool_profile", "subagent"),),
            },
        ),
    ),
    (
        "success_channel_ctx",
        dict(
            session_key="agent:main:s4",
            tool_context=_ctx(CallerKind.CHANNEL, channel_id="C123"),
            provider=_StubProvider("p4"), cloned_selector=_StubSelector("sel4"),
            tool_defs=_DEFS_3, tool_handler=_HANDLER,
            metadata={"tool_profile": "agent"},
            expected={
                "outcome": "success", "provider_name": "p4",
                "cloned_selector_label": "sel4",
                "tool_defs_repr": repr(_DEFS_3), "tool_handler_present": True,
                "ctx_artifact_session_id": "s4", "ctx_caller_kind": "channel",
                "tool_metadata_items": (("tool_profile", "agent"),),
            },
        ),
    ),
    (
        "success_empty_registry",
        dict(
            session_key="agent:main:s5", tool_context=None,
            provider=_StubProvider("p5"), cloned_selector=_StubSelector("sel5"),
            tool_defs=[], tool_handler=None, metadata={},
            expected={
                "outcome": "success", "provider_name": "p5",
                "cloned_selector_label": "sel5",
                "tool_defs_repr": repr([]), "tool_handler_present": False,
                "ctx_artifact_session_id": None, "ctx_caller_kind": None,
                "tool_metadata_items": (),
            },
        ),
    ),
    (
        "success_registry_denies_all",
        dict(
            session_key="agent:main:s6", tool_context=_ctx(),
            provider=_StubProvider("p6"), cloned_selector=_StubSelector("sel6"),
            tool_defs=[], tool_handler=_HANDLER,
            metadata={"tool_profile": "agent"},
            expected={
                "outcome": "success", "provider_name": "p6",
                "cloned_selector_label": "sel6",
                "tool_defs_repr": repr([]), "tool_handler_present": True,
                "ctx_artifact_session_id": "s6", "ctx_caller_kind": "agent",
                "tool_metadata_items": (("tool_profile", "agent"),),
            },
        ),
    ),
    (
        "early_yield_no_provider_no_ctx",
        dict(
            session_key="agent:main:s7", tool_context=None,
            provider=None, cloned_selector=None,
            tool_defs=[], tool_handler=None, metadata=None,
            expected={"outcome": "early_yield"},
        ),
    ),
    (
        "early_yield_no_provider_with_ctx",
        dict(
            session_key="agent:main:s8", tool_context=_ctx(),
            provider=None, cloned_selector=None,
            tool_defs=[], tool_handler=None, metadata=None,
            expected={"outcome": "early_yield"},
        ),
    ),
    (
        "exception_resolver_raises",
        dict(
            session_key="agent:main:s9", tool_context=None,
            provider=None, cloned_selector=None, resolver_raises=ConnectionError,
            tool_defs=[], tool_handler=None, metadata=None,
            expected={"outcome": "exception", "exception_type": "ConnectionError"},
        ),
    ),
    (
        "exception_build_raises",
        dict(
            session_key="agent:main:s10", tool_context=_ctx(),
            provider=_StubProvider("p10"), cloned_selector=_StubSelector("sel10"),
            tool_defs=[], tool_handler=None, metadata=None,
            builder_raises=ValueError,
            expected={"outcome": "exception", "exception_type": "ValueError"},
        ),
    ),
]


async def _drive(runner, case):
    raised = None
    captured = None
    yielded = []
    gen = runner._run_turn(
        message="hi",
        session_key=case["session_key"],
        agent_id="agent:main",
        model=None,
        attachments=[],
        tool_context=case["tool_context"],
        input_mode="user",
        persist_input=False,
        input_provenance=None,
        semantic_message=None,
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


@pytest.mark.parametrize("case_id,case", _CORPUS, ids=[c[0] for c in _CORPUS])
@pytest.mark.asyncio
async def test_provider_and_tools_stage_snapshot(
    case_id, case, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _build_runner()
    state = _ProbeState()
    _patch_resolver(
        runner, case["provider"], case["cloned_selector"],
        raises=case.get("resolver_raises"),
    )
    _patch_builder(
        runner, case["tool_defs"], case["tool_handler"], case["metadata"],
        raises=case.get("builder_raises"),
    )
    _patch_ctx_mutators(runner)
    _patch_observability(runner, state)
    runner._assemble_prompt = _make_assemble_prompt_probe().__get__(runner, TurnRunner)

    captured, yielded, raised = await _drive(runner, case)
    expected = case["expected"]

    if expected["outcome"] == "success":
        assert captured == expected, (
            f"case={case_id}: snapshot diverged.\n"
            f"  expected={expected}\n  actual  ={captured}"
        )
    elif expected["outcome"] == "early_yield":
        assert captured is None and raised is None
        assert len(yielded) == 1 and isinstance(yielded[0], ErrorEvent)
        assert yielded[0].message == "No provider available"
        assert yielded[0].code == "no_provider"
        # Trace + persist parity
        assert len(state.emitted_turn_errors) == 1
        emit = state.emitted_turn_errors[0]
        payload = emit.get("payload", {})
        assert tuple(sorted(payload.keys())) == ("error_chars", "error_code", "error_type")
        assert payload["error_code"] == "no_provider"
        assert payload["error_type"] == "ProviderResolutionError"
        assert payload["error_chars"] == len("No provider available")
        assert len(state.persist_error_calls) == 1
    else:  # exception — runtime's terminal handler converts to ErrorEvent
        assert raised is None and captured is None
        assert len(yielded) == 1 and isinstance(yielded[0], ErrorEvent)
        assert yielded[0].code == "agent_error"
        assert len(state.emitted_turn_errors) == 1
        assert (
            state.emitted_turn_errors[0]["payload"]["error_type"]
            == expected["exception_type"]
        )
