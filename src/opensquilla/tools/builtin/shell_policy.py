"""Safe-bin policy enforcement for shell command execution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Patterns that are always dangerous regardless of context
DEFAULT_DENYLIST: list[str] = [
    r"rm\s+-rf\s+/\*?$",  # rm -rf / and rm -rf /*
    r"mkfs\b",  # format filesystems
    r"dd\s+if=",  # raw disk writes
    r"shutdown\b",  # system shutdown
    r"reboot\b",  # system reboot
    r"halt\b",  # system halt
    r":\(\)\s*\{.*:\|:.*\}",  # fork bomb
    r">\s*/dev/sda",  # overwrite block device
    r"chmod\s+-R\s+777\s+/",  # world-writable root
    r"(?i)\bFormat-Volume\b",  # PowerShell filesystem format
    r"(?i)\bClear-Disk\b",  # PowerShell disk wipe
    r"(?i)\bStop-Computer\b",  # PowerShell system shutdown
    r"(?i)\bRestart-Computer\b",  # PowerShell system reboot
]

DEFAULT_DENYLIST_WIN: list[str] = [
    r"\bFormat-Volume\b",
    r"\bStop-Computer\b",
    r"\bRestart-Computer\b",
    r"\bClear-Disk\b",
    r"\bgit push --force\b",
]

# Patterns that require two-step confirmation (warn, not block)
DEFAULT_WARNLIST: list[str] = [
    r"\brm\b",  # any rm invocation (catches rm, rm -r, rm -R, rm -f, rm -rf /etc, etc.)
    r"chmod\s+-R",  # recursive permission change
    r"chown\s+-R",  # recursive ownership change
    r"git\s+push\s+.*--force",  # force push
    r"(?i)\bDROP\s+",  # SQL drop
    r"(?i)\bTRUNCATE\s+",  # SQL truncate
    r"pip\s+install\s+(?!-e)",  # non-editable pip install
]

DEFAULT_WARNLIST_WIN: list[str] = [
    r"\bdel\b",
    r"\brmdir\b",
    r"\bRemove-Item\b",
    r"\bClear-Disk\b",
    r"\bgit push --force\b",
]

_LEGACY_ENV_WARNED: bool = False


def _resolve_env_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _legacy_denylist_if_set() -> list[str]:
    global _LEGACY_ENV_WARNED

    legacy = os.environ.get("OPENSQUILLA_SHELL_DENYLIST", "").strip()
    if not legacy:
        return []
    if not _LEGACY_ENV_WARNED:
        import structlog

        structlog.get_logger(__name__).warning(
            "shell_policy.legacy_deny_env_detected",
            message=(
                "OPENSQUILLA_SHELL_DENYLIST is deprecated; use "
                "OPENSQUILLA_SAFE_BIN_DENY"
            ),
        )
        _LEGACY_ENV_WARNED = True
    return _resolve_env_list(legacy)


@dataclass
class PolicyResult:
    """Result of a policy check."""

    allowed: bool
    reason: str
    needs_approval: bool = False


# ---------------------------------------------------------------------------
# Gateway self-termination guard
# ---------------------------------------------------------------------------
# Builtin tools execute inside the gateway process, so os.getpid() here IS the
# gateway PID. Agents that fall back to shell commands after a failed config
# attempt have been observed running `Stop-Process -Id <gateway pid> -Force`
# (and the taskkill/kill equivalents), killing the very process that hosts
# them. This guard is structural: it runs before the allow/deny/warn layers,
# cannot be disabled through the OPENSQUILLA_SAFE_BIN_* environment knobs, and
# applies in every host mode, including full/auto host access. Other PIDs and
# process names remain the user's business.

_SELF_KILL_GUIDANCE = (
    "the gateway process may not be terminated from a tool: restarts are "
    "managed by the desktop supervisor (or your service manager). Do not kill "
    "the gateway or edit its config.toml through shell commands — use the "
    "dedicated configuration tools or the Settings UI for configuration "
    "changes."
)

# Process-name fragments that unambiguously refer to the gateway.
_GATEWAY_NAME_FRAGMENT = re.compile(r"opensquilla", re.IGNORECASE)

# PID-bearing kill invocations. Each pattern captures the argument tail that
# carries PIDs; digits found there are compared against the gateway PID.
_PID_KILL_PATTERNS: list[re.Pattern[str]] = [
    # PowerShell: Stop-Process -Id 123[,456]  (also the spps alias)
    re.compile(r"\b(?:stop-process|spps)\b(?P<tail>[^|;\n&]*)", re.IGNORECASE),
    # Windows: taskkill /PID 123 [/PID 456]
    re.compile(r"\btaskkill\b(?P<tail>[^|;\n&]*)", re.IGNORECASE),
    # POSIX: kill [-9|-SIGTERM] 123 456 — also path-invoked (/bin/kill), but
    # not words merely ending in "kill" (taskkill has its own pattern above).
    re.compile(r"(?<![\w.-])(?:[^\s|;&]*[/\\])?kill\b(?P<tail>[^|;\n&]*)", re.IGNORECASE),
]

# Name-targeting kill invocations (no PID needed to hit the gateway).
_NAME_KILL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\b(?:stop-process|spps)\b[^|;\n&]*-(?:name|processname)\s+(?P<name>[^\s|;&]+)",
        re.IGNORECASE,
    ),
    re.compile(r"\btaskkill\b[^|;\n&]*/im\s+(?P<name>[^\s|;&]+)", re.IGNORECASE),
    re.compile(r"\b(?:pkill|killall)\b(?P<name>[^|;\n&]*)", re.IGNORECASE),
]

_PID_TOKEN = re.compile(r"(?<![\w.-])(\d{2,})(?![\w.-])")


def check_gateway_self_kill(command: str, *, gateway_pid: int | None = None) -> str | None:
    """Return a refusal reason when ``command`` targets the gateway process.

    ``gateway_pid`` is injectable for tests; it defaults to the current
    process, which is the gateway for in-process builtin tools.
    """
    pid = os.getpid() if gateway_pid is None else gateway_pid
    for pattern in _PID_KILL_PATTERNS:
        for match in pattern.finditer(command):
            tail = match.group("tail") or ""
            # `kill` as a substring of e.g. `taskkill` is prevented by the
            # lookbehind; `kill -0` style signal flags are digits too, so
            # only compare tokens that stand alone as candidate PIDs and are
            # not signal numbers attached to a dash.
            tail_wo_signals = re.sub(r"-\s*\d+", " ", tail)
            for token in _PID_TOKEN.findall(tail_wo_signals):
                if int(token) == pid:
                    return (
                        f"command targets the gateway process (pid {pid}): {_SELF_KILL_GUIDANCE}"
                    )
    for pattern in _NAME_KILL_PATTERNS:
        for match in pattern.finditer(command):
            target = match.group("name") or ""
            if _GATEWAY_NAME_FRAGMENT.search(target):
                return (
                    f"command targets the gateway by process name: {_SELF_KILL_GUIDANCE}"
                )
    return None


@dataclass
class SafeBinPolicy:
    denylist: list[str]  # regex patterns — if any match, command is denied
    allowlist: list[str]  # regex patterns — if non-empty, only matching commands are allowed
    warnlist: list[str] = field(default_factory=list)  # patterns requiring two-step approval

    @classmethod
    def from_env(cls) -> SafeBinPolicy:
        deny_env = os.environ.get("OPENSQUILLA_SAFE_BIN_DENY", "")
        allow_env = os.environ.get("OPENSQUILLA_SAFE_BIN_ALLOW", "")
        warn_env_present = "OPENSQUILLA_SAFE_BIN_WARN" in os.environ
        warn_env = os.environ.get("OPENSQUILLA_SAFE_BIN_WARN", "")

        deny = _resolve_env_list(deny_env)
        allow = _resolve_env_list(allow_env)
        warn = _resolve_env_list(warn_env)

        if not deny:
            deny = _legacy_denylist_if_set()
            if not deny:
                deny = DEFAULT_DENYLIST_WIN if os.name == "nt" else DEFAULT_DENYLIST
        if not warn and not warn_env_present:
            warn = DEFAULT_WARNLIST_WIN if os.name == "nt" else DEFAULT_WARNLIST

        return cls(denylist=deny, allowlist=allow, warnlist=warn)

    def check(self, command: str) -> PolicyResult:
        """Check command against policy layers: allowlist → denylist → warnlist."""
        # Structural guard first: killing the gateway from inside a tool is
        # never valid, regardless of how the env-configurable layers are set.
        self_kill = check_gateway_self_kill(command)
        if self_kill is not None:
            return PolicyResult(allowed=False, reason=self_kill)

        # Allowlist check: if non-empty, command must match at least one pattern
        if self.allowlist:
            matched = any(
                re.search(pattern, command, flags=re.IGNORECASE if os.name == "nt" else 0)
                for pattern in self.allowlist
            )
            if not matched:
                return PolicyResult(
                    allowed=False,
                    reason=f"command not in allowlist: {command!r}",
                )

        # Denylist check: command must not match any pattern (never bypassable)
        for pattern in self.denylist:
            if re.search(pattern, command, flags=re.IGNORECASE if os.name == "nt" else 0):
                return PolicyResult(
                    allowed=False,
                    reason=f"command blocked by policy (pattern: {pattern!r}): {command!r}",
                )

        # Warnlist check: command needs two-step approval
        for pattern in self.warnlist:
            if re.search(pattern, command, flags=re.IGNORECASE if os.name == "nt" else 0):
                return PolicyResult(
                    allowed=True,
                    reason=f"command requires approval (pattern: {pattern!r}): {command!r}",
                    needs_approval=True,
                )

        return PolicyResult(allowed=True, reason="")


# Module-level singleton loaded from environment at import time.
# Tests may replace this with a custom instance.
_policy: SafeBinPolicy | None = None


def get_policy() -> SafeBinPolicy:
    global _policy
    if _policy is None:
        _policy = SafeBinPolicy.from_env()
    return _policy


def set_policy(policy: SafeBinPolicy) -> None:
    """Override the active policy (useful for testing)."""
    global _policy
    _policy = policy


def check_safe_bin(command: str) -> PolicyResult:
    """Check command against the active policy. Returns PolicyResult."""
    return get_policy().check(command)
