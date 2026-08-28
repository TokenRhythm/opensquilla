"""Push channel approval prompts when a channel-originated run blocks.

When an approval-gated tool call from a channel turn blocks on the queue, the
queue fires a ``requested`` transition. This bridge turns that transition into
an outbound prompt delivered to the originating channel (DM-preferred where the
session's delivery target is a direct chat) so the user who started the turn
can approve or deny with an interactive card or the universal ``/approve
<code>`` text command. On ``resolved`` it releases the short-code binding.

Additive: a missing session manager or channel manager (transient boot/reload
states) is swallowed so notification never breaks queue state or the blocked
run (which still expires on its own deadline). Once the request is proven
channel-originated, though, a DETERMINATELY undeliverable prompt — the
session node is gone, it has no delivery channel, the adapter is no longer
installed, or the send itself fails — denies the approval outright: the
prompt's addressee is the only expected resolver, so an undeliverable prompt
means the answer can never arrive and fast failure beats a silent
multi-minute hang.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import structlog

from opensquilla.channels.approval_prompt import (
    ApprovalPromptRequest,
    bind_short_code,
    release_short_code,
    render_approval_prompt,
)
from opensquilla.channels.contract import channel_capability_profile
from opensquilla.channels.system_messages import (
    ChannelSystemMessageKey,
    render_channel_message,
)
from opensquilla.session.keys import derive_chat_type

log = structlog.get_logger(__name__)


def _get_queue() -> Any:
    from opensquilla.gateway.approval_queue import get_approval_queue

    return get_approval_queue()


def _approval_summary(params: dict[str, Any], *, config: Any = None) -> tuple[str, str]:
    """Return ``(label, value)`` naming what the approval actually gates.

    Sandbox approvals carry no ``command``/``toolName`` — their identifying
    fact is a network host, package bundle, or path. Rendering those keeps
    the admin from having to approve or deny blind.
    """
    kind = str(params.get("approvalKind") or "").strip()
    if kind == "sandbox_network":
        bundle_id = str(params.get("bundle_id") or params.get("bundleId") or "").strip()
        if bundle_id:
            return (
                render_channel_message("approval_label_network", config=config),
                render_channel_message("approval_packages", config=config, bundle_id=bundle_id),
            )
        host = str(params.get("host") or "").strip()
        if host:
            return render_channel_message("approval_label_network_host", config=config), host
    elif kind == "sandbox_path":
        path = str(params.get("path") or "").strip()
        if path:
            access = str(params.get("access") or "").strip()
            return (
                render_channel_message("approval_label_path", config=config),
                f"{path} ({access})" if access else path,
            )
    elif kind == "sandbox_elevation":
        action = params.get("action")
        action = action if isinstance(action, dict) else {}
        display = action.get("display")
        display = display if isinstance(display, dict) else {}
        display_kind = str(display.get("kind") or "").strip()
        target = str(display.get("target") or "").strip()
        if target:
            label_key: ChannelSystemMessageKey = "approval_label_path"
            if display_kind == "network_access":
                label_key = "approval_label_network_host"
            elif display_kind == "run_code":
                label_key = "approval_label_code"
            elif display_kind == "run_command":
                label_key = "approval_label_command"
            return render_channel_message(label_key, config=config), target
        justification = str(action.get("justification") or params.get("justification") or "")
        return render_channel_message("approval_label_command", config=config), justification
    command = str(params.get("command") or "")
    if command:
        return render_channel_message("approval_label_command", config=config), command
    return (
        render_channel_message("approval_label_command", config=config),
        str(params.get("toolName") or params.get("action_kind") or ""),
    )


def _approval_notice(params: dict[str, Any], *, config: Any = None) -> str:
    action = params.get("action")
    action = action if isinstance(action, dict) else {}
    display = action.get("display")
    display = display if isinstance(display, dict) else {}
    if str(display.get("kind") or "") != "delete":
        return ""
    backup_state = str(display.get("backup_state") or "")
    key: ChannelSystemMessageKey | None = None
    if backup_state == "enabled":
        key = "approval_delete_backup_enabled"
    elif backup_state == "disabled":
        key = "approval_delete_backup_disabled"
    elif backup_state == "unavailable_requires_confirmation":
        key = "approval_delete_backup_unavailable"
    return render_channel_message(key, config=config) if key else ""


def _offers_always(params: dict[str, Any]) -> bool:
    """True when the approval carries a durable ``allow_same_type`` choice."""
    choices = params.get("choices")
    if not isinstance(choices, list):
        return False
    return any(
        isinstance(choice, dict) and choice.get("id") == "allow_same_type"
        for choice in choices
    )


def _deny_undeliverable(approval_id: str, reason: str, channel_name: str = "") -> None:
    """Fail closed on a determinately undeliverable channel prompt.

    The originating sender is the only party expected to resolve a channel
    approval; if the prompt cannot reach them, waiting out the full queue
    deadline just leaves the turn hanging in silence. Deny immediately so the
    agent reports the failure while the user is still looking at the chat.
    Racing an operator resolving from the Web UI is fine — ``resolve()`` then
    raises and we keep their answer.
    """
    log.warning(
        "approval_notify.prompt_undeliverable",
        approval_id=approval_id,
        reason=reason,
        channel=channel_name,
    )
    try:
        queue = _get_queue()
        queue.resolve(
            approval_id,
            False,
            elevated_mode=None,
            resolution_metadata={
                "resolutionSource": "approval_delivery_failure",
                "resolutionReason": reason,
            },
        )
    except Exception:  # noqa: BLE001 - already resolved or expired.
        log.info(
            "approval_notify.fail_closed_deny_skipped",
            approval_id=approval_id,
        )


async def _deliver_channel_prompt(
    info: dict[str, Any],
    *,
    session_manager: Any,
    channel_manager: Any,
    config: Any = None,
) -> None:
    params = info.get("params")
    params = params if isinstance(params, dict) else {}
    owner_sender_id = str(params.get("senderId") or "").strip()
    session_key = str(params.get("sessionKey") or "").strip()
    # Only channel-originated requests carry a recorded sender; web/CLI/cron
    # approvals are handled by their own surfaces and must not be re-notified.
    if not owner_sender_id or not session_key:
        return
    approval_id = str(info.get("id") or "")
    if not approval_id:
        return
    # Missing managers are transient boot/reload states — stay additive there
    # rather than denying an approval another surface may still resolve.
    if session_manager is None or channel_manager is None:
        return

    get_session = getattr(session_manager, "get_session", None)
    if not callable(get_session):
        return
    try:
        node = await get_session(session_key)
    except Exception:
        return
    if node is None:
        _deny_undeliverable(approval_id, "session_missing")
        return
    channel_name = getattr(node, "last_channel", None)
    channel_id = getattr(node, "last_to", None)
    thread_id = getattr(node, "last_thread_id", None)
    if not channel_name:
        _deny_undeliverable(approval_id, "no_delivery_channel")
        return

    get_channel = getattr(channel_manager, "get", None)
    if not callable(get_channel):
        return
    adapter = get_channel(channel_name)
    if adapter is None:
        _deny_undeliverable(approval_id, "adapter_missing", str(channel_name))
        return

    short_code = bind_short_code(
        approval_id,
        namespace=str(info.get("namespace") or "exec"),
        session_key=session_key,
        owner_sender_id=owner_sender_id,
        origin_channel_name=str(channel_name or ""),
        origin_channel_id=str(channel_id or ""),
        origin_thread_id=str(thread_id or ""),
    )
    summary_label, summary_value = _approval_summary(params, config=config)
    origin_chat_type = derive_chat_type(session_key)
    origin_is_group: bool | None = None
    if origin_chat_type in {"group", "channel"}:
        origin_is_group = True
    elif origin_chat_type == "direct":
        origin_is_group = False
    request = ApprovalPromptRequest(
        approval_id=approval_id,
        namespace=str(info.get("namespace") or "exec"),
        session_key=session_key,
        command_or_tool=summary_value,
        agent=str(params.get("agent") or ""),
        short_code=short_code,
        offer_always=_offers_always(params),
        summary_label=summary_label,
        notice=_approval_notice(params, config=config),
        origin_channel_id=str(channel_id or ""),
        origin_is_group=origin_is_group,
        origin_chat_type=origin_chat_type if origin_chat_type != "unknown" else "",
        origin_thread_id=str(thread_id or ""),
    )
    profile = channel_capability_profile(adapter)
    rendered = render_approval_prompt(profile, request, config=config)

    from opensquilla.channels.types import OutgoingMessage

    metadata: dict[str, Any] = {}
    reply_to = thread_id or channel_id
    if channel_name == "slack" and thread_id and channel_id:
        metadata["channel"] = channel_id
    if "card" in rendered:
        metadata["card"] = rendered["card"]
    message = OutgoingMessage(
        content=rendered["text"],
        reply_to=reply_to,
        metadata=metadata,
    )
    try:
        await adapter.send(message)
    except Exception as exc:  # noqa: BLE001 - deny below, never raise here.
        log.warning(
            "approval_notify.send_failed",
            channel=channel_name,
            approval_id=approval_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        _deny_undeliverable(approval_id, "send_failed", str(channel_name))


def register_approval_channel_notifier(
    queue: Any,
    *,
    session_manager: Any,
    channel_manager_ref: Callable[[], Any],
    schedule: Callable[[Any], Any],
    config: Any = None,
) -> Callable[[], None]:
    """Subscribe a notifier to queue transitions; returns the remove callable.

    ``channel_manager_ref`` is a zero-arg callable so the channel manager can be
    constructed after this bridge is registered (mirrors the boot wiring used by
    other late-bound channel consumers). ``schedule`` receives the delivery
    coroutine (gateway boot passes ``create_background_task``).
    """

    def _listener(event: str, info: dict[str, Any]) -> None:
        if event == "resolved":
            release_short_code(str(info.get("id") or ""))
            return
        if event != "requested":
            return
        params = info.get("params")
        if not isinstance(params, dict) or not str(params.get("senderId") or "").strip():
            return
        coro = _deliver_channel_prompt(
            info,
            session_manager=session_manager,
            channel_manager=channel_manager_ref(),
            config=config,
        )
        try:
            schedule(coro)
        except RuntimeError:
            coro.close()

    return cast("Callable[[], None]", queue.add_event_listener(_listener))
