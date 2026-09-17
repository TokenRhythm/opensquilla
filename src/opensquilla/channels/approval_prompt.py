"""Shared, surface-agnostic approval-prompt contract for chat channels.

A channel-originated turn that hits an approval-gated tool (e.g. a warned
shell command) blocks on the approval queue. This module renders the prompt
that asks the originating user to approve or deny, and parses their reply,
without binding to any single adapter:

- :func:`render_approval_prompt` returns an interactive card payload when the
  adapter declares ``interactive_cards``, otherwise a plain-text prompt that
  works on every adapter via the universal ``/approve``/``/deny`` commands.
- :func:`parse_approval_action` recognises either a Feishu card action
  (``opensquilla_action == "approval_resolve"``) or the universal text
  command and returns ``(code, decision)``.

The user-facing handle is a SHORT base32 code (default 4 chars,
case-insensitive), never the raw approval/exec id. Raw ids leak in group
history and are hidden elsewhere in the product, so the code is the only
thing shown to a channel and the only thing a user types back. The
code→approval_id binding (and the originating ``sender_id`` for owner-only
resolution) lives durably beside the approval queue in SQLite.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any, Literal

from opensquilla.channels.system_messages import render_channel_message

# Crockford-style base32 alphabet minus easily-confused glyphs (I, L, O, U).
# 4 chars over a 32-symbol alphabet => 1 048 576 combinations, ample for the
# handful of approvals a single session has outstanding at once.
_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_LENGTH = 4

# Universal text command. A leading slash avoids bare-word collisions with
# ordinary chat ("approve the budget" must not resolve anything). The optional
# trailing word extends an approval: ``/approve K7PQ always`` selects the
# durable same-type grant when the approval offers one.
_TEXT_COMMAND_RE = re.compile(
    r"^\s*/(approve|deny)\b\s*([0-9A-Za-z]{2,12})?(?:\s+(always))?\s*$",
    re.IGNORECASE,
)

# Mention-gated groups REQUIRE addressing the bot, and some adapters (Slack,
# Discord) keep the raw mention markup in ``content`` — "<@U123> /approve AB12"
# must still resolve. Strip one leading mention-shaped token before matching.
_LEADING_MENTION_RE = re.compile(r"^\s*<@!?[A-Za-z0-9|]+>\s*")

# Decisions returned by :func:`parse_approval_action`. "always" maps to the
# approval's ``allow_same_type`` choice at the resolution site; the prompt only
# advertises it when the approval actually carries that choice.
DECISION_APPROVE = "approve"
DECISION_DENY = "deny"
DECISION_ALWAYS = "always"


@dataclass(frozen=True)
class ApprovalPromptRequest:
    """Surface-agnostic description of one pending channel approval.

    ``short_code`` is the human handle bound to ``approval_id`` server-side;
    ``session_key`` and ``namespace`` mirror the queue entry so the bridge can
    route the prompt back to the originating channel. ``offer_always`` is set
    when the underlying approval carries an ``allow_same_type`` choice, i.e.
    approving "always" has real durable-grant semantics rather than being a
    placebo.

    ``summary_label`` names what ``command_or_tool`` describes ("Command",
    "Network host", "Path", …) so sandbox approvals render their identifying
    fact instead of "(unknown command)". The ``origin_*`` fields carry the
    originating chat's context; interactive cards embed them in the action
    value (mirroring the clarify-card contract) so a card tap can rebuild the
    exact originating session key.
    """

    approval_id: str
    namespace: str
    session_key: str
    command_or_tool: str
    agent: str
    short_code: str
    offer_always: bool = False
    summary_label: str = "Command"
    notice: str = ""
    origin_channel_id: str = ""
    origin_is_group: bool | None = None
    origin_chat_type: str = ""
    origin_thread_id: str = ""


@dataclass(frozen=True)
class _CodeBinding:
    approval_id: str
    namespace: str
    session_key: str
    owner_sender_id: str
    origin_channel_name: str = ""
    origin_channel_id: str = ""
    origin_thread_id: str = ""
    approver_policy: str = "requester_only"


def _mint_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def normalize_code(code: str) -> str:
    """Canonicalise a user-typed code for case-insensitive lookup."""
    return code.strip().upper()


def bind_short_code(
    approval_id: str,
    *,
    namespace: str,
    session_key: str,
    owner_sender_id: str,
    origin_channel_name: str = "",
    origin_channel_id: str = "",
    origin_thread_id: str = "",
) -> str:
    """Bind ``approval_id`` to a fresh short code (idempotent per approval).

    Returns the existing code when this approval was already bound so a
    re-``requested`` notification does not mint a duplicate handle.
    """
    from opensquilla.gateway.approval_queue import get_approval_queue

    queue = get_approval_queue()
    existing = queue.channel_code_for_approval(approval_id)
    if existing is not None:
        return existing
    while True:
        code = _mint_code()
        if queue.bind_channel_code(
            code,
            approval_id=approval_id,
            namespace=namespace,
            session_key=session_key,
            owner_sender_id=owner_sender_id,
            origin_channel_name=origin_channel_name,
            origin_channel_id=origin_channel_id,
            origin_thread_id=origin_thread_id,
        ):
            return code


def resolve_short_code(code: str) -> _CodeBinding | None:
    """Look up a code's binding, or ``None`` for an unknown/expired code."""
    from opensquilla.gateway.approval_queue import get_approval_queue

    raw = get_approval_queue().resolve_channel_code(normalize_code(code))
    if raw is None:
        return None
    return _CodeBinding(
        approval_id=raw["approval_id"],
        namespace=raw["namespace"],
        session_key=raw["session_key"],
        owner_sender_id=raw["owner_sender_id"],
        origin_channel_name=raw["origin_channel_name"],
        origin_channel_id=raw["origin_channel_id"],
        origin_thread_id=raw["origin_thread_id"],
        approver_policy=raw["approver_policy"],
    )


def release_short_code(approval_id: str) -> None:
    """Drop the binding for a resolved approval (best-effort, idempotent)."""
    from opensquilla.gateway.approval_queue import get_approval_queue

    get_approval_queue().release_channel_code(approval_id)


def reset_short_codes() -> None:
    """Clear all bindings (test helper)."""
    from opensquilla.gateway.approval_queue import get_approval_queue

    get_approval_queue().clear_channel_codes()


def _adapter_supports_interactive_cards(profile: Any) -> bool:
    return bool(getattr(profile, "interactive_cards", False))


def _summary_label(request: ApprovalPromptRequest, *, config: Any = None) -> str:
    """Render the default summary label from the configured locale."""

    if not request.summary_label or request.summary_label == "Command":
        return render_channel_message("approval_label_command", config=config)
    return request.summary_label


def _prompt_text(request: ApprovalPromptRequest, *, config: Any = None) -> str:
    command = request.command_or_tool or render_channel_message(
        "approval_unknown_command", config=config
    )
    label = _summary_label(request, config=config)
    key: Literal["approval_prompt", "approval_prompt_always"] = (
        "approval_prompt_always" if request.offer_always else "approval_prompt"
    )
    rendered = render_channel_message(
        key,
        config=config,
        label=label,
        command=command,
        code=request.short_code,
    )
    if request.notice:
        rendered = f"{rendered}\n⚠️ {request.notice}"
    return rendered


def _interactive_card(request: ApprovalPromptRequest, *, config: Any = None) -> dict[str, Any]:
    """Build a Feishu-style interactive card with Approve/Deny buttons.

    The action ``value`` carries the short code (not the raw approval id) plus
    the ``opensquilla_action`` discriminator that :func:`parse_approval_action`
    keys on, paralleling the existing clarify-card contract. The originating
    chat context (``channel_id``/``is_group``/``chat_type``/``thread_id``)
    rides along, again like the clarify card, so the adapter can rebuild the
    exact originating session key from a card tap — a group-origin approval
    must not be misclassified as a DM at resolution time.
    """
    command = request.command_or_tool or render_channel_message(
        "approval_unknown_command", config=config
    )
    label = _summary_label(request, config=config)
    details = render_channel_message(
        "approval_card_details",
        config=config,
        question=render_channel_message("approval_card_question", config=config),
        label=label,
        command=command,
        code_label=render_channel_message("approval_label_code", config=config),
        code=request.short_code,
    )
    if request.notice:
        details = f"{details}\n\n⚠️ **{request.notice}**"

    def _action_value(decision: str) -> dict[str, Any]:
        value: dict[str, Any] = {
            "opensquilla_action": "approval_resolve",
            "code": request.short_code,
            "decision": decision,
        }
        if request.origin_channel_id:
            value["channel_id"] = request.origin_channel_id
        if request.origin_is_group is not None:
            value["is_group"] = request.origin_is_group
        if request.origin_chat_type:
            value["chat_type"] = request.origin_chat_type
        if request.origin_thread_id:
            value["thread_id"] = request.origin_thread_id
        return value

    actions: list[dict[str, Any]] = [
        {
            "tag": "button",
            "text": {
                "tag": "plain_text",
                "content": render_channel_message("approval_card_approve", config=config),
            },
            "type": "primary",
            "value": _action_value(DECISION_APPROVE),
        },
    ]
    if request.offer_always:
        actions.append(
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": render_channel_message("approval_card_always", config=config),
                },
                "type": "default",
                "value": _action_value(DECISION_ALWAYS),
            }
        )
    actions.append(
        {
            "tag": "button",
            "text": {
                "tag": "plain_text",
                "content": render_channel_message("approval_card_deny", config=config),
            },
            "type": "danger",
            "value": _action_value(DECISION_DENY),
        }
    )
    note_key: Literal["approval_card_note", "approval_card_note_always"] = (
        "approval_card_note_always" if request.offer_always else "approval_card_note"
    )
    note = render_channel_message(note_key, config=config, code=request.short_code)
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": render_channel_message("approval_card_title", config=config),
            },
            "template": "orange",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": details,
                },
            },
            {
                "tag": "action",
                "actions": actions,
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": note,
                    }
                ],
            },
        ],
    }


def render_approval_prompt(
    profile: Any,
    request: ApprovalPromptRequest,
    *,
    config: Any = None,
) -> dict[str, Any]:
    """Render the prompt for ``request`` against an adapter ``profile``.

    Returns a dict with ``text`` always present (the universal fallback that
    every adapter can deliver) and, when the adapter declares
    ``interactive_cards``, an additional ``card`` payload. ``profile`` is the
    adapter's :class:`ChannelCapabilityProfile` (or ``None``).
    """
    payload: dict[str, Any] = {"text": _prompt_text(request, config=config)}
    if _adapter_supports_interactive_cards(profile):
        payload["card"] = _interactive_card(request, config=config)
    return payload


def parse_approval_action(inbound: Any) -> tuple[str, str] | None:
    """Recognise an approval action from inbound channel data.

    Accepts either:

    - a Feishu card action dict carrying
      ``value.opensquilla_action == "approval_resolve"`` (already-parsed
      ``IncomingMessage.metadata`` or a raw ``{"value": {...}}`` mapping), or
    - a plain-text body of the form ``/approve <code>`` / ``/deny <code>`` /
      ``/approve <code> always``.

    Returns ``(short_code, decision)`` with decision one of
    :data:`DECISION_APPROVE` / :data:`DECISION_DENY` / :data:`DECISION_ALWAYS`,
    or ``None`` when the input is not an approval action. A missing code
    yields ``None`` (treated as "no pending" by the caller) rather than a
    silent no-op.
    """
    card = _card_action(inbound)
    if card is not None:
        return card
    text = _inbound_text(inbound)
    if text is None:
        return None
    text = _LEADING_MENTION_RE.sub("", text, count=1)
    match = _TEXT_COMMAND_RE.match(text)
    if match is None:
        return None
    code = match.group(2)
    if not code:
        return None
    verb = match.group(1).lower()
    if verb != "approve":
        return normalize_code(code), DECISION_DENY
    if match.group(3):
        return normalize_code(code), DECISION_ALWAYS
    return normalize_code(code), DECISION_APPROVE


def _card_action(inbound: Any) -> tuple[str, str] | None:
    value = _card_action_value(inbound)
    if value is None:
        return None
    if value.get("opensquilla_action") != "approval_resolve":
        return None
    code = value.get("code")
    if not isinstance(code, str) or not code.strip():
        return None
    decision = str(value.get("decision") or "").lower()
    if decision not in {DECISION_APPROVE, DECISION_DENY, DECISION_ALWAYS}:
        return None
    return normalize_code(code), decision


def _card_action_value(inbound: Any) -> dict[str, Any] | None:
    if isinstance(inbound, dict):
        value = inbound.get("value")
        if isinstance(value, dict):
            return value
        action = inbound.get("action")
        if isinstance(action, dict):
            action_value = action.get("value")
            if isinstance(action_value, dict):
                return action_value
        meta = inbound.get("metadata")
        if isinstance(meta, dict):
            meta_action = meta.get("approval_action")
            if isinstance(meta_action, dict):
                return meta_action
        return None
    metadata = getattr(inbound, "metadata", None)
    if isinstance(metadata, dict):
        meta_action = metadata.get("approval_action")
        if isinstance(meta_action, dict):
            return meta_action
    return None


def _inbound_text(inbound: Any) -> str | None:
    if isinstance(inbound, str):
        return inbound
    if isinstance(inbound, dict):
        content = inbound.get("content")
        return content if isinstance(content, str) else None
    content = getattr(inbound, "content", None)
    return content if isinstance(content, str) else None
