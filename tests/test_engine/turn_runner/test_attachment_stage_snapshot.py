"""Snapshot harness for ``AttachmentStage`` through ``TurnRunner._run_turn``.

Drives an 8-case corpus against ``TurnRunner._run_turn`` with the
``AttachmentStage`` running through the runtime stage path. The harness reuses
the patch helpers to stub all upstream stages
and patches the ``Agent`` constructor so the stub agent's ``run_turn``
raises a sentinel ``BaseException`` carrying the post-slice locals
snapshot.

The corpus includes a count-cap ``ValueError`` case (real production
builder fires) and a raising-fake ``AttachmentMessageBuilderPort`` case
so the pure-pass-through-of-port-exceptions contract is exercised.
"""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import ErrorEvent

# Reuse upstream patch helpers from's equivalence harness — this
# stage sits AFTER all five prior stages so the same upstream patching
# strategy applies. Local duplication would just inflate LOC.
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


def _snapshot_for_extra_messages(extra_msgs: Any) -> dict[str, Any]:
    """Cheap content-block fingerprint of the assembled envelope."""

    if extra_msgs is None:
        return {"is_none": True, "count": 0}
    parts: list[dict[str, Any]] = []
    for msg in extra_msgs:
        content = getattr(msg, "content", []) or []
        parts.append({
            "role": getattr(msg, "role", None),
            "n_blocks": len(content),
            "kinds": tuple(type(block).__name__ for block in content),
        })
    return {"is_none": False, "count": len(extra_msgs), "messages": parts}


# ---------------------------------------------------------------------------
# Stub Agent — captures the post-slice locals when run_turn is invoked
# ---------------------------------------------------------------------------


class _StubAgent:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Match the production Agent's surface for the fields _run_turn
        # reads after construction.
        self.config = kwargs.get("config") or SimpleNamespace(
            request_context_prompt=None
        )

    def set_history(self, _history: Any) -> None:
        # Called by HistoryLoader when history is reconstructed.
        return None

    async def run_turn(
        self,
        turn_input: str,
        *,
        extra_messages: Any = None,
        **_kwargs: Any,
    ):
        # Capture the slice's two output fields and halt.
        snapshot = {
            "outcome": "success",
            "turn_input": turn_input,
            "extra_messages_fingerprint": _snapshot_for_extra_messages(
                extra_messages
            ),
        }
        raise _SliceCapture(snapshot)
        # Unreachable; satisfies the async-generator type expectation.
        yield  # pragma: no cover


# Local budget / thinking / compaction patches — match's harness shape.


def _patch_budget_resolvers(runner: TurnRunner) -> None:
    def _runtime(self, session_key):  # noqa: ARG001, ARG002
        return 60.0

    def _max_iter(self, session_key, mi):  # noqa: ARG001, ARG002
        return mi if mi is not None else 10

    def _iter_t(self, session_key, it):  # noqa: ARG001, ARG002
        return it if it is not None else 30.0

    def _tool_t(self, session_key, tt):  # noqa: ARG001, ARG002
        return tt if tt is not None else 20.0

    def _req_t(self, session_key, rt):  # noqa: ARG001, ARG002
        return rt if rt is not None else 120.0

    def _retries(self, session_key, r):  # noqa: ARG001, ARG002
        return r if r is not None else 3

    runner._resolve_agent_runtime_timeout = _runtime.__get__(runner, TurnRunner)
    runner._resolve_agent_max_iterations = _max_iter.__get__(runner, TurnRunner)
    runner._resolve_agent_iteration_timeout = _iter_t.__get__(runner, TurnRunner)
    runner._resolve_agent_tool_timeout = _tool_t.__get__(runner, TurnRunner)
    runner._resolve_agent_request_timeout = _req_t.__get__(runner, TurnRunner)
    runner._resolve_agent_max_provider_retries = _retries.__get__(runner, TurnRunner)


def _patch_thinking(runner: TurnRunner) -> None:
    runner._resolve_turn_thinking = (
        lambda self, turn: False  # noqa: ARG005
    ).__get__(runner, TurnRunner)


def _patch_compaction_history(runner: TurnRunner) -> None:
    async def _t3(self, *_a, **_kw):  # noqa: ARG002
        return "not_applicable"

    async def _preflight(self, *_a, **_kw):  # noqa: ARG002
        return None

    async def _load_history(self, *_a, **_kw):  # noqa: ARG002
        return None

    runner._maybe_compact_on_t3_upgrade = _t3.__get__(runner, TurnRunner)
    runner._maybe_preflight_compact = _preflight.__get__(runner, TurnRunner)
    runner._load_history = _load_history.__get__(runner, TurnRunner)


# ---------------------------------------------------------------------------
# Corpus — 8 cases per the design.
# ---------------------------------------------------------------------------


def _img_attachment() -> dict[str, str]:
    # Tiny PNG-ish blob — _build_attachment_messages does not parse the
    # bytes for images, only base64-decodes and packs them.
    return {"type": "image/png", "data": base64.b64encode(b"PNGDATA").decode("ascii")}


def _text_attachment(name: str, body: str) -> dict[str, str]:
    return {
        "type": "text/plain",
        "name": name,
        "data": base64.b64encode(body.encode("utf-8")).decode("ascii"),
    }




def _pdf_attachment() -> dict[str, str]:
    # A minimal PDF-shaped blob. Ordinary document bytes are represented by
    # metadata and are not parsed before the provider request.
    return {
        "type": "application/pdf",
        "name": "tiny.pdf",
        "data": base64.b64encode(b"%PDF-1.4\n%trailer\n").decode("ascii"),
    }


# ``_run_turn`` types ``attachments`` as ``list[dict]`` (no ``None``); callers
# normalize ``None`` to ``[]`` at the call site. The AttachmentStage-internal
# ``None`` -> ``[]`` coercion is exercised by the unit suite. The shared
# pipeline stub from sets ``turn.message="EFFECTIVE"`` so the empty-
# attachments cases see ``turn_input="EFFECTIVE"`` regardless of the caller's
# ``message`` field.


def _case(
    case_id: str,
    *,
    message: str,
    attachments: list[dict],
    expected_extra_is_none: bool,
    expected_kinds: tuple[str, ...],
    expected_turn_input: str = "",
    raises_value_error: bool = False,
    raises_runtime_error_via_fake: bool = False,
) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {
        "message": message,
        "attachments": attachments,
        "expected_extra_is_none": expected_extra_is_none,
        "expected_extra_count": 0 if expected_extra_is_none else 1,
        "expected_turn_input": expected_turn_input,
        "expected_kinds": expected_kinds,
        "raises_value_error": raises_value_error,
    }
    if raises_runtime_error_via_fake:
        payload["raises_runtime_error_via_fake"] = True
    return case_id, payload


_CORPUS: list[tuple[str, dict[str, Any]]] = [
    _case(
        "no_attachments_empty_list",
        message="hello",
        attachments=[],
        expected_extra_is_none=True,
        expected_kinds=(),
        expected_turn_input="EFFECTIVE",
    ),
    _case(
        "single_inline_image",
        message="what is this?",
        attachments=[_img_attachment()],
        expected_extra_is_none=False,
        expected_kinds=("ContentBlockText", "ContentBlockImage"),
    ),
    _case(
        "two_text_family_attachments",
        message="review these",
        attachments=[
            _text_attachment("a.txt", "alpha"),
            _text_attachment("b.txt", "beta"),
        ],
        expected_extra_is_none=False,
        expected_kinds=("ContentBlockText", "ContentBlockText", "ContentBlockText"),
    ),
    _case(
        "mixed_image_and_text",
        message="compare",
        attachments=[_img_attachment(), _text_attachment("notes.txt", "context")],
        expected_extra_is_none=False,
        expected_kinds=("ContentBlockText", "ContentBlockImage", "ContentBlockText"),
    ),
    # PDF remains a single metadata block; ordinary file bytes are not parsed
    # or injected into the provider envelope.
    _case(
        "pdf_attachment_text_extraction",
        message="summarize",
        attachments=[_pdf_attachment()],
        expected_extra_is_none=False,
        expected_kinds=("ContentBlockText", "ContentBlockText"),
    ),
    _case(
        "empty_message_with_attachment",
        message="",
        attachments=[_img_attachment()],
        expected_extra_is_none=False,
        expected_kinds=("ContentBlockText", "ContentBlockImage"),
    ),
    # Production cap is _MAX_ATTACHMENT_COUNT = 10; supply 11.
    _case(
        "count_cap_exceeded_value_error",
        message="hi",
        attachments=[_img_attachment() for _ in range(11)],
        expected_extra_is_none=False,
        expected_kinds=(),
        raises_value_error=True,
    ),
    # In legacy mode the production builder fires and produces a
    # ContentBlockText + ContentBlockImage envelope; the raising fake only
    # swaps in for mode=new. The test branches on mode for this case.
    _case(
        "raising_builder_port_propagates",
        message="hi",
        attachments=[_img_attachment()],
        expected_extra_is_none=False,
        expected_kinds=("ContentBlockText", "ContentBlockImage"),
        raises_runtime_error_via_fake=True,
    ),
]


_CORPUS_IDS = [c[0] for c in _CORPUS]


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
        session_flush_service=None,
        session_lock_provider=None,
        diagnostics_state=None,
        turn_hooks=None,
    )


def _setup_runner(monkeypatch: pytest.MonkeyPatch) -> TurnRunner:
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
    _patch_resolve_prompt_config(runner, "FINAL", None, None)
    _patch_session_id(runner, "sess-1")
    _patch_budget_resolvers(runner)
    _patch_thinking(runner)
    _patch_memory_helpers(runner)
    _patch_observability(runner)
    _patch_compaction_history(runner)

    # Replace the Agent class with the stub so the post-slice probe fires when
    # ``agent.run_turn`` is invoked. Patch both runtime and adapter import sites
    # so construction uses the stub consistently.
    monkeypatch.setattr("opensquilla.engine.runtime.Agent", _StubAgent)
    monkeypatch.setattr("opensquilla.engine.agent.Agent", _StubAgent)
    return runner


async def _drive(
    runner: TurnRunner,
    case: dict[str, Any],
):
    captured: dict[str, Any] | None = None
    raised: type[BaseException] | None = None
    yielded: list[Any] = []
    gen = runner._run_turn(
        message=case["message"],
        session_key="agent:main:s1",
        agent_id="agent:main",
        model=None,
        attachments=case["attachments"],
        tool_context=None,
        input_mode="user",
        persist_input=False,
        input_provenance=None,
        history_has_persisted_user=True,
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


@pytest.mark.parametrize("case_id,case", _CORPUS, ids=_CORPUS_IDS)
@pytest.mark.asyncio
async def test_attachment_stage_snapshot(
    case_id: str,
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive each corpus case through the unconditional AttachmentStage path."""
    runner = _setup_runner(monkeypatch)

    # The raising-fake case substitutes the AttachmentStage instance on
    # the runner with one whose builder raises RuntimeError.
    if case.get("raises_runtime_error_via_fake"):
        from opensquilla.engine.turn_runner.attachment_stage import (
            AttachmentStage,
        )

        class _RaisingBuilder:
            def build(  # noqa: ARG002
                self,
                message: str,
                attachments: list[dict],
                *,
                workspace_dir: str | Path | None = None,
                session_id: str | None = None,
            ) -> list[Any] | None:
                raise RuntimeError("raising builder fake")

        runner._attachment_stage = AttachmentStage(builder=_RaisingBuilder())

    captured, yielded, raised = await _drive(runner, case)

    # Both the production ``ValueError`` (count cap) and the raising-fake
    # ``RuntimeError`` propagate through ``_run_turn``'s terminal handler
    # to an ``ErrorEvent``. The probe never fires in either case.
    expect_terminal = case["raises_value_error"] or case.get("raises_runtime_error_via_fake")
    if expect_terminal:
        assert captured is None, f"{case_id}: probe captured unexpectedly"
        assert raised is None
        assert len(yielded) == 1
        assert isinstance(yielded[0], ErrorEvent)
        assert yielded[0].code == "agent_error"
        return

    assert raised is None, f"{case_id} raised: {raised}"
    assert captured is not None, f"{case_id} captured nothing"

    expected_extra_fp = {
        "is_none": case["expected_extra_is_none"],
        "count": case["expected_extra_count"],
    }
    if not case["expected_extra_is_none"]:
        expected_extra_fp["messages"] = [
            {
                "role": "user",
                "n_blocks": len(case["expected_kinds"]),
                "kinds": case["expected_kinds"],
            }
        ]

    assert captured["outcome"] == "success", (
        f"case={case_id}: outcome diverged ({captured!r})"
    )
    assert captured["turn_input"] == case["expected_turn_input"], (
        f"case={case_id}: turn_input diverged "
        f"({captured['turn_input']!r} vs {case['expected_turn_input']!r})"
    )
    assert captured["extra_messages_fingerprint"] == expected_extra_fp, (
        f"case={case_id}: extra_messages fingerprint diverged.\n"
        f"  expected={expected_extra_fp}\n"
        f"  actual  ={captured['extra_messages_fingerprint']}"
    )
