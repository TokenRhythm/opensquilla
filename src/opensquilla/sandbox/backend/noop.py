"""Noop backend used when the sandbox feature switch is off.

Runs the request in-process via :mod:`asyncio` subprocess APIs with no
namespace isolation. Resource caps from the policy are still honoured by
reusing :func:`opensquilla.safety.sandbox.run_sandboxed` for rlimits + wall
timeout. Every invocation emits a ``WARNING`` so the bypass is visible in
logs; disabling the sandbox must never be silent.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time

from opensquilla.safety.sandbox import SandboxLimits, run_sandboxed
from opensquilla.sandbox.backend.base import Backend
from opensquilla.sandbox.types import SandboxRequest, SandboxResult

log = logging.getLogger(__name__)


def _limits_from_policy(request: SandboxRequest) -> SandboxLimits:
    policy = request.policy
    network = "allow" if policy.network.value == "host" else "deny"
    return SandboxLimits(
        cpu_seconds=policy.limits.cpu_seconds,
        memory_mb=policy.limits.memory_mb,
        wall_seconds=int(max(1, policy.limits.wall_timeout_s)),
        network=network,  # type: ignore[arg-type]
        env_whitelist=tuple(policy.env_allowlist),
    )


def _filtered_request_env(request: SandboxRequest) -> dict[str, str]:
    allowlist = set(request.policy.env_allowlist)
    return {
        key: value
        for key, value in request.env.items()
        if key in allowlist and isinstance(value, str)
    }


class NoopBackend(Backend):
    """Runs commands on the host with rlimits but no isolation."""

    name = "noop"

    def available(self) -> bool:
        # The fallback always "works" in the sense that it can launch a
        # subprocess. Returning True unconditionally keeps ``select_backend``
        # simple; callers that want a hard "no sandbox available" signal
        # should inspect ``settings.sandbox`` directly.
        return True

    async def run(self, request: SandboxRequest) -> SandboxResult:
        log.warning(
            "sandbox.bypass: running unsandboxed action=%s level=%s argv_len=%d",
            request.action_kind,
            request.policy.level.label,
            len(request.argv),
        )
        limits = _limits_from_policy(request)
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            functools.partial(
                run_sandboxed,
                list(request.argv),
                limits,
                stdin=request.stdin,
                env=_filtered_request_env(request),
            ),
        )
        elapsed = time.monotonic() - started
        timed_out = result.reason == "wall_limit"
        return SandboxResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            wall_time_s=elapsed,
            backend_used=self.name,
            policy_used=request.policy.summary(),
            truncated_stdout=False,
            truncated_stderr=False,
            timed_out=timed_out,
        )


__all__ = ["NoopBackend"]
