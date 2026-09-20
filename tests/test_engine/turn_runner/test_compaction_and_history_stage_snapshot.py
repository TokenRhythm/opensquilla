"""Snapshot regression net for ``CompactionAndHistoryStage`` through ``TurnRunner._run_turn``.

The corpus enumerates every input shape the stage has been observed to
handle and pins the output snapshot. The harness patches the three
BEFORE-turn helpers the slice owns (``_maybe_preflight_compact``, ``_load_history``,
``_prepend_request_context_prompt``) plus reuses the upstream stage's
patch helpers from the agent-bootstrap snapshot harness so the slice
runs against deterministic stubs.

It then probes ``_build_attachment_messages`` (the line immediately
after the slice in ``_run_turn``) to capture the post-slice locals and
raise a sentinel ``BaseException`` that halts the generator without
touching downstream stages. The raising-stub cases (#12 raising
CompactionHook, #13 raising HistoryLoader) exercise the hook-isolation
contract and the exception-propagation contract.
"""

from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.hooks.types import CompactionState
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.turn_runner import (
    AttachmentMaterializationStats,
    AttachmentStageOutput,
)
from opensquilla.engine.turn_runner.outcome import StageOutcome
from opensquilla.engine.types import ErrorEvent
from opensquilla.provider import Message
from opensquilla.provider.types import ContentBlockImage

# Reuse upstream patch helpers from's equivalence harness — this
# stage sits AFTER AgentBootstrapStage's slice so the same upstream
# patching strategy applies. Local duplication would just inflate LOC.
from .test_agent_bootstrap_stage_snapshot import (
    _make_turn_factory,
    _patch_assemble_prompt,
    _patch_builder,
    _patch_ctx_mutators,
    _patch_memory_helpers,
    _patch_observability,
    _patch_resolve_prompt_config,
    _patch_resolver,
    _patch_router_context,
    _patch_run_pipeline,
    _patch_session_id,
    _StubModelCatalog,
    _StubProvider,
    _StubSelector,
)

# ---------------------------------------------------------------------------
# Sentinel for halting the generator after the slice
# ---------------------------------------------------------------------------


class _SliceCapture(BaseException):
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot


def _capture_locals_at_post_slice() -> dict[str, Any]:
    """Read ``_run_turn``'s locals at the boundary right after the slice.

    The probe is hooked onto ``_build_attachment_messages`` (the very
    next call site after the compaction-and-history slice). At entry,
    the caller's frame contains every local the compaction/history boundary
    must populate.
    """

    frame = sys._getframe(2)
    while frame is not None:
        if "agent" in frame.f_locals and "agent_config" in frame.f_locals:
            break
        frame = frame.f_back
    assert frame is not None, "could not locate _run_turn frame"
    locs = frame.f_locals

    agent = locs.get("agent")
    return {
        "outcome": "success",
        "agent_request_context_prompt_after": getattr(
            getattr(agent, "config", None), "request_context_prompt", None
        ),
    }


# ---------------------------------------------------------------------------
# Local budget / thinking patches —'s helpers take a case dict with
# different fields, so a smaller local stub keeps the corpus simple.
# ---------------------------------------------------------------------------


def _patch_budget_resolvers(runner: TurnRunner) -> None:
    def _runtime(self, session_key):  # noqa: ARG001, ARG002
        return 60.0

    def _max_iter(self, session_key, mi):  # noqa: ARG001, ARG002
        return mi if mi is not None else 10

    def _req_t(self, session_key, rt):  # noqa: ARG001, ARG002
        return rt if rt is not None else 120.0

    def _retries(self, session_key, r):  # noqa: ARG001, ARG002
        return r if r is not None else 3

    runner._resolve_agent_runtime_timeout = _runtime.__get__(runner, TurnRunner)
    runner._resolve_agent_max_iterations = _max_iter.__get__(runner, TurnRunner)
    runner._resolve_agent_request_timeout = _req_t.__get__(runner, TurnRunner)
    runner._resolve_agent_max_provider_retries = _retries.__get__(runner, TurnRunner)


def _patch_thinking(runner: TurnRunner) -> None:
    def _resolve_turn_thinking(self, turn):  # noqa: ARG001, ARG002
        return False

    runner._resolve_turn_thinking = _resolve_turn_thinking.__get__(runner, TurnRunner)


# ---------------------------------------------------------------------------
# Compaction / history-slice patches
# ---------------------------------------------------------------------------


def _patch_preflight(runner, *, raises=None, calls=None):
    async def _preflight(
        self, session_key, context_window_tokens,  # noqa: ARG001, ARG002
        *, compaction_provider=None, compaction_model=None,
    ):
        if calls is not None:
            calls.append({"session_key": session_key})
        if raises is not None:
            raise raises("preflight boom")
        return None

    runner._maybe_preflight_compact = _preflight.__get__(runner, TurnRunner)


def _patch_load_history(runner, *, return_value=None, raises=None, calls=None):
    async def _load(
        self, agent, session_key, *, trim_last_user=True, bound_user_message_id=None
    ):  # noqa: ARG001, ARG002
        if calls is not None:
            calls.append(
                {
                    "trim_last_user": trim_last_user,
                    "bound_user_message_id": bound_user_message_id,
                }
            )
        if raises is not None:
            raise raises("history boom")
        return return_value

    runner._load_history = _load.__get__(runner, TurnRunner)


def _patch_post_slice_probe(runner):
    """Stop at Agent.run_turn, after compaction/history output is installed."""

    factory = runner._agent_bootstrap_stage._agent_factory
    original_build = factory.build

    class _ProbeAgentFactory:
        def build(self, **kwargs: Any):
            agent = original_build(**kwargs)

            async def _probe_run_turn(
                probe_agent: Any,
                *_args: Any,
                **_kwargs: Any,
            ):
                snapshot = {
                    "outcome": "success",
                    "agent_request_context_prompt_after": getattr(
                        getattr(probe_agent, "config", None),
                        "request_context_prompt",
                        None,
                    ),
                }
                raise _SliceCapture(snapshot)
                yield  # pragma: no cover

            agent.run_turn = MethodType(_probe_run_turn, agent)
            return agent

    runner._agent_bootstrap_stage._agent_factory = _ProbeAgentFactory()


# ---------------------------------------------------------------------------
# Recording compaction hook for hook-aware cases
# ---------------------------------------------------------------------------


@dataclass
class _RecordingCompactionHook:
    name: str = "rec"
    before_raises: type[BaseException] | None = None
    events: list[tuple[str, str]] = field(default_factory=list)

    async def before_compact(self, state: CompactionState) -> None:
        self.events.append(("before", state.extra.get("phase", "")))
        if self.before_raises is not None:
            raise self.before_raises("hook before boom")

    async def after_compact(self, state: CompactionState, outcome: Any) -> None:  # noqa: ARG002
        self.events.append(("after", state.extra.get("phase", "")))


# ---------------------------------------------------------------------------
# Corpus — 13 cases per the design.
# ---------------------------------------------------------------------------


_CASE_BASE: dict[str, Any] = dict(
    session_key="agent:main:s1",
    preflight_raises=None,
    history_return=None,
    history_raises=None,
    request_context_in=None,
    expected_final_request_context=None,
    hook=None,
    history_has_persisted_user=True,
)


def _case(case_id: str, **overrides: Any) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = dict(_CASE_BASE)
    payload.update(overrides)
    return case_id, payload


_CORPUS: list[tuple[str, dict[str, Any]]] = [
    _case("preflight_runs"),
    _case("cron_prefix_preflight_runs", session_key="cron:tick:s1"),
    _case(
        "durable_summaries_exist",
        history_return="SUMMARY-X",
        expected_final_request_context="SUMMARY-X",
    ),
    _case(
        "legacy_summary_marker_in_transcript",
        history_return="[Context Summary] body",
        expected_final_request_context="[Context Summary] body",
    ),
    _case("trim_last_user_true", history_has_persisted_user=True),
    _case("trim_last_user_false", history_has_persisted_user=False),
    _case("compaction_hook_registered", hook=_RecordingCompactionHook()),
    _case(
        "raising_compaction_hook_isolated",
        hook=_RecordingCompactionHook(before_raises=RuntimeError),
    ),
    _case("history_loader_raises", history_raises=RuntimeError),
]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _build_runner() -> TurnRunner:
    return TurnRunner(
        provider_selector=None,
        tool_registry=None,
        session_manager=None,
        skill_loader=None,
        usage_tracker=None,
        config=None,
        memory_sync_managers=None,
        model_catalog=_StubModelCatalog(),
        memory_retrievers=None,
        turn_capture_services=None,

        session_lock_provider=None,
        diagnostics_state=None,
        turn_hooks=None,
    )


def _setup_runner(case: dict[str, Any]) -> tuple[TurnRunner, dict[str, list]]:
    runner = _build_runner()
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
    _patch_resolve_prompt_config(
        runner,
        "FINAL",
        None,
        case["request_context_in"],
    )
    _patch_session_id(runner, "sess-1")
    _patch_budget_resolvers(runner)
    _patch_thinking(runner)
    _patch_memory_helpers(runner)
    _patch_observability(runner)

    call_log: dict[str, list] = {"preflight": [], "history": []}
    _patch_preflight(
        runner,
        raises=case["preflight_raises"],
        calls=call_log["preflight"],
    )
    _patch_load_history(
        runner,
        return_value=case["history_return"],
        raises=case["history_raises"],
        calls=call_log["history"],
    )
    _patch_post_slice_probe(runner)

    hook = case["hook"]
    if hook is not None:
        # Re-instantiate the stage with hooks; the production stage
        # exposes ``compaction_hooks`` via constructor only.
        from opensquilla.engine.turn_runner.compaction_and_history_stage import (
            CompactionAndHistoryStage,
        )
        from opensquilla.engine.turn_runner.harness import (
            _RequestContextPrependAdapter,
            _TurnRunnerHistoryLoaderAdapter,
            _TurnRunnerPreflightCompactionAdapter,
        )

        runner._compaction_and_history_stage = CompactionAndHistoryStage(
            preflight=_TurnRunnerPreflightCompactionAdapter(runner),
            history_loader=_TurnRunnerHistoryLoaderAdapter(runner),
            request_context_prepender=_RequestContextPrependAdapter(),
            compaction_hooks=(hook,),
        )

    return runner, call_log


async def _drive(runner: TurnRunner, case: dict[str, Any], *, tool_context: Any = None):
    captured = None
    raised = None
    yielded: list[Any] = []
    gen = runner._run_turn(
        message="hi",
        session_key=case["session_key"],
        agent_id="agent:main",
        model=None,
        attachments=[],
        tool_context=tool_context,
        input_mode="user",
        persist_input=False,
        input_provenance=None,
        history_has_persisted_user=case["history_has_persisted_user"],
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


_CORPUS_IDS = [c[0] for c in _CORPUS]


@pytest.mark.parametrize("case_id,case", _CORPUS, ids=_CORPUS_IDS)
@pytest.mark.asyncio
async def test_compaction_and_history_stage_snapshot(
    case_id: str,
    case: dict[str, Any],
) -> None:
    """Drive each case through the stage; snapshot must match expected."""
    runner, call_log = _setup_runner(case)
    captured, yielded, raised = await _drive(runner, case)

    if case["history_raises"] is not None:
        # HistoryLoader exception propagates through _run_turn's terminal
        # handler -> ErrorEvent yielded; probe never fires.
        assert captured is None
        assert raised is None
        assert len(yielded) == 1
        assert isinstance(yielded[0], ErrorEvent)
        assert yielded[0].code == "agent_error"
        assert len(call_log["preflight"]) == 1
        # And history must have been attempted once before raising.
        assert len(call_log["history"]) == 1
        return

    assert raised is None, f"{case_id} raised: {raised}"
    assert captured is not None, f"{case_id} captured nothing"

    expected_snapshot = {
        "outcome": "success",
        "agent_request_context_prompt_after": case[
            "expected_final_request_context"
        ],
    }
    assert captured == expected_snapshot, (
        f"case={case_id}: snapshot diverged.\n"
        f"  expected={expected_snapshot}\n  actual  ={captured}"
    )

    # Every ordinary turn reaches the single canonical preflight entry.
    assert len(call_log["preflight"]) == 1, f"{case_id}: preflight calls"
    # History always loaded once.
    assert len(call_log["history"]) == 1
    assert (
        call_log["history"][0]["trim_last_user"]
        is case["history_has_persisted_user"]
    )


@pytest.mark.asyncio
async def test_unknown_vision_capability_does_not_skip_compaction() -> None:
    case = dict(_CASE_BASE)
    runner, call_log = _setup_runner(case)
    image_message = Message(
        role="user",
        content=[ContentBlockImage(media_type="image/png", data="c3ludGhldGlj")],
    )

    async def _attachment_run(_inp: Any) -> StageOutcome[AttachmentStageOutput]:
        return StageOutcome.success(
            AttachmentStageOutput(
                extra_messages=[image_message],
                turn_input="",
                stats=AttachmentMaterializationStats(image_count=1),
            )
        )

    runner._attachment_stage = SimpleNamespace(run=_attachment_run)

    captured, _yielded, raised = await _drive(runner, case)

    assert raised is None
    assert captured is not None
    assert len(call_log["preflight"]) == 1
    assert len(call_log["history"]) == 1


@pytest.mark.asyncio
async def test_runtime_compaction_keeps_file_paths_with_image_retention_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.session.compaction import (
        CompactionConfig,
        CompactionRequest,
        compact_context,
        compaction_replay_summary,
    )
    from opensquilla.tools.types import ToolContext
    from tests.helpers.image_bytes import image_bytes

    case = dict(_CASE_BASE)
    runner, _ = _setup_runner(case)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = GatewayConfig()
    config.workspace_dir = str(workspace)
    config.attachments.media_root = str(tmp_path / "media")
    config.attachments.persist_transcripts = False
    runner._config = config
    document_bytes = b"%PDF-1.4\nORIGINAL_DOCUMENT_BODY\n%%EOF"
    document = {
        "mime": "application/pdf", "name": "document.pdf",
        "data": base64.b64encode(document_bytes).decode(),
    }
    image = {
        "mime": "image/png", "name": "image.png",
        "data": base64.b64encode(image_bytes()).decode(),
    }
    seen: dict[str, Any] = {}

    async def summarize(**kwargs: Any) -> str:
        seen["summary_input"] = kwargs["chunk_text"]
        return "Continue reviewing the original document."

    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", summarize)

    async def preflight(
        self: TurnRunner, *args: Any, attachment_path_resolver=None, **kwargs: Any,
    ) -> None:
        assert callable(attachment_path_resolver)
        seen["image_path"] = attachment_path_resolver(image, "s1")
        result = await compact_context(CompactionRequest(
            session_id="s1", context_window_tokens=4_000,
            entries=[
                {
                    "id": 1, "role": "user", "message_id": "file-message", "token_count": 5,
                    "content": json.dumps({"text": "Review.", "attachments": [document, image]}),
                },
                {"id": 2, "role": "assistant", "content": "Received.", "token_count": 5},
                {"id": 3, "role": "user", "content": "Continue.", "token_count": 5},
                {"id": 4, "role": "assistant", "content": "Continuing.", "token_count": 5},
            ],
            config=CompactionConfig(
                model="synthetic-model", api_key="synthetic-key", safety_margin=1.0,
                protected_recent_messages=2, attachment_path_resolver=attachment_path_resolver,
            ), forced_prefix_cut=2, trigger="message_count",
        ))
        seen["result"] = result

    runner._maybe_preflight_compact = MethodType(preflight, runner)
    captured, yielded, raised = await _drive(
        runner, case, tool_context=ToolContext(workspace_dir=str(workspace)),
    )

    assert raised is None
    assert captured is not None, yielded
    assert seen["image_path"] is None
    result = seen["result"]
    assert result.removed_count == 2
    assert result.summary_payload is not None
    paths = [item["path"] for item in result.summary_payload["files_and_artifacts"]]
    assert len(paths) == 1
    path = paths[0]
    assert path.startswith(".opensquilla/attachments/s1/")
    assert (workspace / path).read_bytes() == document_bytes
    assert path in seen["summary_input"]
    assert path in compaction_replay_summary(result)
    assert "ORIGINAL_DOCUMENT_BODY" not in seen["summary_input"]
    assert image["data"] not in seen["summary_input"]
    assert not list(workspace.rglob("*.png"))


@pytest.mark.asyncio
async def test_compaction_hook_fan_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When hooks are registered, the stage fires one before/after pair."""
    case = dict(_CASE_BASE)
    case["hook"] = _RecordingCompactionHook()
    runner, _ = _setup_runner(case)
    captured, _, raised = await _drive(runner, case)
    assert raised is None
    assert captured is not None

    hook = case["hook"]
    assert hook.events == [("before", "preflight"), ("after", "preflight")]


@pytest.mark.asyncio
async def test_raising_hook_does_not_break_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hook that raises in before_compact MUST NOT break the turn."""
    case = dict(_CASE_BASE)
    case["hook"] = _RecordingCompactionHook(before_raises=RuntimeError)
    runner, call_log = _setup_runner(case)
    captured, _, raised = await _drive(runner, case)

    # Turn still completes; preflight still fired.
    assert raised is None
    assert captured is not None
    assert len(call_log["preflight"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("supports_resolver", [False, True])
async def test_attachment_path_adapter_preserves_legacy_runner_signatures(
    supports_resolver: bool,
) -> None:
    from opensquilla.engine.turn_runner.harness import (
        _TurnRunnerPreflightCompactionAdapter,
    )

    seen: dict[str, Any] = {}

    def resolver(attachment: dict[str, Any], session_key: str) -> str | None:
        return "assets/sample.png"

    async def modern(*args: Any, attachment_path_resolver=None, **kwargs: Any) -> None:
        seen["resolver"] = attachment_path_resolver
        return None

    async def legacy(*args: Any, compaction_provider=None, compaction_model=None) -> None:
        seen["legacy_called"] = True
        return None

    runner = SimpleNamespace(
        _maybe_preflight_compact=modern if supports_resolver else legacy,
    )
    adapter = _TurnRunnerPreflightCompactionAdapter(runner)
    kwargs: dict[str, Any] = {
        "session_key": "agent:main:synthetic-compaction",
        "context_window_tokens": 64_000,
        "compaction_provider": None,
        "compaction_model": None,
        "attachment_path_resolver": resolver,
    }
    await adapter.maybe_compact(**kwargs)

    assert seen == ({"resolver": resolver} if supports_resolver else {"legacy_called": True})
