"""Pipeline step + store for the manual ``/meta`` command launch path.

The ``/meta`` command surface does NOT start a meta-skill turn directly.
Instead the ``meta.run`` RPC stamps a *pending launch* into a small
session-keyed, in-process store; the surface then sends a normal turn.
On that next turn the :func:`meta_command_launch` pipeline step pops the
pending launch and writes ``ctx.metadata["meta_launch"] = {"name": ...}``,
which the agent bootstrap stage copies onto ``config.metadata`` so the
agent's turn generator dispatches ``Agent._run_meta_launch(name)``.

The store is intentionally minimal:

* keyed by session id (the pipeline's ``session_key``);
* one-shot — :func:`pending_meta_launch_pop` consumes the entry atomically
  so a launch fires on exactly one turn;
* in-memory only (lost on gateway restart, which is fine: a restart drops
  any half-issued ``/meta`` command and the surface can re-issue it);
* claimed only by its own launch turn — every surface stamps the launch and
  then sends a turn whose text is the ``/meta <name>`` sentinel, so
  :func:`meta_command_launch` consumes the pending entry only when the turn
  message matches that sentinel. A stale stamp (whose launch turn never
  arrived) is never drained by an unrelated normal turn, so it cannot hijack
  the next ordinary message.

The locking pattern mirrors the sticky-cache helpers in
``opensquilla.engine.steps.meta_resolution`` (``_sticky_get`` /
``_sticky_put`` / ``_sticky_drop``).
"""

from __future__ import annotations

import threading

from opensquilla.engine.pipeline import TurnContext

# Module-level, process-wide pending-launch store. Guarded by ``_pending_lock``
# so concurrent turns/RPCs on different sessions never tear the dict.
_pending_lock = threading.Lock()
_pending_meta_launch: dict[str, str] = {}


def pending_meta_launch_put(session_id: str, name: str) -> None:
    """Stamp a pending meta-skill launch for ``session_id``.

    No-op when either argument is empty. A later launch for the same
    session overwrites any earlier (unconsumed) one — the most recent
    ``/meta`` command wins.
    """
    if not session_id or not name:
        return
    with _pending_lock:
        _pending_meta_launch[session_id] = name


def pending_meta_launch_pop(session_id: str) -> str | None:
    """Atomically consume and return the pending launch for ``session_id``.

    Returns the stamped meta-skill name and removes the entry, so a second
    call (with no intervening :func:`pending_meta_launch_put`) returns
    ``None``. Returns ``None`` for an empty session id or no pending entry.
    """
    if not session_id:
        return None
    with _pending_lock:
        return _pending_meta_launch.pop(session_id, None)


def pending_meta_launch_peek(session_id: str) -> str | None:
    """Return the pending launch for ``session_id`` without consuming it.

    Lets :func:`meta_command_launch` check whether the current turn is the
    matching ``/meta`` launch turn before draining the entry, so a non-launch
    turn leaves the pending launch in place for its real launch turn.
    """
    if not session_id:
        return None
    with _pending_lock:
        return _pending_meta_launch.get(session_id)


def _is_launch_turn(ctx: TurnContext, name: str) -> bool:
    """True when this turn is the ``/meta <name>`` launch turn for ``name``.

    Every surface (web SPA, CLI/TUI, channel) stamps the launch via
    ``meta.run`` and then sends a turn whose provider text is ``/meta <name>``
    followed by an optional concrete request (bypassing client slash parsing).
    Matching the sentinel at a whitespace boundary binds the pending launch to
    the turn the surface deliberately issued without accepting a different
    skill whose name merely shares the same prefix.
    """
    sentinel = f"/meta {name}"
    for text in (getattr(ctx, "message", ""), getattr(ctx, "semantic_message", "")):
        if not isinstance(text, str):
            continue
        candidate = text.strip()
        if candidate == sentinel:
            return True
        if candidate.startswith(sentinel):
            suffix = candidate[len(sentinel) :]
            if suffix and suffix[0].isspace():
                return True
    return False


async def meta_command_launch(ctx: TurnContext) -> TurnContext:
    """Seed ``meta_launch`` from a pending ``/meta`` command, if any.

    Always-on (NOT gated on ``auto_trigger``): the whole point of the
    ``/meta`` command is to launch a meta-skill in manual-only mode. The
    step is a cheap no-op when there is no pending launch for the session.

    The pending launch is consumed only when this turn is its matching
    ``/meta <name>`` launch turn. A stamped-but-unclaimed launch is therefore
    left untouched by an ordinary turn, so it cannot hijack the next message.
    """
    session_id = getattr(ctx, "session_key", "") or ""
    name = pending_meta_launch_peek(session_id)
    if not name or not _is_launch_turn(ctx, name):
        return ctx
    pending_meta_launch_pop(session_id)
    ctx.metadata["meta_launch"] = {"name": name}
    return ctx
