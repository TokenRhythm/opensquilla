"""Shared authenticated admission decision for normalized channel events."""

from __future__ import annotations

import weakref
from dataclasses import dataclass
from typing import Any, Literal

import structlog

from opensquilla.channels._util import (
    ChannelAccessPolicy,
    ChannelDmAccess,
    evaluate_policy,
    sender_is_channel_admin,
)
from opensquilla.channels.types import (
    AuthenticatedPrincipal,
    IncomingMessage,
    IngressProvenance,
)
from opensquilla.session.keys import derive_chat_type

log = structlog.get_logger(__name__)

AdmissionReason = Literal[
    "dm_admitted",
    "dm_denied",
    "group_admitted",
    "group_denied",
    "not_mentioned_in_group",
    "not_in_allowlist",
    "pairing_required",
    "pairing_revoked",
    "principal_mismatch",
]


# This is deliberately a route-internal fact, not an adapter-provided claim.
# ``build_channel_route_envelope`` removes it from incoming metadata and the
# dispatch boundary writes it only after checking authenticated provenance.
CHANNEL_ADMIN_VERIFIED_METADATA_KEY = "channel_admin_verified"


def is_authenticated_channel_admin(
    config: Any,
    *,
    channel_name: str | None,
    msg: IncomingMessage,
) -> bool:
    """Return whether this exact authenticated event is a configured admin.

    A configured sender ID is not sufficient on its own: an adapter must have
    authenticated the transport and established a principal whose provider
    subject is exactly the normalized event sender.  The configuration lookup
    is intentionally keyed by the concrete channel entry name, rather than a
    provider type, so a grant on one configured account cannot apply to
    another account of the same provider.
    """

    if not isinstance(channel_name, str) or not channel_name:
        return False
    provenance = _message_provenance(msg)
    principal = provenance.principal
    if not provenance.authenticated or principal is None:
        return False
    if principal.subject_id != msg.sender_id:
        return False
    admin_senders = getattr(config, "channel_admin_senders", None)
    if not isinstance(admin_senders, dict):
        return False
    return sender_is_channel_admin(
        msg.sender_id,
        configured=admin_senders.get(channel_name),
    )


def has_verified_channel_admin_stamp(envelope: Any) -> bool:
    """Return the authenticated-admin fact stamped by channel dispatch."""

    metadata = getattr(envelope, "metadata", None)
    source_kind = getattr(envelope, "source_kind", None)
    source_kind_value = getattr(source_kind, "value", source_kind)
    return bool(
        source_kind_value == "channel"
        and isinstance(metadata, dict)
        and metadata.get("principal_is_owner") is True
        and metadata.get(CHANNEL_ADMIN_VERIFIED_METADATA_KEY) is True
    )


@dataclass(frozen=True, slots=True)
class ChannelAdmissionDecision:
    """The one pre-dispatch decision for an inbound channel event."""

    admit: bool
    reason: AdmissionReason
    is_group: bool
    mentioned: bool
    sender_id: str
    provenance: IngressProvenance
    principal: AuthenticatedPrincipal | None = None
    pairing_id: str | None = None
    pairing_notice: bool = False


_MENTION_GATE_WARNED: dict[int, weakref.ReferenceType[Any] | None] = {}


def _warn_missing_mention_hook(channel: Any) -> None:
    """Emit one warning per channel instance for adapters lacking the hook."""

    key = id(channel)
    existing = _MENTION_GATE_WARNED.get(key)
    if existing is None and key in _MENTION_GATE_WARNED:
        return
    if existing is not None:
        existing_channel = existing()
        if existing_channel is channel:
            return
        if existing_channel is None:
            _MENTION_GATE_WARNED.pop(key, None)

    def _forget_warned_channel(
        _ref: weakref.ReferenceType[Any], key: int = key
    ) -> None:
        _MENTION_GATE_WARNED.pop(key, None)

    try:
        _MENTION_GATE_WARNED[key] = weakref.ref(channel, _forget_warned_channel)
    except TypeError:
        _MENTION_GATE_WARNED[key] = None
    log.warning(
        "channel.mention_gate_default_deny",
        channel_type=type(channel).__name__,
    )


def _message_provenance(msg: IncomingMessage) -> IngressProvenance:
    provenance = getattr(msg, "provenance", None)
    return (
        provenance
        if isinstance(provenance, IngressProvenance)
        else IngressProvenance()
    )


def _is_group_event(msg: IncomingMessage, session_key: str) -> bool:
    metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
    explicit = metadata.get("is_group")
    if isinstance(explicit, bool):
        return explicit

    conversation_kind = metadata.get("conversation_kind")
    if conversation_kind in {"group", "group_dm", "thread", "topic"}:
        return True
    if conversation_kind == "dm":
        return False

    return derive_chat_type(session_key) in {"group", "channel"}


def _is_explicit_interaction(msg: IncomingMessage) -> bool:
    """Return whether provider normalization marks an event as bot-addressed."""

    metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
    if metadata.get("conversation_kind") == "interaction":
        return True
    interaction_type = metadata.get("interaction_type")
    if isinstance(interaction_type, str) and bool(interaction_type.strip()):
        return True
    return isinstance(metadata.get("approval_action"), dict)


def decide_channel_admission(
    channel: Any,
    msg: IncomingMessage,
    session_key: str,
    *,
    config: Any = None,
    channel_name: str | None = None,
) -> ChannelAdmissionDecision:
    """Authenticate identity and evaluate channel access exactly once.

    This function is intentionally free of session, attachment, command, and
    approval side effects. In pairing mode it deliberately creates or refreshes
    the durable access request before dispatch; no inbound content is retained.
    Legacy unverified adapters retain their historic behavior: direct messages
    are admitted by default, while group messages require an explicit mention.
    """

    provenance = _message_provenance(msg)
    principal = provenance.principal if provenance.authenticated else None
    sender_id = principal.subject_id if principal is not None else msg.sender_id
    is_group = _is_group_event(msg, session_key)
    authenticated_admin = is_authenticated_channel_admin(
        config,
        channel_name=channel_name,
        msg=msg,
    )

    if principal is not None and msg.sender_id != principal.subject_id:
        return ChannelAdmissionDecision(
            admit=False,
            reason="principal_mismatch",
            is_group=is_group,
            mentioned=False,
            sender_id=sender_id,
            provenance=provenance,
            principal=principal,
        )

    declared_policy = getattr(channel, "policy", None)
    policy = (
        declared_policy
        if isinstance(declared_policy, ChannelAccessPolicy)
        else ChannelAccessPolicy()
    )

    mentioned = _is_explicit_interaction(msg)
    if (
        is_group
        and policy.group_allowed
        and policy.mention_required_in_group
        and not mentioned
    ):
        hook = getattr(channel, "is_group_mentioned", None)
        if callable(hook):
            mentioned = bool(hook(msg))
        else:
            _warn_missing_mention_hook(channel)

    pairing_status: Literal["pending", "approved", "revoked"] | None = None
    pairing_id: str | None = None
    pairing_notice = False
    if not is_group and policy.dm_access == ChannelDmAccess.PAIRING:
        if authenticated_admin:
            # A configured administrator has already been authenticated by
            # the transport. Do not create a redundant pairing row for the
            # privileged entry path.
            pairing_status = "approved"
        elif not provenance.authenticated:
            # Compatibility for custom/legacy adapters that have not adopted
            # authenticated provenance. Supported authenticated adapters still
            # take the fail-closed pairing path below.
            pairing_status = "approved"
        elif principal is not None:
            store = getattr(channel, "_delivery_store", None)
            channel_name = str(getattr(channel, "_delivery_channel_name", "") or "")
            request_pairing = getattr(store, "request_pairing", None)
            if channel_name and callable(request_pairing):
                record = request_pairing(
                    channel_name=channel_name,
                    provider=provenance.provider or channel_name,
                    account_id=provenance.account_id or channel_name,
                    sender_id=principal.subject_id,
                    sender_name=principal.display_name,
                    # Route only — lets an approval reach a sender who has no
                    # session yet. No inbound content is retained.
                    reply_to=msg.channel_id,
                )
                if record is None:
                    # The store refused a new pending row (queue full). Deny
                    # exactly like an unapproved pairing, but without a
                    # pairing code or notice — there is no row to approve.
                    pairing_status = "pending"
                else:
                    status = str(getattr(record, "status", "pending"))
                    pairing_id = str(getattr(record, "pairing_id", "") or "") or None
                    pairing_notice = int(getattr(record, "request_count", 0) or 0) == 1
                    if status == "pending":
                        pairing_status = "pending"
                    elif status == "approved":
                        pairing_status = "approved"
                    elif status == "revoked":
                        pairing_status = "revoked"

    access = evaluate_policy(
        policy,
        is_group=is_group,
        mentioned=mentioned,
        sender_id=sender_id,
        pairing_status=pairing_status,
    )
    return ChannelAdmissionDecision(
        admit=access.admit,
        reason=access.reason,
        is_group=is_group,
        mentioned=mentioned,
        sender_id=sender_id,
        provenance=provenance,
        principal=principal,
        pairing_id=pairing_id,
        pairing_notice=pairing_notice,
    )
