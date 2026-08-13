"""Noop backend used when the sandbox feature switch is off.

Runs the request with :mod:`asyncio` subprocess APIs and no namespace
isolation. Resource caps from the policy are honoured where the platform
provides the safety layer's rlimit helpers. The subprocess is owned directly
by the caller task,
so cancellation can synchronously stop its process tree instead of merely
cancelling a ``run_in_executor`` waiter while a worker keeps running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from opensquilla.process_tree import (
    capture_process_tree_owner,
    create_owned_subprocess_exec,
)
from opensquilla.safety.sandbox import (
    HAS_RESOURCE,
    SandboxLimits,
    _decode_stream,
    _filtered_env,
    _preexec,
)
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
        child_env = _filtered_env(limits.env_whitelist, _filtered_request_env(request))
        spawn_kwargs: dict[str, object] = {
            "stdin": asyncio.subprocess.PIPE if request.stdin is not None else None,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": str(request.cwd),
            "env": child_env,
            "start_new_session": True,
        }
        if HAS_RESOURCE:
            spawn_kwargs["preexec_fn"] = _preexec(limits)
        try:
            proc = await create_owned_subprocess_exec(
                *request.argv,
                **spawn_kwargs,
            )
        except (OSError, ValueError) as exc:
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr=str(exc),
                wall_time_s=time.monotonic() - started,
                backend_used=self.name,
                policy_used=request.policy.summary(),
                timed_out=False,
            )

        process_tree = capture_process_tree_owner(proc, isolated=True)
        timed_out = False
        stdout: bytes | str | None = None
        stderr: bytes | str | None = None
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=request.stdin),
                timeout=limits.wall_seconds,
            )
        except TimeoutError:
            timed_out = True
            await process_tree.terminate(graceful_timeout=0.2, kill_timeout=1.0)
            with contextlib.suppress(Exception):
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1.0)
        except asyncio.CancelledError:
            await asyncio.shield(
                process_tree.terminate(graceful_timeout=0.2, kill_timeout=1.0)
            )
            raise

        # Successful leaders may have daemonized children that did not inherit
        # stdout/stderr. Do not let them escape this finite sandbox invocation.
        await process_tree.terminate(graceful_timeout=0.2, kill_timeout=1.0)
        elapsed = time.monotonic() - started
        return SandboxResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=_decode_stream(stdout),
            stderr=_decode_stream(stderr),
            wall_time_s=elapsed,
            backend_used=self.name,
            policy_used=request.policy.summary(),
            truncated_stdout=False,
            truncated_stderr=False,
            timed_out=timed_out,
        )


__all__ = ["NoopBackend"]
