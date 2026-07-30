"""Deterministic policy for exact Managed Execution elevation actions."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from opensquilla.provider import Message
from opensquilla.sandbox.elevation import ElevationAction
from opensquilla.sandbox.operation_profile import OperationProfile, classify_command

RuleRiskLevel = Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class RuleAssessment:
    risk_level: RuleRiskLevel
    user_authorization: Literal["unknown", "low", "medium", "high"]
    outcome: Literal["allow", "deny"]
    rationale: str
    human_confirmation_allowed: bool = True
    status: Literal["completed"] = "completed"
    attempt_count: int = 0
    latency_ms: int = 0


_DELETE_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:rm|rmdir|del|erase|remove-item)\b",
    flags=re.IGNORECASE,
)
_RECURSIVE_DELETE_RE = re.compile(
    r"\b(?:rm\s+(?:-[A-Za-z]*r[A-Za-z]*|--recursive)|"
    r"remove-item\b[^\n;&|]*\s-recurse\b|rmdir\s+/s\b|del\s+/s\b)",
    flags=re.IGNORECASE,
)
_DOWNLOAD_EXEC_RE = re.compile(
    r"\b(?:curl|wget|invoke-webrequest|iwr)\b[^\n]*(?:\||&&|;)\s*"
    r"(?:sudo\s+)?(?:sh|bash|zsh|pwsh|powershell|python|node)\b",
    flags=re.IGNORECASE,
)
_OUTBOUND_TRANSFER_RE = re.compile(
    r"\b(?:curl|scp|sftp|rsync|ftp)\b|"
    r"\b(?:invoke-webrequest|iwr)\b[^\n]*(?:-method\s+(?:post|put)|-infile\b)",
    flags=re.IGNORECASE,
)
_OBVIOUS_OBFUSCATED_EXEC_RE = re.compile(
    r"\bpowershell(?:\.exe)?\b[^\n]*(?:-enc(?:odedcommand)?\b)|"
    r"\b(?:iex|invoke-expression)\b[^\n]*(?:frombase64string|base64)|"
    r"\bbase64\b[^\n]*(?:--decode|-d)\b[^\n]*\|\s*(?:sh|bash|zsh|pwsh|powershell)\b|"
    r"\bexec\s*\([^\n]*(?:b64decode|frombase64string)\b|"
    r"\bcertutil\b[^\n]*-decode\b[^\n]*(?:&&|;)\s*[^\n]+",
    flags=re.IGNORECASE,
)
_SYSTEM_DAMAGE_RE = re.compile(
    r"\b(?:format(?:\.com)?\s+[A-Za-z]:|diskpart\b|bootrec\b|"
    r"bcdedit\b[^\n]*(?:/delete|/deletevalue|/set)|"
    r"manage-bde\b[^\n]*-off|cipher\b[^\n]*/w:|dd\b[^\n]*\bof=/dev/)",
    flags=re.IGNORECASE,
)
_SECURITY_OR_PRIVILEGE_DAMAGE_RE = re.compile(
    r"\b(?:set-mppreference\b[^\n]*-disablerealtimemonitoring\s+\$?true|"
    r"netsh\b[^\n]*advfirewall\b[^\n]*state\s+off|"
    r"setenforce\s+0|ufw\s+disable|iptables\s+-F|"
    r"net\s+localgroup\s+administrators\b[^\n]*/add|"
    r"usermod\b[^\n]*(?:-aG\s+(?:sudo|wheel)|--groups\s+(?:sudo|wheel))|"
    r"visudo\b|reg\s+(?:add|delete)\b[^\n]*\\policies\\system\b)",
    flags=re.IGNORECASE,
)
_MASS_ENCRYPTION_RE = re.compile(
    r"\b(?:cipher\s+/e\s+/s:|gpg\b[^\n]*--batch[^\n]*--encrypt|"
    r"openssl\b[^\n]*\benc\b[^\n]*(?:-in|-out))",
    flags=re.IGNORECASE,
)
_SENSITIVE_SECRET_RE = re.compile(
    r"(?:^|[/\\])(?:\.ssh|\.aws|\.gnupg|\.kube|\.azure)(?:[/\\]|$)|"
    r"(?:^|[/\\])(?:id_rsa|id_ed25519|credentials|shadow|sudoers)(?:$|[/\\])|"
    r"(?:^|[/\\])\.env(?:$|[/\\])|"
    r"(?:login data|cookies|web data)$",
    flags=re.IGNORECASE,
)
_CRITICAL_DELETE_TARGET_RE = re.compile(
    r"(?:^|[/\\])(?:id_rsa|id_ed25519|credentials|shadow|sudoers|sam|security|system)$|"
    r"(?:^|[/\\])\.git(?:[/\\]|$)",
    flags=re.IGNORECASE,
)
_POSIX_PATH_RE = re.compile(r"(?<![\w.])/(?:[^\s,;:'\"<>|]+/?)+")
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s,;:'\"<>|]+")

_AUTHORIZATION_WORDS = (
    "access",
    "read",
    "open",
    "inspect",
    "show",
    "write",
    "edit",
    "delete",
    "remove",
    "run",
    "execute",
    "install",
    "download",
    "upload",
    "\u8bbf\u95ee",
    "\u8bfb\u53d6",
    "\u6253\u5f00",
    "\u67e5\u770b",
    "\u663e\u793a",
    "\u5199\u5165",
    "\u4fee\u6539",
    "\u5220\u9664",
    "\u6267\u884c",
    "\u5b89\u88c5",
    "\u4e0b\u8f7d",
    "\u4e0a\u4f20",
)
_SENSITIVE_AUTHORIZATION_WORDS = (
    "ssh",
    "private key",
    "credential",
    "secret",
    "password",
    "cookie",
    "token",
    ".env",
    "\u79c1\u94a5",
    "\u5bc6\u94a5",
    "\u51ed\u636e",
    "\u5bc6\u7801",
    "\u4ee4\u724c",
    "\u6d4f\u89c8\u5668",
)


def _shell_command(action: ElevationAction) -> str:
    if action.action_kind not in {"shell.exec", "shell.background"} or not action.argv:
        return ""
    return action.argv[-1]


def _profile(action: ElevationAction) -> OperationProfile:
    if action.action_kind not in {"shell.exec", "shell.background"}:
        return OperationProfile(action.action_kind)
    return classify_command(action.argv)


def _material_targets(action: ElevationAction) -> tuple[tuple[str, str], ...]:
    return tuple(item for item in action.target_paths if item[1] != "execute")


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").casefold().rstrip("/") or "/"


def _is_protected_system_path(path: str) -> bool:
    normalized = _normalize_path(path)
    # macOS exposes these policy paths through /private aliases.  Tool path
    # discovery resolves the real path before review, so compare the canonical
    # spelling to the same policy roots the user-facing path uses.
    for alias in ("/private/etc", "/private/var", "/private/tmp"):
        if normalized == alias or normalized.startswith(f"{alias}/"):
            normalized = normalized.removeprefix("/private")
            break
    roots = (
        "/boot",
        "/dev",
        "/etc",
        "/proc",
        "/sbin",
        "/sys",
        "/usr",
        "/var",
        "c:/windows",
        "c:/boot",
        "c:/efi",
    )
    return any(normalized == root or normalized.startswith(f"{root}/") for root in roots)


def _delete_targets(action: ElevationAction, command: str) -> tuple[str, ...]:
    shell_delete = bool(_DELETE_RE.search(command))
    return tuple(
        path
        for path, access in _material_targets(action)
        if access == "delete" or (shell_delete and access == "write")
    )


def _is_broad_recursive_delete(action: ElevationAction, command: str) -> bool:
    delete_targets = _delete_targets(action, command)
    if not command and len(delete_targets) >= 20:
        return True
    if not _RECURSIVE_DELETE_RE.search(command):
        return False
    broad_roots = {
        "/",
        "/home",
        "/root",
        "/users",
        "c:",
        "c:/",
        "c:/users",
        "c:/windows",
    }
    home = _normalize_path(str(Path.home()))
    broad_roots.add(home)
    for path in delete_targets:
        normalized = _normalize_path(path)
        if normalized in broad_roots:
            return True
        if re.fullmatch(r"[a-z]:/?", normalized):
            return True
        if normalized.startswith("/") and normalized.count("/") <= 2:
            return True
    return False


def _latest_user_text(transcript: list[Message]) -> str:
    for message in reversed(transcript):
        if message.role != "user":
            continue
        if isinstance(message.content, str):
            text = message.content
        else:
            text = "\n".join(
                str(getattr(block, "text", "") or getattr(block, "content", ""))
                for block in message.content
                if getattr(block, "type", "") in {"text", "compaction"}
            )
        if text.strip():
            return text.casefold()
    return ""


def _mentioned_paths(text: str) -> tuple[str, ...]:
    return tuple(
        _normalize_path(match.group(0).rstrip("/\\).]"))
        for pattern in (_POSIX_PATH_RE, _WINDOWS_PATH_RE)
        for match in pattern.finditer(text)
    )


def _path_is_explicitly_mentioned(text: str, path: str) -> bool:
    normalized = _normalize_path(path)
    for mentioned in _mentioned_paths(text):
        if normalized == mentioned or normalized.startswith(f"{mentioned}/"):
            return True
    basename = normalized.rsplit("/", 1)[-1]
    return len(basename) >= 4 and basename in text


def _has_explicit_user_authorization(
    action: ElevationAction,
    command: str,
    transcript: list[Message],
) -> bool:
    text = _latest_user_text(transcript)
    if not text or not any(word in text for word in _AUTHORIZATION_WORDS):
        return False
    if any(_path_is_explicitly_mentioned(text, path) for path, _ in _material_targets(action)):
        return True
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        tokens = command.split()
    subjects = [
        token.casefold().strip("'\"")
        for token in tokens
        if len(token.strip("'\"")) >= 4 and not token.startswith(("-", "/"))
    ]
    return any(subject in text for subject in subjects)


def _sensitive_access_is_explicit(
    action: ElevationAction,
    command: str,
    transcript: list[Message],
) -> bool:
    text = _latest_user_text(transcript)
    if not text:
        return False
    sensitive_targets = [
        path for path, _ in _material_targets(action) if _SENSITIVE_SECRET_RE.search(path)
    ]
    if any(_path_is_explicitly_mentioned(text, path) for path in sensitive_targets):
        return True
    return any(word in text for word in _SENSITIVE_AUTHORIZATION_WORDS) and bool(
        _SENSITIVE_SECRET_RE.search(command) or sensitive_targets
    )


def _has_sensitive_access(action: ElevationAction, command: str) -> bool:
    return bool(
        _SENSITIVE_SECRET_RE.search(command)
        or any(_SENSITIVE_SECRET_RE.search(path) for path, _ in _material_targets(action))
    )


def _has_sensitive_exfiltration(action: ElevationAction, command: str) -> bool:
    if not _has_sensitive_access(action, command):
        return False
    return bool(action.network_targets or _OUTBOUND_TRANSFER_RE.search(command))


def _critical_risk_reason(
    action: ElevationAction,
    command: str,
    transcript: list[Message],
) -> str | None:
    if _has_sensitive_exfiltration(action, command):
        return "Sensitive local data would be transmitted to an external destination."
    if _has_sensitive_access(action, command) and not _sensitive_access_is_explicit(
        action, command, transcript
    ):
        return "Sensitive local data was not explicitly named by the user."
    if _is_broad_recursive_delete(action, command):
        return "The action performs a broad recursive deletion."
    for path in _delete_targets(action, command):
        if _is_protected_system_path(path) or _CRITICAL_DELETE_TARGET_RE.search(path):
            return "The action deletes a critical system, credential, or repository path."
    if _SYSTEM_DAMAGE_RE.search(command):
        return "The action can damage system, boot, disk, or encryption state."
    if _SECURITY_OR_PRIVILEGE_DAMAGE_RE.search(command):
        return "The action weakens host security or changes privileged access."
    if _MASS_ENCRYPTION_RE.search(command):
        return "The action has a high-confidence mass-encryption signature."
    if _DOWNLOAD_EXEC_RE.search(command):
        return "The action directly pipes downloaded content into an interpreter."
    if _OBVIOUS_OBFUSCATED_EXEC_RE.search(command):
        return "The action contains a high-confidence encoded or obfuscated execution chain."
    return None


def _allowed_risk_level(
    action: ElevationAction,
    profile: OperationProfile,
    command: str,
) -> RuleRiskLevel:
    if (
        action.network_targets
        or profile.needs_network
        or profile.host_effect is not None
        or profile.name == "package_install"
        or action.action_kind in {"code.exec", "shell.background"}
        or _RECURSIVE_DELETE_RE.search(command)
    ):
        return "high"
    if _DELETE_RE.search(command) or _material_targets(action):
        return "medium"
    return "low"


def local_elevation_assessment(
    action: ElevationAction,
    transcript: list[Message],
) -> RuleAssessment:
    """Return a complete deterministic decision; unknown actions default to allow."""

    profile = _profile(action)
    command = _shell_command(action)
    explicit = _has_explicit_user_authorization(action, command, transcript)
    critical_reason = _critical_risk_reason(action, command, transcript)
    if critical_reason is not None:
        sensitive_exfiltration = _has_sensitive_exfiltration(action, command)
        return RuleAssessment(
            risk_level="critical",
            user_authorization="high" if explicit else "unknown",
            outcome="deny",
            rationale=critical_reason,
            human_confirmation_allowed=not sensitive_exfiltration,
        )

    risk_level = _allowed_risk_level(action, profile, command)
    return RuleAssessment(
        risk_level=risk_level,
        user_authorization="high" if explicit else "medium",
        outcome="allow",
        rationale=(
            "The user explicitly authorized this exact non-critical action."
            if explicit
            else "No deterministic hard-risk rule matched; Managed Execution defaults to allow."
        ),
    )


__all__ = ["RuleAssessment", "local_elevation_assessment"]
