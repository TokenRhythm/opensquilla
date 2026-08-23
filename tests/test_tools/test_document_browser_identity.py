from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from opensquilla.engine.types import ToolCall
from opensquilla.tool_boundary import ToolResult
from opensquilla.tools import dispatch as dispatch_module
from opensquilla.tools.builtin import document_browser
from opensquilla.tools.types import SafeToolError, current_tool_context


class _Bridge:
    def __init__(self, *, artifact_id: str, scope_id: str, candidate_handle: str) -> None:
        self._artifact_id = artifact_id
        self._scope_id = scope_id
        self._candidate_handle = candidate_handle

    async def capabilities(self) -> SimpleNamespace:
        return SimpleNamespace(
            version=5,
            browser_inspect=True,
            reload_surface=True,
            screenshot=True,
        )

    async def browser_inspect(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            scope="document",
            nodes=(),
            truncated=False,
            active_preview_artifact_id=self._artifact_id,
            scope_id=self._scope_id,
            candidate_handle=self._candidate_handle,
            binding_generation=1,
        )

    async def reload_surface(self, **_kwargs: object) -> bool:
        raise AssertionError("reload must not run before candidate identity is verified")

    async def screenshot(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(mime="image/png", data=b"synthetic-png", width=320, height=200)


class _ActionBridge(_Bridge):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.inspect_calls: list[dict[str, object]] = []
        self.clicked: list[str] = []

    async def browser_inspect(self, **kwargs: object) -> SimpleNamespace:
        self.inspect_calls.append(dict(kwargs))
        return await super().browser_inspect(**kwargs)

    async def capabilities(self) -> SimpleNamespace:
        value = await super().capabilities()
        value.browser_act = True
        return value

    async def browser_click(
        self,
        *,
        anchor: str,
        focus_only: bool = False,
        candidate_handle: str | None = None,
    ) -> SimpleNamespace:
        assert focus_only is False
        assert candidate_handle == self._candidate_handle
        self.clicked.append(anchor)
        return SimpleNamespace(performed=True, changed=True)


class _WireCapabilityBridge(_Bridge):
    async def capabilities(self) -> dict[str, object]:
        return {
            "version": 5,
            "available": True,
            "browserInspect": True,
            "browserAct": True,
            "screenshot": True,
            "reloadSurface": True,
        }


class _Controller:
    candidate_sha256 = "a" * 64
    candidate_epoch = 7
    candidate_artifact = SimpleNamespace(artifact_id="art-candidate")
    preview_handle = "candidate_0123456789abcdef"

    def __init__(self) -> None:
        self.verifications: list[tuple[str, str]] = []
        self.invalidations: list[str] = []

    async def record_verification(self, *, candidate_sha256: str, verification_token: str) -> None:
        self.verifications.append((candidate_sha256, verification_token))

    async def invalidate_verification(self, *, reason: str) -> None:
        self.invalidations.append(reason)


def _context(bridge: _Bridge, controller: _Controller) -> SimpleNamespace:
    return SimpleNamespace(
        is_owner=True,
        caller_kind="web",
        interaction_mode="interactive",
        subagent_depth=0,
        guest_safe=False,
        surfaced_tools={
            "document_browser_inspect",
            "document_browser_act",
            "document_browser_reload",
            "document_browser_screenshot",
        },
        exclusive_tools=None,
        desktop_artifact_bridge=bridge,
        artifact_candidate_loop_controller=controller,
        _artifact_candidate_preview_bound=True,
        tool_result_media={},
    )


def _scope() -> SimpleNamespace:
    return SimpleNamespace(
        ctx=None,
        context=SimpleNamespace(session_key="agent:main:webchat:preview"),
        document=SimpleNamespace(generation=3),
        revision=SimpleNamespace(revision_id="rev-3"),
    )


async def _invoke_inspect(context: SimpleNamespace) -> str:
    token = current_tool_context.set(cast(Any, context))
    try:
        # Bypass the generic operation guard; this test targets the identity
        # fence inside the raw tool handler.
        return cast(
            str,
            await inspect.unwrap(document_browser.document_browser_inspect)(
                scope="document",
                maxNodes=1,
            ),
        )
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_candidate_inspect_requires_matching_active_surface_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()

    async def html_scope(_tool_name: str, **_kwargs: object):
        return scope, SimpleNamespace(sha256="a" * 64), b"<html></html>", object()

    monkeypatch.setattr(document_browser, "_html_adapter_scope", html_scope)
    controller = _Controller()
    context = _context(
        _Bridge(
            artifact_id="art-candidate",
            scope_id="agent:main:webchat:preview",
            candidate_handle=controller.preview_handle,
        ),
        controller,
    )

    result = json.loads(await _invoke_inspect(context))

    assert result["status"] == "verification_passed"
    assert controller.verifications
    assert "bindingGeneration" not in result


@pytest.mark.asyncio
async def test_candidate_inspect_accepts_wire_camel_case_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()

    async def html_scope(_tool_name: str, **_kwargs: object):
        return scope, SimpleNamespace(sha256="a" * 64), b"<html></html>", object()

    monkeypatch.setattr(document_browser, "_html_adapter_scope", html_scope)
    controller = _Controller()
    context = _context(
        _WireCapabilityBridge(
            artifact_id="art-candidate",
            scope_id="agent:main:webchat:preview",
            candidate_handle=controller.preview_handle,
        ),
        controller,
    )

    result = json.loads(await _invoke_inspect(context))

    assert result["status"] == "verification_passed"


@pytest.mark.asyncio
async def test_candidate_inspect_rejects_switched_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()

    async def html_scope(_tool_name: str, **_kwargs: object):
        return scope, SimpleNamespace(sha256="a" * 64), b"<html></html>", object()

    monkeypatch.setattr(document_browser, "_html_adapter_scope", html_scope)
    controller = _Controller()
    context = _context(
        _Bridge(
            artifact_id="art-other",
            scope_id="agent:main:webchat:preview",
            candidate_handle=controller.preview_handle,
        ),
        controller,
    )

    with pytest.raises(SafeToolError, match="DOCUMENT_BROWSER_PREVIEW_STALE"):
        await _invoke_inspect(context)
    assert controller.invalidations == ["browser_identity_stale"]


@pytest.mark.asyncio
async def test_candidate_reload_rejects_switched_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()

    async def html_scope(_tool_name: str, **_kwargs: object):
        return scope, SimpleNamespace(sha256="a" * 64), b"<html></html>", object()

    monkeypatch.setattr(document_browser, "_html_adapter_scope", html_scope)
    controller = _Controller()
    context = _context(
        _Bridge(
            artifact_id="art-other",
            scope_id="agent:main:webchat:preview",
            candidate_handle=controller.preview_handle,
        ),
        controller,
    )
    token = current_tool_context.set(cast(Any, context))
    try:
        with pytest.raises(SafeToolError, match="DOCUMENT_BROWSER_PREVIEW_STALE"):
            await inspect.unwrap(document_browser.document_browser_reload)()
    finally:
        current_tool_context.reset(token)
    assert controller.invalidations == ["reload_identity_stale"]


@pytest.mark.asyncio
async def test_browser_action_identity_probe_preserves_non_first_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-action identity check must not replace the model's anchor set."""

    scope = _scope()

    async def html_scope(_tool_name: str, **_kwargs: object):
        return scope, SimpleNamespace(sha256="a" * 64), b"<html></html>", object()

    monkeypatch.setattr(document_browser, "_html_adapter_scope", html_scope)
    controller = _Controller()
    bridge = _ActionBridge(
        artifact_id="art-candidate",
        scope_id="agent:main:webchat:preview",
        candidate_handle=controller.preview_handle,
    )
    context = _context(bridge, controller)
    token = current_tool_context.set(cast(Any, context))
    try:
        raw = await inspect.unwrap(document_browser.document_browser_act)(
            action="click",
            anchor="a10",
        )
    finally:
        current_tool_context.reset(token)

    assert json.loads(raw)["status"] == "action_applied"
    assert bridge.inspect_calls == [
        {
            "scope": "document",
            "max_nodes": 1,
            "identity_only": True,
            "candidate_handle": controller.preview_handle,
        }
    ]
    assert bridge.clicked == ["a10"]


@pytest.mark.asyncio
async def test_screenshot_stages_ephemeral_media_without_inlining_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()

    async def html_scope(_tool_name: str, **_kwargs: object):
        return scope, SimpleNamespace(sha256="a" * 64), b"<html></html>", object()

    monkeypatch.setattr(document_browser, "_html_adapter_scope", html_scope)
    controller = _Controller()
    context = _context(
        _Bridge(
            artifact_id="art-candidate",
            scope_id="agent:main:webchat:preview",
            candidate_handle=controller.preview_handle,
        ),
        controller,
    )
    token = current_tool_context.set(cast(Any, context))
    try:
        raw = await inspect.unwrap(document_browser.document_browser_screenshot)(
            _tool_use_id="screenshot-1",
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(raw)
    assert payload["imageAttached"] is True
    assert "dataBase64" not in payload
    assert context.tool_result_media["screenshot-1"][0]["data"]


@pytest.mark.asyncio
async def test_source_patched_replay_uses_exact_audit_sequence_once() -> None:
    emitted: list[dict[str, object]] = []

    class _Service:
        async def audit_event_for_mutation(self, document_id: str, **kwargs: object):
            assert document_id == "doc-1"
            assert kwargs == {"revision_id": "rev-2", "change_set_id": "cs-2"}
            return SimpleNamespace(sequence=11)

    async def emit(payload: dict[str, object]) -> None:
        emitted.append(payload)

    context = SimpleNamespace(
        artifact_session=_Service(),
        artifact_event_emitter=emit,
    )
    assert await document_browser._replay_source_patched_event(
        context,
        document_id="doc-1",
        revision_id="rev-2",
        change_set_id="cs-2",
    )
    assert await document_browser._replay_source_patched_event(
        context,
        document_id="doc-1",
        revision_id="rev-2",
        change_set_id="cs-2",
    )
    assert emitted == [
        {
            "artifactEventSeq": 11,
            "documentId": "doc-1",
            "revisionId": "rev-2",
            "changeSetId": "cs-2",
            "action": "source.patched",
        }
    ]


@pytest.mark.asyncio
async def test_browser_failure_returns_to_candidate_loop() -> None:
    call = ToolCall(
        tool_use_id="browser-failure",
        tool_name="document_browser_inspect",
        arguments={"scope": "document"},
    )
    result = ToolResult(
        tool_use_id=call.tool_use_id,
        tool_name=call.tool_name,
        content=json.dumps(
            {
                "status": "error",
                "error_class": "SafeToolError",
                "retry_allowed": False,
            }
        ),
        is_error=True,
    )

    projected = await dispatch_module._candidate_loop_effect_result(result, tool_call=call)

    assert projected.effect_outcome is not None
    assert projected.effect_outcome.loop_action == "continue"
    assert projected.effect_outcome.retry_policy == "same_turn"
    assert projected.effect_outcome.outcome_code == "document_browser_verification_failed"
    assert (
        projected.effect_outcome.safe_details["documentMutationOutcome"]["status"]
        == "verification_failed"
    )


@pytest.mark.asyncio
async def test_non_retryable_browser_control_result_closes_candidate_loop() -> None:
    """Budget/control envelopes are non-errors but must still trip the fuse."""

    call = ToolCall(
        tool_use_id="browser-control",
        tool_name="document_browser_inspect",
        arguments={"scope": "document"},
    )
    result = ToolResult(
        tool_use_id=call.tool_use_id,
        tool_name=call.tool_name,
        content=json.dumps(
            {
                "status": "control",
                "reason": "tool_run_budget_exhausted",
                "retry_allowed": False,
            }
        ),
        is_error=False,
    )

    projected = await dispatch_module._candidate_loop_effect_result(result, tool_call=call)

    assert projected.effect_outcome is not None
    assert projected.effect_outcome.loop_action == "finalize_without_tools"
    assert projected.effect_outcome.retry_policy == "new_turn"
    assert projected.effect_outcome.outcome_code == "document_tool_budget_exhausted"


@pytest.mark.asyncio
async def test_unavailable_candidate_preview_terminates_without_more_tools() -> None:
    call = ToolCall(
        tool_use_id="writer-preview-unavailable",
        tool_name="document_apply",
        arguments={"mutations": []},
    )
    result = ToolResult(
        tool_use_id=call.tool_use_id,
        tool_name=call.tool_name,
        content=json.dumps(
            {
                "status": "candidate_staged",
                "preview": "unavailable",
                "nextAction": "document_finish_discard",
            }
        ),
        is_error=False,
    )

    projected = await dispatch_module._candidate_loop_effect_result(result, tool_call=call)

    assert projected.effect_outcome is not None
    assert projected.effect_outcome.loop_action == "finalize_without_tools"
    assert projected.effect_outcome.retry_policy == "new_turn"
    assert projected.effect_outcome.outcome_code == "document_preview_unavailable"
    payload = json.loads(projected.content)
    assert payload["retry_allowed"] is False
    assert payload["category"] == "DOCUMENT_PREVIEW_UNAVAILABLE"
    assert projected.terminates_turn is True


@pytest.mark.asyncio
async def test_discard_without_candidate_is_idempotent_noop() -> None:
    class _EmptyController:
        state = SimpleNamespace(status="open")
        change_set = None
        candidate_artifact = None

        async def discard_without_candidate(self) -> None:
            self.state = SimpleNamespace(status="discarded")

    controller = _EmptyController()
    context = SimpleNamespace(
        artifact_candidate_loop_controller=controller,
        desktop_artifact_bridge=None,
    )
    token = current_tool_context.set(cast(Any, context))
    try:
        first = json.loads(
            await inspect.unwrap(document_browser.document_finish)(
                decision="discard",
                summary="nothing to change",
            )
        )
        second = json.loads(
            await inspect.unwrap(document_browser.document_finish)(
                decision="discard",
                summary="nothing to change",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert first["status"] == "discarded"
    assert first["noOp"] is True
    assert first["candidateCleanup"] == "none"
    assert second["status"] == "discarded"
    assert second["replayed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_mode", ["false", "raises"])
async def test_discard_restore_failure_keeps_preview_mapping_for_outer_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    restore_mode: str,
) -> None:
    """A failed native restore must remain retryable after draft rejection."""

    class _DiscardController:
        preview_handle = "candidate_0123456789abcdef"
        candidate_artifact = None
        state = SimpleNamespace(status="candidate_staged", candidate_sha256="a" * 64)

        def __init__(self) -> None:
            self.discarded = False

        async def discard(self, **_kwargs: object) -> None:
            self.discarded = True
            self.state = SimpleNamespace(status="discarded", candidate_sha256=None)

    class _RestoreBridge:
        async def restore_canonical_preview(self, _candidate_handle: str) -> bool:
            if restore_mode == "raises":
                raise RuntimeError("simulated bridge outage")
            return False

    retired: list[str] = []

    class _PreviewService:
        def retire_candidate_preview(self, handle: str) -> None:
            retired.append(handle)

    async def missing_scope(_tool_name: str, **_kwargs: object) -> object:
        raise SafeToolError("scope unavailable")

    monkeypatch.setattr(document_browser, "_html_adapter_scope", missing_scope)
    controller = _DiscardController()
    context = SimpleNamespace(
        agent_id="agent-main",
        artifact_session_id="session-1",
        artifact_candidate_loop_controller=controller,
        desktop_artifact_bridge=_RestoreBridge(),
        artifact_preview_service=_PreviewService(),
        _artifact_candidate_preview_bound=True,
    )
    token = current_tool_context.set(cast(Any, context))
    try:
        raw = await inspect.unwrap(document_browser.document_finish)(
            decision="discard",
            summary="stop editing",
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(raw)
    assert payload["status"] == "discarded"
    assert payload["preview"] == "not_confirmed"
    assert controller.discarded is True
    assert context._artifact_candidate_preview_cleanup_pending is True
    assert context._artifact_candidate_preview_bound is True
    assert retired == []
