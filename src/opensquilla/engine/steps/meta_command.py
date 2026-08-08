"""Pipeline step + stores for trusted meta-skill control launches.

The ``/meta`` command surface does NOT start a meta-skill turn directly.
Instead the ``meta.run`` RPC stamps a *pending launch* into a small
session-keyed, in-process store; the surface then sends a normal turn.
On that next turn the :func:`meta_command_launch` pipeline step pops the
pending launch and writes ``ctx.metadata["meta_launch"] = {"name": ...}``.
The launch turn may also carry an explicit user request using
``/meta <name> -- <request>``; in that case the marker includes ``request``
so the orchestrator receives the request instead of the command wrapper.

The store is intentionally minimal:

* keyed by session id (the pipeline's ``session_key``);
* one-shot — :func:`pending_meta_launch_pop` consumes the entry atomically
  so a launch fires on exactly one turn;
* optionally bound to the durable ``clientRequestId`` shared by ``meta.run``
  and its launch turn. Consumed ids leave a short-lived, bounded tombstone so
  a late duplicate ``meta.run`` cannot recreate a marker after ``chat.send``
  has already been accepted;
* identified markers start with a short staging TTL. Durable chat acceptance
  promotes an exact, valid launch to accepted state before runtime activation;
  accepted markers never time-expire while their turn waits in a queue;
* retained as a compatibility cache; identified production launches are also
  staged in the session database and bound to their accepted ingress turn, so
  this cache may be empty after a gateway restart without losing the intent;
* claimed only by its own launch turn — every surface stamps the launch and
  then sends a turn whose text is the ``/meta <name>`` sentinel, optionally
  followed by ``-- <request>``, so
  :func:`meta_command_launch` consumes the pending entry only when the turn
  message matches that sentinel. A stale stamp (whose launch turn never
  arrived) is never drained by an unrelated normal turn, so it cannot hijack
  the next ordinary message.

The locking pattern mirrors the sticky-cache helpers in
``opensquilla.engine.steps.meta_resolution`` (``_sticky_get`` /
``_sticky_put`` / ``_sticky_drop``).

Failed-step replay uses a separate nonce-keyed store and an exact, token-free
``/meta-replay <nonce>`` sentinel.  The replay capability token is consumed by
the gateway RPC *before* the turn is sent, so it never enters the chat
transcript, provider context, or logs.  The non-secret nonce only correlates a
committed replay with its hidden turn; each pending replay remains one-shot and
bound to the session, source run, skill name, and replay mode even when turns
arrive out of order.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

from opensquilla.engine.pipeline import TurnContext
from opensquilla.session.turn_context import current_turn_context

# Module-level, process-wide pending-launch store. Guarded by ``_pending_lock``
# so concurrent turns/RPCs on different sessions never tear the dict.
_pending_lock = threading.Lock()

_META_LAUNCH_STAGED_TTL_SECONDS = 15 * 60.0
_META_LAUNCH_TOMBSTONE_TTL_SECONDS = 15 * 60.0
_META_LAUNCH_REQUEST_LIMIT = 1024


@dataclass(frozen=True)
class PendingMetaLaunch:
    """One staged or durably accepted manual launch for a session."""

    name: str
    client_request_id: str | None = None
    accepted: bool = False
    staged_expires_at: float | None = None


@dataclass(frozen=True)
class _ConsumedMetaLaunch:
    """Short-lived idempotency tombstone for a claimed manual launch."""

    name: str
    expires_at: float


_pending_meta_launch: dict[str, dict[str | None, PendingMetaLaunch]] = {}
_consumed_meta_launch: dict[tuple[str, str], _ConsumedMetaLaunch] = {}

_META_REPLAY_SENTINEL = "/meta-replay"
_META_REPLAY_TURN_TTL_SECONDS = 5 * 60.0


@dataclass(frozen=True)
class PendingMetaReplay:
    """Trusted replay payload staged after a replay ticket is committed."""

    nonce: str
    run_id: str
    name: str
    mode: str
    expires_at: float


_pending_meta_replay: dict[str, dict[str, PendingMetaReplay]] = {}


def _valid_replay_nonce(value: str) -> bool:
    return len(value) == 32 and all(
        character in "0123456789abcdef" for character in value
    )


def format_meta_replay_sentinel(nonce: str) -> str:
    """Return the exact hidden-turn command for one committed replay."""

    if not _valid_replay_nonce(nonce):
        raise ValueError("meta replay nonce must be 32 lowercase hexadecimal characters")
    return f"{_META_REPLAY_SENTINEL} {nonce}"


def _purge_consumed_meta_launch_locked(now: float) -> None:
    expired = [
        key for key, item in _consumed_meta_launch.items() if item.expires_at <= now
    ]
    for key in expired:
        _consumed_meta_launch.pop(key, None)


def _purge_staged_meta_launch_locked(now: float) -> None:
    empty_sessions: list[str] = []
    for session_id, entries in _pending_meta_launch.items():
        expired = [
            request_id
            for request_id, launch in entries.items()
            if request_id is not None
            and not launch.accepted
            and launch.staged_expires_at is not None
            and launch.staged_expires_at <= now
        ]
        for request_id in expired:
            entries.pop(request_id, None)
        if not entries:
            empty_sessions.append(session_id)
    for session_id in empty_sessions:
        _pending_meta_launch.pop(session_id, None)


def _pending_request_count_locked() -> int:
    return sum(
        1
        for entries in _pending_meta_launch.values()
        for request_id in entries
        if request_id is not None
    )


def _remember_consumed_meta_launch_locked(
    session_id: str,
    launch: PendingMetaLaunch,
    *,
    now: float,
) -> None:
    request_id = launch.client_request_id
    if not request_id:
        return
    _purge_consumed_meta_launch_locked(now)
    key = (session_id, request_id)
    # Refreshing an existing key should also make it the newest bounded entry.
    _consumed_meta_launch.pop(key, None)
    while len(_consumed_meta_launch) >= _META_LAUNCH_REQUEST_LIMIT:
        _consumed_meta_launch.pop(next(iter(_consumed_meta_launch)))
    _consumed_meta_launch[key] = _ConsumedMetaLaunch(
        name=launch.name,
        expires_at=now + _META_LAUNCH_TOMBSTONE_TTL_SECONDS,
    )


def _normalize_client_request_id(client_request_id: str | None) -> str | None:
    if client_request_id is None:
        return None
    if not isinstance(client_request_id, str) or not client_request_id.strip():
        raise ValueError("client_request_id must be a non-empty string")
    normalized = client_request_id.strip()
    if len(normalized) > 256:
        raise ValueError("client_request_id must not exceed 256 characters")
    return normalized


def pending_meta_launch_put(
    session_id: str,
    name: str,
    *,
    client_request_id: str | None = None,
) -> str | None:
    """Stamp a pending meta-skill launch for ``session_id``.

    No-op when either argument is empty. Legacy launches without a request id
    retain the historical session-scoped latest-wins behavior. Identified
    launches can coexist and are claimed only by their matching turns.

    When ``client_request_id`` is supplied, repeating the same session/name/id
    tuple returns ``"replayed"`` without replacing the marker. A replay after
    the marker was claimed also returns ``"replayed"`` and does not recreate
    it. Reusing the id for another skill returns ``"conflict"``. Legacy callers
    that omit the id retain the historical latest-wins behavior. ``"capacity"``
    means no marker was accepted; live markers are never silently evicted.
    """
    if not session_id or not name:
        return None
    request_id = _normalize_client_request_id(client_request_id)
    with _pending_lock:
        if request_id is not None:
            now = time.monotonic()
            _purge_consumed_meta_launch_locked(now)
            _purge_staged_meta_launch_locked(now)
            consumed = _consumed_meta_launch.get((session_id, request_id))
            if consumed is not None:
                return "replayed" if consumed.name == name else "conflict"

            entries = _pending_meta_launch.get(session_id, {})
            current = entries.get(request_id)
            if current is not None:
                return "replayed" if current.name == name else "conflict"

            if _pending_request_count_locked() >= _META_LAUNCH_REQUEST_LIMIT:
                return "capacity"
            entries = _pending_meta_launch.setdefault(session_id, {})
            entries[request_id] = PendingMetaLaunch(
                name=name,
                client_request_id=request_id,
                staged_expires_at=now + _META_LAUNCH_STAGED_TTL_SECONDS,
            )
            return "stamped"

        # Legacy callers remain session-scoped and latest-wins. Bound launches
        # use their own ids, so adding compatibility state never swaps two
        # explicitly identified launch turns.
        entries = _pending_meta_launch.setdefault(session_id, {})
        entries.pop(None, None)
        entries[None] = PendingMetaLaunch(name=name, accepted=True)
        return "stamped"


def pending_meta_launch_pop(
    session_id: str,
    *,
    client_request_id: str | None = None,
) -> str | None:
    """Atomically consume and return the pending launch for ``session_id``.

    Returns the stamped meta-skill name and removes the entry, so a second
    call (with no intervening :func:`pending_meta_launch_put`) returns
    ``None``. Returns ``None`` for an empty session id or no pending entry.
    """
    if not session_id:
        return None
    request_id = _normalize_client_request_id(client_request_id)
    with _pending_lock:
        _purge_staged_meta_launch_locked(time.monotonic())
        entries = _pending_meta_launch.get(session_id)
        if not entries:
            return None
        if request_id is not None:
            launch = entries.pop(request_id, None)
        else:
            # Historical callers had a single session-scoped marker. Preserve
            # their behavior by returning the most recently staged entry when
            # this compatibility helper is used without an id.
            latest_request_id = next(reversed(entries))
            launch = entries.pop(latest_request_id)
        if not entries:
            _pending_meta_launch.pop(session_id, None)
        if launch is None:
            return None
        _remember_consumed_meta_launch_locked(
            session_id,
            launch,
            now=time.monotonic(),
        )
        return launch.name


def pending_meta_launch_clear_session(
    session_id: str,
    *,
    preserve_client_request_id: str | None = None,
    preserve_message: object = None,
) -> int:
    """Drop pre-boundary launch markers while retaining the reset turn itself.

    A same-key reset can itself be an exact MetaSkill launch. In that case only
    the marker matching both its request identity (or the legacy ``None`` key)
    and exact command name survives; ordinary reset/delete calls preserve none.
    """

    if not session_id:
        return 0
    request_id = _normalize_client_request_id(preserve_client_request_id)
    parsed = _parse_launch_text(preserve_message) if isinstance(preserve_message, str) else None
    preserve_name = parsed[0] if parsed is not None else None
    with _pending_lock:
        entries = _pending_meta_launch.get(session_id)
        if not entries:
            return 0
        retained: dict[str | None, PendingMetaLaunch] = {}
        if preserve_name is not None:
            if request_id is not None:
                bound = entries.get(request_id)
                if bound is not None and bound.name == preserve_name:
                    retained[request_id] = bound
            legacy = entries.get(None)
            if legacy is not None and legacy.name == preserve_name:
                retained[None] = legacy
        removed = len(entries) - len(retained)
        if retained:
            _pending_meta_launch[session_id] = retained
        else:
            _pending_meta_launch.pop(session_id, None)
        return removed


def pending_meta_launch_peek(
    session_id: str,
    *,
    client_request_id: str | None = None,
) -> str | None:
    """Return the pending launch for ``session_id`` without consuming it.

    Lets :func:`meta_command_launch` check whether the current turn is the
    matching ``/meta`` launch turn before draining the entry, so a non-launch
    turn leaves the pending launch in place for its real launch turn.
    """
    if not session_id:
        return None
    request_id = _normalize_client_request_id(client_request_id)
    with _pending_lock:
        _purge_staged_meta_launch_locked(time.monotonic())
        entries = _pending_meta_launch.get(session_id)
        if not entries:
            return None
        launch = (
            entries.get(request_id)
            if request_id is not None
            else entries[next(reversed(entries))]
        )
        return launch.name if launch is not None else None


def pending_meta_launch_promote(
    session_id: str,
    *,
    client_request_id: str,
    message: object,
    semantic_message: object = None,
) -> str | None:
    """Promote one staged marker after its exact launch turn is durable.

    The caller must invoke this only after durable turn acceptance and before
    runtime activation. The command grammar and skill name are revalidated so
    an unrelated request that reuses the id cannot make a staged marker
    immortal. Returns ``"promoted"``, ``"accepted"`` for an idempotent repeat,
    or ``None`` when no exact valid staged marker exists.
    """

    if not session_id:
        return None
    request_id = _normalize_client_request_id(client_request_id)
    if request_id is None:
        return None
    parsed_names = {
        parsed[0]
        for value in (message, semantic_message)
        if isinstance(value, str)
        and (parsed := _parse_launch_text(value)) is not None
    }
    if not parsed_names:
        return None

    now = time.monotonic()
    with _pending_lock:
        _purge_staged_meta_launch_locked(now)
        entries = _pending_meta_launch.get(session_id)
        if not entries:
            return None
        launch = entries.get(request_id)
        if launch is None or launch.name not in parsed_names:
            return None
        if launch.accepted:
            return "accepted"
        entries[request_id] = PendingMetaLaunch(
            name=launch.name,
            client_request_id=request_id,
            accepted=True,
        )
        return "promoted"


def pending_meta_launch_restage(
    session_id: str,
    *,
    client_request_id: str,
) -> bool:
    """Return a just-promoted marker to bounded staging after clean rollback."""

    if not session_id:
        return False
    request_id = _normalize_client_request_id(client_request_id)
    if request_id is None:
        return False
    now = time.monotonic()
    with _pending_lock:
        entries = _pending_meta_launch.get(session_id)
        launch = entries.get(request_id) if entries else None
        if launch is None or not launch.accepted:
            return False
        assert entries is not None
        entries[request_id] = PendingMetaLaunch(
            name=launch.name,
            client_request_id=request_id,
            staged_expires_at=now + _META_LAUNCH_STAGED_TTL_SECONDS,
        )
        return True


def pending_meta_launch_cancel_accepted(
    session_id: str,
    *,
    client_request_id: str,
) -> bool:
    """Consume an accepted marker whose durable turn cannot be activated."""

    if not session_id:
        return False
    request_id = _normalize_client_request_id(client_request_id)
    if request_id is None:
        return False
    now = time.monotonic()
    with _pending_lock:
        entries = _pending_meta_launch.get(session_id)
        launch = entries.get(request_id) if entries else None
        if launch is None or not launch.accepted:
            return False
        assert entries is not None
        entries.pop(request_id, None)
        if not entries:
            _pending_meta_launch.pop(session_id, None)
        _remember_consumed_meta_launch_locked(session_id, launch, now=now)
        return True


def pending_meta_launch_state(
    session_id: str,
    *,
    client_request_id: str,
) -> str | None:
    """Return ``staged``/``accepted`` for one live identified marker."""

    if not session_id:
        return None
    request_id = _normalize_client_request_id(client_request_id)
    if request_id is None:
        return None
    with _pending_lock:
        _purge_staged_meta_launch_locked(time.monotonic())
        entries = _pending_meta_launch.get(session_id)
        launch = entries.get(request_id) if entries else None
        if launch is None:
            return None
        return "accepted" if launch.accepted else "staged"


def pending_meta_launch_consumed_count(session_id: str) -> int:
    """Return live request-id tombstones for one session (test/read aid)."""

    if not session_id:
        return 0
    with _pending_lock:
        _purge_consumed_meta_launch_locked(time.monotonic())
        return sum(1 for key in _consumed_meta_launch if key[0] == session_id)


def _pending_meta_launch_record(
    session_id: str,
    client_request_id: str | None,
) -> PendingMetaLaunch | None:
    if not session_id:
        return None
    with _pending_lock:
        _purge_staged_meta_launch_locked(time.monotonic())
        entries = _pending_meta_launch.get(session_id)
        if not entries:
            return None
        if client_request_id is not None:
            bound = entries.get(client_request_id)
            if bound is not None and bound.accepted:
                return bound
        return entries.get(None)


def _claim_pending_meta_launch(
    session_id: str,
    expected: PendingMetaLaunch,
) -> bool:
    """Claim only the exact record previously matched to the launch turn."""

    if not session_id:
        return False
    with _pending_lock:
        entries = _pending_meta_launch.get(session_id)
        if not entries:
            return False
        current = entries.get(expected.client_request_id)
        if current is not expected:
            return False
        entries.pop(expected.client_request_id, None)
        if not entries:
            _pending_meta_launch.pop(session_id, None)
        _remember_consumed_meta_launch_locked(
            session_id,
            current,
            now=time.monotonic(),
        )
        return True


def pending_meta_replay_put(
    session_id: str,
    *,
    run_id: str,
    name: str,
    mode: str,
    ttl_seconds: float = _META_REPLAY_TURN_TTL_SECONDS,
) -> str:
    """Stage one trusted, token-free replay launch for ``session_id``.

    The gateway consumes the short-lived capability token before calling this
    helper.  The returned non-secret nonce correlates the committed replay with
    its hidden turn, so concurrent turns may arrive in any order without
    swapping run/mode bindings. Entries are still one-shot and expire if their
    hidden turn never arrives.
    """

    if not session_id or not run_id or not name or not mode:
        return ""
    now = time.monotonic()
    expires_at = now + max(0.0, float(ttl_seconds))
    with _pending_lock:
        entries = _pending_meta_replay.setdefault(session_id, {})
        expired = [key for key, item in entries.items() if item.expires_at <= now]
        for key in expired:
            entries.pop(key, None)
        nonce = uuid.uuid4().hex
        while nonce in entries:  # pragma: no cover - UUID collision guard
            nonce = uuid.uuid4().hex
        entries[nonce] = PendingMetaReplay(
            nonce=nonce,
            run_id=run_id,
            name=name,
            mode=mode,
            expires_at=expires_at,
        )
        return nonce


def pending_meta_replay_pop(session_id: str, nonce: str) -> PendingMetaReplay | None:
    """Consume only the exact unexpired replay bound to session + ``nonce``."""

    if not session_id or not _valid_replay_nonce(nonce):
        return None
    now = time.monotonic()
    with _pending_lock:
        entries = _pending_meta_replay.get(session_id, {})
        expired = [key for key, item in entries.items() if item.expires_at <= now]
        for key in expired:
            entries.pop(key, None)
        replay = entries.pop(nonce, None)
        if not entries:
            _pending_meta_replay.pop(session_id, None)
        return replay


def pending_meta_replay_count(session_id: str) -> int:
    """Return the number of currently queued replay launches (test/read aid)."""

    if not session_id:
        return 0
    now = time.monotonic()
    with _pending_lock:
        entries = _pending_meta_replay.get(session_id, {})
        live = {key: item for key, item in entries.items() if item.expires_at > now}
        if live:
            _pending_meta_replay[session_id] = live
        else:
            _pending_meta_replay.pop(session_id, None)
        return len(live)


def _parse_replay_command(text: object) -> tuple[bool, str | None]:
    """Recognise the replay command namespace and strictly parse its nonce.

    ``is_command`` remains true for the legacy generic sentinel and malformed
    variants so they terminate locally instead of leaking into provider input.
    """

    if not isinstance(text, str):
        return False, None
    stripped = text.strip()
    parts = stripped.split(maxsplit=1)
    if not parts or parts[0] != _META_REPLAY_SENTINEL:
        return False, None
    if len(parts) != 2:
        return True, None
    nonce = parts[1]
    if not _valid_replay_nonce(nonce):
        return True, None
    if stripped != format_meta_replay_sentinel(nonce):
        return True, None
    return True, nonce


def _parse_launch_text(text: str) -> tuple[str, str | None] | None:
    """Parse an exact manual launch sentinel.

    Supported forms are ``/meta <name>`` and
    ``/meta <name> -- <request>``. The separator is deliberately mandatory
    for a request: accepting arbitrary text after the skill name would make a
    stale pending stamp too easy for an unrelated message to claim.
    """
    stripped = text.strip()
    parts = stripped.split(None, 2)
    if len(parts) < 2 or parts[0] != "/meta":
        return None

    name = parts[1]
    if len(parts) == 2:
        return name, None

    suffix = parts[2]
    if suffix == "--":
        return name, ""
    if not suffix.startswith("--") or len(suffix) < 3 or not suffix[2].isspace():
        return None
    return name, suffix[2:].lstrip()


def manual_meta_control_correlation(client_request_id: str) -> str:
    """Return the content-free durable correlation for a manual launch."""

    request_id = _normalize_client_request_id(client_request_id)
    if request_id is None:
        raise ValueError("manual meta control requires a client request id")
    return f"request:{request_id}"


def replay_meta_control_correlation(nonce: str) -> str:
    """Return the content-free durable correlation for a committed replay."""

    if not _valid_replay_nonce(nonce):
        raise ValueError("meta replay nonce must be 32 lowercase hexadecimal characters")
    return f"nonce:{nonce}"


def parse_meta_control_sentinel(
    message: object,
    semantic_message: object,
    *,
    client_request_id: str,
) -> dict[str, str] | None:
    """Parse one exact hidden control turn for durable-intent lookup.

    Message and semantic text may be identical projections of the same input.
    Conflicting projections fail closed. A malformed replay command is never
    reclassified as a manual launch.
    """

    values = [
        value
        for value in (message, semantic_message)
        if isinstance(value, str) and value.strip()
    ]
    replay_commands = [_parse_replay_command(value) for value in values]
    replay_commands = [parsed for parsed in replay_commands if parsed[0]]
    if replay_commands:
        if len(replay_commands) != len(values):
            return None
        nonces = {nonce for _is_command, nonce in replay_commands if nonce is not None}
        if any(nonce is None for _is_command, nonce in replay_commands) or len(nonces) != 1:
            return None
        nonce = next(iter(nonces))
        assert nonce is not None
        return {
            "kind": "replay",
            "correlation_id": replay_meta_control_correlation(nonce),
        }

    launches = [_parse_launch_text(value) for value in values]
    if not launches or any(parsed is None for parsed in launches):
        return None
    valid_launches = [parsed for parsed in launches if parsed is not None]
    names = {parsed[0] for parsed in valid_launches}
    if len(names) != 1:
        return None
    try:
        correlation_id = manual_meta_control_correlation(client_request_id)
    except ValueError:
        return None
    return {
        "kind": "manual",
        "name": next(iter(names)),
        "correlation_id": correlation_id,
    }


def _launch_marker(ctx: TurnContext, name: str) -> dict[str, str] | None:
    """Return the launch marker when this turn strictly matches ``name``.

    Every surface (web SPA, CLI/TUI, channel) stamps the launch via
    ``meta.run`` and then sends a launch sentinel (bypassing client slash
    parsing). Matching both the command grammar and the stamped skill name is
    what binds the pending launch to the turn the surface deliberately issued,
    so an unrelated normal message or a command for another skill never claims
    it.
    """
    for text in (getattr(ctx, "message", ""), getattr(ctx, "semantic_message", "")):
        if not isinstance(text, str):
            continue
        parsed = _parse_launch_text(text)
        if parsed is None or parsed[0] != name:
            continue
        marker = {"name": name}
        if parsed[1] is not None:
            marker["request"] = parsed[1]
        return marker
    return None


def _turn_client_request_id() -> str | None:
    """Return the durable request identity attached by turn ingress."""

    turn_context = current_turn_context()
    if not isinstance(turn_context, dict):
        return None
    value = turn_context.get("client_request_id")
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    return normalized if len(normalized) <= 256 else None


def _durable_meta_control() -> dict[str, object] | None:
    """Return the server-bound control payload carried by durable ingress."""

    turn_context = current_turn_context()
    if not isinstance(turn_context, dict):
        return None
    value = turn_context.get("meta_control")
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    required = ("intent_id", "kind", "name", "correlation_id")
    if any(not isinstance(value.get(field), str) or not value.get(field) for field in required):
        return None
    return value


async def meta_command_launch(ctx: TurnContext) -> TurnContext:
    """Seed ``meta_launch`` from a pending ``/meta`` command, if any.

    Always-on (NOT gated on ``auto_trigger``): the whole point of the
    ``/meta`` command is to launch a meta-skill in manual-only mode. The
    step is a cheap no-op when there is no pending launch for the session.

    The pending launch is consumed only when this turn is its matching
    ``/meta <name>`` or ``/meta <name> -- <request>`` launch turn. A
    stamped-but-unclaimed launch is therefore left untouched by an ordinary
    turn, so it cannot hijack the next message.
    """
    session_id = getattr(ctx, "session_key", "") or ""

    # Identified launches and committed replays are bound to the accepted turn
    # in SQLite. This path deliberately runs before the compatibility caches:
    # clearing process memory (restart, long queue, worker replacement) cannot
    # invalidate a control the durable ingress transaction already accepted.
    durable_control = _durable_meta_control()
    if durable_control is not None:
        request_id = _turn_client_request_id() or ""
        parsed = parse_meta_control_sentinel(
            getattr(ctx, "message", ""),
            getattr(ctx, "semantic_message", ""),
            client_request_id=request_id,
        )
        kind = str(durable_control["kind"])
        correlation_id = str(durable_control["correlation_id"])
        if (
            parsed is None
            or parsed.get("kind") != kind
            or parsed.get("correlation_id") != correlation_id
        ):
            if kind == "replay":
                ctx.metadata["meta_replay_error"] = (
                    "This replay request is invalid or does not match its accepted turn. "
                    "Choose Retry failed step again."
                )
            return ctx
        name = str(durable_control["name"])
        if kind == "manual":
            if parsed.get("name") != name:
                return ctx
            marker = _launch_marker(ctx, name)
            if marker is not None:
                ctx.metadata["meta_launch"] = marker
            return ctx
        if kind == "replay":
            run_id = durable_control.get("run_id")
            mode = durable_control.get("mode")
            if (
                isinstance(run_id, str)
                and run_id
                and isinstance(mode, str)
                and mode in {"failed-step", "partial-context"}
            ):
                ctx.metadata["meta_replay"] = {
                    "run_id": run_id,
                    "name": name,
                    "mode": mode,
                }
                return ctx
            ctx.metadata["meta_replay_error"] = (
                "This replay request is invalid. Choose Retry failed step again."
            )
            return ctx

    # Replay is deliberately checked before the ordinary /meta launch.  The
    # hidden turn carries no capability token: the token has already been
    # validated and consumed by meta.runs.replay, keeping secrets out of the
    # transcript.  An expired/forged/reused sentinel is still terminated
    # locally via ``meta_replay_error`` so it can never fall through to the LLM.
    replay_commands = [
        _parse_replay_command(text)
        for text in (
            getattr(ctx, "message", ""),
            getattr(ctx, "semantic_message", ""),
        )
    ]
    replay_commands = [parsed for parsed in replay_commands if parsed[0]]
    if replay_commands:
        nonces = {nonce for _is_command, nonce in replay_commands if nonce is not None}
        malformed = any(nonce is None for _is_command, nonce in replay_commands)
        nonce = next(iter(nonces)) if len(nonces) == 1 and not malformed else ""
        replay = pending_meta_replay_pop(session_id, nonce)
        if replay is None:
            ctx.metadata["meta_replay_error"] = (
                "This replay request is invalid, expired, or was already used. "
                "Choose Retry failed step again."
            )
            return ctx
        ctx.metadata["meta_replay"] = {
            "run_id": replay.run_id,
            "name": replay.name,
            "mode": replay.mode,
        }
        return ctx

    launch = _pending_meta_launch_record(session_id, _turn_client_request_id())
    if launch is None:
        return ctx
    marker = _launch_marker(ctx, launch.name)
    if marker is None:
        return ctx
    # The record may have been superseded by another ``meta.run`` while this
    # turn was matching its sentinel. Claiming by object identity prevents the
    # older turn from popping or launching the newer record.
    if not _claim_pending_meta_launch(session_id, launch):
        return ctx
    ctx.metadata["meta_launch"] = marker
    return ctx
