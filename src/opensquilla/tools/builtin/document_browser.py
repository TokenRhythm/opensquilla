"""Bound-preview browser tools for PromptAnnotation agent loops.

These tools deliberately expose only the already-authenticated Electron
artifact bridge.  They do not accept a URL, a file path, JavaScript, or raw
CDP payload.  The bridge is optional: a document turn may still use the
source-level tools when no desktop preview is attached, but browser failures
are returned as bounded, model-actionable errors.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from typing import Any, Literal

from opensquilla.artifact_session import Actor, ActorKind
from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.tools.builtin.artifact_editing import _actor, _emit_artifact_state
from opensquilla.tools.builtin.document_editing import _html_adapter_scope
from opensquilla.tools.registry import tool
from opensquilla.tools.types import PlanAccess, SafeToolError, current_tool_context

_SHA256_RE = r"^[0-9a-fA-F]{64}$"
_TOKEN_RE = r"^vfy_[A-Za-z0-9_-]{16,128}$"
_V4_BROWSER_CAPABILITIES = frozenset(
    {"browser_inspect", "browser_act", "screenshot", "reload_surface"}
)


class DocumentBridgeToolError(SafeToolError):
    """Sanitized bridge failure with loop-control metadata for dispatch."""

    def __init__(
        self,
        user_message: str,
        *,
        category: str,
        retry_policy: str,
        next_action: str,
        terminal_binding_loss: bool = False,
    ) -> None:
        super().__init__(user_message)
        self.category = category
        self.retry_policy = retry_policy
        self.next_action = next_action
        self.terminal_binding_loss = terminal_binding_loss


def _terminal_preview_error(message: str) -> DocumentBridgeToolError:
    return DocumentBridgeToolError(
        message,
        category="DOCUMENT_PREVIEW_UNAVAILABLE",
        retry_policy="new_turn",
        next_action="finalize_without_tools",
        terminal_binding_loss=True,
    )


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ctx() -> Any:
    context = current_tool_context.get()
    if context is None:
        raise _terminal_preview_error(
            "DOCUMENT_BROWSER_UNAVAILABLE: No bound document preview is available."
        )
    surfaced = getattr(context, "surfaced_tools", None)
    exclusive = getattr(context, "exclusive_tools", None)
    if exclusive is not None and not any(
        name in exclusive
        for name in (
            "document_browser_inspect",
            "document_browser_act",
            "document_browser_screenshot",
            "document_browser_reload",
        )
    ):
        raise _terminal_preview_error(
            "DOCUMENT_BROWSER_UNAVAILABLE: Browser tools are not authorized for this turn."
        )
    if surfaced is not None and not any(
        name in surfaced
        for name in (
            "document_browser_inspect",
            "document_browser_act",
            "document_browser_screenshot",
            "document_browser_reload",
        )
    ):
        raise _terminal_preview_error(
            "DOCUMENT_BROWSER_UNAVAILABLE: Browser tools are not authorized for this turn."
        )
    bridge = getattr(context, "desktop_artifact_bridge", None)
    if bridge is None:
        raise _terminal_preview_error(
            "DOCUMENT_BROWSER_UNAVAILABLE: The bound desktop preview is unavailable."
        )
    return context


async def _capability(context: Any, name: str) -> None:
    """Fail closed when a real bridge advertises a disabled operation.

    Small test doubles and older embedded callers may not implement
    ``capabilities``; those are allowed to call the fixed method and receive
    the bridge's own bounded response.
    """

    capabilities = getattr(context.desktop_artifact_bridge, "capabilities", None)
    if capabilities is None:
        return
    try:
        value = await capabilities()
    except Exception as exc:  # noqa: BLE001 - never expose transport details
        raise _terminal_preview_error(
            "DOCUMENT_BROWSER_UNAVAILABLE: The bound desktop preview is unavailable."
        ) from exc
    # Browser inspection/action is intentionally a protocol-v4 capability.
    # A v3 shell may expose a stale/partial boolean surface while lacking the
    # candidate-preview binding and active-surface fencing required by this
    # loop.  Treat a missing or malformed version as unavailable rather than
    # widening the authority based on an optimistic flag.
    if name in _V4_BROWSER_CAPABILITIES:
        raw_version = getattr(value, "version", None)
        if raw_version is None and isinstance(value, Mapping):
            raw_version = value.get("version")
        if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 4:
            raise _terminal_preview_error(
                "DOCUMENT_BROWSER_UNAVAILABLE: Protocol-v4 bound preview is required."
            )
    # The in-process bridge client exposes snake_case dataclass fields, while
    # a few embedded/test transports return the original wire-format
    # camelCase record.  Treat both spellings identically and fail closed if
    # neither is explicitly true.  Without this normalization a valid v4
    # client could negotiate the loop successfully in ``rpc_sessions`` but
    # have every subsequent browser tool reject its capability check.
    aliases = {
        "browser_inspect": ("browser_inspect", "browserInspect"),
        "browser_act": ("browser_act", "browserAct"),
        "screenshot": ("screenshot",),
        "reload_surface": ("reload_surface", "reloadSurface"),
    }
    names = aliases.get(name, (name,))
    advertised = (
        any(value.get(alias) is True for alias in names)
        if isinstance(value, Mapping)
        else any(getattr(value, alias, False) is True for alias in names)
    )
    if not bool(advertised):
        raise _terminal_preview_error(
            f"DOCUMENT_BROWSER_UNAVAILABLE: The preview does not support {name}."
        )


def _bridge_error(exc: BaseException, operation: str) -> SafeToolError:
    # Bridge errors intentionally do not include endpoint, token, URL, or raw
    # exception text.  A stable code is sufficient for the model to choose a
    # source-only repair or finish(discard).
    code = str(getattr(exc, "code", "failed") or "failed")
    safe_code = "".join(ch for ch in code.upper() if ch.isalnum() or ch == "_")[:48]
    normalized_code = code.strip().lower().replace("_", "-")
    terminal_binding_loss = normalized_code in {
        "binding-terminal-unavailable",
        "binding-unavailable",
        "transport-unavailable",
        "unavailable",
    }
    action_result_unknown = normalized_code == "action-result-unknown"
    category = (
        "DOCUMENT_PREVIEW_UNAVAILABLE"
        if terminal_binding_loss
        else "DOCUMENT_ACTION_RESULT_UNKNOWN"
        if action_result_unknown
        else f"DOCUMENT_BROWSER_{operation.upper()}_{safe_code or 'FAILED'}"
    )
    return DocumentBridgeToolError(
        f"{category}: The bound preview operation did not complete.",
        category=category,
        retry_policy="new_turn" if terminal_binding_loss else "same_turn",
        next_action=(
            "finalize_without_tools"
            if terminal_binding_loss
            else "reinspect"
            if action_result_unknown
            else "retry"
        ),
        terminal_binding_loss=terminal_binding_loss,
    )


async def _bound_sha256(context: Any) -> tuple[str | None, int | None, str | None]:
    """Read only the current bound head metadata, never a workspace file."""

    try:
        _scope, ref, _payload, _adapter = await _html_adapter_scope(
            "document_browser_inspect"
        )
    except Exception:  # noqa: BLE001 - browser inspection remains useful without metadata
        return None, None, None
    document = getattr(_scope, "document", None)
    return (
        str(getattr(ref, "sha256", "")) or None,
        getattr(document, "generation", None),
        getattr(_scope, "revision", None) and getattr(_scope.revision, "revision_id", None),
    )


async def _bound_scope_id(context: Any) -> str | None:
    """Return the authenticated session scope for active-surface fencing."""

    try:
        scope, _ref, _payload, _adapter = await _html_adapter_scope(
            "document_browser_inspect"
        )
    except Exception:  # noqa: BLE001 - identity is required only for candidate receipts
        return None
    value = getattr(getattr(scope, "context", None), "session_key", None)
    return value if isinstance(value, str) and value else None


async def _assert_candidate_preview_identity(context: Any, *, operation: str) -> str | None:
    """Fence a browser side effect to the current candidate surface."""

    controller = getattr(context, "artifact_candidate_loop_controller", None)
    candidate_sha256 = getattr(controller, "candidate_sha256", None)
    if not isinstance(candidate_sha256, str) or not candidate_sha256:
        return None
    expected_candidate_handle = getattr(controller, "preview_handle", None)
    if not isinstance(expected_candidate_handle, str):
        raise _terminal_preview_error(
            "DOCUMENT_BROWSER_PREVIEW_UNAVAILABLE: The candidate preview handle is unavailable."
        )
    await _capability(context, "browser_inspect")
    try:
        snapshot = await context.desktop_artifact_bridge.browser_inspect(
            scope="document",
            max_nodes=1,
            identity_only=True,
            candidate_handle=expected_candidate_handle,
        )
    except Exception as exc:  # noqa: BLE001 - normalize bridge failures
        await _invalidate_candidate_verification(context, reason=f"{operation}_identity_failed")
        raise _bridge_error(exc, operation) from None
    candidate_blob = getattr(controller, "candidate_artifact", None)
    expected_artifact_id = getattr(candidate_blob, "artifact_id", None)
    expected_scope_id = await _bound_scope_id(context)
    if (
        not bool(getattr(context, "_artifact_candidate_preview_bound", False))
        or not isinstance(expected_artifact_id, str)
        or not isinstance(expected_candidate_handle, str)
        or not isinstance(expected_scope_id, str)
        or snapshot.scope != "document"
        or snapshot.active_preview_artifact_id != expected_artifact_id
        or snapshot.candidate_handle != expected_candidate_handle
        or snapshot.scope_id != expected_scope_id
    ):
        await _invalidate_candidate_verification(context, reason=f"{operation}_identity_stale")
        raise SafeToolError(
            "DOCUMENT_BROWSER_PREVIEW_STALE: The browser action is not bound to "
            "the current candidate preview. Inspect the current candidate again."
        )
    # Return the exact handle observed by the identity probe.  The following
    # bridge operation carries it in-band so the native handler can reject a
    # candidate replacement that happens between the probe and the action.
    return expected_candidate_handle


def _new_verification_token(*, sha256: str | None, payload: object) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
    # The random suffix prevents a token from becoming a stable oracle while
    # the digest lets diagnostics correlate a receipt to this observation.
    return f"vfy_{digest}_{secrets.token_urlsafe(12)}"


def _context_actor(context: Any) -> Actor:
    actor_id = str(getattr(context, "agent_id", "") or "").strip()
    if not actor_id:
        raise SafeToolError("DOCUMENT_FINISH_UNAVAILABLE: No authenticated agent identity.")
    return Actor(ActorKind.AGENT, actor_id)


async def _replay_source_patched_event(
    context: Any,
    *,
    document_id: str,
    revision_id: str,
    change_set_id: str,
) -> bool:
    """Replay the post-commit notification using its exact audit sequence.

    ``document_finish`` can be retried after the SQLite commit succeeded but
    the first response/event was lost.  The revision and ChangeSet are then
    already terminal, so this helper only emits metadata; it never mutates
    durable state.  It deliberately refuses to use an unrelated latest audit
    row, because the WebUI treats ``artifactEventSeq`` as a per-document
    monotonic fence.
    """

    if bool(getattr(context, "_artifact_source_patched_emitted", False)):
        return True
    emitter = getattr(context, "artifact_event_emitter", None)
    service = getattr(context, "artifact_session", None)
    if not callable(emitter) or service is None:
        return False
    try:
        exact_lookup = getattr(service, "audit_event_for_mutation", None)
        if callable(exact_lookup):
            audit = await exact_lookup(
                document_id,
                revision_id=revision_id,
                change_set_id=change_set_id,
            )
        else:
            audit = None
            list_events = getattr(service, "list_audit_events", None)
            if callable(list_events):
                for event in await list_events(document_id):
                    if event.revision_id != revision_id or event.change_set_id != change_set_id:
                        continue
                    if audit is None or event.sequence > audit.sequence:
                        audit = event
        if audit is None:
            return False
        await emitter(
            {
                "artifactEventSeq": audit.sequence,
                "documentId": document_id,
                "revisionId": revision_id,
                "changeSetId": change_set_id,
                "action": "source.patched",
            }
        )
    except Exception:  # noqa: BLE001 - notification recovery is best effort
        return False
    setattr(context, "_artifact_source_patched_emitted", True)
    return True


async def _invalidate_candidate_verification(context: Any, *, reason: str) -> None:
    """Invalidate both the process-local receipt and controller state."""

    setattr(context, "_artifact_browser_verification_token", None)
    setattr(context, "_artifact_browser_verification_sha256", None)
    setattr(context, "_artifact_browser_binding_generation", None)
    controller = getattr(context, "artifact_candidate_loop_controller", None)
    invalidate = getattr(controller, "invalidate_verification", None)
    if callable(invalidate):
        try:
            await invalidate(reason=reason)
        except Exception:  # noqa: BLE001 - closed/stale candidates already fail closed
            pass


def _retire_candidate_preview(context: Any, controller: Any) -> None:
    """Drop the Gateway's opaque candidate mapping after loop termination."""

    preview_service = getattr(context, "artifact_preview_service", None)
    retire = getattr(preview_service, "retire_candidate_preview", None)
    handle = getattr(controller, "preview_handle", None)
    if not callable(retire) or not isinstance(handle, str):
        return
    try:
        retire(handle)
    except Exception:  # noqa: BLE001 - terminal state remains durable
        # The mapping is in-memory and bounded by its lease sweeper.  A failed
        # best-effort retirement must never turn a successful discard/commit
        # into a model-visible ambiguous mutation.
        pass


_INSPECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "string",
            "enum": ["document", "selection", "viewport"],
            "description": "Inspect the bound preview document, selected area, or viewport.",
        },
        "maxNodes": {
            "type": "integer",
            "minimum": 1,
            "maximum": 200,
            "description": "Maximum number of bounded DOM/accessibility nodes to return.",
        },
    },
    "required": ["scope"],
    "additionalProperties": False,
}


@tool(
    name="document_browser_inspect",
    description=(
        "Inspect the currently bound Electron HTML preview and return a bounded DOM/accessibility "
        "snapshot plus a verificationToken. The token is valid only until the next edit, browser "
        "action, reload, or surface change. This tool never accepts a URL, path, JavaScript, or "
        "CDP."
    ),
    params=_INSPECT_SCHEMA,
    owner_only=True,
    exposed_by_default=False,
    plan_access=PlanAccess.READ_ONLY,
    result_budget_class="artifact",
    sandbox=SandboxToolDescriptor.artifact(kind="document.browser.inspect"),
)
async def document_browser_inspect(
    scope: Literal["document", "selection", "viewport"] = "document",
    maxNodes: int = 100,  # noqa: N803 - public schema uses camelCase
) -> str:
    context = _ctx()
    if scope not in {"document", "selection", "viewport"}:
        raise SafeToolError(
            "DOCUMENT_BROWSER_SCOPE_INVALID: Choose document, selection, or viewport."
        )
    if isinstance(maxNodes, bool) or not isinstance(maxNodes, int) or not 1 <= maxNodes <= 200:
        raise SafeToolError(
            "DOCUMENT_BROWSER_MAX_NODES_INVALID: maxNodes must be between 1 and 200."
        )
    await _capability(context, "browser_inspect")
    candidate_controller = getattr(context, "artifact_candidate_loop_controller", None)
    candidate_sha256 = getattr(candidate_controller, "candidate_sha256", None)
    candidate_is_staged = isinstance(candidate_sha256, str) and bool(candidate_sha256)
    candidate_handle = (
        getattr(candidate_controller, "preview_handle", None)
        if candidate_is_staged
        else None
    )
    if candidate_is_staged and not isinstance(candidate_handle, str):
        raise _terminal_preview_error(
            "DOCUMENT_BROWSER_PREVIEW_UNAVAILABLE: The candidate preview handle is unavailable."
        )
    try:
        snapshot = await context.desktop_artifact_bridge.browser_inspect(
            scope=scope,
            max_nodes=maxNodes,
            candidate_handle=candidate_handle,
        )
    except Exception as exc:  # noqa: BLE001 - normalize bridge failures
        await _invalidate_candidate_verification(context, reason="browser_inspect_failed")
        raise _bridge_error(exc, "inspect") from None
    sha256, generation, revision_id = await _bound_sha256(context)
    candidate_epoch = (
        getattr(candidate_controller, "candidate_epoch", None)
        if candidate_is_staged
        else None
    )
    if candidate_is_staged:
        if not bool(getattr(context, "_artifact_candidate_preview_bound", False)):
            raise _terminal_preview_error(
                "DOCUMENT_BROWSER_PREVIEW_UNAVAILABLE: The current Electron client "
                "cannot bind this candidate preview."
            )
        candidate_blob = getattr(candidate_controller, "candidate_artifact", None)
        expected_artifact_id = getattr(candidate_blob, "artifact_id", None)
        expected_candidate_handle = getattr(candidate_controller, "preview_handle", None)
        expected_scope_id = await _bound_scope_id(context)
        if (
            not isinstance(expected_artifact_id, str)
            or not isinstance(expected_candidate_handle, str)
            or not isinstance(expected_scope_id, str)
            or not expected_scope_id
            or snapshot.scope != scope
            or snapshot.active_preview_artifact_id != expected_artifact_id
            or snapshot.candidate_handle != expected_candidate_handle
            or snapshot.scope_id != expected_scope_id
        ):
            await _invalidate_candidate_verification(context, reason="browser_identity_stale")
            raise SafeToolError(
                "DOCUMENT_BROWSER_PREVIEW_STALE: The active preview is not bound to "
                "the current candidate; inspect the current candidate again."
            )
        sha256 = candidate_sha256
    binding_generation = getattr(snapshot, "binding_generation", None)
    if candidate_is_staged and (
        isinstance(binding_generation, bool)
        or not isinstance(binding_generation, int)
        or binding_generation < 1
    ):
        await _invalidate_candidate_verification(
            context,
            reason="browser_binding_generation_unavailable",
        )
        raise SafeToolError(
            "DOCUMENT_BROWSER_VERIFICATION_STALE: The preview binding changed; inspect again."
        )
    nodes = [
        {
            "anchor": node.anchor,
            "role": node.role,
            "name": node.name,
            "text": node.text,
            "interactive": node.interactive,
            "disabled": node.disabled,
            "selected": node.selected,
        }
        for node in snapshot.nodes
    ]
    receipt_payload = {
        "scope": snapshot.scope,
        "nodes": nodes,
        "sha256": sha256,
        "generation": generation,
        "revisionId": revision_id,
        "candidateEpoch": candidate_epoch,
        "bindingGeneration": binding_generation,
    }
    token = _new_verification_token(sha256=sha256, payload=receipt_payload)
    # ToolContext is intentionally process-local and not serialized.  Keeping
    # the receipt here gives document_finish a strict same-turn check without
    # adding a public token store or a database migration.
    setattr(context, "_artifact_browser_verification_token", token)
    setattr(context, "_artifact_browser_verification_sha256", sha256)
    setattr(context, "_artifact_browser_binding_generation", binding_generation)
    if candidate_controller is not None and candidate_is_staged and sha256 is not None:
        try:
            await candidate_controller.record_verification(
                candidate_sha256=sha256,
                verification_token=token,
            )
        except Exception as exc:  # noqa: BLE001 - stale candidate is actionable
            raise SafeToolError(
                "DOCUMENT_BROWSER_VERIFICATION_STALE: The candidate changed; inspect again."
            ) from exc
    return _json(
        {
            "status": "verification_passed",
            "scope": snapshot.scope,
            "nodes": nodes,
            "truncated": snapshot.truncated,
            "verificationToken": token,
            "candidateSha256": sha256,
            "candidateEpoch": candidate_epoch,
            "generation": generation,
            "revisionId": revision_id,
        }
    )


async def _final_browser_health_check(context: Any, expected_sha256: str) -> str:
    """Refresh the runtime receipt immediately before a durable commit.

    A receipt from an earlier tool call must not survive a surface switch,
    reload, or renderer exception that happened after that call.  Reusing the
    normal bounded inspect path keeps the active-surface, protocol-v4, CDP,
    resource-health, candidate-epoch, and SHA checks in one place.  The fresh
    token is process-local and is never accepted from a client other than the
    model's immediately preceding inspect result.
    """

    expected_binding_generation = getattr(
        context,
        "_artifact_browser_binding_generation",
        None,
    )
    try:
        raw = await document_browser_inspect(scope="document", maxNodes=1)
        payload = json.loads(raw)
    except SafeToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize a final health failure
        raise SafeToolError(
            "DOCUMENT_FINISH_VERIFICATION_FAILED: Reinspect the current preview before commit."
        ) from exc
    if not isinstance(payload, dict):
        raise SafeToolError(
            "DOCUMENT_FINISH_VERIFICATION_FAILED: The preview health receipt is invalid."
        )
    candidate_sha = payload.get("candidateSha256")
    token = payload.get("verificationToken")
    fresh_binding_generation = getattr(
        context,
        "_artifact_browser_binding_generation",
        None,
    )
    if (
        payload.get("status") != "verification_passed"
        or not isinstance(candidate_sha, str)
        or candidate_sha.lower() != expected_sha256.lower()
        or not isinstance(token, str)
        or not re.fullmatch(_TOKEN_RE, token)
        or expected_binding_generation != fresh_binding_generation
    ):
        await _invalidate_candidate_verification(
            context,
            reason="finish_binding_generation_stale",
        )
        raise SafeToolError(
            "DOCUMENT_FINISH_VERIFICATION_STALE: The candidate changed; inspect again."
        )
    return token


_ACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["click", "focus", "type", "press", "scroll"],
        },
        "anchor": {"type": "string", "minLength": 1, "maxLength": 128},
        "text": {"type": "string", "maxLength": 16_384},
        "replace": {"type": "boolean"},
        "key": {
            "type": "string",
            "enum": [
                "Enter",
                "Tab",
                "Escape",
                "Backspace",
                "Delete",
                "Space",
                "ArrowUp",
                "ArrowDown",
                "ArrowLeft",
                "ArrowRight",
                "Home",
                "End",
                "PageUp",
                "PageDown",
            ],
        },
        "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
        "amount": {"type": "string", "enum": ["line", "page"]},
    },
    "required": ["action"],
    "additionalProperties": False,
}


@tool(
    name="document_browser_act",
    description=(
        "Perform one bounded action in the currently bound HTML preview: click/focus a returned "
        "anchor, type text, press a safe key, or scroll. The action invalidates the previous "
        "verificationToken; inspect again before committing. No navigation, JavaScript, URL, or "
        "raw CDP."
    ),
    params=_ACT_SCHEMA,
    owner_only=True,
    exposed_by_default=False,
    plan_access=PlanAccess.READ_ONLY,
    result_budget_class="artifact",
    sandbox=SandboxToolDescriptor.artifact(kind="document.browser.act"),
)
async def document_browser_act(
    action: Literal["click", "focus", "type", "press", "scroll"],
    anchor: str | None = None,
    text: str | None = None,
    replace: bool = False,
    key: str | None = None,
    direction: Literal["up", "down", "left", "right"] | None = None,
    amount: Literal["line", "page"] | None = None,
) -> str:
    context = _ctx()
    await _capability(context, "browser_act")
    candidate_handle = await _assert_candidate_preview_identity(context, operation="act")
    # Even a failed/unchanged action may have reached the renderer before the
    # bridge reported an error.  Require a fresh inspect receipt afterwards.
    await _invalidate_candidate_verification(context, reason="browser_action")
    try:
        if action == "click":
            if not anchor:
                raise SafeToolError("DOCUMENT_BROWSER_ANCHOR_REQUIRED: click requires an anchor.")
            result = await context.desktop_artifact_bridge.browser_click(
                anchor=anchor,
                candidate_handle=candidate_handle,
            )
        elif action == "focus":
            if not anchor:
                raise SafeToolError("DOCUMENT_BROWSER_ANCHOR_REQUIRED: focus requires an anchor.")
            result = await context.desktop_artifact_bridge.browser_click(
                anchor=anchor,
                focus_only=True,
                candidate_handle=candidate_handle,
            )
        elif action == "type":
            if not anchor or not isinstance(text, str):
                raise SafeToolError(
                    "DOCUMENT_BROWSER_INPUT_REQUIRED: type requires anchor and text."
                )
            result = await context.desktop_artifact_bridge.browser_type(
                anchor=anchor,
                text=text,
                replace=replace,
                candidate_handle=candidate_handle,
            )
        elif action == "press":
            if not isinstance(key, str):
                raise SafeToolError("DOCUMENT_BROWSER_KEY_REQUIRED: press requires a safe key.")
            result = await context.desktop_artifact_bridge.browser_press(
                key=key,
                candidate_handle=candidate_handle,
            )
        elif action == "scroll":
            if direction is None or amount is None:
                raise SafeToolError(
                    "DOCUMENT_BROWSER_SCROLL_REQUIRED: scroll requires direction and amount."
                )
            result = await context.desktop_artifact_bridge.browser_scroll(
                direction=direction,
                amount=amount,
                candidate_handle=candidate_handle,
            )
        else:
            raise SafeToolError("DOCUMENT_BROWSER_ACTION_INVALID: Unsupported preview action.")
    except SafeToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize bridge failures
        raise _bridge_error(exc, "act") from None
    await _invalidate_candidate_verification(context, reason="browser_action")
    return _json(
        {
            "status": "action_applied",
            "action": action,
            "performed": bool(getattr(result, "performed", False)),
            "changed": bool(getattr(result, "changed", False)),
            "nextAction": "document_browser_inspect",
        }
    )


@tool(
    name="document_browser_screenshot",
    description=(
        "Capture a bounded PNG screenshot of the currently bound HTML preview. Vision-capable "
        "models receive it as an ephemeral image evidence block; text-only models receive "
        "dimensions/status and should use DOM or console inspection. It never changes the "
        "Document."
    ),
    params={"type": "object", "properties": {}, "additionalProperties": False},
    owner_only=True,
    exposed_by_default=False,
    plan_access=PlanAccess.READ_ONLY,
    result_budget_class="artifact",
    runtime_only_arguments={"_tool_use_id"},
    sandbox=SandboxToolDescriptor.artifact(kind="document.browser.screenshot"),
)
async def document_browser_screenshot(_tool_use_id: str = "") -> str:
    context = _ctx()
    await _capability(context, "screenshot")
    # A screenshot has no public identity fields in the bridge payload.  When
    # a candidate is staged, perform a bounded identity-only DOM probe first
    # so diagnostic pixels cannot silently come from a canonical or switched
    # surface.  The probe also keeps the latest candidate receipt alive for a
    # subsequent explicit finish decision.
    candidate_handle = await _assert_candidate_preview_identity(context, operation="screenshot")
    try:
        shot = await context.desktop_artifact_bridge.screenshot(
            candidate_handle=candidate_handle,
        )
    except Exception as exc:  # noqa: BLE001 - normalize bridge failures
        raise _bridge_error(exc, "screenshot") from None
    # Keep the provider-facing result small.  The bounded PNG is carried as an
    # ephemeral, authenticated image attachment and consumed by Agent before
    # the next provider request; it is never written to the workspace or
    # persisted in the transcript.  Text-only models still receive the
    # dimensions/status below and can use DOM/console evidence instead.
    media = getattr(context, "tool_result_media", None)
    if isinstance(media, dict) and _tool_use_id:
        media[_tool_use_id] = [
            {
                "mime": shot.mime,
                "data": base64.b64encode(shot.data).decode("ascii"),
                "width": shot.width,
                "height": shot.height,
            }
        ]
    return _json(
        {
            "status": "ok",
            "mime": shot.mime,
            "width": shot.width,
            "height": shot.height,
            "imageAttached": bool(media is not None and _tool_use_id),
            "nextAction": "document_browser_inspect",
        }
    )


@tool(
    name="document_browser_reload",
    description=(
        "Reload the currently bound HTML preview surface. Reload invalidates all browser anchors "
        "and verification receipts; inspect again before calling document_finish."
    ),
    params={"type": "object", "properties": {}, "additionalProperties": False},
    owner_only=True,
    exposed_by_default=False,
    plan_access=PlanAccess.READ_ONLY,
    result_budget_class="artifact",
    sandbox=SandboxToolDescriptor.artifact(kind="document.browser.reload"),
)
async def document_browser_reload() -> str:
    context = _ctx()
    await _capability(context, "reload_surface")
    # Reload is a browser-side effect too.  When a candidate is staged, fence
    # it to the same v4 offline preview before invalidating the receipt; a
    # switched canonical surface must never be reloaded by the agent.
    candidate_handle = await _assert_candidate_preview_identity(context, operation="reload")
    await _invalidate_candidate_verification(context, reason="browser_reload")
    try:
        reloaded = await context.desktop_artifact_bridge.reload_surface(
            candidate_handle=candidate_handle,
        )
    except Exception as exc:  # noqa: BLE001 - normalize bridge failures
        raise _bridge_error(exc, "reload") from None
    await _invalidate_candidate_verification(context, reason="browser_reload")
    return _json(
        {
            "status": "reloaded" if reloaded else "reload_not_confirmed",
            "reloaded": bool(reloaded),
            "nextAction": "document_browser_inspect",
        }
    )


_FINISH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["commit", "discard"]},
        "expectedCandidateSha256": {
            "type": "string",
            "pattern": _SHA256_RE,
            "description": "The candidateSha256 from the latest verification result.",
        },
        "verificationToken": {
            "type": "string",
            "pattern": _TOKEN_RE,
            "description": "The verificationToken from the latest document_browser_inspect.",
        },
        "summary": {"type": "string", "maxLength": 2_000},
    },
    "required": ["decision"],
    "additionalProperties": False,
}


@tool(
    name="document_finish",
    description=(
        "End the bound-document agent loop. Use commit only after the latest preview inspection "
        "returned a verificationToken and the candidate still matches; use discard when the task "
        "cannot be completed safely. This is the only loop-control tool and never accepts paths."
    ),
    params=_FINISH_SCHEMA,
    owner_only=True,
    exposed_by_default=False,
    plan_access=PlanAccess.READ_ONLY,
    result_budget_class="artifact",
    sandbox=SandboxToolDescriptor.artifact(kind="document.finish"),
    runtime_only_arguments={"_tool_use_id"},
)
async def document_finish(
    decision: Literal["commit", "discard"],
    expectedCandidateSha256: str | None = None,  # noqa: N803
    verificationToken: str | None = None,  # noqa: N803
    summary: str | None = None,
    _tool_use_id: str | None = None,
) -> str:
    context = current_tool_context.get()
    if context is None:
        raise SafeToolError("DOCUMENT_FINISH_UNAVAILABLE: No bound document turn is active.")
    if decision not in {"commit", "discard"}:
        raise SafeToolError("DOCUMENT_FINISH_DECISION_INVALID: Choose commit or discard.")
    controller = getattr(context, "artifact_candidate_loop_controller", None)
    if controller is None:
        raise SafeToolError(
            "DOCUMENT_FINISH_UNAVAILABLE: No candidate controller is bound to this turn."
        )
    # A retry may arrive on a freshly reconstructed controller after the
    # original process committed/rejected the DRAFT but lost its response.
    # Reconcile the turn-local durable ChangeSet before requiring a live
    # preview receipt; terminal replay must remain possible without Electron.
    reconcile_checked = not callable(getattr(controller, "reconcile", None))
    try:
        if str(getattr(getattr(controller, "state", None), "status", "")) not in {
            "committed",
            "discarded",
        }:
            reconcile = getattr(controller, "reconcile", None)
            if callable(reconcile):
                await reconcile()
        reconcile_checked = True
    except Exception:
        # A normal open candidate still follows the strict preview/token path;
        # only a successful durable read changes the terminal replay branch.
        pass
    # ``discard`` is intentionally total for an untouched loop.  There is no
    # DRAFT to reject and no candidate preview to restore in this state, so
    # close the turn as an idempotent no-op instead of manufacturing durable
    # state or requiring an Electron bridge that was never bound.
    terminal_status = str(getattr(getattr(controller, "state", None), "status", ""))
    if decision == "discard" and terminal_status == "open" and reconcile_checked:
        change_set = getattr(controller, "change_set", None)
        candidate_blob = getattr(controller, "candidate_artifact", None)
        preview_bound = bool(
            getattr(context, "_artifact_candidate_preview_bound", False)
            or getattr(context, "_artifact_candidate_preview_cleanup_pending", False)
        )
        if change_set is None and candidate_blob is None and not preview_bound:
            mark_empty_discard = getattr(controller, "discard_without_candidate", None)
            if callable(mark_empty_discard):
                try:
                    await mark_empty_discard()
                except Exception as exc:  # noqa: BLE001 - sanitize state conflict
                    raise SafeToolError(
                        "DOCUMENT_FINISH_DISCARD_FAILED: The empty loop could not be closed."
                    ) from exc
            return _json(
                {
                    "status": "discarded",
                    "noOp": True,
                    "candidateCleanup": "none",
                    "preview": "not_available",
                    "terminal_response": (
                        summary or "There were no document changes to discard."
                    )[:2_000],
                }
            )
    # A tool response can be replayed after the durable transaction succeeded
    # (for example when the client lost the first response).  Return the
    # existing terminal receipt instead of requiring a fresh preview token or
    # attempting a second revision/event.  The opposite decision remains a
    # terminal conflict.
    if terminal_status == "committed":
        if decision != "commit":
            raise SafeToolError(
                "DOCUMENT_FINISH_TERMINAL_CONFLICT: The candidate is already committed."
            )
        recovered = getattr(controller, "reconcile", None)
        if not callable(recovered):
            raise SafeToolError(
                "DOCUMENT_FINISH_RECONCILE_REQUIRED: The committed result cannot be replayed."
            )
        try:
            recovered_result = await recovered()
        except Exception as exc:  # noqa: BLE001 - sanitize durable read failure
            raise SafeToolError(
                "DOCUMENT_FINISH_RECONCILE_REQUIRED: The committed result is unavailable."
            ) from exc
        if not recovered_result:
            raise SafeToolError(
                "DOCUMENT_FINISH_RECONCILE_REQUIRED: The committed result is unavailable."
            )
        applied, change_set = recovered_result
        # The first finish may have crashed after the atomic commit and before
        # its transient ``source.patched`` notification.  Replay that event
        # from the exact durable audit row; failure to notify must not turn an
        # already-applied revision into an apparent tool failure.
        await _replay_source_patched_event(
            context,
            document_id=applied.document.document_id,
            revision_id=applied.revision.revision_id,
            change_set_id=change_set.change_set_id,
        )
        return _json(
            {
                "status": "applied",
                "replayed": True,
                "candidateSha256": applied.revision.artifact_sha256,
                "revision": {"generation": applied.revision.generation},
                "changeSet": {"state": change_set.status.value},
                "terminal_response": (
                    summary or "The document preview was already committed."
                )[:2_000],
            }
        )
    if terminal_status == "discarded":
        if decision != "discard":
            raise SafeToolError(
                "DOCUMENT_FINISH_TERMINAL_CONFLICT: The candidate was already discarded."
            )
        return _json(
            {
                "status": "discarded",
                "replayed": True,
                "terminal_response": (
                    summary or "The document changes were already discarded."
                )[:2_000],
            }
        )
    if decision == "discard":
        scope = None
        session_id: str | None = None
        try:
            # Prefer the live bound scope so the normal owner/head checks still
            # run.  If the candidate blob has already been cleaned up (or the
            # canonical preview was torn down), discard must remain able to
            # reject the draft instead of leaving a DRAFT row behind.
            scope, _ref, _payload, _adapter = await _html_adapter_scope("document_finish")
            actor = _actor(scope)
            session_id = scope.context.session_id
        except Exception:
            actor = _context_actor(context)
            session_id = getattr(context, "artifact_session_id", None)
            if not isinstance(session_id, str) or not session_id:
                artifact_context = getattr(context, "artifact_context", None)
                session_id = getattr(artifact_context, "session_id", None)
        candidate_blob = getattr(controller, "candidate_artifact", None)
        had_candidate_preview = bool(
            candidate_blob is not None
            or getattr(context, "_artifact_candidate_preview_bound", False)
            or getattr(context, "_artifact_candidate_preview_cleanup_pending", False)
        )
        if not isinstance(session_id, str) or not session_id:
            raise SafeToolError(
                "DOCUMENT_FINISH_DISCARD_FAILED: The candidate session is unavailable."
            )
        try:
            await controller.discard(actor=actor, reason=summary)
        except SafeToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize persistence failures
            raise SafeToolError(
                "DOCUMENT_FINISH_DISCARD_FAILED: The candidate could not be discarded safely."
            ) from exc
        cleanup = "not_available"
        if candidate_blob is not None:
            try:
                from opensquilla.artifacts import ArtifactStore

                media_root = getattr(context, "artifact_media_root", None)
                if not isinstance(media_root, str) or not media_root:
                    raise ValueError("artifact media root is unavailable")
                store = ArtifactStore(media_root)
                deleted = await asyncio.to_thread(
                    store.delete_ref,
                    session_id=session_id,
                    artifact_id=candidate_blob.artifact_id,
                )
                cleanup = "deleted" if deleted else "orphan_gc"
            except Exception:  # noqa: BLE001 - orphan GC remains safe
                cleanup = "orphan_gc"
        restore_status = "not_available"
        bridge = getattr(context, "desktop_artifact_bridge", None)
        # An empty DRAFT has never replaced the active preview.  Avoid a
        # restore call in that case so an explicit discard cannot detach a
        # canonical surface owned by another turn.
        restore = (
            getattr(bridge, "restore_canonical_preview", None)
            if had_candidate_preview
            else None
        )
        preview_handle = getattr(controller, "preview_handle", None)
        if callable(restore):
            try:
                if not isinstance(preview_handle, str):
                    raise SafeToolError(
                        "DOCUMENT_FINISH_PREVIEW_HANDLE_MISSING: "
                        "The candidate preview handle is unavailable."
                    )
                restore_status = (
                    "restored" if await restore(preview_handle) else "not_confirmed"
                )
            except Exception:  # noqa: BLE001 - durable discard already succeeded
                restore_status = "not_confirmed"
        restore_pending = callable(restore) and restore_status != "restored"
        # Keep the binding marked active while canonical restoration is
        # pending.  This mirrors the commit path and lets outer turn cleanup
        # retry the bridge operation with the same opaque handle.  A false
        # restore result is not proof that the candidate surface disappeared.
        setattr(context, "_artifact_candidate_preview_bound", restore_pending)
        setattr(context, "_artifact_candidate_preview_cleanup_pending", restore_pending)
        setattr(context, "_artifact_browser_verification_token", None)
        setattr(context, "_artifact_browser_verification_sha256", None)
        setattr(context, "_artifact_browser_binding_generation", None)
        # Native restore deliberately retains its opaque handle when the
        # Gateway release fails, so a later cleanup pass can retry.  Retiring
        # the Gateway mapping here would make that retry impossible (the
        # native surface would hold a handle that now resolves to NOT_FOUND).
        if not restore_pending:
            _retire_candidate_preview(context, controller)
        return _json(
            {
                "status": "discarded",
                "candidateCleanup": cleanup,
                "preview": restore_status,
                "terminal_response": (summary or "The document changes were discarded.")[:2_000],
            }
        )
    if not isinstance(expectedCandidateSha256, str) or not re.fullmatch(
        _SHA256_RE, expectedCandidateSha256
    ):
        raise SafeToolError(
            "DOCUMENT_FINISH_SHA_REQUIRED: Commit requires the latest candidate SHA."
        )
    expected_candidate_sha256 = expectedCandidateSha256.lower()
    if not isinstance(verificationToken, str) or not re.fullmatch(
        _TOKEN_RE, verificationToken
    ):
        raise SafeToolError(
            "DOCUMENT_FINISH_VERIFICATION_REQUIRED: Commit requires a fresh verification token."
        )
    if not bool(getattr(context, "_artifact_candidate_preview_bound", False)):
        raise _terminal_preview_error(
            "DOCUMENT_FINISH_PREVIEW_UNAVAILABLE: The candidate preview is not bound "
            "to the active Electron surface."
        )
    expected_token = getattr(context, "_artifact_browser_verification_token", None)
    expected_sha = getattr(context, "_artifact_browser_verification_sha256", None)
    if expected_token != verificationToken or expected_sha != expected_candidate_sha256:
        raise SafeToolError(
            "DOCUMENT_FINISH_VERIFICATION_STALE: Inspect the current preview again before commit."
        )

    # The model-provided receipt is necessary but not sufficient: the renderer
    # may have failed, reloaded, or switched active surfaces since its last
    # inspect.  A final bounded inspect obtains a fresh token and performs the
    # native bridge's active-surface/runtime-health checks immediately before
    # crossing the durable revision boundary.
    fresh_verification_token = await _final_browser_health_check(
        context,
        expected_candidate_sha256,
    )

    try:
        scope, _ref, _payload, _adapter = await _html_adapter_scope("document_finish")
        applied, change_set = await controller.commit(
            actor=_actor(scope),
            expected_candidate_sha256=expected_candidate_sha256,
            verification_token=fresh_verification_token,
            tool_use_id=_tool_use_id,
        )
        preview_status = "not_available"
        bridge = getattr(context, "desktop_artifact_bridge", None)
        restore = getattr(bridge, "restore_canonical_preview", None)
        if callable(restore):
            try:
                preview_handle = getattr(controller, "preview_handle", None)
                if not isinstance(preview_handle, str):
                    raise SafeToolError(
                        "DOCUMENT_FINISH_PREVIEW_HANDLE_MISSING: "
                        "The candidate preview handle is unavailable."
                    )
                preview_status = (
                    "restored" if await restore(preview_handle) else "not_confirmed"
                )
            except Exception:  # noqa: BLE001 - durable commit remains authoritative
                preview_status = "not_confirmed"
        # A durable commit can succeed while the first tool response is lost.
        # Keep the event at-most-once for this bound turn, while still
        # retrying it if the initial best-effort notification failed.
        if not bool(getattr(context, "_artifact_source_patched_emitted", False)):
            emitted = await _emit_artifact_state(
                scope,
                action="source.patched",
                revision_id=applied.revision.revision_id,
                change_set_id=change_set.change_set_id,
            )
            if emitted:
                setattr(context, "_artifact_source_patched_emitted", True)
        restored = preview_status == "restored"
        setattr(context, "_artifact_candidate_preview_bound", not restored)
        setattr(context, "_artifact_candidate_preview_cleanup_pending", not restored)
        setattr(context, "_artifact_browser_verification_token", None)
        setattr(context, "_artifact_browser_verification_sha256", None)
        setattr(context, "_artifact_browser_binding_generation", None)
        if restored:
            _retire_candidate_preview(context, controller)
        else:
            # Keep the opaque Gateway mapping and the native handle available
            # for the UI/turn cleanup retry.  Retiring it here would make a
            # failed release impossible to reconcile after the durable commit.
            pass
    except Exception as exc:  # noqa: BLE001 - sanitized to model
        raise SafeToolError(
            "DOCUMENT_FINISH_COMMIT_FAILED: The candidate could not be committed safely."
        ) from exc
    return _json(
        {
            "status": "applied",
            "terminal_response": (summary or "The document preview was verified and updated.")[
                :2_000
            ],
            "candidateSha256": applied.revision.artifact_sha256,
            "revision": {"generation": applied.revision.generation},
            "changeSet": {"state": change_set.status.value},
            "preview": preview_status,
        }
    )


__all__ = [
    "document_browser_act",
    "document_browser_inspect",
    "document_browser_reload",
    "document_browser_screenshot",
    "document_finish",
]
