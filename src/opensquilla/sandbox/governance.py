"""Approval gate, denial ledger, and post-denial guard.

This module is the bridge between :class:`~opensquilla.sandbox.types.SandboxPolicy`
(produced by :mod:`opensquilla.sandbox.policy`) and the already-existing
:class:`~opensquilla.gateway.approval_queue.ApprovalQueue`.

Public surface:

* :func:`action_fingerprint` — stable hash over the request's identifying
  fields. Used to key the denial ledger (§8.5) and the stale-output cache
  (§8.3).
* :class:`DenialLedger` — per-session counter + purge hook. Records every
  denial event and knows when the threshold (§8.5) is tripped.
* :class:`ApprovalGate` — wraps the legacy queue and returns a typed
  :data:`ApprovalDecision` (either :data:`ALLOW` or a :class:`DenialResult`).
* :func:`post_denial_guard` — enforces §8.4 on the *next* request after a
  denial: lower-privilege / explain / narrower-approval only.
* :func:`gate_execution` — top-level entry point that composes threshold
  check, post-denial guard, and approval wait.
* :func:`on_successful_exec` — hook for tool handlers to record that a
  sandboxed execution produced output the agent might reuse.

The module does **not** hold sandbox backends, does not invoke subprocesses,
and does not know about any concrete tool. It talks to the approval queue
through a small Protocol so tests can inject a fake.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from opensquilla.sandbox.stale_output_cache import StaleOutputCache, get_stale_output_cache
from opensquilla.sandbox.types import (
    ALLOW,
    ApprovalDecision,
    ApprovedHostExecution,
    DenialReason,
    DenialResult,
    FollowupTag,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
    SuggestedNextStep,
)

log = logging.getLogger(__name__)

DEFAULT_DENIAL_THRESHOLD = 3
DEFAULT_APPROVAL_TIMEOUT_S: float | None = None


# ─── Fingerprinting ────────────────────────────────────────────────────────


def action_fingerprint(request: SandboxRequest) -> str:
    """Stable hash over the request's identifying fields.

    The fingerprint groups two calls as "the same dangerous intent" for
    §§8.3 and 8.5. It covers:

    * ``action_kind`` — the tool-level tag
    * ``argv`` tuple — the actual command and arguments
    * ``cwd`` — a different working directory is a different action
    * critical subset of ``env`` (only ``PATH`` today) — enough to catch
      the common case of switching binaries via env, without making every
      ambient env change a new fingerprint

    Timestamps, approval_id-like volatile values, and the ``reason`` field
    are deliberately excluded. The output is a hex digest prefix so it is
    short enough to appear in log lines without wrapping.
    """
    critical_env = {k: request.env[k] for k in ("PATH",) if k in request.env}
    payload = {
        "action_kind": request.action_kind,
        "argv": list(request.argv),
        "cwd": str(request.cwd),
        "env": critical_env,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


# ─── Approval queue contract ──────────────────────────────────────────────


class _ApprovalQueueLike(Protocol):
    """Minimal slice of :class:`opensquilla.gateway.approval_queue.ApprovalQueue`.

    We deliberately do not import the concrete queue here so the gate can
    be unit-tested with a simple fake. The real queue already satisfies
    this protocol structurally.
    """

    def request(self, namespace: str = ..., params: dict | None = ...) -> str: ...

    async def wait(self, approval_id: str, timeout: float | None = ...) -> bool: ...

    def resolve(self, approval_id: str, approved: bool) -> None: ...

    def consume(self, approval_id: str) -> None: ...


# ─── Denial ledger ─────────────────────────────────────────────────────────


@dataclass
class _SessionState:
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0
    threshold_total: int = 0
    autonomous_paused: bool = False
    last_fingerprint: str | None = None
    last_reason: DenialReason | None = None


class DenialLedger:
    """Per-session denial counter with a stale-output purge hook.

    Thread-safe via a single ``asyncio.Lock``. The ledger lives for the
    lifetime of a session; a future follow-up is to persist it to the
    session store, which is tracked in :mod:`opensquilla.sandbox` follow-up
    notes.
    """

    def __init__(
        self,
        threshold: int = DEFAULT_DENIAL_THRESHOLD,
        *,
        stale_output_cache: StaleOutputCache | None = None,
    ) -> None:
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        self._threshold = threshold
        self._sessions: dict[str, _SessionState] = {}
        self._cache = (
            stale_output_cache if stale_output_cache is not None else get_stale_output_cache()
        )
        self._lock = asyncio.Lock()

    @property
    def threshold(self) -> int:
        return self._threshold

    def _state(self, session_id: str) -> _SessionState:
        state = self._sessions.get(session_id)
        if state is None:
            state = _SessionState()
            self._sessions[session_id] = state
        return state

    async def record_denial(
        self,
        session_id: str,
        fingerprint: str,
        reason: DenialReason,
        *,
        threshold_eligible: bool = True,
    ) -> None:
        """Increment counters and purge any cached success for ``fingerprint``.

        The purge is always run — even for non-human denials — so §8.3
        holds uniformly across denial reasons.
        """
        async with self._lock:
            state = self._state(session_id)
            state.counts[fingerprint] = state.counts.get(fingerprint, 0) + 1
            state.total += 1
            if threshold_eligible:
                state.threshold_total += 1
            state.last_fingerprint = fingerprint
            state.last_reason = reason
        await self._cache.purge(session_id, fingerprint)

    async def count(self, session_id: str, fingerprint: str) -> int:
        async with self._lock:
            return self._sessions.get(session_id, _SessionState()).counts.get(fingerprint, 0)

    async def count_session(self, session_id: str) -> int:
        async with self._lock:
            return self._sessions.get(session_id, _SessionState()).total

    async def is_paused(self, session_id: str) -> bool:
        async with self._lock:
            state = self._sessions.get(session_id)
            return bool(state and state.autonomous_paused)

    async def last_denial(self, session_id: str) -> tuple[str | None, DenialReason | None]:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return (None, None)
            return (state.last_fingerprint, state.last_reason)

    async def mark_paused(self, session_id: str) -> None:
        async with self._lock:
            self._state(session_id).autonomous_paused = True

    async def threshold_reached(self, session_id: str) -> bool:
        async with self._lock:
            state = self._sessions.get(session_id)
            return bool(state and state.threshold_total >= self._threshold)

    async def reset_session(self, session_id: str) -> None:
        """Drop all ledger state for a session (e.g. on session end)."""
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def clear_pause(self, session_id: str) -> bool:
        """Clear an autonomous pause and reset the denial counters for a session.

        Returns whether the session had been paused. Resetting the threshold
        counters (not merely the pause flag) is required so the next gated action
        starts fresh instead of immediately re-tripping ``threshold_reached`` and
        re-pausing. This is the operator-facing recovery from §8.5's sticky pause.
        """
        async with self._lock:
            state = self._sessions.get(session_id)
            was_paused = bool(state and state.autonomous_paused)
            if state is not None:
                state.autonomous_paused = False
                state.counts.clear()
                state.total = 0
                state.threshold_total = 0
                state.last_fingerprint = None
                state.last_reason = None
            return was_paused

    async def purge_stale_outputs(self, session_id: str, fingerprint: str) -> bool:
        """Expose the §8.3 purge as a direct public call for integration."""
        return await self._cache.purge(session_id, fingerprint)


# ─── Approval gate ─────────────────────────────────────────────────────────

_HUMAN_REJECTED_NEXT_STEP = SuggestedNextStep.LOWER_PRIVILEGE
_THRESHOLD_NEXT_STEP = SuggestedNextStep.ASK_USER
_REPEAT_INTENT_NEXT_STEP = SuggestedNextStep.NARROWER_APPROVAL
_POLICY_DENY_NEXT_STEP = SuggestedNextStep.REPLAN
_NETWORK_ACTION_PREFIXES = ("network.", "web.")
_L3_HOST_EXEC_ACTIONS = frozenset({"shell.exec", "shell.background", "code.exec"})


def _channel_approval_routing(
    request: SandboxRequest,
    policy: SandboxPolicy,
    *,
    fingerprint: str,
    session_id: str,
) -> dict[str, object] | DenialResult | None:
    """Return the verified route for a Channel approval, or deny it closed.

    Channel turns do not have a local approval surface. A queued approval must
    therefore carry the authenticated sender and exact session key so the
    channel notifier can deliver the prompt and authorize its response. Keep
    this at the common governance gate because Safe mode actions reach
    it without going through the managed-execution elevation helpers.
    """

    try:
        from opensquilla.tools.types import CallerKind, current_tool_context

        ctx = current_tool_context.get()
    except Exception:  # pragma: no cover - context lookup is defensive
        ctx = None
    if ctx is None:
        return None
    caller_kind = getattr(ctx, "caller_kind", None)
    if caller_kind is not CallerKind.CHANNEL and str(caller_kind) != CallerKind.CHANNEL.value:
        return None

    from opensquilla.sandbox.elevation import channel_admin_approval_identity

    identity = channel_admin_approval_identity()
    if identity is not None and session_id == identity[1]:
        return {"senderId": identity[0], "sessionKey": identity[1]}

    return DenialResult(
        reason=DenialReason.POLICY_DENIED,
        suggested_next_step=SuggestedNextStep.ASK_USER,
        level=policy.level,
        action_fingerprint=fingerprint,
        message=(
            "This Channel action cannot request approval because the authenticated "
            "administrator sender or session route is unavailable. Ask a configured "
            "channel administrator to retry it."
        ),
        retryable=False,
    )


class ApprovalGate:
    """Turns policy + approval queue into an :data:`ApprovalDecision`.

    * If ``policy.require_approval`` is false, :meth:`gate` returns
      :data:`ALLOW` immediately.
    * Otherwise it enqueues an approval request on the provided queue,
      awaits the human decision for as long as the owning task lives, and returns
      :data:`ALLOW` on approve or a :class:`DenialResult` on reject.
    * ``namespace`` selects which approval queue namespace to use — the
      existing code uses ``"exec"`` for shell/code and ``"plugin"`` for
      MCP; this gate defaults to ``"exec"`` to match the §8 contract.
    """

    def __init__(
        self,
        queue: _ApprovalQueueLike,
        *,
        namespace: str = "exec",
        timeout: float | None = DEFAULT_APPROVAL_TIMEOUT_S,
    ) -> None:
        self._queue = queue
        self._namespace = namespace
        self._timeout = timeout

    async def gate(
        self,
        request: SandboxRequest,
        policy: SandboxPolicy,
        *,
        session_id: str,
        extra_params: dict[str, object] | None = None,
    ) -> ApprovalDecision:
        """Ask the human for approval when policy requires it.

        Emits a structured debug log line with the decision for §7.4
        auditability. This call does **not** consult the ledger or the
        post-denial guard — that composition lives in :func:`gate_execution`.
        """
        from opensquilla.tools.run_mode import full_host_access_active

        if full_host_access_active():
            return ALLOW

        fingerprint = action_fingerprint(request)
        if not policy.require_approval:
            _log_decision(
                request,
                policy,
                fingerprint,
                decision="allow",
                approval_required=False,
                session_id=session_id,
            )
            return ALLOW
        if request.run_mode == "trusted":
            result = DenialResult(
                reason=DenialReason.POLICY_DENIED,
                suggested_next_step=SuggestedNextStep.ASK_USER,
                level=policy.level,
                action_fingerprint=fingerprint,
                message=(
                    "Trusted-Sandbox blocked this action before requesting approval. "
                    "This operation requires elevated approval, but Trusted-Sandbox "
                    "does not expose a visible approval surface for this tool call. "
                    "Ask the user to switch to Full Host Access or retry with a "
                    "narrower workspace-safe operation."
                ),
                retryable=False,
            )
            _log_decision(
                request,
                policy,
                fingerprint,
                decision="deny",
                approval_required=True,
                session_id=session_id,
                reason=result.reason.value,
            )
            return result
        params: dict[str, object] = {
            "action_kind": request.action_kind,
            "argv": list(request.argv),
            "cwd": str(request.cwd),
            "level": policy.level.label,
            "reason": request.reason,
            "session_id": session_id,
            "fingerprint": fingerprint,
        }
        if extra_params:
            params.update(extra_params)
        channel_routing = _channel_approval_routing(
            request,
            policy,
            fingerprint=fingerprint,
            session_id=session_id,
        )
        if isinstance(channel_routing, DenialResult):
            _log_decision(
                request,
                policy,
                fingerprint,
                decision="deny",
                approval_required=True,
                session_id=session_id,
                reason=channel_routing.reason.value,
            )
            return channel_routing
        if channel_routing is not None:
            # The authenticated ingress identity takes precedence over any
            # call-site metadata so a tool cannot select another recipient.
            params.update(channel_routing)
        approval_id = self._queue.request(namespace=self._namespace, params=params)
        try:
            approved = await self._queue.wait(approval_id, timeout=self._timeout)
        except Exception:
            # Defensive: if the queue implementation itself errors, surface a
            # structured denial rather than crashing the tool call. The queue
            # is expected not to raise, but this guards against regressions.
            log.exception("sandbox.approval_wait_failed", extra={"approval_id": approval_id})
            approved = False

        if approved:
            if (
                policy.level is SecurityLevel.LOCKED
                and request.action_kind in _L3_HOST_EXEC_ACTIONS
            ):
                try:
                    self._queue.consume(approval_id)
                except Exception as exc:
                    result = DenialResult(
                        reason=DenialReason.POLICY_DENIED,
                        suggested_next_step=SuggestedNextStep.ASK_USER,
                        level=policy.level,
                        action_fingerprint=fingerprint,
                        message=(
                            "The approved L3 execution grant could not be consumed; "
                            "the action was not executed."
                        ),
                        retryable=False,
                    )
                    _log_decision(
                        request,
                        policy,
                        fingerprint,
                        decision="deny",
                        approval_required=True,
                        session_id=session_id,
                        reason=f"{result.reason.value}:{type(exc).__name__}",
                    )
                    return result
            _log_decision(
                request,
                policy,
                fingerprint,
                decision="allow",
                approval_required=True,
                session_id=session_id,
            )
            if (
                policy.level is SecurityLevel.LOCKED
                and request.action_kind in _L3_HOST_EXEC_ACTIONS
            ):
                return ApprovedHostExecution(
                    approval_id=approval_id,
                    action_fingerprint=fingerprint,
                    level=policy.level,
                )
            return ALLOW

        result = DenialResult(
            reason=DenialReason.HUMAN_REJECTED,
            suggested_next_step=_HUMAN_REJECTED_NEXT_STEP,
            level=policy.level,
            action_fingerprint=fingerprint,
            message=(
                f"Human approval was not granted for {request.action_kind!r}. "
                "Switch to a lower-privilege alternative, explain the limitation "
                "to the user, or request a narrower approval."
            ),
            retryable=True,
        )
        _log_decision(
            request,
            policy,
            fingerprint,
            decision="deny",
            approval_required=True,
            session_id=session_id,
            reason=result.reason.value,
        )
        return result


# ─── Post-denial guard ────────────────────────────────────────────────────

_ALLOWED_FOLLOWUPS: frozenset[FollowupTag] = frozenset(
    {
        FollowupTag.LOWER_PRIVILEGE,
        FollowupTag.EXPLAIN,
        FollowupTag.NARROWER_APPROVAL,
    }
)


def post_denial_guard(
    request: SandboxRequest,
    *,
    last_denied_fingerprint: str | None,
    followup_tag: FollowupTag = FollowupTag.NONE,
    level: SecurityLevel,
) -> ApprovalDecision:
    """Enforce §8.4 on the *next* request after a denial.

    Returns :data:`ALLOW` if the request is permitted, or a
    :class:`DenialResult` with ``reason=REPEATED_SAME_INTENT`` if the agent
    is trying to retry the same fingerprint without a valid follow-up tag.

    An untagged request (``followup_tag=NONE``) whose fingerprint matches
    the last denial is blocked. A tagged follow-up is allowed even with the
    same fingerprint — the tag is the agent's explicit acknowledgement that
    this is a narrower or explanatory attempt, not a blind retry.
    """
    if last_denied_fingerprint is None:
        return ALLOW

    fingerprint = action_fingerprint(request)
    if fingerprint != last_denied_fingerprint:
        # Different intent; policy guard doesn't apply. The ledger still
        # counts this action independently if it later denies.
        return ALLOW

    if followup_tag in _ALLOWED_FOLLOWUPS:
        return ALLOW

    return DenialResult(
        reason=DenialReason.REPEATED_SAME_INTENT,
        suggested_next_step=_REPEAT_INTENT_NEXT_STEP,
        level=level,
        action_fingerprint=fingerprint,
        message=(
            "This action was just denied. Retry is only permitted as a "
            "lower-privilege alternative, an explanation to the user, or a "
            "narrower approval request."
        ),
        retryable=False,
    )


# ─── Top-level entry ──────────────────────────────────────────────────────


async def gate_execution(
    request: SandboxRequest,
    policy: SandboxPolicy,
    *,
    session_id: str,
    ledger: DenialLedger,
    approval_gate: ApprovalGate,
    followup_tag: FollowupTag = FollowupTag.NONE,
) -> ApprovalDecision:
    """Compose threshold, post-denial guard, and approval into one call.

    Order of checks:

    1. If the ledger already marked this session as paused, refuse
       immediately — §8.5's human-takeover state is sticky.
    2. Threshold — if total denials in this session >= threshold, pause
       the session and return ``THRESHOLD_EXCEEDED``.
    3. Post-denial guard — block a blind repeat of the last denied
       fingerprint unless tagged as a §8.4-allowed follow-up.
    4. Approval gate — ask the human if ``policy.require_approval``.

    Any denial path records itself into the ledger so future calls see
    consistent state.
    """
    from opensquilla.tools.run_mode import full_host_access_active

    if full_host_access_active():
        return ALLOW

    if await ledger.is_paused(session_id):
        return _pause_denial(request, policy, session_id)

    if await ledger.threshold_reached(session_id):
        return await _pause_and_deny(request, policy, session_id=session_id, ledger=ledger)

    last_fp, _ = await ledger.last_denial(session_id)
    guard = post_denial_guard(
        request,
        last_denied_fingerprint=last_fp,
        followup_tag=followup_tag,
        level=policy.level,
    )
    if isinstance(guard, DenialResult):
        await ledger.record_denial(
            session_id,
            guard.action_fingerprint,
            guard.reason,
            threshold_eligible=_counts_toward_pause_threshold(request, guard.reason),
        )
        # After recording, re-check threshold: a guard denial counts.
        if await ledger.threshold_reached(session_id):
            return await _pause_and_deny(request, policy, session_id=session_id, ledger=ledger)
        return guard

    decision = await approval_gate.gate(request, policy, session_id=session_id)
    if isinstance(decision, DenialResult):
        await ledger.record_denial(
            session_id,
            decision.action_fingerprint,
            decision.reason,
            threshold_eligible=_counts_toward_pause_threshold(request, decision.reason),
        )
        if await ledger.threshold_reached(session_id):
            return await _pause_and_deny(request, policy, session_id=session_id, ledger=ledger)
    return decision


def _counts_toward_pause_threshold(request: SandboxRequest, reason: DenialReason) -> bool:
    if request.action_kind.startswith(_NETWORK_ACTION_PREFIXES) and reason in {
        DenialReason.POLICY_DENIED,
        DenialReason.REPEATED_SAME_INTENT,
        DenialReason.RUNTIME_UNCONFIGURED,
    }:
        return False
    return True


def _pause_denial(request: SandboxRequest, policy: SandboxPolicy, session_id: str) -> DenialResult:
    """Build the sticky paused-session denial without touching the ledger."""
    fingerprint = action_fingerprint(request)
    result = DenialResult(
        reason=DenialReason.THRESHOLD_EXCEEDED,
        suggested_next_step=_THRESHOLD_NEXT_STEP,
        level=policy.level,
        action_fingerprint=fingerprint,
        message=(
            "Autonomous execution is paused after repeated denials. "
            "Hand control to the human operator before trying again."
        ),
        retryable=False,
    )
    _log_decision(
        request,
        policy,
        fingerprint,
        decision="deny",
        approval_required=policy.require_approval,
        session_id=session_id,
        reason=result.reason.value,
    )
    return result


async def _pause_and_deny(
    request: SandboxRequest,
    policy: SandboxPolicy,
    *,
    session_id: str,
    ledger: DenialLedger,
) -> DenialResult:
    await ledger.mark_paused(session_id)
    return _pause_denial(request, policy, session_id)


# ─── Successful-exec hook ─────────────────────────────────────────────────


async def on_successful_exec(
    request: SandboxRequest,
    payload: Any,
    *,
    session_id: str,
    cache: StaleOutputCache | None = None,
) -> str:
    """Record a successful execution's payload for future §8.3 purging.

    Returns the fingerprint so callers can log it. The payload is stored
    *only* for later invalidation — it is not read back by this module.
    The orchestration layer decides whether to surface stale cached output
    to the agent; here we only track what *would* be purged on denial.
    """
    from opensquilla.tools.run_mode import full_host_access_active

    if full_host_access_active():
        return ""

    fp = action_fingerprint(request)
    target = cache if cache is not None else get_stale_output_cache()
    await target.record_success(session_id, fp, payload)
    return fp


# ─── Internal helpers ─────────────────────────────────────────────────────


def _log_decision(
    request: SandboxRequest,
    policy: SandboxPolicy,
    fingerprint: str,
    *,
    decision: str,
    approval_required: bool,
    session_id: str,
    reason: str | None = None,
) -> None:
    log.debug(
        "sandbox.gate_decision",
        extra={
            "action_kind": request.action_kind,
            "level": policy.level.label,
            "approval_required": approval_required,
            "decision": decision,
            "fingerprint": fingerprint[:12],
            "session_id": session_id,
            "reason": reason,
        },
    )


__all__ = [
    "DEFAULT_APPROVAL_TIMEOUT_S",
    "DEFAULT_DENIAL_THRESHOLD",
    "ApprovalGate",
    "DenialLedger",
    "action_fingerprint",
    "gate_execution",
    "on_successful_exec",
    "post_denial_guard",
]
