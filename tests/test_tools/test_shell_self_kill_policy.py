"""Structural guard: shell commands can never terminate the gateway process.

Agents observed failing an audio-config attempt have fallen back to
``Stop-Process -Id <gateway pid> -Force`` (and taskkill/kill equivalents),
killing the process that hosts them. The guard runs before every configurable
policy layer, so neither environment overrides nor full host access modes can
disable it, while other PIDs and process names stay manageable.
"""

from __future__ import annotations

import os

import pytest

from opensquilla.tools.builtin.shell_policy import (
    SafeBinPolicy,
    check_gateway_self_kill,
    check_safe_bin,
    get_policy,
    set_policy,
)

GW_PID = 43210


@pytest.mark.parametrize(
    "command",
    [
        f"Stop-Process -Id {GW_PID} -Force",
        f"stop-process -id {GW_PID}",
        f"Stop-Process -Force -Id {GW_PID}",
        f"spps -Id {GW_PID}",
        f"taskkill /PID {GW_PID}",
        f"taskkill /F /PID {GW_PID}",
        f"TASKKILL /f /pid {GW_PID}",
        f"kill {GW_PID}",
        f"kill -9 {GW_PID}",
        f"kill -SIGTERM {GW_PID}",
        f"kill -TERM {GW_PID} 99999",
        f"/bin/kill {GW_PID}",
    ],
)
def test_pid_directed_kills_of_the_gateway_are_refused(command: str) -> None:
    reason = check_gateway_self_kill(command, gateway_pid=GW_PID)
    assert reason is not None
    assert "desktop supervisor" in reason
    assert "config.toml" in reason


@pytest.mark.parametrize(
    "command",
    [
        "Stop-Process -Name opensquilla-gateway",
        "Stop-Process -ProcessName OpenSquilla-Gateway -Force",
        "taskkill /IM opensquilla-gateway.exe /F",
        "pkill -f opensquilla-gateway",
        "pkill -f opensquilla",
        "killall opensquilla-gateway",
    ],
)
def test_name_directed_kills_of_the_gateway_are_refused(command: str) -> None:
    assert check_gateway_self_kill(command, gateway_pid=GW_PID) is not None


@pytest.mark.parametrize(
    "command",
    [
        f"kill -9 {GW_PID + 1}",
        f"taskkill /PID {GW_PID + 1}",
        f"Stop-Process -Id {GW_PID + 1}",
        "Stop-Process -Name notepad",
        "taskkill /IM chrome.exe",
        "pkill -f my-training-script",
        "killall Dock",
        f"echo pid is {GW_PID}",
        f"ls {GW_PID}",
        f"kill -{GW_PID}",  # a signal number is not a PID
        "git log --oneline",
    ],
)
def test_other_processes_and_ordinary_commands_stay_allowed(command: str) -> None:
    assert check_gateway_self_kill(command, gateway_pid=GW_PID) is None


def test_guard_uses_the_real_gateway_pid_by_default() -> None:
    assert check_gateway_self_kill(f"kill {os.getpid()}") is not None
    assert check_gateway_self_kill(f"kill {os.getpid() + 1}") is None


def test_guard_survives_permissive_and_custom_policies() -> None:
    """Env-configurable layers cannot re-allow gateway termination."""
    original = get_policy()
    try:
        # Everything-allowed policy: empty deny/warn, allowlist matching all.
        set_policy(SafeBinPolicy(denylist=[], allowlist=[r".*"], warnlist=[]))
        result = check_safe_bin(f"Stop-Process -Id {os.getpid()} -Force")
        assert not result.allowed
        assert "desktop supervisor" in result.reason

        # Other commands still pass through the permissive policy.
        assert check_safe_bin("echo ok").allowed
    finally:
        set_policy(original)


def test_guard_applies_before_the_denylist_reason() -> None:
    """The refusal names the self-kill guard, not a generic deny pattern."""
    original = get_policy()
    try:
        set_policy(SafeBinPolicy(denylist=[r"never-matches"], allowlist=[], warnlist=[]))
        result = check_safe_bin(f"taskkill /F /PID {os.getpid()}")
        assert not result.allowed
        assert "gateway process" in result.reason
    finally:
        set_policy(original)
