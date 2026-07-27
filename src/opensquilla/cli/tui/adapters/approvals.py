"""Interactive tool-approval handling for terminal chat surfaces.

The shared turn-stream loop invokes its ``approval_handler`` for every
``tool_result`` event. This module supplies the terminal implementation:
it parses approval/blocked envelopes, presents actionable requests on the
active surface (OpenTUI overlay or plain-console prompt), and resolves the
decision through the caller-provided resolver. Non-approval payloads are a
strict no-op, and every failure path (timeout, dead bridge, EOF, malformed
answer) denies rather than approves.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import structlog
from rich.markup import escape as _escape_markup

from opensquilla.cli.chat import turn_stream as _turn_stream
from opensquilla.cli.ui import console as _default_console

log = structlog.get_logger(__name__)

_ACTIONABLE_STATUSES = frozenset({"approval_required", "approval_pending"})
_BLOCKED_STATUS = "blocked"


@dataclass(frozen=True)
class ApprovalChoice:
    """One resolution option offered by an approval envelope."""

    id: str
    label: str
    approved: bool = True


@dataclass(frozen=True)
class ApprovalEnvelope:
    """Parsed approval/blocked tool-result payload."""

    status: str
    approval_id: str
    tool: str
    summary: str
    message: str
    choices: tuple[ApprovalChoice, ...] = ()

    @property
    def actionable(self) -> bool:
        return bool(self.approval_id) and self.status in _ACTIONABLE_STATUSES


def _payload_from_result(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _parse_choices(raw: Any) -> tuple[ApprovalChoice, ...]:
    if not isinstance(raw, list):
        return ()
    choices: list[ApprovalChoice] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            choices.append(ApprovalChoice(id=item.strip(), label=item.strip()))
            continue
        if not isinstance(item, dict):
            continue
        choice_id = str(item.get("id") or "").strip()
        if not choice_id:
            continue
        choices.append(
            ApprovalChoice(
                id=choice_id,
                label=str(item.get("label") or choice_id),
                approved=bool(item.get("approved", True)),
            )
        )
    return tuple(choices)


def _summary_from_payload(payload: dict[str, Any]) -> str:
    command = str(payload.get("command") or "").strip()
    if command:
        return command
    path = str(payload.get("path") or "").strip()
    if path:
        access = str(payload.get("access") or "").strip()
        return f"{access} access to {path}" if access else f"access to {path}"
    host = str(payload.get("host") or "").strip()
    if host:
        return f"network access to {host}"
    bundle_id = str(payload.get("bundle_id") or "").strip()
    if bundle_id:
        return f"package bundle {bundle_id}"
    return str(payload.get("message") or "").strip()


def parse_approval_envelope(result: Any) -> ApprovalEnvelope | None:
    """Parse a tool result into an approval envelope, or None when it is not one.

    Tolerates both dict payloads and JSON-encoded strings; anything without an
    approval/blocked ``status`` is treated as a plain tool result.
    """
    payload = _payload_from_result(result)
    if payload is None:
        return None
    status = str(payload.get("status") or "")
    if status not in _ACTIONABLE_STATUSES and status != _BLOCKED_STATUS:
        return None
    tool = str(payload.get("tool") or payload.get("approvalKind") or "tool")
    return ApprovalEnvelope(
        status=status,
        approval_id=str(payload.get("approval_id") or ""),
        tool=tool,
        summary=_summary_from_payload(payload),
        message=str(payload.get("message") or payload.get("warning") or "").strip(),
        choices=_parse_choices(payload.get("choices")),
    )


def _first_choice_matching(
    envelope: ApprovalEnvelope,
    *,
    approved: bool,
) -> ApprovalChoice | None:
    for choice in envelope.choices:
        if choice.approved == approved:
            return choice
    return None


def decide_from_response(
    envelope: ApprovalEnvelope,
    *,
    approved: bool,
    choice: str | None,
) -> tuple[bool, str | None]:
    """Map a surface decision onto a (approved, choice_id) resolution.

    When the envelope carries choices, the choice entry is authoritative for
    the approved flag (the resolver rejects mismatches); a bare approve/deny
    is mapped to the first choice with a matching polarity. Envelopes without
    choices resolve on the boolean alone.
    """
    if not envelope.choices:
        return approved, None
    if choice:
        for entry in envelope.choices:
            if entry.id == choice:
                return entry.approved, entry.id
    matching = _first_choice_matching(envelope, approved=approved)
    if matching is not None:
        return matching.approved, matching.id
    denying = _first_choice_matching(envelope, approved=False)
    if denying is not None:
        return False, denying.id
    return False, None


def deny_decision(envelope: ApprovalEnvelope) -> tuple[bool, str | None]:
    """The safe default resolution for an envelope: an explicit deny."""
    return decide_from_response(envelope, approved=False, choice=None)


async def _render_notice(renderer: Any, message: str, *, style: str = "yellow") -> None:
    try:
        await _turn_stream.renderer_status(renderer, message, style=style)
    except Exception as exc:
        log.warning("tui.approval.notice_failed", error=str(exc))


def _blocked_notice(envelope: ApprovalEnvelope) -> str:
    detail = envelope.message or envelope.summary
    suffix = f": {detail}" if detail else ""
    return f"blocked by policy — {envelope.tool}{suffix}"


def _unattended_notice(envelope: ApprovalEnvelope) -> str:
    return (
        f"approval required for {envelope.tool} — no interactive approval surface "
        "here; resolve it from the gateway control console"
    )


def _approval_requester(handle: Any) -> Callable[..., Awaitable[Any]] | None:
    """Duck-type the OpenTUI approval round-trip on an output handle.

    Plugin wrappers always expose a callable ``request_approval`` that no-ops
    for non-IPC handles, so honour their ``supports_request_approval`` flag
    when present before trusting the callable.
    """
    requester = getattr(handle, "request_approval", None)
    if not callable(requester):
        return None
    supports = getattr(handle, "supports_request_approval", None)
    if supports is not None and not supports:
        return None
    return cast(Callable[..., Awaitable[Any]], requester)


async def _resolve_decision(
    envelope: ApprovalEnvelope,
    renderer: Any,
    resolve_approval: Callable[..., Awaitable[Any]],
    decision: tuple[bool, str | None],
) -> None:
    approved, choice = decision
    try:
        result = await resolve_approval(envelope.approval_id, approved, choice=choice)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(
            "tui.approval.resolve_failed",
            approval_id=envelope.approval_id,
            approved=approved,
            choice=choice,
            error=str(exc),
        )
        await _render_notice(renderer, f"failed to resolve approval: {exc}", style="red")
        return
    canonical_approved = approved
    if isinstance(result, dict):
        if bool(result.get("pending")) and not bool(result.get("resolved")):
            await _render_notice(
                renderer,
                f"approval is being resolved on another surface — {envelope.tool}",
                style="yellow",
            )
            return
        if bool(result.get("resolved")) and "approved" in result:
            canonical_approved = bool(result["approved"])
    log.info(
        "tui.approval.resolved",
        approval_id=envelope.approval_id,
        tool=envelope.tool,
        approved=canonical_approved,
        choice=choice,
    )
    label = "approved" if canonical_approved else "denied"
    external = " by another surface" if canonical_approved != approved else ""
    await _render_notice(
        renderer,
        f"approval {label}{external} — {envelope.tool}",
        style="green" if canonical_approved else "yellow",
    )


def _host_request_payload(envelope: ApprovalEnvelope) -> dict[str, object]:
    # Envelope text is tool/model-derived and untrusted. The console path
    # escapes Rich markup; the host draws raw cells, so the risk there is
    # control bytes — strip them before the text crosses the IPC boundary
    # (the surface send path sanitizes again as the shared choke point).
    from opensquilla.cli.tui.backend.render_summary import (  # noqa: PLC0415
        sanitize_terminal_text,
    )

    summary = sanitize_terminal_text(envelope.summary)
    message = sanitize_terminal_text(envelope.message)
    payload: dict[str, object] = {
        "id": envelope.approval_id,
        "tool": envelope.tool,
        "summary": summary,
        "choices": [choice.id for choice in envelope.choices],
    }
    # The console prompt prints the envelope's rationale line; the overlay must
    # carry it too. Message-only envelopes already surface it as the summary,
    # so only send a message that adds information — compared after
    # sanitization, the same form the host will render.
    if message and message != summary:
        payload["message"] = message
    return payload


async def _handle_via_host(
    envelope: ApprovalEnvelope,
    renderer: Any,
    resolve_approval: Callable[..., Awaitable[Any]],
    requester: Callable[..., Awaitable[Any]],
) -> None:
    try:
        response = await requester(_host_request_payload(envelope))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(
            "tui.approval.host_request_failed",
            approval_id=envelope.approval_id,
            error=str(exc),
        )
        response = None
    if response is None:
        # Timeout, dead bridge, or surface teardown: deny explicitly so the
        # pending entry never dangles as an implicit approval opportunity.
        await _resolve_decision(envelope, renderer, resolve_approval, deny_decision(envelope))
        return
    if bool(getattr(response, "resolved_externally", False)):
        approved = bool(getattr(response, "approved", False))
        label = "approved" if approved else "denied"
        await _render_notice(
            renderer,
            f"approval {label} in another client — {envelope.tool}",
            style="green" if approved else "yellow",
        )
        return
    decision = decide_from_response(
        envelope,
        approved=bool(getattr(response, "approved", False)),
        choice=getattr(response, "choice", None),
    )
    await _resolve_decision(envelope, renderer, resolve_approval, decision)


def _console_prompt_text(envelope: ApprovalEnvelope) -> str:
    if envelope.choices:
        return f"Approve? [y/N/1-{len(envelope.choices)}]: "
    return "Approve? [y/N]: "


def _console_decision(
    envelope: ApprovalEnvelope,
    answer: str | None,
) -> tuple[bool, str | None]:
    normalized = (answer or "").strip().lower()
    if normalized.isdigit() and envelope.choices:
        index = int(normalized) - 1
        if 0 <= index < len(envelope.choices):
            selected = envelope.choices[index]
            return selected.approved, selected.id
        return deny_decision(envelope)
    if normalized in {"y", "yes"}:
        return decide_from_response(envelope, approved=True, choice=None)
    return deny_decision(envelope)


async def _handle_via_console(
    envelope: ApprovalEnvelope,
    renderer: Any,
    resolve_approval: Callable[..., Awaitable[Any]],
    output_console: Any,
    prompt_reader: Callable[[str], Awaitable[str]],
) -> None:
    # Envelope text is tool/model-derived and untrusted: escape it so it can
    # neither break Rich markup nor inject styling into the prompt.
    header = f"approval required: {envelope.tool}"
    if envelope.summary:
        header += f" — {envelope.summary}"
    output_console.print(f"[yellow]{_escape_markup(header)}[/yellow]")
    if envelope.message:
        output_console.print(f"[dim]{_escape_markup(envelope.message)}[/dim]")
    for index, choice in enumerate(envelope.choices, start=1):
        output_console.print(f"[dim]  {index}) {_escape_markup(choice.label)}[/dim]")
    # Run the prompt as a shielded child future so a turn cancellation (Ctrl+C)
    # while we are blocked reading the console does not abandon a live stdin
    # reader. A blocking ``input()`` cannot be interrupted; abandoning it would
    # let the runtime spawn a second reader that races the orphan for the user's
    # next line. On cancel we deny (best effort) and reap the reader so it stays
    # the sole stdin consumer, discarding whatever line it eventually returns.
    reader = asyncio.ensure_future(prompt_reader(_console_prompt_text(envelope)))
    try:
        answer: str | None = await asyncio.shield(reader)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await _resolve_decision(envelope, renderer, resolve_approval, deny_decision(envelope))
        with contextlib.suppress(BaseException):
            await asyncio.shield(reader)
        raise
    except (EOFError, KeyboardInterrupt):
        answer = None
    except Exception as exc:
        log.warning(
            "tui.approval.prompt_failed",
            approval_id=envelope.approval_id,
            error=str(exc),
        )
        answer = None
    await _resolve_decision(
        envelope,
        renderer,
        resolve_approval,
        _console_decision(envelope, answer),
    )


def tui_approval_handler(
    *,
    output_console: Any | None = None,
    prompt_reader: Callable[[str], Awaitable[str]] | None = None,
) -> Callable[..., Awaitable[None]]:
    """Build the terminal approval handler for the shared turn-stream loop.

    The returned handler matches the ``approval_handler`` call signature:
    ``handler(result, renderer, resolve_approval, *, elevated_state=None,
    surface=None)``. It routes through the renderer's output handle — an
    OpenTUI handle answers via the host overlay round-trip, a plain terminal
    handle prompts on the console — and renders an informational notice when
    no interactive path is reachable (replay/headless renderers), never
    blocking the turn.
    """
    active_console = _default_console if output_console is None else output_console

    async def _default_prompt_reader(prompt: str) -> str:
        return await asyncio.to_thread(active_console.input, prompt)

    read_answer = _default_prompt_reader if prompt_reader is None else prompt_reader

    async def _handle(
        result: Any,
        renderer: Any,
        resolve_approval: Callable[..., Awaitable[Any]],
        *,
        elevated_state: dict[str, str | None] | None = None,
        surface: object | None = None,
    ) -> None:
        del elevated_state, surface
        envelope = parse_approval_envelope(result)
        if envelope is None:
            return
        if envelope.status == _BLOCKED_STATUS:
            await _render_notice(renderer, _blocked_notice(envelope))
            return
        if not envelope.approval_id:
            await _render_notice(
                renderer,
                f"approval {envelope.status.removeprefix('approval_')} — {envelope.tool}",
            )
            return
        handle = getattr(renderer, "output_handle", None)
        requester = _approval_requester(handle)
        if requester is not None:
            await _handle_via_host(envelope, renderer, resolve_approval, requester)
            return
        if handle is not None:
            await _handle_via_console(
                envelope,
                renderer,
                resolve_approval,
                active_console,
                read_answer,
            )
            return
        # Replay/headless renderers reach here: nothing to prompt on, so render
        # where it can be seen and leave the request pending rather than decide
        # on the user's behalf.
        log.info(
            "tui.approval.unattended",
            approval_id=envelope.approval_id,
            tool=envelope.tool,
        )
        await _render_notice(renderer, _unattended_notice(envelope))

    return _handle
