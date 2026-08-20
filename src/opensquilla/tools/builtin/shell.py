"""Shell built-in tools: exec_command, background_process, process."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import ntpath
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from opensquilla.gateway.approval_queue import (
    classify_command as classify_approval_command,
)
from opensquilla.gateway.approval_queue import (
    get_approval_queue,
)
from opensquilla.process_tree import (
    ProcessTreeOwner,
    capture_process_tree_owner,
    create_owned_popen,
    create_owned_subprocess_exec,
    create_owned_subprocess_shell,
)
from opensquilla.sandbox.backend.bubblewrap import (
    BubblewrapBackend,
    LinuxProxyBridgeHost,
    build_bwrap_plan,
    materialize_linux_exec_wrapper,
)
from opensquilla.sandbox.backend.linux_limits import resource_preexec_from_limits
from opensquilla.sandbox.backend.linux_protected_create import (
    ProtectedCreateRegistration,
    SyntheticMountRegistration,
    cleanup_protected_create_registrations,
    cleanup_synthetic_mount_registrations,
    register_protected_create_targets,
    register_synthetic_mount_targets,
)
from opensquilla.sandbox.backend.linux_readiness import probe_bwrap
from opensquilla.sandbox.backend.noop import NoopBackend
from opensquilla.sandbox.backend.seatbelt import (
    SeatbeltBackend,
    build_seatbelt_argv,
    render_seatbelt_profile,
    seatbelt_env_for_policy,
)
from opensquilla.sandbox.backend.unavailable import UnavailableBackend
from opensquilla.sandbox.backup_vault import (
    BackupTooLarge,
    BackupUnavailable,
    BackupVault,
    summarize_backup_receipts,
)
from opensquilla.sandbox.command_policy import (
    CommandAction,
    decide_shell_command,
    parse_shell_segments,
    strip_shell_heredoc_bodies,
)
from opensquilla.sandbox.denial_attribution import is_likely_sandbox_denied
from opensquilla.sandbox.elevation import (
    ApprovalDisplay,
    ElevationAction,
    gate_elevated_action,
)
from opensquilla.sandbox.escalation import (
    build_path_approval_params,
    current_tool_mounts,
    grant_temporary_mount_for_current_tool,
    request_sandbox_approval,
)
from opensquilla.sandbox.file_mutation_broker import (
    FILE_DELETE_WARNING,
    OVERSIZE_BACKUP_WARNING,
    RECURSIVE_DELETE_WARNING,
    UNAVAILABLE_BACKUP_WARNING,
    FileMutationBroker,
    MutationDenied,
    MutationPlan,
    ObjectIdentityChanged,
)
from opensquilla.sandbox.file_policy import authority_roots_for_state
from opensquilla.sandbox.integration import (
    SandboxRuntime,
    active_file_system_profile,
    active_sandbox_policy,
    consume_backend_denial_retry,
    escalate_backend_denial,
    escalate_unavailable_backend_in_managed_mode,
    gate_action,
    get_runtime,
    preflight_subprocess_managed_network,
    prepare_subprocess_managed_network_proxy,
    reject_windows_guest_process,
    run_under_backend,
)
from opensquilla.sandbox.managed_proxy_env import (
    NO_PROXY_ENV_KEYS,
    OPENSQUILLA_NETWORK_ENV_KEY,
    PROXY_ACTIVE_ENV_KEY,
    PROXY_CONTROL_ENV,
    PROXY_ENV_KEYS,
)
from opensquilla.sandbox.operation_profile import (
    OperationProfile,
    classify_command,
    shell_command_approval_variants,
)
from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.sandbox.path_aliases import resolve_workspace_alias
from opensquilla.sandbox.path_validation import (
    MountDecision,
    decide_path_access,
    logical_tool_path,
    trusted_write_auto_grant_allowed,
)
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.sandbox.policy import LevelHints
from opensquilla.sandbox.runtime_launcher import apply_bundled_runtime_path
from opensquilla.sandbox.runtime_manifest import RuntimeManifestError
from opensquilla.sandbox.types import (
    ApprovedHostExecution,
    DenialResult,
    MountMode,
    MountSpec,
    NetworkMode,
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SandboxResult,
    sandbox_path_text,
)
from opensquilla.skills.runtime_env import (
    MEDIA_FONTS_DIR_ENV,
    PAPER_FONTS_ENV,
    managed_skill_env,
    managed_toolchain_readonly_paths,
)
from opensquilla.subprocess_encoding import apply_utf8_child_env, decode_subprocess_output
from opensquilla.tools.builtin.shell_policy import PolicyResult as SafeBinPolicyResult
from opensquilla.tools.builtin.shell_policy import check_safe_bin
from opensquilla.tools.path_policy import reject_foreign_host_path
from opensquilla.tools.registry import tool
from opensquilla.tools.run_mode import (
    current_run_mode,
    full_host_access_active,
    trusted_sandbox_active,
)
from opensquilla.tools.source_diff_preservation import (
    endgame_git_freeze_block_json,
    source_diff_preservation_block_json,
)
from opensquilla.tools.types import (
    CallerKind,
    ToolError,
    current_tool_context,
)
from opensquilla.tools.write_tracking import (
    classify_workspace_path,
    enforce_workspace_write_deny_effects,
    mutation_ledger_text_hash,
    record_observed_workspace_mutations,
    snapshot_current_workspace_mutations,
    summarize_patch_hygiene_warning,
)

log = structlog.get_logger(__name__)

_DEFAULT_EXEC_TIMEOUT = 60.0
_MAX_EXEC_TIMEOUT = 600.0
_APPROVAL_RETRY_WAIT_SECONDS = 180.0
_EXEC_TOOL_TIMEOUT_PADDING = _APPROVAL_RETRY_WAIT_SECONDS + 5.0
_DEFAULT_BACKGROUND_TIMEOUT = 1800.0
_MAX_BACKGROUND_TIMEOUT = 5400.0
_DEFAULT_PROCESS_WAIT_TIMEOUT = 600.0
_CODING_PROCESS_WAIT_TIMEOUT = 5400.0
_MAX_PROCESS_WAIT_TIMEOUT = 5400.0
_PROCESS_WAIT_TIMEOUT_PADDING = 5.0
_BACKGROUND_TERMINATE_TIMEOUT = 1.0
_BACKGROUND_KILL_TIMEOUT = 1.0
_EXEC_TERMINATE_TIMEOUT = 0.25
_EXEC_KILL_TIMEOUT = 0.25
_EXEC_STDIN_WRITE_CHUNK_BYTES = 64 * 1024
_EXEC_STDIN_GUARD_CHUNK_CHARS = 64 * 1024
_EXEC_STDIN_GUARD_OVERLAP_CHARS = 1024
_COMMAND_AUDIT_MAX_CHARS = 4096
_EXEC_TIMEOUT_OUTPUT_TAIL_CHARS = 20000
_POWERSHELL_SCRIPT_PROFILE_MAX_CHARS = 128 * 1024
_WINDOWS_ENV_CANONICAL_KEYS = {
    "COMSPEC": "ComSpec",
    "PATH": "PATH",
    "PATHEXT": "PATHEXT",
    "SYSTEMROOT": "SystemRoot",
    "TEMP": "TEMP",
    "TMP": "TMP",
    "USERPROFILE": "USERPROFILE",
    "WINDIR": "WINDIR",
}


def _apply_safe_command_policy(
    command: str,
    result: SafeBinPolicyResult,
) -> SafeBinPolicyResult:
    if full_host_access_active():
        return result
    decision = decide_shell_command(command, active_sandbox_policy())
    if decision.action is CommandAction.DENY:
        raise ToolError(f"command denied by Safe policy: {decision.code}")
    return SafeBinPolicyResult(
        allowed=bool(result.allowed),
        needs_approval=decision.action is CommandAction.APPROVAL,
        reason=(
            f"command requires Safe approval: {decision.code}"
            if decision.action is CommandAction.APPROVAL
            else ""
        ),
    )


@dataclass
class _PendingRecursiveDelete:
    broker: FileMutationBroker
    plan: MutationPlan
    action: ElevationAction


_PENDING_RECURSIVE_DELETES: dict[str, _PendingRecursiveDelete] = {}
_MAX_PENDING_RECURSIVE_DELETES = 256
_DELETE_RISK_RE = re.compile(r"(?i)\b(?:rm|unlink|Remove-Item|ri|del|erase|rmdir|rd)(?:\.exe)?\b")
_DELETE_COMMAND_BASENAMES = frozenset(
    {"del", "erase", "rd", "remove-item", "ri", "rm", "rmdir", "unlink"}
)
_DELETE_WRAPPER_BASENAMES = frozenset(
    {
        "&",
        ".",
        "bash",
        "busybox",
        "call",
        "chrt",
        "cmd",
        "command",
        "env",
        "eval",
        "exec",
        "find",
        "fish",
        "ionice",
        "nice",
        "nohup",
        "parallel",
        "powershell",
        "pwsh",
        "setsid",
        "sh",
        "start",
        "stdbuf",
        "sudo",
        "time",
        "timeout",
        "xargs",
        "zsh",
    }
)
_INERT_DELETE_WORD_COMMANDS = frozenset(
    {
        "basename",
        "cat",
        "dirname",
        "echo",
        "egrep",
        "fgrep",
        "file",
        "grep",
        "head",
        "ls",
        "printf",
        "pwd",
        "stat",
        "tail",
        "type",
        "wc",
        "whence",
        "where",
        "which",
    }
)
_RECURSIVE_DELETE_RISK_RE = re.compile(
    r"(?is)(?:"
    r"\brm(?:\.exe)?\b[^\r\n;&|]*(?:\s-[A-Za-z]*r[A-Za-z]*\b|\s--recursive\b)"
    r"|\b(?:Remove-Item|ri)\b[^\r\n;&|]*\s-Recurse\b"
    r"|\b(?:rmdir|rd)(?:\.exe)?\b[^\r\n;&|]*\s/(?:s|S)\b"
    r")"
)
_DYNAMIC_DELETE_PATH_RE = re.compile(r"[*?\[\]{}$`]|%\w+%")
_WINDOWS_DYNAMIC_VARIABLE_RE = re.compile(r'''%[^%"'\r\n]+%|![^!"'\r\n]+!''')
_WINDOWS_DYNAMIC_EXECUTABLE_RE = re.compile(
    rf'''{_WINDOWS_DYNAMIC_VARIABLE_RE.pattern}|\^'''
)


def _remember_pending_recursive_delete(
    approval_id: str,
    pending: _PendingRecursiveDelete,
) -> None:
    """Keep the exact-action cache bounded against abandoned approvals."""

    key = str(approval_id)
    _PENDING_RECURSIVE_DELETES.pop(key, None)
    _PENDING_RECURSIVE_DELETES[key] = pending
    while len(_PENDING_RECURSIVE_DELETES) > _MAX_PENDING_RECURSIVE_DELETES:
        oldest = next(iter(_PENDING_RECURSIVE_DELETES))
        _PENDING_RECURSIVE_DELETES.pop(oldest, None)


def _delete_target(
    command: str,
    cwd: str,
    *,
    windows: bool | None = None,
) -> tuple[Path | None, bool] | None:
    """Parse one standalone literal shell deletion and its recursive scope."""

    native_windows = os.name == "nt" if windows is None else windows
    analysis_command = strip_shell_heredoc_bodies(command)
    folded_delete = False
    folded_dynamic = False
    if native_windows:
        quote_folded = analysis_command.replace('"', "").replace("'", "")
        folded_delete = bool(
            not _DELETE_RISK_RE.search(analysis_command)
            and _DELETE_RISK_RE.search(quote_folded)
        )
        folded_dynamic = bool(
            not _WINDOWS_DYNAMIC_VARIABLE_RE.search(analysis_command)
            and _WINDOWS_DYNAMIC_VARIABLE_RE.search(quote_folded)
        )

    def _dynamic_executable_token(token: str) -> bool:
        candidate = token.strip()
        if (
            len(candidate) >= 2
            and candidate[0] == candidate[-1]
            and candidate[0] in {"'", '"'}
        ):
            candidate = candidate[1:-1]
        return bool(_DYNAMIC_DELETE_PATH_RE.search(candidate)) or (
            native_windows and bool(_WINDOWS_DYNAMIC_EXECUTABLE_RE.search(candidate))
        )

    try:
        tokens = shlex.split(analysis_command, posix=not native_windows)
    except ValueError:
        return None
    if not tokens:
        return None
    try:
        segments = parse_shell_segments(
            analysis_command if native_windows else command,
            platform="windows" if native_windows else "linux",
        )
    except ValueError:
        segments = ()
    if len(segments) > 1:
        parsed_segments = tuple(
            _delete_target(segment.source, cwd, windows=native_windows)
            for segment in segments
        )
        delete_segments = tuple(item for item in parsed_segments if item is not None)
        if delete_segments:
            return (None, any(recursive for _target, recursive in delete_segments))
        return None
    assignment_index = 0
    if not native_windows:
        while assignment_index < len(tokens) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*",
            tokens[assignment_index],
        ):
            assignment_index += 1
    tokens = tokens[assignment_index:]
    if not tokens:
        return None
    if _dynamic_executable_token(tokens[0]):
        return (None, bool(_RECURSIVE_DELETE_RISK_RE.search(analysis_command)))
    executable = _shell_command_basename(tokens[0])
    if (folded_delete or folded_dynamic) and executable not in _INERT_DELETE_WORD_COMMANDS:
        return (None, bool(_RECURSIVE_DELETE_RISK_RE.search(analysis_command)))
    delete_syntax_present = bool(_DELETE_RISK_RE.search(analysis_command)) or any(
        _DELETE_RISK_RE.search(token)
        or _shell_command_basename(token) in _DELETE_COMMAND_BASENAMES
        for token in tokens
    )
    dynamic_wrapper_payload = executable in _DELETE_WRAPPER_BASENAMES and any(
        _dynamic_executable_token(token) for token in tokens[1:]
    )
    if not delete_syntax_present and not dynamic_wrapper_payload:
        return None
    recursive_hint = bool(_RECURSIVE_DELETE_RISK_RE.search(analysis_command))
    if re.search(r"\$\(|`|[<>]\(", analysis_command):
        return (None, recursive_hint)

    def _nested_command(start: int) -> str | None:
        if start >= len(tokens):
            return None
        return " ".join(shlex.quote(token) for token in tokens[start:])

    def _script_command(start: int) -> str | None:
        if start >= len(tokens):
            return None
        script = " ".join(tokens[start:]).strip()
        if len(script) >= 2 and script[0] == script[-1] and script[0] in {"'", '"'}:
            script = script[1:-1]
        return script or None

    if executable in {"bash", "fish", "sh", "zsh"}:
        if len(tokens) >= 3 and tokens[1] in {"-c", "-lc"}:
            script = tokens[2].strip()
            if len(script) >= 2 and script[0] == script[-1] and script[0] in {"'", '"'}:
                script = script[1:-1]
            return _delete_target(script, cwd, windows=native_windows)
        return (None, recursive_hint)
    if executable in {"powershell", "pwsh"}:
        index = 1
        safe_flags = {"-nologo", "-noninteractive", "-noprofile", "-mta", "-sta"}
        while index < len(tokens):
            folded = tokens[index].casefold()
            if folded in {"-c", "-command"} and index + 1 < len(tokens):
                wrapped_script = _script_command(index + 1)
                return (
                    _delete_target(wrapped_script, cwd, windows=native_windows)
                    if wrapped_script is not None
                    else None
                )
            if folded in safe_flags:
                index += 1
                continue
            return (None, recursive_hint)
        return (None, recursive_hint)
    if executable == "cmd":
        for index, token in enumerate(tokens[1:], start=1):
            option = token.strip("'\"").casefold()
            if option in {"/c", "/k"}:
                wrapped_script = _script_command(index + 1)
                return (
                    _delete_target(wrapped_script, cwd, windows=native_windows)
                    if wrapped_script is not None
                    else None
                )
            if option in {"/d", "/s", "/q", "/a", "/u"} or re.fullmatch(
                r"/[efv]:(?:on|off)", option
            ):
                continue
            break
        return None
    if executable == "command":
        if any(token in {"-v", "-V"} for token in tokens[1:]):
            return None
        index = 1
        while index < len(tokens) and tokens[index] in {"-p", "--"}:
            index += 1
        nested = _nested_command(index)
        return (
            _delete_target(nested, cwd, windows=native_windows)
            if nested is not None
            else None
        )
    if executable == "env":
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token in {"-S", "--split-string"}:
                wrapped_script = _script_command(index + 1)
                return (
                    _delete_target(wrapped_script, cwd, windows=native_windows)
                    if wrapped_script is not None
                    else (
                        None,
                        bool(_RECURSIVE_DELETE_RISK_RE.search(analysis_command)),
                    )
                )
            if token in {"-C", "--chdir"}:
                return (None, recursive_hint)
            if token.startswith("--chdir="):
                return (None, recursive_hint)
            if token in {"-u", "--unset"}:
                if index + 1 >= len(tokens):
                    return (None, recursive_hint)
                index += 2
                continue
            if token.startswith("--unset="):
                index += 1
                continue
            if token in {"-i", "--ignore-environment", "-0", "--null"}:
                index += 1
                continue
            if token == "--":
                index += 1
                break
            if token.startswith("-"):
                return (None, recursive_hint)
            if "=" in token:
                index += 1
                continue
            break
        nested = _nested_command(index)
        return (
            _delete_target(nested, cwd, windows=native_windows)
            if nested is not None
            else None
        )
    if executable == "sudo":
        index = 1
        value_flags = {"-g", "--group", "-h", "--host", "-p", "--prompt", "-u", "--user"}
        safe_flags = {
            "-E",
            "--preserve-env",
            "-H",
            "--set-home",
            "-k",
            "--reset-timestamp",
            "-n",
            "--non-interactive",
            "-S",
            "--stdin",
        }
        while index < len(tokens):
            token = tokens[index]
            if token in {"-D", "--chdir"}:
                return (None, recursive_hint)
            if token.startswith("--chdir="):
                return (None, recursive_hint)
            if token in value_flags:
                if index + 1 >= len(tokens):
                    return (None, recursive_hint)
                index += 2
                continue
            if token.startswith(("--group=", "--host=", "--prompt=", "--user=")):
                index += 1
                continue
            if token in safe_flags or token.startswith("--preserve-env="):
                index += 1
                continue
            if token == "--":
                index += 1
                break
            if token.startswith("-"):
                return (None, recursive_hint)
            break
        nested = _nested_command(index)
        return (
            _delete_target(nested, cwd, windows=native_windows)
            if nested is not None
            else None
        )
    if executable == "busybox":
        nested = _nested_command(1)
        return (
            _delete_target(nested, cwd, windows=native_windows)
            if nested is not None
            else None
        )
    if executable in {"exec", "nohup", "time"}:
        index = 1
        if executable == "time" and index < len(tokens) and tokens[index] == "-p":
            index += 1
        if index < len(tokens) and tokens[index] == "--":
            index += 1
        if index < len(tokens) and tokens[index].startswith("-"):
            return (None, bool(_RECURSIVE_DELETE_RISK_RE.search(analysis_command)))
        nested = _nested_command(index)
        return (
            _delete_target(nested, cwd, windows=native_windows)
            if nested is not None
            else None
        )
    if executable == "nice":
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token in {"-n", "--adjustment"}:
                index += 2
                continue
            if token.startswith("--adjustment=") or re.fullmatch(r"-\d+", token):
                index += 1
                continue
            if token == "--":
                index += 1
            break
        nested = _nested_command(index)
        return (
            _delete_target(nested, cwd, windows=native_windows)
            if nested is not None
            else None
        )
    if executable not in {
        "rm",
        "unlink",
        "remove-item",
        "ri",
        "del",
        "erase",
        "rmdir",
        "rd",
    }:
        try:
            segments = parse_shell_segments(
                analysis_command if native_windows else command,
                platform="windows" if native_windows else "linux",
            )
        except ValueError:
            segments = ()
        if len(segments) > 1 and any(
            _DELETE_RISK_RE.search(segment.source) for segment in segments
        ):
            return (None, bool(_RECURSIVE_DELETE_RISK_RE.search(analysis_command)))
        if executable not in _INERT_DELETE_WORD_COMMANDS:
            return (None, bool(_RECURSIVE_DELETE_RISK_RE.search(analysis_command)))
        return None
    if re.search(r"&&|\|\||[;\r\n|]", analysis_command):
        # Preserve the fact that this is a recognizable deletion while making
        # the unrepresentable target explicit to the caller.
        return (None, bool(_RECURSIVE_DELETE_RISK_RE.search(analysis_command)))
    recursive = bool(_RECURSIVE_DELETE_RISK_RE.search(analysis_command))
    target_tokens: list[str] = []
    operands_only = False
    path_value_expected = False
    rm_directory_allowed = False
    for token in tokens[1:]:
        raw_token = token.strip()
        quoted_token = (
            len(raw_token) >= 2
            and raw_token[0] == raw_token[-1]
            and raw_token[0] in {"'", '"'}
        )
        cleaned = raw_token.strip("'\"")
        folded = cleaned.casefold()
        if path_value_expected:
            if native_windows and "," in cleaned and not quoted_token:
                return (None, recursive)
            target_tokens.append(cleaned)
            path_value_expected = False
            continue
        if not operands_only and cleaned == "--":
            operands_only = True
            continue
        if not operands_only:
            if executable == "rm" and cleaned.startswith("-"):
                if cleaned in {"--force", "--recursive", "--dir"}:
                    recursive = recursive or cleaned == "--recursive"
                    rm_directory_allowed = rm_directory_allowed or cleaned == "--dir"
                    continue
                if not cleaned.startswith("--") and set(cleaned[1:]) <= {
                    "f",
                    "r",
                    "R",
                    "d",
                }:
                    recursive = recursive or "r" in cleaned[1:].lower()
                    rm_directory_allowed = rm_directory_allowed or "d" in cleaned[1:]
                    continue
                return (None, recursive)
            if executable in {"remove-item", "ri"} and cleaned.startswith("-"):
                if folded in {"-force", "-recurse"}:
                    recursive = recursive or folded == "-recurse"
                    continue
                if folded in {"-literalpath", "-path"}:
                    path_value_expected = True
                    continue
                return (None, recursive)
            if executable in {"del", "erase"} and re.fullmatch(
                r"/[^/\\]+", cleaned
            ):
                if re.fullmatch(r"/[FQ]+", cleaned, re.IGNORECASE):
                    continue
                return (None, recursive)
            if executable in {"rmdir", "rd"} and re.fullmatch(
                r"/[^/\\]+", cleaned
            ):
                if re.fullmatch(r"/[SQ]+", cleaned, re.IGNORECASE):
                    continue
                return (None, recursive)
            if executable in {"unlink", "rmdir"} and cleaned.startswith("-"):
                return (None, recursive)
        if native_windows and "," in cleaned and not quoted_token:
            return (None, recursive)
        target_tokens.append(cleaned)
    if path_value_expected or len(target_tokens) != 1:
        return (None, recursive)
    raw_target = target_tokens[0]
    if re.search(r"(?:^|[\\/])\.{1,2}[\\/]*$", raw_target):
        return (None, recursive)
    if _dynamic_executable_token(raw_target):
        return (None, recursive)
    candidate = Path(os.path.expandvars(raw_target)).expanduser()
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    target = candidate.parent.resolve(strict=False) / candidate.name
    # Recursive flags applied to a regular file still describe one file
    # deletion. Symlinks are likewise deleted as links, never traversed.
    recursive = recursive and target.is_dir() and not target.is_symlink()
    if (
        executable in {"rm", "unlink", "del", "erase"}
        and target.is_dir()
        and not target.is_symlink()
        and not recursive
        and not rm_directory_allowed
    ):
        return None
    if executable in {"rmdir", "rd"} and (
        not target.is_dir() or target.is_symlink()
    ):
        return None
    return target, recursive


def _recursive_delete_action(
    command: str,
    cwd: str,
    plan: MutationPlan,
    *,
    without_backup: bool = False,
    backup_enabled: bool = True,
) -> ElevationAction:
    warning = plan.warning or (
        OVERSIZE_BACKUP_WARNING
        if without_backup
        else RECURSIVE_DELETE_WARNING
        if plan.recursive
        else FILE_DELETE_WARNING
    )
    operation_marker = "recursive-delete" if plan.recursive else "file-delete"
    if plan.recursive:
        action_kind = (
            "fs.recursive_delete_without_backup" if without_backup else "fs.recursive_delete"
        )
    else:
        action_kind = "fs.file_delete_without_backup" if without_backup else "fs.file_delete"
    return ElevationAction(
        tool_name="exec_command",
        action_kind=action_kind,
        argv=(operation_marker, str(plan.target)),
        cwd=cwd,
        sandbox_permissions="require_escalated",
        justification=warning,
        target_paths=((str(plan.target), "delete"),),
        content_digest=plan.fingerprint(),
        risk_markers=(
            (operation_marker, "without-backup") if without_backup else (operation_marker,)
        ),
        display=ApprovalDisplay(
            kind="delete",
            target=str(plan.target),
            destructive=True,
            irreversible=without_backup or not backup_enabled,
            backup_state=(
                "unavailable_requires_confirmation"
                if without_backup
                else "enabled"
                if backup_enabled
                else "disabled"
            ),
        ),
    )


def _recursive_delete_approval_envelope(
    gate: Any,
    *,
    warning: str,
    action: ElevationAction,
    recursive: bool,
) -> str:
    display = action.display or ApprovalDisplay(kind="sensitive_operation")
    payload = gate.to_envelope()
    payload.update(
        {
            "warning": warning,
            "target": display.target,
            "recursive": recursive,
            "irreversible": display.irreversible,
            "backup_state": display.backup_state,
        }
    )
    return json.dumps(payload, ensure_ascii=False)


async def _gate_recursive_delete(
    command: str,
    *,
    cwd: str | None,
    approval_id: str | None,
    require_exact: bool = False,
) -> str | None:
    """Approve, back up, and execute one literal shell delete in Safe."""

    if full_host_access_active():
        return None
    effective_cwd = cwd or str(Path.cwd())
    parsed_delete = _delete_target(command, effective_cwd)
    if parsed_delete is None:
        return None
    pending = _PENDING_RECURSIVE_DELETES.get(str(approval_id or ""))
    if approval_id and pending is None:
        return json.dumps(
            {
                "status": "approval_action_mismatch",
                "requested": True,
                "allowed": False,
                "approval_id": approval_id,
                "reason": "approval_action_mismatch",
                "recursive": bool(_RECURSIVE_DELETE_RISK_RE.search(command)),
                "irreversible": True,
            },
            ensure_ascii=False,
        )
    if pending is None:
        target, recursive = parsed_delete
        if target is None:
            return json.dumps(
                {
                    "status": "blocked",
                    "reason": "recursive_delete_target_not_static",
                    "recursive": recursive,
                    "irreversible": True,
                    "message": (
                        f"{RECURSIVE_DELETE_WARNING if recursive else FILE_DELETE_WARNING} "
                        "请把删除改成仅包含一个字面量路径的独立命令，"
                        "OpenSquilla 才能在审批后先备份再删除。"
                    ),
                },
                ensure_ascii=False,
            )
        if not target.exists() and not target.is_symlink():
            # Preserve normal no-op/error semantics for commands such as
            # ``rm -f missing``; there is no current object to approve or back up.
            return None
        context = current_tool_context.get()
        config = getattr(context, "sandbox_gateway_config", None)
        state_dir = str(getattr(config, "state_dir", "") or "").strip()
        policy = active_sandbox_policy()

        def _create_backup_vault() -> BackupVault:
            if not state_dir:
                raise BackupUnavailable(reason="state_dir_unavailable")
            return BackupVault(Path(state_dir) / "backup-vault")

        broker = FileMutationBroker(
            policy=policy,
            vault_factory=_create_backup_vault,
            authority_roots=(authority_roots_for_state(state_dir) if state_dir else ()),
        )
        try:
            plan = await asyncio.to_thread(
                broker.plan_delete,
                target,
                recursive=recursive,
            )
        except MutationDenied as exc:
            return json.dumps(
                {
                    "status": "blocked",
                    "reason": str(exc) or "file_mutation_denied",
                    "target": str(target),
                    "message": "This delete target cannot be approved.",
                },
                ensure_ascii=False,
            )
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            return json.dumps(
                {
                    "status": "blocked",
                    "reason": (
                        "recursive_delete_target_invalid" if recursive else "delete_target_invalid"
                    ),
                    "target": str(target),
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        pending = _PendingRecursiveDelete(
            broker=broker,
            plan=plan,
            action=_recursive_delete_action(
                command,
                effective_cwd,
                plan,
                backup_enabled=policy.files.recursive_delete_backup_enabled,
            ),
        )
    context = current_tool_context.get()
    gate = gate_elevated_action(
        pending.action,
        approval_id=approval_id,
        session_key=getattr(context, "session_key", None),
        # This approval authorizes a brokered sandbox action, not host
        # execution.  The real Safe profile keeps authority reads denied.
        file_system_profile=FileSystemPermissionProfile.full_access(),
    )
    if not gate.allowed:
        gate_status = str(getattr(gate, "status", "approval_required"))
        if approval_id and gate_status in {
            "approval_denied",
            "approval_invalid",
            "approval_action_mismatch",
            "approval_session_mismatch",
        }:
            _PENDING_RECURSIVE_DELETES.pop(approval_id, None)
        elif gate.approval_id:
            _remember_pending_recursive_delete(gate.approval_id, pending)
        return _recursive_delete_approval_envelope(
            gate,
            warning=pending.plan.warning
            or (RECURSIVE_DELETE_WARNING if pending.plan.recursive else FILE_DELETE_WARNING),
            action=pending.action,
            recursive=pending.plan.recursive,
        )
    if approval_id:
        _PENDING_RECURSIVE_DELETES.pop(approval_id, None)
    plan = pending.plan
    if plan.backup_override_token is None:
        plan = pending.broker.approve(plan)
    try:
        result = await asyncio.to_thread(pending.broker.execute, plan)
    except BackupUnavailable as exc:
        override = pending.broker.approve_without_backup(pending.plan, exc)
        override_pending = _PendingRecursiveDelete(
            broker=pending.broker,
            plan=override,
            action=_recursive_delete_action(
                command,
                effective_cwd,
                override,
                without_backup=True,
            ),
        )
        second_gate = gate_elevated_action(
            override_pending.action,
            approval_id=None,
            session_key=getattr(context, "session_key", None),
            file_system_profile=FileSystemPermissionProfile.full_access(),
        )
        if second_gate.approval_id:
            _remember_pending_recursive_delete(
                second_gate.approval_id,
                override_pending,
            )
        return _recursive_delete_approval_envelope(
            second_gate,
            warning=override.warning
            or (
                OVERSIZE_BACKUP_WARNING.format(
                    size_bytes=exc.size_bytes,
                    quota_bytes=exc.quota_bytes,
                )
                if isinstance(exc, BackupTooLarge)
                else UNAVAILABLE_BACKUP_WARNING
            ),
            action=override_pending.action,
            recursive=override_pending.plan.recursive,
        )
    except ObjectIdentityChanged as exc:
        return json.dumps(
            {
                "status": "blocked",
                "reason": (
                    "recursive_delete_target_changed" if plan.recursive else "delete_target_changed"
                ),
                "target": str(plan.target),
                "message": str(exc),
            },
            ensure_ascii=False,
        )
    except OSError as exc:
        return json.dumps(
            {
                "status": "blocked",
                "reason": (
                    "recursive_delete_backup_failed" if plan.recursive else "delete_backup_failed"
                ),
                "target": str(plan.target),
                "message": str(exc),
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "status": "deleted",
            "target": str(plan.target),
            "recursive": plan.recursive,
            "backup": (
                summarize_backup_receipts((result.backup,))[0]
                if result.backup is not None
                else None
            ),
        },
        ensure_ascii=False,
    )


def _gate_safe_command_approval(
    command: str,
    *,
    cwd: str | None,
    reason: str,
    approval_id: str | None,
) -> str | None:
    effective_cwd = cwd or str(Path.cwd())
    action = ElevationAction(
        tool_name="exec_command",
        action_kind="sandbox.command",
        argv=("safe-command", command),
        cwd=effective_cwd,
        sandbox_permissions="require_escalated",
        justification=reason,
        content_digest=hashlib.sha256(command.encode("utf-8")).hexdigest(),
        risk_markers=("safe-command-approval",),
        display=ApprovalDisplay(kind="run_command", target=command),
    )
    context = current_tool_context.get()
    gate = gate_elevated_action(
        action,
        approval_id=approval_id,
        session_key=getattr(context, "session_key", None),
        # The approval is consumed only as an exact user decision; execution
        # remains inside Safe, so denied-read roots are not being bypassed.
        file_system_profile=FileSystemPermissionProfile.full_access(),
    )
    return None if gate.allowed else json.dumps(gate.to_envelope(), ensure_ascii=False)


def _base_shell_environment() -> dict[str, str]:
    ctx = current_tool_context.get()
    if ctx is not None and ctx.guest_safe:
        return _runtime_shell_environment(
            dict(ctx.environment or {}),
            require_bundled=True,
        )
    return _runtime_shell_environment(dict(os.environ))


def _guest_requires_managed_runtime() -> bool:
    context = current_tool_context.get()
    return bool(context is not None and context.guest_safe)


def _managed_skill_environment(environment: dict[str, str]) -> dict[str, str]:
    """Keep strict Guest PATH limited to verified Runtime Packs."""

    if _guest_requires_managed_runtime():
        return dict(environment)
    return managed_skill_env(environment)


def _direct_runtime_command(
    command: str,
    *,
    windows: bool | None = None,
) -> tuple[str, str] | None:
    """Return ``(component, executable)`` for one unwrapped direct runtime call."""

    native_windows = os.name == "nt" if windows is None else windows
    platform_name = "windows" if native_windows else "linux"
    try:
        segments = parse_shell_segments(command, platform=platform_name)
        if len(segments) != 1 or segments[0].source.strip() != command.strip():
            return None
        tokens = shlex.split(segments[0].source, posix=not native_windows)
    except ValueError:
        return None
    if not tokens:
        return None
    executable = tokens[0].strip().strip("'\"")
    if not executable or any(marker in executable for marker in ("/", "\\", ":", "$", "`")):
        return None
    name = _shell_command_basename(executable)
    if name in {"python", "python3"}:
        return ("python", executable)
    if name in {"node", "npm", "npx"}:
        return ("node", executable)
    if native_windows and name in {"git", "bash"}:
        return ("gitBash", executable)
    return None


def _strict_runtime_unavailable_envelope(
    command: str,
    environment: dict[str, str],
) -> dict[str, object] | None:
    """Classify a missing direct runtime without guessing about compound shell code."""

    if not _guest_requires_managed_runtime():
        return None
    runtime_command = _direct_runtime_command(command)
    if runtime_command is None:
        return None
    component_id, executable = runtime_command
    path_key = next((key for key in environment if key.casefold() == "path"), "PATH")
    if shutil.which(executable, path=environment.get(path_key, "")) is not None:
        return None
    try:
        from opensquilla.runtime_packs import status_snapshot

        status = status_snapshot()
        component = next(
            (
                item
                for item in status.components
                if item.component_id == component_id
            ),
            None,
        )
        runtime_policy = active_sandbox_policy().runtimes
        enabled = bool(
            runtime_policy.enabled
            and {
                "python": runtime_policy.python,
                "node": runtime_policy.node,
                "gitBash": runtime_policy.git_bash,
            }[component_id]
        )
        if enabled and component is not None and component.availability.value == "ready":
            return None
    except (OSError, RuntimeError, ValueError):
        pass
    return {
        "status": "failed",
        "code": "RUNTIME_UNAVAILABLE",
        "componentId": component_id,
        "retryable": False,
        "message": (
            f"The managed {component_id} runtime is unavailable for strict execution."
        ),
    }


def _runtime_shell_environment(
    environment: dict[str, str],
    *,
    require_bundled: bool = False,
) -> dict[str, str]:
    try:
        return apply_bundled_runtime_path(
            environment,
            mode=current_run_mode() or "safe",
            policy=active_sandbox_policy().runtimes,
            require_bundled=require_bundled,
        )
    except RuntimeManifestError as exc:
        raise ToolError(str(exc)) from exc
_SANDBOX_NETWORK_HINT = (
    "Hint: sandboxed shell/code has no direct network. Use sandbox_network approval "
    "or Safe mode network access, then retry the shell command through the "
    "managed proxy. Do not switch to separate web download tools for package "
    "installs unless the user explicitly asks for an offline workaround."
)
_SANDBOX_NETWORK_DISABLED_HINT = (
    "Hint: sandboxed shell/code has no direct network because sandbox "
    "network_default is configured as 'none'. Set [sandbox] "
    'network_default = "proxy_allowlist" in the gateway config and restart '
    "the gateway to enable the managed proxy. Do not switch to separate web "
    "download tools for package installs unless the user explicitly asks for "
    "an offline workaround."
)
_SANDBOX_NETWORK_HINT_PREFIX = "Hint: sandboxed shell/code has no direct network"
_SANDBOX_NETWORK_FAILURE_MARKERS: tuple[str, ...] = (
    "could not resolve host",
    "could not resolve proxy",
    "temporary failure in name resolution",
    "name or service not known",
    "getaddrinfo failed",
    "network is unreachable",
    "nodename nor servname provided",
    "name resolution failed",
    "failed to resolve",
    "curl: (6)",
)
_SHELL_NULL_REDIRECT_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s;|&]))\d*[<>]{1,2}\s*(?:/dev/null|nul:?)(?=$|[\s;|&])",
    re.IGNORECASE,
)
_WINDOWS_DOS_DEVICE_NAMES = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "nul",
        "prn",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)
_PROTECTED_METADATA_NAMES = frozenset({".git", ".codex", ".agents"})
_WINDOWS_ABSOLUTE_PATH_IN_SCRIPT_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]",
    re.IGNORECASE,
)
_WINDOWS_POSIX_TMP_QUOTED_RE = re.compile(r"(?P<quote>['\"])(?P<path>/tmp(?:/[^'\"]*)?)(?P=quote)")
_WINDOWS_POSIX_TMP_BARE_RE = re.compile(
    r"(?<![A-Za-z0-9_./:\\-])(?P<path>/tmp(?:/[^\s'\";&|<>)]*)?)"
)
_WINDOWS_ROOT_TMP_QUOTED_RE = re.compile(
    r"(?P<quote>['\"])(?P<path>(?:[A-Za-z]:[\\/]|[\\/])tmp(?:[\\/][^'\"]*)?)(?P=quote)",
    re.IGNORECASE,
)
_WINDOWS_ROOT_TMP_BARE_RE = re.compile(
    r"(?<![A-Za-z0-9_./:\\-])"
    r"(?P<path>(?:[A-Za-z]:[\\/]|[\\/])tmp(?:[\\/][^\s'\";&|<>)]*)?)",
    re.IGNORECASE,
)
_WINDOWS_SHELL_ARG_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')
_WINDOWS_SHELL_PATH_FLAGS = frozenset(
    {
        "-destination",
        "-filepath",
        "-literalpath",
        "-name",
        "-path",
    }
)
_WINDOWS_SHELL_VALUE_FLAGS = frozenset(
    {
        "-encoding",
        "-erroraction",
        "-errorvariable",
        "-ev",
        "-exclude",
        "-filter",
        "-include",
        "-inputobject",
        "-itemtype",
        "-outvariable",
        "-ov",
        "-stream",
        "-type",
        "-value",
        "-warningaction",
        "-warningvariable",
    }
)
_WINDOWS_SHELL_CREATE_COMMANDS = frozenset({"md", "mkdir", "new-item", "ni"})
_WINDOWS_SHELL_CONTENT_COMMANDS = frozenset({"add-content", "out-file", "set-content"})
_WINDOWS_SHELL_REMOVE_COMMANDS = frozenset({"del", "erase", "remove-item", "rm"})
_WINDOWS_SHELL_READ_COMMANDS = frozenset(
    {
        "cat",
        "dir",
        "gc",
        "gci",
        "get-childitem",
        "get-content",
        "ls",
        "test-path",
        "type",
    }
)
_WINDOWS_CMD_PATHLESS_SWITCHES = frozenset(
    {
        "/a",
        "/b",
        "/c",
        "/d",
        "/l",
        "/n",
        "/o",
        "/p",
        "/q",
        "/r",
        "/s",
        "/t",
        "/w",
        "/x",
    }
)
_MASKED_PIPELINE_FAILURE_WARNING = (
    "[shell_warning:masked_pipeline_failure]\n"
    "This command returned exit_code=0, but the output contains failure markers "
    "and the command uses a shell pipeline. The pipeline may have hidden an "
    "upstream failure. Treat this result as failed; rerun without the pipe or "
    "with pipefail before relying on it."
)
_PIPELINE_RE = re.compile(r"(?<!\|)\|(?!\|)")
_PIPELINE_FAILURE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"BUILD FAILURE", re.IGNORECASE),
    re.compile(r"FAILURES!!!", re.IGNORECASE),
    re.compile(r"(?:^|\n)FAILED\s+\S+", re.IGNORECASE),
    re.compile(r"(?:^|\n)FAIL\s+\S+", re.IGNORECASE),
    re.compile(r"\b\d+\s+failed(?:,|\s|$)", re.IGNORECASE),
    re.compile(r"FAILED \((?:failures|errors)=\d+", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*(?:Test Suites|Tests):\s+\d+\s+failed", re.IGNORECASE),
    re.compile(r"Failed to execute goal", re.IGNORECASE),
    re.compile(r"Traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*(?:/bin/)?(?:ba)?sh: .*: not found", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*command not found", re.IGNORECASE),
    re.compile(r"No such file or directory", re.IGNORECASE),
    re.compile(r"cannot find symbol", re.IGNORECASE),
    re.compile(r"Compilation failed", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*error\[[A-Z0-9]+\]:", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*\[ERROR\]\s+", re.IGNORECASE),
)
_SHELL_SOURCE_EXTENSIONS = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".cs",
        ".go",
        ".h",
        ".hh",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".m",
        ".mm",
        ".php",
        ".py",
        ".pyi",
        ".rb",
        ".rs",
        ".scala",
        ".swift",
        ".ts",
        ".tsx",
    }
)
_SHELL_SOURCE_MUTATION_REQUIRED_TOOLS = frozenset(
    {"read_source", "edit_source", "source_symbols", "exec_command"}
)
_SHELL_SOURCE_MUTATION_FORBIDDEN_TOOLS = frozenset(
    {"read_file", "write_file", "edit_file", "apply_patch", "execute_code"}
)
_PYTHON_OPEN_WRITE_PATH_RE = re.compile(
    r"open\(\s*(['\"])(?P<path>[^'\"]+)\1\s*,\s*(['\"])[wa][^'\"]*\3",
    re.DOTALL,
)
_PYTHON_PATH_WRITE_PATH_RE = re.compile(
    r"Path\(\s*(['\"])(?P<path>[^'\"]+)\1\s*\)\.(?:write_text|write_bytes)\(",
    re.DOTALL,
)
PROCESS_ACTIONS: frozenset[str] = frozenset(
    {"eof", "kill", "list", "log", "poll", "remove", "submit", "wait", "write"}
)

# Background process session store
_bg_sessions: dict[str, _BgSession] = {}


@dataclass
class _BgSession:
    session_id: str
    command: str
    process: asyncio.subprocess.Process
    process_tree: ProcessTreeOwner | None = None
    session_key: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    is_owner_run: bool = False
    local_urls: list[str] = field(default_factory=list)
    output_bytes: bytearray = field(default_factory=bytearray)
    output_lines: list[str] = field(default_factory=list)
    done: bool = False
    timed_out: bool = False
    killed: bool = False
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    returncode: int | None = None
    collector_task: asyncio.Task[None] | None = None
    cleanup_callbacks: list[Callable[[], None]] = field(default_factory=list)
    async_cleanup_callbacks: list[Callable[[], Awaitable[None]]] = field(default_factory=list)


@dataclass(frozen=True)
class _SpawnedBackgroundProcess:
    process: asyncio.subprocess.Process
    process_tree: ProcessTreeOwner
    cleanup_callbacks: list[Callable[[], None]] = field(default_factory=list)
    async_cleanup_callbacks: list[Callable[[], Awaitable[None]]] = field(default_factory=list)


def _audit_command(command: str) -> str:
    if len(command) <= _COMMAND_AUDIT_MAX_CHARS:
        return command
    return command[:_COMMAND_AUDIT_MAX_CHARS] + "...[truncated]"


def _looks_like_sandbox_network_failure(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _SANDBOX_NETWORK_FAILURE_MARKERS)


def _append_sandbox_network_hint(text: str, *, force: bool = False) -> str:
    if _SANDBOX_NETWORK_HINT_PREFIX in text:
        return text
    if not force and not _looks_like_sandbox_network_failure(text):
        return text
    return text.rstrip() + "\n" + _sandbox_network_hint() + "\n"


def _sandbox_network_hint() -> str:
    runtime = get_runtime()
    settings = getattr(runtime, "settings", None) if runtime is not None else None
    if getattr(settings, "network_default", None) == "none":
        return _SANDBOX_NETWORK_DISABLED_HINT
    return _SANDBOX_NETWORK_HINT


def _profile_shell_command(
    command: str,
    *,
    workdir: str | None = None,
) -> OperationProfile:
    profile = classify_command(("sh", "-lc", command))
    script_profile = _profile_referenced_powershell_file(command, workdir=workdir)
    if script_profile is None:
        return profile
    if profile.host_effect is not None:
        return profile
    if (
        script_profile.host_effect is not None
        or script_profile.needs_network
        or script_profile.requested_paths
        or script_profile.requested_write_paths
        or script_profile.high_impact
    ):
        return script_profile
    return profile


def _level_hints_for_shell_profile(
    profile: OperationProfile,
    *,
    warnlist_matched: bool = False,
) -> LevelHints:
    trusted_warnlist_auto_handled = warnlist_matched and trusted_sandbox_active()
    return LevelHints(
        needs_network=profile.needs_network,
        high_impact=profile.high_impact and not trusted_warnlist_auto_handled,
    )


def _looks_like_masked_pipeline_failure(command: str, returncode: int | None, output: str) -> bool:
    if returncode != 0:
        return False
    if _MASKED_PIPELINE_FAILURE_WARNING in output:
        return False
    if not _PIPELINE_RE.search(command):
        return False
    if "||" in command:
        return False
    return any(pattern.search(output) for pattern in _PIPELINE_FAILURE_MARKERS)


def _append_masked_pipeline_failure_warning(
    command: str,
    returncode: int | None,
    output: str,
) -> str:
    if not _looks_like_masked_pipeline_failure(command, returncode, output):
        return output
    if output:
        return f"{_MASKED_PIPELINE_FAILURE_WARNING}\n{output}"
    return _MASKED_PIPELINE_FAILURE_WARNING + "\n"


def _append_patch_hygiene_warning(command: str, cwd: str | None, output: str) -> str:
    paths = _git_paths_from_shell_result(command, output)
    if not paths or cwd is None:
        return output
    repo = Path(cwd).expanduser().resolve(strict=False)
    resolved_paths = [
        candidate if candidate.is_absolute() else repo / candidate
        for raw in paths
        if (candidate := Path(raw.replace("\\", "/")))
    ]
    warning = summarize_patch_hygiene_warning(resolved_paths)
    if not warning or warning in output:
        return output
    return f"{warning}\n{output}"


def _git_paths_from_shell_result(command: str, output: str) -> list[str]:
    lowered = command.casefold()
    if "git diff" in lowered:
        if "--name-only" in lowered:
            return _git_name_only_paths(output)
        return _git_diff_paths(output)
    if "git status" in lowered:
        return _git_status_paths(output)
    return []


def _git_diff_paths(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        if not line.startswith("diff --git "):
            continue
        try:
            paths.append(line.split(" b/", 1)[1])
        except IndexError:
            continue
    return paths


def _git_name_only_paths(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith(("fatal:", "warning:", "error:")):
            continue
        paths.append(candidate)
    return paths


def _git_status_paths(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        if not line or line.startswith("##") or len(line) < 4:
            continue
        candidate = line[3:].strip()
        if " -> " in candidate:
            candidate = candidate.rsplit(" -> ", 1)[1]
        if candidate:
            paths.append(candidate)
    return paths


def _sandbox_effectively_off() -> bool:
    runtime = get_runtime()
    effective = getattr(runtime, "effective", None) if runtime is not None else None
    return runtime is None or not bool(getattr(effective, "sandbox_enabled", False))


def _context_run_mode() -> str | None:
    return current_run_mode()


def _context_elevated_mode() -> str | None:
    """Legacy compatibility: only Full Host Access counts as elevated."""
    return "full" if full_host_access_active() else None


def _host_execution_allowed() -> bool:
    if full_host_access_active():
        return True
    runtime = get_runtime()
    effective = getattr(runtime, "effective", None) if runtime is not None else None
    return runtime is not None and not bool(getattr(effective, "sandbox_enabled", False))


def _profile_referenced_powershell_file(
    command: str,
    *,
    workdir: str | None = None,
) -> OperationProfile | None:
    script_path = _referenced_powershell_file(command, workdir=workdir)
    if script_path is None:
        return None
    with contextlib.suppress(OSError, UnicodeError):
        if script_path.suffix.lower() not in {".ps1", ".psm1"}:
            return None
        with script_path.open("r", encoding="utf-8", errors="replace") as handle:
            script = handle.read(_POWERSHELL_SCRIPT_PROFILE_MAX_CHARS)
        if not script.strip():
            return None
        normalized_script = re.sub(r"[\r\n]+", ";", script)
        return classify_command(("powershell", "-NoProfile", "-Command", normalized_script))
    return None


def _referenced_powershell_file(
    command: str,
    *,
    workdir: str | None = None,
) -> Path | None:
    tokens = _windows_shell_tokens(command)
    for index, token in enumerate(tokens):
        if _shell_command_basename(token) not in {"powershell", "pwsh"}:
            continue
        cursor = index + 1
        while cursor < len(tokens):
            option = tokens[cursor].lower()
            if option in {"-command", "-c", "-encodedcommand", "-ec"}:
                break
            if option in {"-file", "-f"}:
                if cursor + 1 >= len(tokens):
                    return None
                return _resolve_shell_script_path(tokens[cursor + 1], workdir=workdir)
            cursor += 1
    return None


def _resolve_shell_script_path(raw_path: str, *, workdir: str | None = None) -> Path:
    cleaned = raw_path.strip().strip("'\"")
    expanded = os.path.expandvars(os.path.expanduser(cleaned))
    path = Path(expanded)
    if not path.is_absolute():
        base = Path(workdir).expanduser() if workdir else Path.cwd()
        path = base / path
    workspace = _workspace_root_for_path_access()
    alias = resolve_workspace_alias(path, workspace)
    if alias is not None:
        path = alias
    return path.resolve(strict=False)


def _referenced_powershell_file_digest(
    command: str,
    *,
    workdir: str | None = None,
) -> tuple[str, str] | None:
    script_path = _referenced_powershell_file(command, workdir=workdir)
    if script_path is None:
        return None
    try:
        digest = hashlib.sha256()
        with script_path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                digest.update(chunk)
    except OSError:
        return None
    return str(script_path), digest.hexdigest()


_SCRIPT_INTERPRETER_INLINE_FLAGS: dict[str, frozenset[str]] = {
    "shell": frozenset({"-c", "--command"}),
    "python": frozenset({"-c", "-m"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "ruby": frozenset({"-e"}),
    "perl": frozenset({"-e", "-E"}),
    "php": frozenset({"-r"}),
}
_SCRIPT_INTERPRETER_OPTIONS_WITH_VALUES: dict[str, frozenset[str]] = {
    "python": frozenset({"-W", "-X", "--check-hash-based-pycs"}),
    "node": frozenset({"--require", "-r", "--loader", "--import"}),
    "ruby": frozenset({"-I", "-r"}),
    "perl": frozenset({"-I", "-M", "-m"}),
    "php": frozenset({"-d", "-c"}),
}


def _script_interpreter_family(executable: str) -> str | None:
    name = _shell_command_basename(executable)
    if name in {"sh", "bash", "dash", "fish", "ksh", "zsh"}:
        return "shell"
    if re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|pypy\d*)", name):
        return "python"
    if name in {"node", "nodejs", "deno", "bun"}:
        return "node"
    if name in {"ruby", "jruby"}:
        return "ruby"
    if name == "perl":
        return "perl"
    if name == "php":
        return "php"
    return None


def _script_argument_for_interpreter(
    tokens: list[str],
    *,
    family: str,
) -> str | None:
    inline_flags = _SCRIPT_INTERPRETER_INLINE_FLAGS.get(family, frozenset())
    options_with_values = _SCRIPT_INTERPRETER_OPTIONS_WITH_VALUES.get(family, frozenset())
    cursor = 1
    while cursor < len(tokens):
        token = tokens[cursor]
        if token == "--":
            return tokens[cursor + 1] if cursor + 1 < len(tokens) else None
        if token in inline_flags:
            return None
        if family == "shell" and token.startswith("-") and "c" in token[1:]:
            return None
        if family == "python" and token.startswith(("-c", "-m")):
            return None
        if token in options_with_values:
            cursor += 2
            continue
        if token.startswith("-"):
            cursor += 1
            continue
        return token
    return None


def _script_file_digest(
    raw_path: str,
    *,
    interpreter: str,
    workdir: str | None,
) -> dict[str, str | None]:
    script_path = _resolve_shell_script_path(raw_path, workdir=workdir)
    digest_value: str | None = None
    try:
        digest = hashlib.sha256()
        with script_path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                digest.update(chunk)
        digest_value = digest.hexdigest()
    except OSError:
        pass
    return {
        "interpreter": interpreter,
        "path": str(script_path),
        "sha256": digest_value,
    }


def _referenced_script_file_digests(
    command: str,
    *,
    workdir: str | None = None,
) -> list[dict[str, str | None]]:
    referenced: list[dict[str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for tokens in _iter_shell_command_tokens(command):
        if not tokens:
            continue
        family = _script_interpreter_family(tokens[0])
        raw_path = (
            _script_argument_for_interpreter(tokens, family=family) if family is not None else None
        )
        interpreter = tokens[0]
        if raw_path is None and family is None:
            executable = tokens[0].strip().strip("'\"")
            if executable.startswith(("/", "./", "../", "~/", "\\", ".\\")):
                raw_path = executable
                interpreter = "direct"
        if raw_path is None:
            continue
        record = _script_file_digest(
            raw_path,
            interpreter=interpreter,
            workdir=workdir,
        )
        key = (str(record["interpreter"]), str(record["path"]))
        if key not in seen:
            seen.add(key)
            referenced.append(record)
    return referenced


def _shell_command_basename(value: str) -> str:
    name = value.strip().strip("'\"").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _approval_command_pattern_class(command: str, settings: Any) -> str | None:
    first_allow: str | None = None
    for variant in shell_command_approval_variants(command):
        pattern_class = classify_approval_command(
            variant,
            settings.allow_patterns,
            settings.deny_patterns,
        )
        if pattern_class == "deny":
            return "deny"
        if pattern_class == "allow" and first_allow is None:
            first_allow = "allow"
    return first_allow


def _approval_policy_denial(
    tool_name: str,
    command: str,
    warning: str,
) -> dict[str, object] | None:
    settings = get_approval_queue().get_settings()
    pattern_class = _approval_command_pattern_class(command, settings)
    if pattern_class != "deny":
        return None
    log.warning(
        "shell_approval_denied_pattern",
        command=_audit_command(command),
        tool=tool_name,
    )
    return {
        "status": "approval_denied",
        "approval_id": "",
        "command": command,
        "warning": warning,
        "message": "This command was denied by the active approval policy.",
    }


def _without_shell_null_redirections(command: str) -> str:
    return _SHELL_NULL_REDIRECT_RE.sub(" ", command)


def _workdir_is_configured_workspace(workdir: str | None) -> bool:
    if not workdir:
        return False
    ctx = current_tool_context.get()
    workspace_dir = getattr(ctx, "workspace_dir", None) if ctx is not None else None
    if not workspace_dir:
        return False
    try:
        cwd = Path(workdir).expanduser().resolve(strict=False)
        workspace = Path(workspace_dir).expanduser().resolve(strict=False)
        return cwd == workspace or workspace in cwd.parents
    except (OSError, RuntimeError):
        return False


def _sensitive_payload_block(tool_name: str, text: str) -> str | None:
    from opensquilla.tools.builtin.web import (
        _sensitive_body_block,
        _sensitive_body_marker,
        _sensitive_url_marker,
    )

    for token in text.split():
        stripped = token.strip("'\"")
        if stripped.startswith(("http://", "https://")):
            marker = _sensitive_url_marker(stripped)
            if marker is not None:
                return _sensitive_body_block(tool_name, marker)
    marker = _sensitive_body_marker(text)
    if marker is not None:
        return _sensitive_body_block(tool_name, marker)
    return None


def _iter_stdin_guard_chunks(text: str) -> Iterator[str]:
    if len(text) <= _EXEC_STDIN_GUARD_CHUNK_CHARS:
        yield text
        return
    step = _EXEC_STDIN_GUARD_CHUNK_CHARS - _EXEC_STDIN_GUARD_OVERLAP_CHARS
    start = 0
    while start < len(text):
        end = min(len(text), start + _EXEC_STDIN_GUARD_CHUNK_CHARS)
        yield text[start:end]
        if end >= len(text):
            break
        start += step


_SENSITIVE_HTTP_UPLOAD_RE = re.compile(
    r"\b(?:curl(?:\.exe)?|wget(?:\.exe)?)\b[^\n]*(?:"
    r"--upload-file\b|(?:^|\s)-T(?:\s|$)|--data(?:-ascii|-binary|-raw|-urlencode)?\b|"
    r"(?:^|\s)-d(?:\s|$)|--form\b|(?:^|\s)-F(?:\s|$)|--post-file\b|--body-file\b)|"
    r"\b(?:invoke-webrequest|invoke-restmethod|iwr|irm)\b[^\n]*(?:"
    r"-infile\b|-body\b|-method\s+(?:post|put|patch)\b)",
    flags=re.IGNORECASE,
)


def _looks_like_sensitive_copy_upload(command: str, marker: str) -> bool:
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False
    executable = Path(tokens[0].strip("'\"")).name.casefold()
    if executable not in {"scp", "scp.exe", "sftp", "sftp.exe", "rsync", "rsync.exe"}:
        return False
    operands = [token.strip("'\"") for token in tokens[1:] if not token.startswith("-")]
    if len(operands) < 2:
        return False
    destination = operands[-1]
    remote_destination = bool(re.match(r"^(?:[^@\s:]+@)?[^\s:/\\]+:.+", destination))
    if not remote_destination:
        return False
    from opensquilla.sandbox.sensitive_paths import sensitive_path_in_text

    return any(sensitive_path_in_text(source) == marker for source in operands[:-1])


def _sensitive_external_transfer_block(
    tool_name: str,
    command: str,
    *,
    workdir: str | None = None,
    stdin: str | None = None,
) -> str | None:
    """Block only high-confidence attempts to send local secrets externally."""

    if _context_elevated_mode() == "full":
        return None
    from opensquilla.sandbox.sensitive_paths import sensitive_path_in_text
    from opensquilla.tools.builtin.web import _sensitive_body_marker

    checked_command = _without_shell_null_redirections(command)
    ctx = current_tool_context.get()
    workspace = ctx.workspace_dir if ctx is not None else None
    checked_text = f"{workdir} {checked_command}" if workdir else checked_command
    marker = sensitive_path_in_text(checked_text, workspace=workspace)
    outbound = bool(_SENSITIVE_HTTP_UPLOAD_RE.search(checked_command))
    if marker is not None and (
        outbound or _looks_like_sensitive_copy_upload(checked_command, marker)
    ):
        return json.dumps(
            {
                "status": "blocked",
                "reason": "sensitive_external_transfer",
                "tool": tool_name,
                "sensitive_path": marker,
            },
            ensure_ascii=False,
        )
    if not outbound or stdin is None:
        return None
    for chunk in _iter_stdin_guard_chunks(stdin):
        marker = sensitive_path_in_text(chunk, workspace=workspace)
        payload_marker = _sensitive_body_marker(chunk)
        if marker is None and payload_marker is None:
            continue
        payload: dict[str, str] = {
            "status": "blocked",
            "reason": "sensitive_external_transfer",
            "tool": tool_name,
        }
        if marker is not None:
            payload["sensitive_path"] = marker
        else:
            payload["sensitive_payload"] = str(payload_marker)
        return json.dumps(payload, ensure_ascii=False)
    return None


def _sensitive_shell_block(
    tool_name: str,
    command: str,
    *,
    workdir: str | None = None,
    stdin: str | None = None,
) -> str | None:
    if _context_elevated_mode() == "full" or _sandbox_path_access_enabled():
        return None

    from opensquilla.sandbox.sensitive_paths import build_block_envelope, sensitive_path_in_text

    checked_command = _without_shell_null_redirections(command)
    include_workdir = bool(workdir) and not _workdir_is_configured_workspace(workdir)
    checked_text = f"{workdir} {checked_command}" if include_workdir else checked_command
    ctx = current_tool_context.get()
    workspace = ctx.workspace_dir if ctx is not None else None
    marker = sensitive_path_in_text(checked_text, workspace=workspace)
    if marker is not None:
        return json.dumps(
            build_block_envelope(checked_text, marker, tool_name=tool_name),
            ensure_ascii=False,
        )

    payload_block = _sensitive_payload_block(tool_name, checked_text)
    if payload_block is not None:
        return payload_block
    if stdin is None:
        return None

    for stdin_chunk in _iter_stdin_guard_chunks(stdin):
        marker = sensitive_path_in_text(stdin_chunk, workspace=workspace)
        if marker is not None:
            return json.dumps(
                build_block_envelope(
                    f"{checked_command}\n[stdin omitted]",
                    marker,
                    tool_name=tool_name,
                ),
                ensure_ascii=False,
            )
    for stdin_chunk in _iter_stdin_guard_chunks(stdin):
        payload_block = _sensitive_payload_block(tool_name, stdin_chunk)
        if payload_block is not None:
            return payload_block
    return None


def _workspace_lockdown_roots() -> list[Path]:
    ctx = current_tool_context.get()
    if ctx is None or not ctx.workspace_lockdown:
        return []
    roots: list[Path] = []
    if ctx.workspace_dir:
        roots.append(Path(ctx.workspace_dir).expanduser().resolve(strict=False))
    if ctx.scratch_dir:
        roots.append(Path(ctx.scratch_dir).expanduser().resolve(strict=False))
    return roots


def _path_inside_any_root(path: Path, roots: list[Path]) -> bool:
    candidate = path.expanduser().resolve(strict=False)
    for root in roots:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _path_access_required_envelope(
    decision: MountDecision,
    *,
    approval_id: str | None = None,
) -> dict[str, object] | None:
    ctx = current_tool_context.get()
    workspace_root = _workspace_root_for_path_access()
    approval = build_path_approval_params(
        decision,
        session_key=getattr(ctx, "session_key", None) if ctx is not None else None,
        workspace=str(workspace_root) if workspace_root is not None else None,
    )
    if approval is None:
        return {
            "status": "path_access_required",
            "path": decision.normalized_path,
            "access": decision.access,
            "message": _path_access_message(workspace_root),
        }
    return request_sandbox_approval(
        approval,
        approval_id=approval_id,
        message=_path_access_message(workspace_root),
        denied_message=_path_access_denied_message(workspace_root),
    )


def _path_access_message(workspace_root: Path | None) -> str:
    workspace = str(workspace_root) if workspace_root is not None else "the configured workspace"
    return (
        f"The requested path is outside the current workspace ({workspace}). "
        "Ask the user whether to add this path as read-only or read/write access."
    )


def _path_access_denied_message(workspace_root: Path | None) -> str:
    workspace = str(workspace_root) if workspace_root is not None else "the configured workspace"
    return (
        "The user denied access outside the current workspace. "
        "Do not ask for the same access again in this turn. "
        "Explain that the requested path cannot be inspected from the current "
        f"workspace ({workspace}) unless the user approves access or changes run mode. "
        "Do not substitute details from other repositories or prior comparison context."
    )


def _path_access_blocked_envelope(decision: MountDecision) -> dict[str, object]:
    ctx = current_tool_context.get()
    if ctx is not None and bool(getattr(ctx, "guest_safe", False)):
        reason = (
            "GUEST_WRITE_OUTSIDE_DEFAULT_WORKSPACE"
            if decision.access == "rw"
            else "GUEST_SENSITIVE_PATH_DENIED"
        )
        return {
            "status": "blocked",
            "reason": reason,
            "path": decision.normalized_path,
            "message": (
                "Web guest mode can modify only the configured default workspace."
                if decision.access == "rw"
                else "Web guest mode cannot read credential or authority data."
            ),
        }
    return {
        "status": "blocked",
        "reason": decision.reason,
        "path": decision.normalized_path,
        "message": decision.reason,
    }


def _guest_path_access_block(
    decision: MountDecision,
) -> dict[str, object] | None:
    ctx = current_tool_context.get()
    if ctx is None or not bool(getattr(ctx, "guest_safe", False)):
        return None
    return _path_access_blocked_envelope(
        dataclasses.replace(decision, status="blocked", reason="guest_boundary")
    )


def _guest_host_execution_block(
    sandbox_permissions: str,
) -> dict[str, object] | None:
    ctx = current_tool_context.get()
    if (
        sandbox_permissions != "require_escalated"
        or ctx is None
        or not bool(getattr(ctx, "guest_safe", False))
    ):
        return None
    return {
        "status": "blocked",
        "reason": "GUEST_HOST_EXECUTION_DENIED",
        "message": (
            "Web guest permissions cannot be elevated to host execution. "
            "Authenticate with an authorized token first."
        ),
    }


def _sandbox_path_access_enabled() -> bool:
    runtime = get_runtime()
    if runtime is None or not runtime.effective.sandbox_enabled:
        return False
    return not full_host_access_active()


def _workspace_root_for_path_access() -> Path | None:
    ctx = current_tool_context.get()
    if ctx is not None and ctx.workspace_dir:
        return Path(ctx.workspace_dir).expanduser().resolve(strict=False)
    runtime = get_runtime()
    runtime_workspace = getattr(runtime, "workspace", None) if runtime is not None else None
    if runtime_workspace is not None:
        return Path(runtime_workspace).expanduser().resolve(strict=False)
    return None


def _windows_sandbox_backend_active(runtime: object | None = None) -> bool:
    runtime = get_runtime() if runtime is None else runtime
    backend = getattr(runtime, "backend", None) if runtime is not None else None
    backend_name = str(getattr(backend, "name", "") or "")
    return backend_name.startswith("windows_")


def _windows_session_slug() -> str:
    ctx = current_tool_context.get()
    raw = str(getattr(ctx, "session_key", None) or "default")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-._")
    if not slug:
        return "default"
    return slug[:80]


def _windows_session_tmp_root() -> Path | None:
    workspace = _workspace_root_for_path_access()
    if workspace is None:
        return None
    return (workspace / ".opensquilla" / "tmp" / _windows_session_slug()).resolve(strict=False)


def _windows_tmp_tail(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    lower = normalized.lower()
    if lower == "/tmp" or lower.startswith("/tmp/"):
        return normalized[4:].lstrip("/")
    match = re.match(r"^[A-Za-z]:/tmp(?:/(.*))?$", normalized, re.IGNORECASE)
    if match:
        return match.group(1) or ""
    return None


def _windows_translate_tmp_path(path: str) -> str:
    tail = _windows_tmp_tail(path)
    if tail is None:
        return path
    root = _windows_session_tmp_root()
    if root is None:
        return path
    mapped = root.joinpath(*[part for part in tail.split("/") if part]) if tail else root
    mapped.parent.mkdir(parents=True, exist_ok=True)
    if not tail:
        mapped.mkdir(parents=True, exist_ok=True)
    return str(mapped)


def _windows_translate_posix_tmp_path(path: str) -> str:
    return _windows_translate_tmp_path(path)


def _windows_translate_tmp_references(command: str) -> str:
    def replace_quoted(match: re.Match[str]) -> str:
        quote = match.group("quote")
        return f"{quote}{_windows_translate_tmp_path(match.group('path'))}{quote}"

    translated = _WINDOWS_POSIX_TMP_QUOTED_RE.sub(replace_quoted, command)
    translated = _WINDOWS_ROOT_TMP_QUOTED_RE.sub(replace_quoted, translated)
    translated = _WINDOWS_POSIX_TMP_BARE_RE.sub(
        lambda match: _windows_translate_tmp_path(match.group("path")),
        translated,
    )
    return _WINDOWS_ROOT_TMP_BARE_RE.sub(
        lambda match: _windows_translate_tmp_path(match.group("path")),
        translated,
    )


def _windows_translate_posix_tmp_references(command: str) -> str:
    return _windows_translate_tmp_references(command)


def _apply_windows_session_tmp_env(env: dict[str, str]) -> None:
    root = _windows_session_tmp_root()
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    value = str(root)
    env["TEMP"] = value
    env["TMP"] = value
    env["TMPDIR"] = value


def _append_windows_app_alias_path(
    env: dict[str, str],
    *,
    runtime: object | None = None,
) -> None:
    # WindowsApps is a host-controlled executable-alias directory.  Strict
    # Guest execution must keep PATH limited to receipt-verified Runtime Packs,
    # even when winget or a Store Python alias is present for the desktop user.
    if _guest_requires_managed_runtime():
        return
    if os.name != "nt" and not _windows_sandbox_backend_active(runtime):
        return
    candidates: list[Path] = []
    local_appdata = env.get("LOCALAPPDATA") or os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "Microsoft" / "WindowsApps")
    else:
        userprofile = env.get("USERPROFILE") or os.environ.get("USERPROFILE")
        if userprofile:
            candidates.append(Path(userprofile) / "AppData" / "Local" / "Microsoft" / "WindowsApps")

    existing_key = next((key for key in env if key.casefold() == "path"), "PATH")
    existing_value = env.get(existing_key, "")
    existing_entries = [part.strip() for part in existing_value.split(";") if part.strip()]
    existing_keys = {
        str(Path(part).expanduser().resolve(strict=False)).casefold() for part in existing_entries
    }
    additions: list[str] = []
    for candidate in candidates:
        if not (candidate / "winget.exe").exists():
            continue
        resolved = candidate.expanduser().resolve(strict=False)
        key = str(resolved).casefold()
        if key in existing_keys:
            continue
        existing_keys.add(key)
        additions.append(str(resolved))
    if not additions:
        return
    env[existing_key] = ";".join([*existing_entries, *additions])


def _sandbox_shell_policy_cwd(cwd: str | None) -> Path | None:
    workspace = _workspace_root_for_path_access()
    if workspace is not None:
        return workspace
    if cwd:
        return Path(cwd).expanduser().resolve(strict=False)
    return None


def _trusted_windows_cmd_path() -> str:
    comspec = os.environ.get("COMSPEC", "")
    if _is_absolute_cmd_exe(comspec):
        return comspec
    system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or ""
    if system_root and "\x00" not in system_root and ntpath.isabs(system_root):
        return ntpath.join(system_root, "System32", "cmd.exe")
    return r"C:\Windows\System32\cmd.exe"


def _is_absolute_cmd_exe(path: str) -> bool:
    return "\x00" not in path and ntpath.isabs(path) and ntpath.basename(path).lower() == "cmd.exe"


def _trusted_windows_powershell_path() -> str:
    system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or ""
    if system_root and "\x00" not in system_root and ntpath.isabs(system_root):
        return ntpath.join(
            system_root,
            "System32",
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe",
        )
    return r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


_WINDOWS_POWERSHELL_PROXY_PRELUDE = r"""
$__opensquillaProxy = $env:HTTPS_PROXY;
if ([string]::IsNullOrWhiteSpace($__opensquillaProxy)) {
    $__opensquillaProxy = $env:HTTP_PROXY
};
if (-not [string]::IsNullOrWhiteSpace($__opensquillaProxy)) {
    $PSDefaultParameterValues['Invoke-WebRequest:Proxy'] = $__opensquillaProxy;
    $PSDefaultParameterValues['Invoke-RestMethod:Proxy'] = $__opensquillaProxy;
    [System.Net.WebRequest]::DefaultWebProxy = [System.Net.WebProxy]::new($__opensquillaProxy);
    [System.Net.WebRequest]::DefaultWebProxy.Credentials = `
        [System.Net.CredentialCache]::DefaultCredentials
};
""".strip()


def _windows_with_powershell_proxy_defaults(command: str) -> str:
    prelude = _WINDOWS_POWERSHELL_PROXY_PRELUDE
    command = command.strip()
    if not command:
        return prelude
    return f"{prelude.rstrip(';')}; {command}"


def _windows_direct_powershell_argv(command: str) -> tuple[str, ...]:
    return (
        _trusted_windows_powershell_path(),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        _windows_with_powershell_proxy_defaults(command),
    )


def _windows_shell_host_argv(
    command: str,
    *,
    cwd: Path | str | None = None,
) -> tuple[str, ...]:
    argv = [
        sys.executable,
        "-c",
        _WINDOWS_SANDBOX_SHELL_HOST_CODE,
        _trusted_windows_powershell_path(),
        _windows_powershell_compat_command(command),
    ]
    if cwd is not None:
        cwd_text = str(cwd)
        argv.append(cwd_text)
        argv.append(str(Path(cwd_text) / ".opensquilla-cache" / "shell-host"))
    return tuple(argv)


_WINDOWS_SANDBOX_SHELL_HOST_CODE = r"""
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request

_REMOVE_ITEM_RE = re.compile(
    r"^(?:Remove-Item|rm|del|erase)\b(?P<rest>.*)$",
    re.IGNORECASE,
)
_TEST_PATH_RE = re.compile(
    r"^Test-Path\b(?P<rest>.*)$",
    re.IGNORECASE,
)
_INVOKE_PYTHON_RE = re.compile(
    r"^Invoke-OpenSquillaPythonProcess\s+"
    r"-FilePath\s+'(?P<path>(?:''|[^'])*)'\s+"
    r"-Arguments\s+@\((?P<args>.*)\)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_PATH_TOKEN_RE = re.compile(
    r"(?:-(?:LiteralPath|Path)\s+)?(?P<quote>['\"])(?P<path>.*?)(?P=quote)",
    re.IGNORECASE,
)
_EXPLICIT_BARE_PATH_RE = re.compile(
    r"-(?:LiteralPath|Path)\s+(?P<path>(?!['\"])[^\s;{}]+)",
    re.IGNORECASE,
)
_ARG_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')
_OUTPUT_RE = re.compile(r"^(?:Write-Output|echo)\s+(?P<text>.+)$", re.IGNORECASE)
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_SELECT_STATUS_CODE_RE = re.compile(
    r"\bSelect(?:-Object)?\s+-ExpandProperty\s+StatusCode\b",
    re.IGNORECASE,
)
_STATUS_CODE_WRITE_OUTPUT_RE = re.compile(
    r"\b(?:Write-Output|echo)\s+\(\s*"
    r"(?P<quote>['\"])(?P<prefix>.*?)(?P=quote)\s*\+\s*"
    r"\$\w+\.StatusCode\s*\)",
    re.IGNORECASE | re.DOTALL,
)
_WRITE_OUTPUT_STATUS_CODE_RE = re.compile(
    r"\b(?:Write-Output|echo)\s+\$\w+\.StatusCode\b",
    re.IGNORECASE,
)
_SELECT_HTTP_LINE_RE = re.compile(
    r"\bSelect-String\b.*(?P<quote>['\"])HTTP/(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_ICMP_SHELL_COMMAND_RE = re.compile(
    r"(?<![\w.-])(?:pathping|tracert|ping)(?:\.exe)?(?![\w.-])",
    re.IGNORECASE,
)
_IF_REMOVE_RE = re.compile(
    r"^if\s*\(.*?Test-Path.+?\)\s*\{\s*(?P<remove>Remove-Item\b.+?)\s*\}$",
    re.IGNORECASE | re.DOTALL,
)
_TRY_CATCH_RE = re.compile(
    r"^\s*try\s*\{\s*(?P<body>.*?)\s*\}\s*catch\s*\{.*\}\s*$",
    re.IGNORECASE | re.DOTALL,
)
_ICMP_TOOL_NAMES = {"pathping", "ping", "tracert"}
_ICMP_POWERSHELL_PATTERNS = (
    "test-connection",
    "test-netconnection",
    "system.net.networkinformation.ping",
    "networkinformation.ping",
)
_VALUE_FLAGS = {
    "-credential",
    "-ea",
    "-erroraction",
    "-ev",
    "-errorvariable",
    "-exclude",
    "-filter",
    "-include",
    "-ov",
    "-outvariable",
    "-stream",
    "-wa",
    "-warningaction",
    "-wv",
    "-warningvariable",
}


def _strip_outer_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _looks_like_path(value):
    return bool(
        re.match(r"^[A-Za-z]:[\\/]", value)
        or value.startswith("\\\\")
        or value.startswith(".\\")
        or value.startswith("./")
        or value.startswith("\\")
        or value.startswith("/")
    )


def _split_statements(script):
    statements = []
    current = []
    quote = ""
    escaped = False
    for char in script:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "`":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            current.append(char)
            quote = char
            continue
        if char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            continue
        current.append(char)
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def _nested_powershell_command(command):
    match = re.match(
        r"^\s*powershell(?:\.exe)?\b(?P<args>.*)$",
        command,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    args = match.group("args")
    command_match = re.search(
        r"(?:^|\s)-(?:Command|c)\s+(?P<script>.+)$",
        args,
        re.IGNORECASE | re.DOTALL,
    )
    if not command_match:
        return None
    return _strip_outer_quotes(command_match.group("script"))


def _remove_statement_path(statement):
    match = _REMOVE_ITEM_RE.match(statement)
    if not match:
        return None
    return _statement_path_from_rest(match.group("rest"))


def _test_path_statement_path(statement):
    match = _TEST_PATH_RE.match(statement)
    if not match:
        return None
    return _statement_path_from_rest(match.group("rest"))


def _statement_path_from_rest(rest):
    path_match = _PATH_TOKEN_RE.search(rest)
    if not path_match:
        explicit_bare = _EXPLICIT_BARE_PATH_RE.search(rest)
        if explicit_bare:
            return explicit_bare.group("path")
        skip_next = False
        for token in _ARG_TOKEN_RE.findall(rest):
            token = _strip_outer_quotes(token)
            folded = token.lower()
            if skip_next:
                skip_next = False
                continue
            if folded in _VALUE_FLAGS:
                skip_next = True
                continue
            if token.startswith("-"):
                continue
            if _looks_like_path(token):
                return token
        return None
    return path_match.group("path")


def _if_remove_statement_path(statement):
    match = _IF_REMOVE_RE.match(statement)
    if not match:
        return None
    return _remove_statement_path(match.group("remove"))


def _output_statement_text(statement):
    match = _OUTPUT_RE.match(statement)
    if not match:
        return None
    text = match.group("text")
    if re.search(r"[<>|&]", text):
        return None
    if re.search(r"[$(){}\[\]+]", text):
        return None
    return _strip_outer_quotes(text)


def _remove_path(path, *, recurse, force):
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            if recurse:
                shutil.rmtree(path)
            else:
                os.rmdir(path)
        else:
            if force:
                try:
                    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
                except OSError:
                    pass
            os.remove(path)
    except FileNotFoundError:
        return None
    except Exception as exc:
        return f"{path}: {type(exc).__name__}: {exc}"
    return None


def _handle_simple_delete_script(script):
    statements = _split_statements(script)
    if not statements:
        return None
    operations = []
    recurse = False
    force = False
    for statement in statements:
        path = _remove_statement_path(statement)
        if path is not None:
            recurse = recurse or bool(re.search(r"\s-Recurse\b", statement, re.IGNORECASE))
            force = force or bool(re.search(r"\s-Force\b", statement, re.IGNORECASE))
            operations.append(("remove", path))
            continue
        path = _if_remove_statement_path(statement)
        if path is not None:
            recurse = recurse or bool(re.search(r"\s-Recurse\b", statement, re.IGNORECASE))
            force = force or bool(re.search(r"\s-Force\b", statement, re.IGNORECASE))
            operations.append(("remove", path))
            continue
        output = _output_statement_text(statement)
        if output is not None:
            operations.append(("output", output))
            continue
        path = _test_path_statement_path(statement)
        if path is not None:
            operations.append(("test_path", path))
            continue
        return None
    errors = [
        error
        for operation, value in operations
        if operation == "remove"
        if (error := _remove_path(value, recurse=recurse, force=force))
    ]
    if errors:
        sys.stderr.write("\n".join(errors))
        return 1
    for operation, value in operations:
        if operation == "output":
            print(value)
        elif operation == "test_path":
            print("True" if os.path.exists(value) else "False")
    return 0


def _ps_single_quote(value):
    return "'" + value.replace("'", "''") + "'"


def _python_process_prelude():
    target = _ps_single_quote(sys.executable)
    return (
        "function ConvertTo-OpenSquillaNativeArgumentLine { "
        "param([string[]]$Arguments) "
        "$quoted = foreach ($arg in $Arguments) { "
        "$value = [string]$arg; "
        "if ($value.Length -eq 0) { '\"\"' } "
        "elseif ($value -notmatch '[\\s\"]') { $value } "
        "else { '\"' + (($value -replace '\\\\', '\\\\') -replace '\"', '\\\"') + '\"' } "
        "}; "
        "$quoted -join ' ' "
        "}; "
        "function Invoke-OpenSquillaPythonProcess { "
        "param([Parameter(Mandatory=$true)][string]$FilePath, [string[]]$Arguments = @()) "
        "$argumentLine = ConvertTo-OpenSquillaNativeArgumentLine -Arguments $Arguments; "
        "$psi = New-Object System.Diagnostics.ProcessStartInfo; "
        "$psi.FileName = $FilePath; "
        "$psi.Arguments = $argumentLine; "
        "$psi.WorkingDirectory = (Get-Location).Path; "
        "$psi.UseShellExecute = $false; "
        "$psi.RedirectStandardOutput = $true; "
        "$psi.RedirectStandardError = $true; "
        "$process = New-Object System.Diagnostics.Process; "
        "$process.StartInfo = $psi; "
        "[void]$process.Start(); "
        "$stdout = $process.StandardOutput.ReadToEnd(); "
        "$stderr = $process.StandardError.ReadToEnd(); "
        "$process.WaitForExit(); "
        "if ($stdout) { [Console]::Out.Write($stdout) }; "
        "if ($stderr) { [Console]::Error.Write($stderr) }; "
        "$global:LASTEXITCODE = $process.ExitCode; "
        "if ($process.ExitCode -ne 0) { "
        "Write-Error ('Python process exited with code ' + $process.ExitCode) "
        "} "
        "}; "
        "function python { "
        f"Invoke-OpenSquillaPythonProcess -FilePath {target} -Arguments $args "
        "}; "
        "function python3 { "
        f"Invoke-OpenSquillaPythonProcess -FilePath {target} -Arguments $args "
        "}; "
        f"function py {{ Invoke-OpenSquillaPythonProcess -FilePath {target} -Arguments $args }}; "
    )


def _with_python_aliases(command):
    return _python_process_prelude() + command


def _python_sitecustomize_source():
    return r'''
import os
import subprocess
import tempfile

if os.name == "nt" and os.environ.get("OPENSQUILLA_WINDOWS_APPCONTAINER_TEMPFILE_PATCH") == "1":
    def _opensquilla_mkdtemp(suffix=None, prefix=None, dir=None):
        sanitized = tempfile._sanitize_params(prefix, suffix, dir)
        prefix, suffix, dir = sanitized[:3]
        names = tempfile._get_candidate_names()
        for _ in range(tempfile.TMP_MAX):
            name = next(names)
            path = os.path.join(dir, prefix + name + suffix)
            try:
                os.mkdir(path)
            except FileExistsError:
                continue
            return os.path.abspath(path)
        raise FileExistsError(tempfile._errno.EEXIST, "No usable temporary directory name found")

    tempfile.mkdtemp = _opensquilla_mkdtemp

    _opensquilla_check_output = subprocess.check_output

    def _opensquilla_patched_check_output(*popenargs, **kwargs):
        env = kwargs.get("env")
        site_dir = os.environ.get("OPENSQUILLA_WINDOWS_APPCONTAINER_SITE_DIR")
        if isinstance(env, dict) and site_dir:
            env = dict(env)
            env["OPENSQUILLA_WINDOWS_APPCONTAINER_TEMPFILE_PATCH"] = "1"
            env["OPENSQUILLA_WINDOWS_APPCONTAINER_SITE_DIR"] = site_dir
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = site_dir + (os.pathsep + existing if existing else "")
            kwargs["env"] = env
        return _opensquilla_check_output(*popenargs, **kwargs)

    subprocess.check_output = _opensquilla_patched_check_output
'''.strip()


def _prepare_python_sitecustomize(tmp):
    if not tmp:
        return ""
    site_dir = os.path.join(tmp, "opensquilla-python-sitecustomize")
    os.makedirs(site_dir, exist_ok=True)
    sitecustomize = os.path.join(site_dir, "sitecustomize.py")
    with open(sitecustomize, "w", encoding="utf-8") as handle:
        handle.write(_python_sitecustomize_source())
        handle.write("\n")
    return site_dir


def _split_ps_single_quoted_array(raw):
    args = []
    index = 0
    while index < len(raw):
        while index < len(raw) and raw[index] in " \t\r\n,":
            index += 1
        if index >= len(raw):
            break
        if raw[index] != "'":
            return None
        index += 1
        value = []
        while index < len(raw):
            char = raw[index]
            if char == "'":
                if index + 1 < len(raw) and raw[index + 1] == "'":
                    value.append("'")
                    index += 2
                    continue
                index += 1
                break
            value.append(char)
            index += 1
        else:
            return None
        args.append("".join(value))
        while index < len(raw) and raw[index] in " \t\r\n":
            index += 1
        if index < len(raw):
            if raw[index] != ",":
                return None
            index += 1
    return args


def _env_with_python_sitecustomize(site_dir):
    env = os.environ.copy()
    if not site_dir:
        return env
    env["OPENSQUILLA_WINDOWS_APPCONTAINER_TEMPFILE_PATCH"] = "1"
    env["OPENSQUILLA_WINDOWS_APPCONTAINER_SITE_DIR"] = site_dir
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = site_dir + (os.pathsep + existing if existing else "")
    return env


def _handle_python_process_script(script, cwd, site_dir):
    match = _INVOKE_PYTHON_RE.match(script.strip())
    if match is None:
        return None
    args = _split_ps_single_quoted_array(match.group("args"))
    if args is None:
        return None
    executable = match.group("path").replace("''", "'")
    result = subprocess.run(
        [executable, *args],
        cwd=cwd or None,
        env=_env_with_python_sitecustomize(site_dir),
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


def _host_command_name(token):
    name = os.path.basename(token).lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return name


def _host_tokens(script):
    return [_strip_outer_quotes(token) for token in _ARG_TOKEN_RE.findall(script)]


def _host_executable_index(tokens):
    return 1 if tokens and tokens[0] == "&" and len(tokens) > 1 else 0


def _strip_assignment_tokens(tokens):
    if len(tokens) >= 3 and tokens[0].startswith("$") and tokens[1] == "=":
        return tokens[2:]
    return tokens


def _proxy_allowlist_active():
    return (
        os.environ.get("OPENSQUILLA_SANDBOX_NETWORK", "").lower() == "proxy_allowlist"
        or os.environ.get("CODEX_NETWORK_PROXY_ACTIVE") == "1"
    )


def _proxy_allowlist_icmp_block_reason(script):
    if not _proxy_allowlist_active():
        return None
    lowered_script = script.lower()
    if any(pattern in lowered_script for pattern in _ICMP_POWERSHELL_PATTERNS):
        return "windows_default PROXY_ALLOWLIST blocks PowerShell ICMP diagnostics"
    for statement in _split_statements(script):
        tokens = _host_tokens(statement)
        if not tokens:
            continue
        executable_index = _host_executable_index(tokens)
        if executable_index >= len(tokens):
            continue
        command = _host_command_name(tokens[executable_index])
        if command in _ICMP_TOOL_NAMES:
            return "windows_default PROXY_ALLOWLIST blocks ICMP diagnostic tools"
        if command in {"cmd", "powershell", "pwsh"} and _ICMP_SHELL_COMMAND_RE.search(
            " ".join(tokens[executable_index + 1 :])
        ):
            return "windows_default PROXY_ALLOWLIST blocks ICMP diagnostic tools"
    return None


def _windowsapps_alias_path(path):
    return "\\microsoft\\windowsapps" in os.path.normpath(path).lower()


def _direct_tool_candidates(command):
    if command in {"npm", "npx", "pnpm", "yarn"}:
        return (f"{command}.cmd", f"{command}.exe")
    if command == "git":
        return ("git.exe", "git.cmd")
    if command == "node":
        return ("node.exe",)
    return ()


def _which_exact(candidate):
    if os.path.dirname(candidate):
        if os.path.isfile(candidate):
            yield candidate
        return
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        path = os.path.join(directory.strip('"'), candidate)
        if os.path.isfile(path):
            yield path


def _resolve_direct_tool(command):
    for candidate in _direct_tool_candidates(command):
        for path in _which_exact(candidate):
            if _windowsapps_alias_path(path):
                continue
            return path
    return None


def _handle_direct_tool_script(script, cwd, site_dir):
    statements = _split_statements(script)
    if len(statements) != 1:
        return None
    tokens = _host_tokens(statements[0])
    if not tokens:
        return None
    executable_index = _host_executable_index(tokens)
    if executable_index >= len(tokens):
        return None
    command = _host_command_name(tokens[executable_index])
    executable = _resolve_direct_tool(command)
    if executable is None:
        return None
    result = subprocess.run(
        [executable, *tokens[executable_index + 1 :]],
        cwd=cwd or None,
        env=_env_with_python_sitecustomize(site_dir),
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


def _http_proxy_for_url(url):
    if url.lower().startswith("https://"):
        return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    return os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")


def _managed_http_open(method, url, timeout):
    proxy_url = _http_proxy_for_url(url)
    if not proxy_url:
        return None
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    request = urllib.request.Request(url, method=method.upper())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    try:
        body = response.read()
        status = int(getattr(response, "status", getattr(response, "code", 0)) or 0)
        reason = str(getattr(response, "reason", "") or "")
        headers = list(response.headers.items())
        return status, reason, headers, body
    finally:
        response.close()


def _option_value(tokens, names, default=None):
    folded = {name.lower() for name in names}
    for index, token in enumerate(tokens):
        if token.lower() in folded and index + 1 < len(tokens):
            return tokens[index + 1]
    return default


def _http_url_from_tokens(tokens):
    uri = _option_value(tokens, {"-Uri", "-Url"})
    if isinstance(uri, str) and _HTTP_URL_RE.match(uri):
        return uri
    for token in tokens[1:]:
        if _HTTP_URL_RE.match(token):
            return token
    return None


def _status_code_output_prefix(script):
    match = _STATUS_CODE_WRITE_OUTPUT_RE.search(script)
    if match is not None:
        return match.group("prefix")
    return None


def _writes_plain_status_code(script):
    return bool(_WRITE_OUTPUT_STATUS_CODE_RE.search(script))


def _handle_managed_invoke_webrequest(script, output_script=""):
    command, _pipe, pipeline = script.partition("|")
    tokens = _host_tokens(command)
    tokens = _strip_assignment_tokens(tokens)
    if not tokens or _host_command_name(tokens[0]) not in {"invoke-webrequest", "iwr", "wget"}:
        return None
    url = _http_url_from_tokens(tokens)
    if not url:
        return None
    method = str(_option_value(tokens, {"-Method"}, "GET"))
    timeout_raw = _option_value(tokens, {"-TimeoutSec"}, "30")
    try:
        timeout = max(1, int(timeout_raw))
    except (TypeError, ValueError):
        timeout = 30
    result = _managed_http_open(method, url, timeout)
    if result is None:
        return None
    status, _reason, _headers, body = result
    output_prefix = _status_code_output_prefix(output_script)
    if output_prefix is not None:
        sys.stdout.write(f"{output_prefix}{status}\n")
    elif _SELECT_STATUS_CODE_RE.search(pipeline) or _writes_plain_status_code(output_script):
        sys.stdout.write(f"{status}\n")
    else:
        sys.stdout.write(body.decode("utf-8", "replace"))
    return 0


def _handle_managed_curl(script, output_script=""):
    tokens = _host_tokens(script)
    tokens = _strip_assignment_tokens(tokens)
    if not tokens or _host_command_name(tokens[0]) != "curl":
        return None
    url = _http_url_from_tokens(tokens)
    if not url:
        return None
    head = "-I" in tokens or "--head" in tokens
    result = _managed_http_open("HEAD" if head else "GET", url, 30)
    if result is None:
        return None
    status, reason, headers, body = result
    status_line = f"HTTP/1.1 {status} {reason}".rstrip()
    if _SELECT_HTTP_LINE_RE.search(script) and re.search(
        r"\b(?:Write-Output|echo)\s+\$\w+\b",
        output_script,
        re.IGNORECASE,
    ):
        sys.stdout.write(status_line + "\n")
        return 0
    sys.stdout.write(status_line + "\r\n")
    for name, value in headers:
        sys.stdout.write(f"{name}: {value}\r\n")
    sys.stdout.write("\r\n")
    if not head:
        sys.stdout.buffer.write(body)
    return 0


def _unwrap_try_catch_script(script):
    match = _TRY_CATCH_RE.match(script)
    if match is None:
        return None
    return match.group("body").strip()


def _handle_managed_http_script(script):
    candidates = [script]
    try_body = _unwrap_try_catch_script(script)
    if try_body:
        candidates.append(try_body)
    for candidate in candidates:
        statements = _split_statements(candidate)
        if len(statements) > 1:
            command_statement = statements[0]
            output_script = "; ".join(statements[1:])
            for handler in (_handle_managed_invoke_webrequest, _handle_managed_curl):
                result = handler(command_statement, output_script)
                if result is not None:
                    return result
        for handler in (_handle_managed_invoke_webrequest, _handle_managed_curl):
            result = handler(candidate)
            if result is not None:
                return result
    return None


def _with_sandbox_environment(command, cwd, tmp, python_site_dir):
    prelude = _powershell_proxy_prelude()
    if tmp:
        quoted_tmp = _ps_single_quote(tmp)
        prelude += (
            f"$env:TEMP = {quoted_tmp}; "
            f"$env:TMP = {quoted_tmp}; "
            f"$env:TMPDIR = {quoted_tmp}; "
        )
    if python_site_dir:
        quoted_site_dir = _ps_single_quote(python_site_dir)
        prelude += (
            "$env:OPENSQUILLA_WINDOWS_APPCONTAINER_TEMPFILE_PATCH = '1'; "
            f"$env:OPENSQUILLA_WINDOWS_APPCONTAINER_SITE_DIR = {quoted_site_dir}; "
            "if ($env:PYTHONPATH) { "
            f"$env:PYTHONPATH = {quoted_site_dir} + ';' + $env:PYTHONPATH "
            "} "
            f"else {{ $env:PYTHONPATH = {quoted_site_dir} }}; "
        )
    if not cwd:
        return prelude + command
    quoted_cwd = _ps_single_quote(cwd)
    return prelude + (
        f"try {{ Set-Location -LiteralPath {quoted_cwd} -ErrorAction Stop }} "
        f"catch {{ Write-Error $_; exit 1 }}; {command}"
    )


def _powershell_proxy_prelude():
    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    if not proxy:
        return ""
    quoted_proxy = _ps_single_quote(proxy)
    return (
        f"$__opensquillaProxy = {quoted_proxy}; "
        "if (-not [string]::IsNullOrWhiteSpace($__opensquillaProxy)) { "
        "$PSDefaultParameterValues['Invoke-WebRequest:Proxy'] = $__opensquillaProxy; "
        "$PSDefaultParameterValues['Invoke-RestMethod:Proxy'] = $__opensquillaProxy; "
        "[System.Net.WebRequest]::DefaultWebProxy = "
        "[System.Net.WebProxy]::new($__opensquillaProxy); "
        "[System.Net.WebRequest]::DefaultWebProxy.Credentials = "
        "[System.Net.CredentialCache]::DefaultCredentials "
        "}; "
    )


def _with_final_exit_code(command):
    return (
        f"{command}; "
        "if ($global:LASTEXITCODE -is [int] -and $global:LASTEXITCODE -ne 0) "
        "{ exit $global:LASTEXITCODE }; "
        "if (-not $?) { exit 1 }"
    )


def main():
    if len(sys.argv) not in {3, 4, 5}:
        sys.stderr.write("windows sandbox shell host expects powershell path and command")
        return 2
    powershell = sys.argv[1]
    command = sys.argv[2]
    cwd = sys.argv[3] if len(sys.argv) >= 4 else ""
    tmp = sys.argv[4] if len(sys.argv) == 5 else ""
    python_site_dir = _prepare_python_sitecustomize(tmp)
    nested_command = _nested_powershell_command(command)
    effective_command = nested_command if nested_command is not None else command
    icmp_block_reason = _proxy_allowlist_icmp_block_reason(effective_command)
    if icmp_block_reason is not None:
        sys.stderr.write(icmp_block_reason + "\n")
        return 2
    direct_tool_result = _handle_direct_tool_script(effective_command, cwd, python_site_dir)
    if direct_tool_result is not None:
        return direct_tool_result
    managed_http_result = _handle_managed_http_script(effective_command)
    if managed_http_result is not None:
        return managed_http_result
    remove_result = _handle_simple_delete_script(effective_command)
    if remove_result is not None:
        return remove_result
    python_process_result = _handle_python_process_script(
        effective_command,
        cwd,
        python_site_dir,
    )
    if python_process_result is not None:
        return python_process_result

    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _with_final_exit_code(
                _with_sandbox_environment(
                    _with_python_aliases(effective_command),
                    cwd,
                    tmp,
                    python_site_dir,
                )
            ),
        ],
        check=False,
    )
    return result.returncode


raise SystemExit(main())
""".strip()


def _sandbox_shell_backend_argv(
    command: str,
    runtime: object,
    *,
    cwd: Path | str | None = None,
) -> tuple[str, ...]:
    backend = getattr(runtime, "backend", None)
    backend_name = getattr(backend, "name", "")
    if backend_name.startswith("windows_"):
        return _windows_shell_host_argv(command, cwd=cwd)
    return ("sh", "-lc", command)


def _sandbox_shell_backend_cwd(cwd: str | None, request: SandboxRequest) -> Path:
    if cwd:
        return Path(cwd).expanduser().resolve(strict=False)
    return request.cwd


async def _run_backend_with_managed_network(
    request: SandboxRequest,
    *,
    runtime: SandboxRuntime | None,
) -> SandboxResult:
    if getattr(request.policy, "network", None) is not NetworkMode.PROXY_ALLOWLIST:
        return await run_under_backend(request, runtime=runtime)
    managed_network = await prepare_subprocess_managed_network_proxy(
        request,
        runtime=runtime,
    )
    try:
        return await run_under_backend(managed_network.request, runtime=runtime)
    finally:
        await managed_network.cleanup()


def _trusted_managed_network_policy(
    policy: SandboxPolicy,
    runtime: object | None,
) -> SandboxPolicy:
    if getattr(policy, "network", None) is NetworkMode.PROXY_ALLOWLIST:
        return policy
    settings = getattr(runtime, "settings", None) if runtime is not None else None
    if getattr(settings, "network_default", None) != "proxy_allowlist":
        return policy
    if not trusted_sandbox_active():
        return policy
    ctx = current_tool_context.get()
    if getattr(policy, "network", None) is NetworkMode.NONE and (
        ctx is None or getattr(ctx, "sandbox_run_context", None) is None
    ):
        return policy
    if not isinstance(policy, SandboxPolicy):
        return policy
    return dataclasses.replace(policy, network=NetworkMode.PROXY_ALLOWLIST, network_proxy=None)


_trusted_windows_managed_network_policy = _trusted_managed_network_policy


def _windows_strip_outer_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _windows_shell_tokens(script: str) -> list[str]:
    return [_windows_strip_outer_quotes(token) for token in _WINDOWS_SHELL_ARG_RE.findall(script)]


def _windows_split_logical_and(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    index = 0
    while index < len(script):
        char = script[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        if char == "`":
            current.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"":
            current.append(char)
            quote = char
            index += 1
            continue
        if char == "&" and index + 1 < len(script) and script[index + 1] == "&":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            index += 2
            continue
        current.append(char)
        index += 1
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def _windows_ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_ps_array_literal(values: list[str]) -> str:
    if not values:
        return "@()"
    return "@(" + ",".join(_windows_ps_single_quote(value) for value in values) + ")"


def _windows_python_executable_token(token: str) -> bool:
    command = _windows_shell_command_name(token)
    if command not in {"python", "python3", "pythonw"}:
        return False
    return any(separator in token for separator in ("\\", "/", ":"))


def _windows_powershell_python_process_statement(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    executable_index = 1 if tokens[0] == "&" and len(tokens) > 1 else 0
    executable = tokens[executable_index]
    if not _windows_python_executable_token(executable):
        return None
    arguments = tokens[executable_index + 1 :]
    return (
        "Invoke-OpenSquillaPythonProcess "
        f"-FilePath {_windows_ps_single_quote(executable)} "
        f"-Arguments {_windows_ps_array_literal(arguments)}"
    )


def _windows_cmd_shim_statement(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    executable_index = 1 if tokens[0] == "&" and len(tokens) > 1 else 0
    executable = tokens[executable_index]
    command = _windows_shell_command_name(executable)
    if command not in {"npm", "npx", "pnpm", "yarn"}:
        return None
    if any(separator in executable for separator in ("\\", "/", ":")):
        return None
    if ntpath.splitext(ntpath.basename(executable))[1]:
        return None
    argv = [f"{command}.cmd", *tokens[executable_index + 1 :]]
    return "& " + " ".join(_windows_ps_single_quote(arg) for arg in argv)


def _windows_nested_powershell_command(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    executable_index = 1 if tokens[0] == "&" and len(tokens) > 1 else 0
    executable = tokens[executable_index]
    if _windows_shell_command_name(executable) not in {"powershell", "pwsh"}:
        return None
    for index, token in enumerate(tokens[executable_index + 1 :], start=executable_index + 1):
        if token.lower() in {"-c", "-command"} and index + 1 < len(tokens):
            return _windows_strip_outer_quotes(" ".join(tokens[index + 1 :]))
    return None


def _windows_powershell_compat_statement(statement: str) -> str:
    tokens = _windows_shell_tokens(statement)
    nested_powershell = _windows_nested_powershell_command(tokens)
    if nested_powershell is not None:
        return nested_powershell
    python_statement = _windows_powershell_python_process_statement(tokens)
    if python_statement is not None:
        return python_statement
    cmd_shim_statement = _windows_cmd_shim_statement(tokens)
    if cmd_shim_statement is not None:
        return cmd_shim_statement
    if len(tokens) < 3:
        return statement
    if _windows_shell_command_name(tokens[0]) != "mkdir":
        return statement
    if tokens[1].lower() != "-p":
        return statement
    paths = [token for token in tokens[2:] if token and not token.startswith("-")]
    if not paths:
        return statement
    return "; ".join(
        f"New-Item -ItemType Directory -Force -Path {_windows_ps_single_quote(path)} | Out-Null"
        for path in paths
    )


def _windows_powershell_compat_command(command: str) -> str:
    statements = _windows_split_logical_and(command)
    if not statements:
        return command
    converted = [_windows_powershell_compat_statement(statement) for statement in statements]
    if len(converted) == 1:
        return converted[0]
    return " ; if (-not $?) { exit 1 }; ".join(converted)


def _windows_split_statements(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    for char in script:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "`":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            current.append(char)
            quote = char
            continue
        if char in ";&":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            continue
        current.append(char)
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def _windows_shell_command_name(token: str) -> str:
    name = ntpath.basename(token).lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return name


def _windows_shell_command_after_option(
    tokens: list[str],
    options: frozenset[str],
) -> str | None:
    for index, token in enumerate(tokens[1:], start=1):
        if token.lower() in options and index + 1 < len(tokens):
            return " ".join(tokens[index + 1 :])
    return None


def _windows_shell_token_looks_like_path(token: str) -> bool:
    if not token or token == "-":
        return False
    lowered = token.lower()
    if lowered.startswith(("http://", "https://")):
        return False
    return (
        lowered in {".venv", "venv"}
        or token.startswith(("/", "\\", "./", ".\\", "../", "..\\"))
        or ntpath.isabs(token)
        or "\\" in token
        or "/" in token
    )


def _windows_paths_from_tokens(
    tokens: list[str],
    *,
    positional: bool = True,
    skip_pathless_switches: bool = False,
) -> list[str]:
    paths: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        if skip_pathless_switches and any(
            lowered == switch or lowered.startswith(f"{switch}:")
            for switch in _WINDOWS_CMD_PATHLESS_SWITCHES
        ):
            index += 1
            continue
        if lowered in _WINDOWS_SHELL_PATH_FLAGS and index + 1 < len(tokens):
            paths.append(tokens[index + 1])
            index += 2
            continue
        if any(lowered.startswith(f"{flag}:") for flag in _WINDOWS_SHELL_PATH_FLAGS):
            paths.append(token.split(":", 1)[1])
            index += 1
            continue
        if lowered in _WINDOWS_SHELL_VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        if positional and _windows_shell_token_looks_like_path(token):
            paths.append(token)
        index += 1
    return paths


def _windows_shell_read_targets(command: str) -> list[str]:
    targets: list[str] = []
    for statement in _windows_split_statements(command):
        tokens = _windows_shell_tokens(statement)
        if not tokens:
            continue
        command_name = _windows_shell_command_name(tokens[0])
        nested: str | None = _windows_nested_powershell_command(tokens)
        if nested is None and command_name == "cmd":
            nested = _windows_shell_command_after_option(tokens, frozenset({"/c", "/k"}))
        if nested is not None:
            for target in _windows_shell_read_targets(_windows_strip_outer_quotes(nested)):
                if target not in targets:
                    targets.append(target)
            continue
        if command_name in _WINDOWS_SHELL_READ_COMMANDS:
            for target in _windows_paths_from_tokens(tokens, skip_pathless_switches=True):
                if target not in targets:
                    targets.append(target)
    return targets


def _windows_python_venv_targets(tokens: list[str]) -> list[str]:
    if len(tokens) < 4:
        return []
    command = _windows_shell_command_name(tokens[0])
    if not re.fullmatch(r"py|python(?:\d+(?:\.\d+)*)?", command):
        return []
    if tokens[1].lower() != "-m" or tokens[2].lower() != "venv":
        return []
    for token in tokens[3:]:
        if not token.startswith("-"):
            return [token]
    return []


def _windows_uv_venv_targets(tokens: list[str]) -> list[str]:
    if len(tokens) < 2 or _windows_shell_command_name(tokens[0]) != "uv":
        return []
    if tokens[1].lower() != "venv":
        return []
    index = 2
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        if lowered in {"--python", "-p", "--seed", "--prompt"}:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return [token]
    return [".venv"]


def _windows_shell_write_targets(command: str) -> list[str]:
    targets: list[str] = []
    for target in _basic_shell_write_targets(command):
        if target not in targets:
            targets.append(target)
    for statement in _windows_split_statements(command):
        tokens = _windows_shell_tokens(statement)
        if not tokens:
            continue
        command_name = _windows_shell_command_name(tokens[0])
        nested: str | None = _windows_nested_powershell_command(tokens)
        if nested is None and command_name == "cmd":
            nested = _windows_shell_command_after_option(tokens, frozenset({"/c", "/k"}))
        if nested is not None:
            for target in _windows_shell_write_targets(_windows_strip_outer_quotes(nested)):
                if target not in targets:
                    targets.append(target)
            continue
        if command_name in _WINDOWS_SHELL_REMOVE_COMMANDS:
            for target in _windows_paths_from_tokens(tokens):
                if target not in targets:
                    targets.append(target)
            continue
        if command_name in _WINDOWS_SHELL_CREATE_COMMANDS:
            for target in _windows_paths_from_tokens(tokens):
                if target not in targets:
                    targets.append(target)
            continue
        if command_name in _WINDOWS_SHELL_CONTENT_COMMANDS:
            for target in _windows_paths_from_tokens(tokens):
                if target not in targets:
                    targets.append(target)
            continue
        for target in (*_windows_python_venv_targets(tokens), *_windows_uv_venv_targets(tokens)):
            if target not in targets:
                targets.append(target)
    return targets


def _active_sandbox_mounts() -> list[dict[str, object]]:
    mounts = current_tool_mounts()
    runtime = get_runtime()
    settings = getattr(runtime, "settings", None) if runtime is not None else None
    if (
        sys.platform.startswith("linux")
        and bool(getattr(settings, "host_root_readonly", False))
        and not any(str(item.get("path") or "") == "/" for item in mounts)
    ):
        mounts.insert(0, {"path": "/", "access": "ro"})
    return mounts


def _policy_with_active_tool_mounts(policy: SandboxPolicy) -> SandboxPolicy:
    if not hasattr(policy, "mounts"):
        return policy
    windows_backend = _windows_sandbox_backend_active()
    initial_mounts = tuple(
        mount
        for mount in policy.mounts
        if not _windows_optional_mount_is_stale(mount, windows_backend=windows_backend)
    )
    writable_host_paths = {str(mount.host_path) for mount in initial_mounts if mount.mode == "rw"}
    mounts_by_target = {
        (str(mount.host_path), sandbox_path_text(mount.sandbox_path)): mount
        for mount in initial_mounts
    }
    for mount in _active_sandbox_mounts():
        raw_path = mount.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        access = str(mount.get("access") or "ro").strip()
        mode: MountMode = "rw" if access == "rw" else "ro"
        host_path = Path(raw_path).expanduser().resolve(strict=False)
        if windows_backend and not host_path.exists():
            continue
        if str(host_path) in writable_host_paths:
            mode = "rw"
        mounts_by_target[(str(host_path), sandbox_path_text(host_path))] = MountSpec(
            host_path=host_path,
            sandbox_path=host_path,
            mode=mode,
            required=False,
        )
    return dataclasses.replace(policy, mounts=tuple(mounts_by_target.values()))


def _policy_with_managed_toolchain_mounts(policy: SandboxPolicy) -> SandboxPolicy:
    """Make activated, receipt-validated managed packages visible read-only."""

    if not hasattr(policy, "mounts"):
        return policy
    env_allowlist = tuple(
        dict.fromkeys((*policy.env_allowlist, MEDIA_FONTS_DIR_ENV, PAPER_FONTS_ENV))
    )
    mounts_by_target = {
        (str(mount.host_path), sandbox_path_text(mount.sandbox_path)): mount
        for mount in policy.mounts
    }
    managed_paths = (
        []
        if _guest_requires_managed_runtime()
        else list(managed_toolchain_readonly_paths())
    )
    try:
        from opensquilla.runtime_packs import runtime_roots

        managed_paths.extend(runtime_roots(active_sandbox_policy().runtimes))
    except (OSError, RuntimeError, ValueError):
        # A broken optional Runtime Pack must never weaken or abort sandbox
        # policy construction; it is simply omitted from PATH and mounts.
        pass
    for path in managed_paths:
        key = (str(path), sandbox_path_text(path))
        existing = mounts_by_target.get(key)
        if existing is not None and existing.mode == "rw":
            continue
        mounts_by_target[key] = MountSpec(
            host_path=path,
            sandbox_path=path,
            mode="ro",
            required=False,
        )
    return dataclasses.replace(
        policy,
        mounts=tuple(mounts_by_target.values()),
        env_allowlist=env_allowlist,
    )


def _windows_optional_mount_is_stale(mount: MountSpec, *, windows_backend: bool) -> bool:
    return windows_backend and not mount.required and not mount.host_path.exists()


def _windows_shell_runtime_mount_paths() -> tuple[Path, ...]:
    paths: list[Path] = []
    for raw in (
        sys.prefix,
        sys.base_prefix,
        str(Path(sys.executable).parent),
        str(Path(getattr(sys, "_base_executable", "")).parent),
    ):
        if not raw:
            continue
        path = Path(raw).expanduser().resolve(strict=False)
        if not path.exists():
            continue
        if path not in paths:
            paths.append(path)
    return tuple(paths)


def _windows_runtime_readonly_roots() -> tuple[Path, ...]:
    if not _windows_sandbox_backend_active():
        return ()
    try:
        from opensquilla.sandbox.backend import windows_default

        roots = windows_default._runtime_readonly_roots()
    except Exception:
        return ()
    return tuple(Path(root).expanduser().resolve(strict=False) for root in roots)


def _runtime_readonly_roots(runtime: object | None = None) -> tuple[Path, ...]:
    if _windows_sandbox_backend_active(runtime):
        return _windows_runtime_readonly_roots()

    roots: list[Path] = []
    for raw in (sys.prefix,):
        if not raw:
            continue
        path = Path(raw).expanduser().resolve(strict=False)
        if path.exists() and path not in roots:
            roots.append(path)
    return tuple(roots)


def _policy_with_windows_shell_runtime_mounts(
    policy: SandboxPolicy,
    runtime: object | None,
) -> SandboxPolicy:
    if not _windows_sandbox_backend_active(runtime) or not hasattr(policy, "mounts"):
        return policy
    mounts_by_path = {str(mount.host_path): mount for mount in policy.mounts}
    for path in _windows_shell_runtime_mount_paths():
        existing = mounts_by_path.get(str(path))
        if existing is not None and existing.mode == "rw":
            continue
        mounts_by_path[str(path)] = MountSpec(
            host_path=path,
            sandbox_path=path,
            mode="ro",
            required=True,
        )
    return dataclasses.replace(policy, mounts=tuple(mounts_by_path.values()))


def _policy_with_wall_timeout(
    policy: SandboxPolicy,
    wall_timeout_s: float,
) -> SandboxPolicy:
    if not hasattr(policy, "limits"):
        return policy
    return dataclasses.replace(
        policy,
        limits=dataclasses.replace(
            policy.limits,
            wall_timeout_s=max(0.01, float(wall_timeout_s)),
        ),
    )


def _sandbox_workdir_access_envelope(
    workdir: str | None,
    *,
    write: bool = False,
    approval_id: str | None = None,
    allow_trusted_auto_mount: bool = False,
) -> dict[str, object] | None:
    if not workdir or not _sandbox_path_access_enabled():
        return None
    workspace = _workspace_root_for_path_access()
    if workspace is None:
        return None
    decision = decide_path_access(
        workdir,
        workspace=workspace,
        mounts=_active_sandbox_mounts(),
        write=write,
        profile=active_file_system_profile(workspace),
    )
    if decision.status == "allowed":
        return None
    if decision.status == "blocked":
        return _path_access_blocked_envelope(decision)
    guest_block = _guest_path_access_block(decision)
    if guest_block is not None:
        return guest_block
    if (
        allow_trusted_auto_mount
        and trusted_sandbox_active()
        and (
            not write
            or trusted_write_auto_grant_allowed(
                decision.normalized_path,
                workspace=_workspace_root_for_path_access(),
            )
        )
        and grant_temporary_mount_for_current_tool(decision)
    ):
        return None
    return _path_access_required_envelope(decision, approval_id=approval_id)


def _sandbox_read_path_access_envelope(
    profile: OperationProfile,
    workdir: str | None,
    *,
    command: str = "",
    approval_id: str | None = None,
    allow_trusted_auto_mount: bool = False,
) -> dict[str, object] | None:
    read_paths = _shell_read_access_targets(command, profile)
    if not read_paths or not _sandbox_path_access_enabled():
        return None
    workspace = _workspace_root_for_path_access()
    if workspace is None:
        return None
    target_workdir = _shell_redirection_workdir(command, workdir)
    for raw_path in read_paths:
        resolved = _resolve_shell_write_target(raw_path, target_workdir)
        decision = decide_path_access(
            resolved,
            workspace=workspace,
            mounts=_active_sandbox_mounts(),
            write=False,
            profile=active_file_system_profile(workspace),
            logical_path=_logical_shell_write_target(raw_path, target_workdir),
        )
        if decision.status == "allowed":
            continue
        if decision.status == "blocked":
            return _path_access_blocked_envelope(decision)
        guest_block = _guest_path_access_block(decision)
        if guest_block is not None:
            return guest_block
        if (
            allow_trusted_auto_mount
            and trusted_sandbox_active()
            and grant_temporary_mount_for_current_tool(decision)
        ):
            continue
        return _path_access_required_envelope(decision, approval_id=approval_id)
    return None


def _shell_read_access_targets(
    command: str,
    profile: OperationProfile,
) -> tuple[str, ...]:
    targets: list[str] = []
    for target in (
        *getattr(profile, "requested_paths", ()),
        *_windows_shell_read_targets(command),
    ):
        if _is_special_shell_write_target(target):
            continue
        if target not in targets:
            targets.append(target)
    return tuple(targets)


def _sandbox_write_path_access_envelope(
    profile: OperationProfile,
    workdir: str | None,
    command: str,
    *,
    stdin: str | None = None,
    approval_id: str | None = None,
    allow_trusted_auto_mount: bool = False,
) -> dict[str, object] | None:
    write_paths = _shell_write_access_targets(command, profile, stdin=stdin)
    if not write_paths or not _sandbox_path_access_enabled():
        return None
    workspace = _workspace_root_for_path_access()
    if workspace is None:
        return None
    target_workdir = _shell_redirection_workdir(command, workdir)
    shell_file_targets = frozenset(_shell_write_targets_from_inputs(command, stdin))
    for raw_path in write_paths:
        resolved = _resolve_shell_write_target(raw_path, target_workdir)
        decision = decide_path_access(
            resolved,
            workspace=workspace,
            mounts=_active_sandbox_mounts(),
            write=True,
            profile=active_file_system_profile(workspace),
            logical_path=_logical_shell_write_target(raw_path, target_workdir),
        )
        if decision.status == "allowed":
            if allow_trusted_auto_mount:
                _grant_precise_windows_file_mount_for_allowed_write_target(
                    raw_path,
                    decision,
                    shell_file_targets,
                )
            continue
        if decision.status == "blocked":
            return _path_access_blocked_envelope(decision)
        guest_block = _guest_path_access_block(decision)
        if guest_block is not None:
            return guest_block
        if (
            allow_trusted_auto_mount
            and trusted_sandbox_active()
            and trusted_write_auto_grant_allowed(
                decision.normalized_path,
                workspace=_workspace_root_for_path_access(),
            )
            and grant_temporary_mount_for_current_tool(
                decision,
                prefer_file=_shell_write_target_prefers_file(raw_path, shell_file_targets),
            )
        ):
            continue
        return _path_access_required_envelope(decision, approval_id=approval_id)
    return None


def _protected_metadata_write_block(
    tool_name: str,
    command: str,
    workdir: str | None,
    profile: OperationProfile,
    *,
    stdin: str | None = None,
) -> dict[str, object] | None:
    if full_host_access_active():
        return None
    workspace = _workspace_root_for_path_access()
    file_system_profile = active_file_system_profile(workspace) if workspace is not None else None
    target_workdir = _shell_redirection_workdir(command, workdir)
    for raw_path in _shell_write_access_targets(command, profile, stdin=stdin):
        logical = _logical_shell_write_target(raw_path, target_workdir)
        resolved = _resolve_shell_write_target(raw_path, target_workdir)
        protected_root = (
            file_system_profile.protected_metadata_root(logical)
            if file_system_profile is not None
            else None
        )
        protected_name = (
            protected_root.name
            if protected_root is not None
            else next(
                (part for part in logical.parts if part in _PROTECTED_METADATA_NAMES),
                None,
            )
        )
        if protected_name is None:
            continue
        return {
            "status": "blocked",
            "reason": "protected_metadata",
            "tool": tool_name,
            "command": command,
            "target": raw_path,
            "resolved_path": str(resolved),
            "protected_name": protected_name,
            "message": (
                f"Refusing to write inside protected metadata path {protected_name}. "
                "This path remains read-only inside the sandbox."
            ),
        }
    return None


def _grant_precise_windows_file_mount_for_allowed_write_target(
    raw_path: str,
    decision: MountDecision,
    shell_file_targets: frozenset[str],
) -> None:
    if not trusted_sandbox_active() or not _windows_sandbox_backend_active():
        return
    if not _shell_write_target_prefers_file(raw_path, shell_file_targets):
        return
    candidate = Path(decision.normalized_path).expanduser().resolve(strict=False)
    if not candidate.exists() or candidate.is_dir():
        return
    candidate_text = str(candidate)
    for mount in _active_sandbox_mounts():
        raw_mount_path = mount.get("path")
        access = str(mount.get("access") or "ro").strip()
        if (
            isinstance(raw_mount_path, str)
            and raw_mount_path.strip()
            and access == "rw"
            and str(Path(raw_mount_path).expanduser().resolve(strict=False)) == candidate_text
        ):
            return
    grant_temporary_mount_for_current_tool(
        MountDecision(
            status="request",
            normalized_path=candidate_text,
            access="rw",
            reason="windows_existing_file_write_target",
        ),
        prefer_file=True,
    )


def _shell_write_access_targets(
    command: str,
    profile: OperationProfile,
    *,
    stdin: str | None = None,
) -> tuple[str, ...]:
    targets: list[str] = []
    for target in (
        *_shell_write_targets_from_inputs(command, stdin),
        *_mutating_command_write_targets_from_inputs(command, stdin),
        *getattr(profile, "requested_write_paths", ()),
    ):
        if _is_special_shell_write_target(target):
            continue
        if target not in targets:
            targets.append(target)
    return tuple(targets)


def _shell_write_target_prefers_file(
    raw_target: str,
    shell_file_targets: frozenset[str],
) -> bool:
    if raw_target in shell_file_targets:
        return True
    cleaned = raw_target.strip().strip("'\"")
    return bool(ntpath.splitext(cleaned)[1] or Path(cleaned).suffix)


def _resolve_shell_write_target(raw_target: str, workdir: str | None) -> Path:
    cleaned = raw_target.strip().strip("'\"")
    path = Path(cleaned).expanduser()
    if not path.is_absolute():
        base = Path(workdir).expanduser() if workdir else Path.cwd()
        path = base / path
    alias = resolve_workspace_alias(path, _workspace_root_for_path_access())
    if alias is not None:
        path = alias
    return path.resolve(strict=False)


def _logical_shell_write_target(raw_target: str, workdir: str | None) -> Path:
    cleaned = raw_target.strip().strip("'\"")
    base = (
        Path(workdir).expanduser() if workdir else (_workspace_root_for_path_access() or Path.cwd())
    )
    return logical_tool_path(cleaned, base=base)


def _shell_target_is_relative(raw_target: str) -> bool:
    cleaned = raw_target.strip().strip("'\"")
    if not cleaned:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", cleaned):
        return False
    return not Path(cleaned).expanduser().is_absolute()


def _windows_dos_device_targets_are_special() -> bool:
    return os.name == "nt" or _windows_sandbox_backend_active()


def _is_windows_dos_device_target(raw_target: object) -> bool:
    cleaned = str(raw_target or "").strip().strip("'\"")
    if not cleaned:
        return False
    basename = ntpath.basename(cleaned.rstrip("\\/"))
    if not basename:
        return False
    stem = basename.split(":", 1)[0].split(".", 1)[0].lower()
    return stem in _WINDOWS_DOS_DEVICE_NAMES


def _is_special_shell_write_target(raw_target: object) -> bool:
    if not _windows_dos_device_targets_are_special():
        return False
    return _is_windows_dos_device_target(raw_target)


@dataclass(frozen=True)
class _LeadingCdPrefix:
    targets: tuple[str, ...] = ()
    unsafe_reason: str = ""


_DYNAMIC_SHELL_PATH_MARKERS = frozenset("$`*?[]{}%()<>")
_SHELL_COMMAND_BOUNDARIES = frozenset({"&&", "||", "|", "&", "(", ")", "{", "}"})
_SHELL_COMMAND_PREFIXES = frozenset(
    {"!", "builtin", "command", "do", "elif", "else", "if", "then", "time", "until", "while"}
)
_SHELL_REDIRECTION_OPERATORS = frozenset(
    {"<", "<<", "<<-", "<<<", "<&", "<>", ">", ">&", ">>", "&>", "&>>", ">|"}
)
_UNMODELLED_WORKDIR_COMMANDS = frozenset(
    {
        ".",
        "eval",
        "pop-location",
        "popd",
        "push-location",
        "pushd",
        "set-location",
        "source",
    }
)
_INLINE_SHELL_COMMANDS = frozenset({"bash", "sh"})


def _shell_statement_separator(token: str) -> bool:
    if token == ";":
        return True
    if token and set(token) == {"\n"}:
        return True
    return token.startswith(";") and bool(token[1:]) and set(token[1:]) == {"\n"}


def _literal_shell_cd_target(target: str) -> bool:
    return bool(target) and not any(marker in target for marker in _DYNAMIC_SHELL_PATH_MARKERS)


def _shell_control_token(token: str) -> bool:
    return _shell_statement_separator(token) or token in _SHELL_COMMAND_BOUNDARIES


def _shell_assignment_prefix(token: str) -> bool:
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token) is not None


def _contains_unmodelled_workdir_change(tokens: tuple[str, ...], start: int = 0) -> bool:
    """Return whether a simple command can change cwd outside the modelled grammar."""

    command_position = True
    skip_redirection_target = False
    for index in range(start, len(tokens)):
        token = tokens[index]
        if _shell_control_token(token):
            command_position = True
            skip_redirection_target = False
            continue
        if not command_position:
            continue
        if skip_redirection_target:
            skip_redirection_target = False
            continue
        if token in _SHELL_REDIRECTION_OPERATORS:
            skip_redirection_target = True
            continue
        if (
            token.isdecimal()
            and index + 1 < len(tokens)
            and tokens[index + 1] in _SHELL_REDIRECTION_OPERATORS
        ):
            continue
        command = token.casefold() if token in {".", "!"} else Path(token).name.casefold()
        if command == "cd" or command in _UNMODELLED_WORKDIR_COMMANDS:
            return True
        if command in _INLINE_SHELL_COMMANDS:
            for argument in tokens[index + 1 :]:
                if _shell_control_token(argument):
                    break
                if (
                    argument.startswith("-")
                    and not argument.startswith("--")
                    and "c" in argument[1:]
                ):
                    return True
        if token in _SHELL_COMMAND_PREFIXES or _shell_assignment_prefix(token):
            continue
        command_position = False
    return False


def _inline_shell_script(tokens: tuple[str, ...]) -> str | None:
    """Return a directly nested ``sh -c`` script when its wrapper is simple."""

    if not tokens or Path(tokens[0]).name.casefold() not in _INLINE_SHELL_COMMANDS:
        return None
    command_index: int | None = None
    for index, token in enumerate(tokens[1:], start=1):
        if _shell_control_token(token) or token in _SHELL_REDIRECTION_OPERATORS:
            return None
        if token == "--":
            continue
        if token.startswith("-"):
            if "c" in token[1:]:
                command_index = index + 1
                break
            continue
        return None
    if command_index is None or command_index >= len(tokens):
        return None
    if _shell_control_token(tokens[command_index]):
        return None
    if any(
        _shell_control_token(token) or token in _SHELL_REDIRECTION_OPERATORS
        for token in tokens[command_index + 1 :]
    ):
        return None
    return tokens[command_index]


def _leading_cd_prefix(command: str, *, _depth: int = 0) -> _LeadingCdPrefix:
    """Parse the deliberately small leading-``cd`` grammar used for path gates."""

    lexer = shlex.shlex(
        command,
        posix=True,
        punctuation_chars=";&|<>\n",
    )
    lexer.commenters = ""
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = tuple(lexer)
    except ValueError:
        return _LeadingCdPrefix(unsafe_reason="untrusted_workdir")
    if not tokens:
        return _LeadingCdPrefix()
    if Path(tokens[0]).name.casefold() in _INLINE_SHELL_COMMANDS:
        nested = _inline_shell_script(tokens)
        if nested is None or _depth >= 4:
            return _LeadingCdPrefix(unsafe_reason="untrusted_workdir")
        return _leading_cd_prefix(nested, _depth=_depth + 1)
    if tokens[0] != "cd":
        if _contains_unmodelled_workdir_change(tokens):
            return _LeadingCdPrefix(unsafe_reason="untrusted_workdir")
        return _LeadingCdPrefix()

    targets: list[str] = []
    index = 0
    while index < len(tokens) and tokens[index] == "cd":
        index += 1
        options_terminated = False
        if index < len(tokens) and tokens[index] == "--":
            options_terminated = True
            index += 1
        if index >= len(tokens) or _shell_control_token(tokens[index]):
            return _LeadingCdPrefix(unsafe_reason="dynamic_workdir")

        target = tokens[index]
        index += 1
        if (
            not _literal_shell_cd_target(target)
            or target == "-"
            or (target.startswith("-") and not options_terminated)
        ):
            return _LeadingCdPrefix(unsafe_reason="dynamic_workdir")
        targets.append(target)
        if index == len(tokens):
            return _LeadingCdPrefix(targets=tuple(targets))

        operator = tokens[index]
        if operator == "&&":
            index += 1
            if index == len(tokens):
                return _LeadingCdPrefix(unsafe_reason="untrusted_workdir")
        elif _shell_statement_separator(operator):
            index += 1
        elif (
            operator == "||"
            and index + 2 < len(tokens)
            and tokens[index + 1] == "exit"
            and _shell_statement_separator(tokens[index + 2])
        ):
            index += 3
        else:
            return _LeadingCdPrefix(unsafe_reason="untrusted_workdir")

        if index == len(tokens):
            return _LeadingCdPrefix(targets=tuple(targets))
        if tokens[index] == "cd":
            continue
        if _contains_unmodelled_workdir_change(tokens, index):
            return _LeadingCdPrefix(unsafe_reason="untrusted_workdir")
        return _LeadingCdPrefix(targets=tuple(targets))

    return _LeadingCdPrefix(targets=tuple(targets))


def _shell_redirection_workdir(command: str, workdir: str | None) -> str | None:
    leading_cd = _leading_cd_prefix(command)
    if not leading_cd.targets:
        return workdir
    path = Path(workdir).expanduser() if workdir else Path.cwd()
    for raw_target in leading_cd.targets:
        target = Path(raw_target).expanduser()
        path = target if target.is_absolute() else path / target
        path = path.resolve(strict=False)
    return str(path)


def _is_shell_null_write_target(raw_target: str) -> bool:
    return raw_target.strip().strip("'\"") == os.devnull


def _basic_shell_write_targets(command: str) -> list[str]:
    targets: list[str] = []
    redirection_pattern = r"(?:^|\s)(?:\d?>{1,2}|&>{1,2})\s*(['\"]?)([^'\"\s|&;]+)\1"
    targets.extend(
        target
        for match in re.finditer(redirection_pattern, command)
        if not _is_special_shell_write_target(target := match.group(2))
    )
    tee_pattern = r"(?:^|\s)tee(?:\s+-[A-Za-z]+)*\s+(['\"]?)([^'\"\s|&;]+)\1"
    targets.extend(
        target
        for match in re.finditer(tee_pattern, command)
        if not _is_special_shell_write_target(target := match.group(2))
    )
    return targets


def _shell_write_targets(command: str) -> list[str]:
    targets = _basic_shell_write_targets(command)
    if _windows_sandbox_backend_active():
        for target in _windows_shell_write_targets(command):
            if target not in targets:
                targets.append(target)
    targets = [target for target in targets if not _is_special_shell_write_target(target)]
    return targets


_STDIN_BASIC_WRITE_MARKERS = (
    ">",
    "tee",
    "set-content",
    "add-content",
    "out-file",
    "new-item",
    "copy-item",
    "move-item",
    "remove-item",
)
_STDIN_MUTATOR_MARKERS = (
    "sed",
    "perl",
    "rm",
    "unlink",
    "mv",
    "cp",
    "dd",
    "truncate",
    "touch",
    "mkdir",
    "rmdir",
    "git",
)


def _stdin_may_contain_write_syntax(stdin: str, markers: tuple[str, ...]) -> bool:
    lowered = stdin.casefold()
    return any(marker in lowered for marker in markers)


def _shell_write_targets_from_inputs(command: str, stdin: str | None = None) -> list[str]:
    targets = _shell_write_targets(command)
    if stdin is not None and _stdin_may_contain_write_syntax(
        stdin,
        _STDIN_BASIC_WRITE_MARKERS,
    ):
        for stdin_chunk in _iter_stdin_guard_chunks(stdin):
            targets.extend(_shell_write_targets(stdin_chunk))
    return targets


_WRITE_DENY_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})


def _write_deny_lever_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _WRITE_DENY_TRUE_ENV_VALUES


# Screen widenings that shipped alongside effect enforcement: ln/link link
# names, sh -c wrapper unwrapping, deno eval, and the bun/lua interpreter
# code flags. They stay behind the effect lever so a deployment that leaves
# OPENSQUILLA_WORKSPACE_WRITE_DENY_EFFECT unset keeps the pre-lever screen
# surface byte-for-byte, even with the older COMMAND_TARGETS/
# INTERPRETER_TARGETS screens enabled.
_HARDENED_ONLY_MUTATORS = frozenset({"ln", "link"})
_HARDENED_ONLY_INTERPRETERS = frozenset({"bun", "lua", "luajit"})


def _write_deny_matcher_hardening_enabled() -> bool:
    from opensquilla.tools.write_policy import workspace_write_deny_effect_mode

    return workspace_write_deny_effect_mode() != "off"


_SHORT_OPTIONS_WITH_I_RE = re.compile(r"^-[A-Za-z]*i")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_COMMAND_PREFIX_WORDS = frozenset({"command", "env", "nohup", "sudo", "time"})


def _mutator_command_segments(command: str) -> list[str]:
    """Split a shell command on unquoted ``|``, ``||``, ``&&``, ``;``, newlines.

    Operators inside quotes stay in their segment (``sed 's|a|b|'``,
    ``git commit -m 'a; b'``) and heredoc bodies are dropped entirely: their
    text is data, not commands. Best-effort, like the extraction it feeds.
    """

    segments: list[str] = []
    current: list[str] = []
    heredoc_delimiters: list[str] = []
    index = 0
    length = len(command)

    def flush() -> None:
        segment = "".join(current)
        current.clear()
        if segment.strip():
            segments.append(segment)

    while index < length:
        char = command[index]
        if char == "\\":
            current.append(command[index : index + 2])
            index += 2
            continue
        if char == "'":
            end = command.find("'", index + 1)
            end = length - 1 if end == -1 else end
            current.append(command[index : end + 1])
            index = end + 1
            continue
        if char == '"':
            scan = index + 1
            while scan < length and command[scan] != '"':
                scan += 2 if command[scan] == "\\" else 1
            current.append(command[index : min(scan + 1, length)])
            index = scan + 1
            continue
        if command.startswith("<<", index) and not command.startswith("<<<", index):
            scan = index + 2
            if scan < length and command[scan] == "-":
                scan += 1
            while scan < length and command[scan] in " \t":
                scan += 1
            quote = command[scan] if scan < length and command[scan] in "'\"" else ""
            if quote:
                scan += 1
            start = scan
            if quote:
                while scan < length and command[scan] != quote:
                    scan += 1
                delimiter = command[start:scan]
                if scan < length:
                    scan += 1
            else:
                while scan < length and command[scan] not in " \t\n;|&<>":
                    scan += 1
                delimiter = command[start:scan]
            if delimiter:
                heredoc_delimiters.append(delimiter)
            current.append(command[index:scan])
            index = scan
            continue
        if char == "\n":
            flush()
            index += 1
            if heredoc_delimiters:
                while heredoc_delimiters and index < length:
                    line_end = command.find("\n", index)
                    if line_end == -1:
                        line_end = length
                    if command[index:line_end].strip() == heredoc_delimiters[0]:
                        heredoc_delimiters.pop(0)
                    index = line_end + 1
                heredoc_delimiters.clear()
            continue
        if char == ";":
            flush()
            index += 1
            continue
        if char == "|":
            flush()
            index += 2 if command.startswith("||", index) else 1
            continue
        if command.startswith("&&", index):
            flush()
            index += 2
            continue
        current.append(char)
        index += 1
    flush()
    return segments


def _segment_argv(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _positional_args(
    argv: list[str],
    value_flags: frozenset[str] = frozenset(),
) -> list[str]:
    args: list[str] = []
    skip_value = False
    positional_only = False
    for token in argv:
        if skip_value:
            skip_value = False
            continue
        if positional_only:
            args.append(token)
            continue
        if token == "--":
            positional_only = True
            continue
        if token.startswith("-") and token != "-":
            if token in value_flags:
                skip_value = True
            continue
        args.append(token)
    return args


def _sed_write_targets(argv: list[str]) -> list[str]:
    options = argv[1:]
    inplace = any(
        _SHORT_OPTIONS_WITH_I_RE.match(token) or token.startswith("--in-place")
        for token in options
        if token.startswith("-") and token != "--"
    )
    if not inplace:
        return []
    script_from_flag = any(
        token in ("-e", "-f") or token.startswith(("--expression", "--file")) for token in options
    )
    positionals = _positional_args(
        options, value_flags=frozenset({"-e", "-f", "--expression", "--file"})
    )
    if not script_from_flag and positionals and positionals[0] == "":
        # BSD sed -i '' idiom: the empty token is -i's backup suffix.
        positionals = positionals[1:]
    if not script_from_flag and positionals:
        # Without -e/-f the first positional is the sed script, not a file.
        positionals = positionals[1:]
    return positionals


_PERL_REST_ARG_OPTION_CHARS = frozenset("0CDFIeEmMx")


def _perl_token_enables_inplace(token: str) -> bool:
    # Perl bundles single-char switches, and several consume the rest of the
    # token as their argument (-Ilib, -MSome::Module, -e'code'); an 'i'
    # inside such an argument is not the in-place switch.
    if not token.startswith("-") or token == "--":
        return False
    for char in token[1:]:
        if char == "i":
            return True
        if char in _PERL_REST_ARG_OPTION_CHARS or not char.isalnum():
            return False
    return False


def _perl_write_targets(argv: list[str]) -> list[str]:
    options = argv[1:]
    inplace = any(
        _perl_token_enables_inplace(token)
        for token in options
        if token.startswith("-") and token != "--"
    )
    if not inplace:
        return []
    script_from_flag = any(token.startswith(("-e", "-E")) for token in options)
    positionals = _positional_args(options, value_flags=frozenset({"-e", "-E"}))
    if not script_from_flag and positionals:
        # Without -e/-E the first positional is the program file, not input.
        positionals = positionals[1:]
    return positionals


def _rm_write_targets(argv: list[str]) -> list[str]:
    return _positional_args(argv[1:])


def _mv_write_targets(argv: list[str]) -> list[str]:
    # mv mutates every operand: sources are removed, the destination written.
    options = argv[1:]
    targets = [
        token.split("=", 1)[1] for token in options if token.startswith("--target-directory=")
    ]
    targets.extend(
        options[index + 1]
        for index, token in enumerate(options)
        if token in ("-t", "--target-directory") and index + 1 < len(options)
    )
    targets.extend(_positional_args(options, value_flags=frozenset({"-t", "--target-directory"})))
    return targets


def _cp_write_targets(argv: list[str]) -> list[str]:
    options = argv[1:]
    targets = [
        token.split("=", 1)[1] for token in options if token.startswith("--target-directory=")
    ]
    targets.extend(
        options[index + 1]
        for index, token in enumerate(options)
        if token in ("-t", "--target-directory") and index + 1 < len(options)
    )
    positionals = _positional_args(options, value_flags=frozenset({"-t", "--target-directory"}))
    if not targets and len(positionals) >= 2:
        targets.append(positionals[-1])
    return targets


def _dd_write_targets(argv: list[str]) -> list[str]:
    return [token[3:] for token in argv[1:] if token.startswith("of=")]


def _truncate_write_targets(argv: list[str]) -> list[str]:
    return _positional_args(argv[1:], value_flags=frozenset({"-s", "--size", "-r", "--reference"}))


def _git_write_targets(argv: list[str]) -> list[str]:
    tokens = argv[1:]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-C", "-c", "--git-dir", "--work-tree"):
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(tokens):
        return []
    if tokens[index] not in ("rm", "mv"):
        return []
    sub_args = tokens[index + 1 :]
    option_args = sub_args[: sub_args.index("--")] if "--" in sub_args else sub_args
    if tokens[index] == "rm" and "--cached" in option_args:
        # git rm --cached only unstages; the worktree file is untouched.
        return []
    return _positional_args(sub_args)


def _ln_write_targets(argv: list[str]) -> list[str]:
    # ln writes the link NAME (a symlink or hardlink appearing at a protected
    # path is a mutation of that path); the link target is only read.
    options = argv[1:]
    targets = [
        token.split("=", 1)[1]
        for token in options
        if token.startswith("--target-directory=")
    ]
    targets.extend(
        options[index + 1]
        for index, token in enumerate(options)
        if token in ("-t", "--target-directory") and index + 1 < len(options)
    )
    positionals = _positional_args(
        options,
        value_flags=frozenset({"-t", "--target-directory", "-S", "--suffix"}),
    )
    if targets:
        return targets
    if len(positionals) >= 2:
        return [positionals[-1]]
    if len(positionals) == 1:
        # `ln [-s] TARGET` creates ./<basename of TARGET> in the cwd.
        base = positionals[0].replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return [base] if base else []
    return []


_MUTATOR_WRITE_TARGET_EXTRACTORS: dict[str, Callable[[list[str]], list[str]]] = {
    "sed": _sed_write_targets,
    "gsed": _sed_write_targets,
    "perl": _perl_write_targets,
    "rm": _rm_write_targets,
    "unlink": _rm_write_targets,
    "mv": _mv_write_targets,
    "cp": _cp_write_targets,
    "dd": _dd_write_targets,
    "truncate": _truncate_write_targets,
    "touch": _rm_write_targets,
    "mkdir": _rm_write_targets,
    "rmdir": _rm_write_targets,
    "git": _git_write_targets,
    "ln": _ln_write_targets,
    "link": _ln_write_targets,
}


def _strip_command_prefix_tokens(argv: list[str]) -> list[str]:
    while argv and (
        _ENV_ASSIGNMENT_RE.match(argv[0])
        or argv[0].lower() in _COMMAND_PREFIX_WORDS
    ):
        argv = argv[1:]
    return argv


def _mutating_command_write_targets(command: str, depth: int = 0) -> list[str]:
    """Best-effort write targets of common in-place file mutators.

    Only consulted by the workspace write deny gate when
    OPENSQUILLA_WORKSPACE_WRITE_DENY_COMMAND_TARGETS is enabled; plain
    redirection and tee targets are covered by _shell_write_targets_from_inputs
    unconditionally. Variable expansion, command substitution, and interpreter
    one-liners are out of scope (the latter behind
    OPENSQUILLA_WORKSPACE_WRITE_DENY_INTERPRETER_TARGETS).
    """

    hardened = _write_deny_matcher_hardening_enabled()
    targets: list[str] = []
    for segment in _mutator_command_segments(command):
        argv = _strip_command_prefix_tokens(_segment_argv(segment))
        if not argv:
            continue
        if hardened and depth < _SHELL_WRAPPER_MAX_DEPTH:
            for inner_command in _shell_wrapper_inner_commands(argv):
                for target in _mutating_command_write_targets(inner_command, depth + 1):
                    if target and target not in targets:
                        targets.append(target)
        name = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if not hardened and name in _HARDENED_ONLY_MUTATORS:
            continue
        extractor = _MUTATOR_WRITE_TARGET_EXTRACTORS.get(name)
        if extractor is None:
            continue
        for target in extractor(argv):
            if target and target not in targets:
                targets.append(target)
    return targets


def _mutating_command_write_targets_from_inputs(
    command: str,
    stdin: str | None = None,
) -> list[str]:
    targets = _mutating_command_write_targets(command)
    if stdin is not None and _stdin_may_contain_write_syntax(
        stdin,
        _STDIN_MUTATOR_MARKERS,
    ):
        for stdin_chunk in _iter_stdin_guard_chunks(stdin):
            for target in _mutating_command_write_targets(stdin_chunk):
                if target not in targets:
                    targets.append(target)
    return targets


_INTERPRETER_CODE_FLAGS: dict[str, frozenset[str]] = {
    "python": frozenset({"-c"}),
    "ruby": frozenset({"-e"}),
    "perl": frozenset({"-e", "-E"}),
    "php": frozenset({"-r"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "nodejs": frozenset({"-e", "--eval", "-p", "--print"}),
    "bun": frozenset({"-e", "--eval", "-p", "--print"}),
    "lua": frozenset({"-e"}),
    "luajit": frozenset({"-e"}),
}

_INTERPRETER_WRITE_MODE_CHARS = frozenset("wax+")

# Literal-path write forms inside interpreter code strings. Only string
# literals are extractable; variables and computed paths stay out of scope.
_INTERPRETER_OPEN_CALL_RE = re.compile(
    r"\b(?:open|fopen)\s*\(\s*(?P<pq>['\"])(?P<path>(?:(?!(?P=pq)).)+)(?P=pq)"
    r"\s*,\s*(?:mode\s*=\s*)?(?P<mq>['\"])(?P<mode>(?:(?!(?P=mq)).)*)(?P=mq)"
)
_INTERPRETER_PATH_MUTATE_RE = re.compile(
    r"\bPath\s*\(\s*(?P<pq>['\"])(?P<path>(?:(?!(?P=pq)).)+)(?P=pq)\s*\)"
    r"\s*\.\s*(?:write_text|write_bytes|unlink|rmdir|touch|rename|replace)\s*\("
)
_INTERPRETER_FS_WRITE_RE = re.compile(
    r"\b(?:writeFileSync|appendFileSync|writeFile|appendFile"
    r"|writeTextFileSync|writeTextFile"
    r"|unlinkSync|rmSync|rmdirSync|renameSync|truncateSync|removeSync)"
    r"\s*\(\s*(?P<pq>['\"])(?P<path>(?:(?!(?P=pq)).)+)(?P=pq)"
)
_INTERPRETER_FILE_WRITE_RE = re.compile(
    r"\b(?:(?:File|IO)\s*\.\s*(?:write|binwrite|append|delete|unlink|truncate)"
    r"|FileUtils\s*\.\s*(?:rm_rf|rm_r|rm|remove|mv|move|touch)"
    r"|file_put_contents"
    r"|os\s*\.\s*(?:remove|unlink|rename|replace|truncate)"
    r"|shutil\s*\.\s*(?:rmtree|move))"
    r"\s*\(\s*(?P<pq>['\"])(?P<path>(?:(?!(?P=pq)).)+)(?P=pq)"
)
# Perl two-arg open with the mode fused into the path string: open(FH, '>f').
_INTERPRETER_PERL_OPEN2_RE = re.compile(
    r"\bopen\s*\([^()]*?(?P<pq>['\"])\s*>{1,2}\s*(?P<path>(?:(?!(?P=pq)).)+)(?P=pq)"
)
# Perl three-arg open: open(FH, '>', 'f').
_INTERPRETER_PERL_OPEN3_RE = re.compile(
    r"\bopen\s*\([^()]*?(?P<mq>['\"])\s*>{1,2}\s*(?P=mq)"
    r"\s*,\s*(?P<pq>['\"])(?P<path>(?:(?!(?P=pq)).)+)(?P=pq)"
)


def _normalized_command_name(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _interpreter_code_flag_set(name: str) -> frozenset[str] | None:
    flags = _INTERPRETER_CODE_FLAGS.get(name)
    if flags is not None:
        return flags
    return _INTERPRETER_CODE_FLAGS.get(name.rstrip("0123456789."))


def _deno_eval_code_strings(argv: list[str]) -> list[str]:
    # deno delivers inline code via the `eval` subcommand instead of a flag.
    if not argv or _normalized_command_name(argv[0]) != "deno":
        return []
    tokens = argv[1:]
    if not tokens or tokens[0] != "eval":
        return []
    positionals = _positional_args(tokens[1:], value_flags=frozenset({"--ext"}))
    return positionals[:1]


_SHELL_WRAPPER_NAMES = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_SHELL_WRAPPER_DASH_C_RE = re.compile(r"^-[A-Za-z]*c$")
_SHELL_WRAPPER_MAX_DEPTH = 3


def _shell_wrapper_inner_commands(argv: list[str]) -> list[str]:
    """Command strings run by a `sh -c` style wrapper, if any.

    Handles busybox indirection and bundled short options (`bash -lc cmd`).
    Only the first -c operand is the command string; later positionals become
    $0/$@ for it.
    """

    argv = _strip_command_prefix_tokens(argv)
    if argv and _normalized_command_name(argv[0]) == "busybox":
        argv = argv[1:]
    if not argv or _normalized_command_name(argv[0]) not in _SHELL_WRAPPER_NAMES:
        return []
    tokens = argv[1:]
    for index, token in enumerate(tokens):
        if token == "--":
            break
        if _SHELL_WRAPPER_DASH_C_RE.match(token) and index + 1 < len(tokens):
            return [tokens[index + 1]]
    return []


def _interpreter_code_strings(
    argv: list[str],
    code_flags: frozenset[str],
) -> list[str]:
    codes: list[str] = []
    tokens = argv[1:]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        if token in code_flags:
            if index + 1 < len(tokens):
                codes.append(tokens[index + 1])
                index += 2
                continue
        elif token.startswith("-"):
            for flag in code_flags:
                if len(flag) == 2 and len(token) > 2 and token.startswith(flag):
                    codes.append(token[2:])
                    break
                if flag.startswith("--") and token.startswith(flag + "="):
                    codes.append(token[len(flag) + 1 :])
                    break
        index += 1
    return codes


def _interpreter_code_write_targets(code: str) -> list[str]:
    targets: list[str] = []

    def add(path: str) -> None:
        cleaned = path.strip()
        if cleaned and not _is_special_shell_write_target(cleaned) and cleaned not in targets:
            targets.append(cleaned)

    for match in _INTERPRETER_OPEN_CALL_RE.finditer(code):
        mode = match.group("mode").lower()
        if any(char in _INTERPRETER_WRITE_MODE_CHARS for char in mode):
            add(match.group("path"))
    for pattern in (
        _INTERPRETER_PATH_MUTATE_RE,
        _INTERPRETER_FS_WRITE_RE,
        _INTERPRETER_FILE_WRITE_RE,
        _INTERPRETER_PERL_OPEN2_RE,
        _INTERPRETER_PERL_OPEN3_RE,
    ):
        for match in pattern.finditer(code):
            add(match.group("path"))
    return targets


def _interpreter_write_targets(argv: list[str]) -> list[str]:
    argv = _strip_command_prefix_tokens(argv)
    if not argv:
        return []
    hardened = _write_deny_matcher_hardening_enabled()
    codes = _deno_eval_code_strings(argv) if hardened else []
    if not codes:
        name = _normalized_command_name(argv[0])
        if not hardened and name in _HARDENED_ONLY_INTERPRETERS:
            return []
        code_flags = _interpreter_code_flag_set(name)
        if code_flags is None:
            return []
        codes = _interpreter_code_strings(argv, code_flags)
    targets: list[str] = []
    for code in codes:
        for target in _interpreter_code_write_targets(code):
            if target not in targets:
                targets.append(target)
    return targets


def _command_reads_interpreter_program_from_stdin(command: str) -> bool:
    for segment in _mutator_command_segments(command):
        argv = _strip_command_prefix_tokens(_segment_argv(segment))
        if not argv:
            continue
        code_flags = _interpreter_code_flag_set(_normalized_command_name(argv[0]))
        if code_flags is None:
            continue
        if _interpreter_code_strings(argv, code_flags):
            # Program came from -c/-e; stdin is data for it.
            continue
        positionals = _positional_args(argv[1:], value_flags=code_flags)
        if not positionals or positionals[0] == "-":
            # Bare `python3` / `python3 -`: stdin is the program text.
            return True
    return False


def _interpreter_write_targets_from_command(command: str, depth: int = 0) -> list[str]:
    """Best-effort write targets of interpreter one-liners.

    Only consulted by the workspace write deny gate when
    OPENSQUILLA_WORKSPACE_WRITE_DENY_INTERPRETER_TARGETS is enabled. Covers
    literal write-path forms in -c/-e/-r code strings; variable expansion and
    computed paths remain out of scope.
    """

    hardened = _write_deny_matcher_hardening_enabled()
    targets: list[str] = []
    for segment in _mutator_command_segments(command):
        argv = _segment_argv(segment)
        if hardened and depth < _SHELL_WRAPPER_MAX_DEPTH:
            for inner_command in _shell_wrapper_inner_commands(argv):
                for target in _interpreter_write_targets_from_command(inner_command, depth + 1):
                    if target not in targets:
                        targets.append(target)
        for target in _interpreter_write_targets(argv):
            if target not in targets:
                targets.append(target)
    return targets


def _interpreter_write_targets_from_inputs(
    command: str,
    stdin: str | None = None,
) -> list[str]:
    targets = _interpreter_write_targets_from_command(command)
    if stdin is not None:
        stdin_is_program = _command_reads_interpreter_program_from_stdin(command)
        for stdin_chunk in _iter_stdin_guard_chunks(stdin):
            extra = _interpreter_write_targets_from_command(stdin_chunk)
            if stdin_is_program:
                # `python3 - <<EOF` / piped program text: the stdin itself is
                # interpreter code, not shell commands.
                extra = extra + _interpreter_code_write_targets(stdin_chunk)
            for target in extra:
                if target not in targets:
                    targets.append(target)
    return targets


def _shell_workdir_requires_write(
    command: str,
    profile: OperationProfile,
    stdin: str | None = None,
) -> bool:
    for target in _shell_write_targets_from_inputs(command, stdin):
        if _shell_target_is_relative(target):
            return True
    for target in _mutating_command_write_targets_from_inputs(command, stdin):
        if _shell_target_is_relative(target):
            return True
    for target in getattr(profile, "requested_write_paths", ()):
        if _shell_target_is_relative(str(target)):
            return True
    return False


def _shell_workspace_relative_path(path: Path) -> str | None:
    ctx = current_tool_context.get()
    if ctx is None or not ctx.workspace_dir:
        return None
    workspace = Path(ctx.workspace_dir).expanduser().resolve(strict=False)
    try:
        return path.resolve(strict=False).relative_to(workspace).as_posix()
    except ValueError:
        return None


def _source_like_shell_target(raw_target: str, workdir: str | None) -> str | None:
    if _is_shell_null_write_target(raw_target):
        return None
    resolved = _resolve_shell_write_target(raw_target, workdir)
    relative_path = _shell_workspace_relative_path(resolved)
    if relative_path is None:
        return None
    if Path(relative_path).suffix.casefold() not in _SHELL_SOURCE_EXTENSIONS:
        return None
    if classify_workspace_path(relative_path) != "source":
        return None
    return relative_path


def _shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _inplace_shell_source_targets(command: str, workdir: str | None) -> list[str]:
    tokens = _shell_tokens(command)
    if not tokens:
        return []
    if not any(token in {"sed", "perl"} or token.endswith(("/sed", "/perl")) for token in tokens):
        return []
    if not any(
        token == "-i" or token.startswith("-i") or token.startswith("-pi") for token in tokens
    ):
        return []

    targets: list[str] = []
    for token in tokens:
        if token.startswith("-") or token in {"sed", "perl", "&&", ";", "|"}:
            continue
        if Path(token.strip("'\"")).suffix.casefold() not in _SHELL_SOURCE_EXTENSIONS:
            continue
        target = _source_like_shell_target(token, workdir)
        if target is not None:
            targets.append(target)
    return sorted(set(targets))


def _python_shell_source_targets(command: str, workdir: str | None) -> list[str]:
    if "python" not in command and "Path(" not in command and "open(" not in command:
        return []
    targets: list[str] = []
    for regex in (_PYTHON_OPEN_WRITE_PATH_RE, _PYTHON_PATH_WRITE_PATH_RE):
        for match in regex.finditer(command):
            raw_target = match.group("path")
            target = _source_like_shell_target(raw_target, workdir)
            if target is not None:
                targets.append(target)
    return sorted(set(targets))


def _shell_source_mutation_signal(command: str, workdir: str | None) -> dict[str, Any] | None:
    target_workdir = _shell_redirection_workdir(command, workdir)
    redirection_targets = [
        target
        for raw_target in _shell_write_targets(command)
        if (target := _source_like_shell_target(raw_target, target_workdir)) is not None
    ]
    inplace_targets = _inplace_shell_source_targets(command, target_workdir)
    python_targets = _python_shell_source_targets(command, target_workdir)

    reasons: list[str] = []
    if redirection_targets:
        reasons.append("redirection_or_tee")
    if inplace_targets:
        reasons.append("inplace_editor")
    if python_targets:
        reasons.append("python_write")

    targets = sorted(set(redirection_targets + inplace_targets + python_targets))
    if not targets:
        return None
    return {
        "shell_source_mutation_suspected": True,
        "shell_source_mutation_reason": ",".join(reasons),
        "shell_source_mutation_targets": targets[:20],
        "shell_source_mutation_target_count": len(targets),
    }


def _shell_source_mutation_telemetry_enabled() -> bool:
    ctx = current_tool_context.get()
    if ctx is None or ctx.allowed_tools is None:
        return False
    allowed = set(ctx.allowed_tools)
    return _SHELL_SOURCE_MUTATION_REQUIRED_TOOLS <= allowed and not (
        _SHELL_SOURCE_MUTATION_FORBIDDEN_TOOLS & allowed
    )


def _emit_shell_source_mutation_signal(
    *,
    tool_name: str,
    command: str,
    signal_payload: dict[str, Any] | None,
) -> None:
    if not signal_payload:
        return
    ctx = current_tool_context.get()
    callback = getattr(ctx, "on_runtime_event", None) if ctx is not None else None
    if callback is None:
        return
    event = {
        "feature": "shell_source_mutation",
        "name": "shell.source_mutation_suspected",
        "tool": tool_name,
        "tool_name": tool_name,
        "command_hash": mutation_ledger_text_hash(command),
        "agent_id": getattr(ctx, "agent_id", None),
        "session_key": getattr(ctx, "session_key", None),
        **signal_payload,
    }
    try:
        callback(event)
    except Exception:
        return


def _workspace_lockdown_shell_block(
    tool_name: str,
    command: str,
    workdir: str | None,
    *,
    stdin: str | None = None,
) -> dict[str, object] | None:
    roots = _workspace_lockdown_roots()
    if not roots:
        return None
    target_workdir = _shell_redirection_workdir(command, workdir)
    for target in _shell_write_targets_from_inputs(command, stdin):
        if _is_shell_null_write_target(target):
            continue
        resolved = _resolve_shell_write_target(target, target_workdir)
        if _path_inside_any_root(resolved, roots):
            continue
        return {
            "status": "blocked",
            "reason": "workspace_lockdown",
            "tool": tool_name,
            "command": command,
            "target": target,
            "resolved_path": str(resolved),
            "allowed_roots": [str(root) for root in roots],
            "message": (
                f"{tool_name} blocked by workspace lockdown: shell write target "
                f"{resolved} is outside allowed roots."
            ),
            "retryable": False,
        }
    return None


def _runtime_readonly_shell_block(
    tool_name: str,
    command: str,
    workdir: str | None,
    *,
    stdin: str | None = None,
    runtime: object | None = None,
) -> dict[str, object] | None:
    if full_host_access_active():
        return None
    roots = _runtime_readonly_roots(runtime)
    if not roots:
        return None
    runtime_mutation = _runtime_python_environment_mutation(command, workdir, roots)
    if runtime_mutation is not None:
        operation, root = runtime_mutation
        return {
            "status": "blocked",
            "reason": "runtime_readonly",
            "tool": tool_name,
            "command": command,
            "runtime_operation": operation,
            "readonly_root": str(root),
            "message": (
                f"{tool_name} blocked by sandbox runtime read-only policy: "
                f"{operation} would modify the OpenSquilla runtime environment under {root}. "
                "Create a project virtual environment in a writable workspace path, or install "
                "runtime dependencies outside Safe mode."
            ),
            "retryable": False,
        }
    for target in _shell_write_targets_from_inputs(command, stdin):
        resolved = _resolve_shell_write_target(target, workdir)
        candidate = resolved.expanduser().resolve(strict=False)
        for root in roots:
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            return {
                "status": "blocked",
                "reason": "runtime_readonly",
                "tool": tool_name,
                "command": command,
                "target": target,
                "resolved_path": str(candidate),
                "readonly_root": str(root),
                "message": (
                    f"{tool_name} blocked by sandbox runtime read-only policy: "
                    f"shell write target {candidate} is under read-only runtime root {root}."
                ),
                "retryable": False,
            }
    return None


def _windows_runtime_readonly_shell_block(
    tool_name: str,
    command: str,
    workdir: str | None,
    *,
    stdin: str | None = None,
) -> dict[str, object] | None:
    return _runtime_readonly_shell_block(
        tool_name,
        command,
        workdir,
        stdin=stdin,
        runtime=get_runtime(),
    )


def _runtime_python_environment_mutation(
    command: str,
    workdir: str | None,
    roots: tuple[Path, ...],
) -> tuple[str, Path] | None:
    for tokens in _iter_shell_command_tokens(command):
        result = _runtime_python_environment_mutation_from_tokens(tokens, workdir, roots)
        if result is not None:
            return result
    return None


def _iter_shell_command_tokens(command: str) -> Iterator[list[str]]:
    for statement in _split_shell_statements(command):
        try:
            tokens = shlex.split(statement, posix=not _statement_has_windows_path(statement))
        except ValueError:
            continue
        if not tokens:
            continue
        nested = _nested_posix_shell_command(tokens)
        if nested is not None:
            yield from _iter_shell_command_tokens(nested)
            continue
        yield tokens


def _split_shell_statements(command: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            current.append(char)
            quote = char
            index += 1
            continue
        if char == "&" and index + 1 < len(command) and command[index + 1] == "&":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            index += 2
            continue
        if char in ";|":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def _statement_has_windows_path(statement: str) -> bool:
    return _WINDOWS_ABSOLUTE_PATH_IN_SCRIPT_RE.search(statement) is not None


def _nested_posix_shell_command(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    command = Path(tokens[0]).name.lower()
    if command not in {"bash", "dash", "fish", "ksh", "sh", "zsh"}:
        return None
    for index, token in enumerate(tokens[1:], start=1):
        if token in {"-c", "-lc"} and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _runtime_python_environment_mutation_from_tokens(
    tokens: list[str],
    workdir: str | None,
    roots: tuple[Path, ...],
) -> tuple[str, Path] | None:
    command = Path(tokens[0]).name.lower()
    explicit_runtime_python = _explicit_python_command_targets_runtime(
        tokens[0],
        workdir,
        roots,
    )
    if len(tokens) >= 3 and _python_command_name(tokens[0]):
        if tokens[1:3] == ["-m", "ensurepip"]:
            if _explicit_command_path(tokens[0]) and not explicit_runtime_python:
                return None
            return "python -m ensurepip", (
                _runtime_root_for_command(tokens[0], workdir, roots)
                if explicit_runtime_python
                else roots[0]
            )
        if (
            explicit_runtime_python
            and len(tokens) >= 4
            and tokens[1:3] == ["-m", "pip"]
            and tokens[3] == "install"
        ):
            return "python -m pip install", _runtime_root_for_command(tokens[0], workdir, roots)

    if (
        command in {"pip", "pip3"}
        and len(tokens) >= 2
        and tokens[1] == "install"
        and _explicit_command_inside_runtime(tokens[0], workdir, roots)
    ):
        return "pip install", _runtime_root_for_command(tokens[0], workdir, roots)

    return None


def _python_command_name(executable: str) -> bool:
    name = ntpath.basename(executable.strip("'\"")).lower()
    for suffix in (".exe", ".cmd", ".bat"):
        name = name.removesuffix(suffix)
    return (
        re.fullmatch(
            r"python(?:\d+(?:\.\d+)*)?",
            name,
        )
        is not None
    )


def _explicit_python_command_targets_runtime(
    executable: str,
    workdir: str | None,
    roots: tuple[Path, ...],
) -> bool:
    if not _python_command_name(executable):
        return False
    return _explicit_command_inside_runtime(executable, workdir, roots)


def _explicit_command_inside_runtime(
    executable: str,
    workdir: str | None,
    roots: tuple[Path, ...],
) -> bool:
    path_text = executable.strip().strip("'\"")
    if _windows_explicit_path(path_text):
        candidate_text = _normalize_windows_path_text(path_text)
        return any(
            _windows_path_inside(candidate_text, _normalize_windows_path_text(str(root)))
            for root in roots
        )
    path = Path(path_text).expanduser()
    if path_text.startswith(("/", "~", "./", "../")):
        if not path.is_absolute():
            base = Path(workdir).expanduser() if workdir else Path.cwd()
            path = base / path
        candidate = path.resolve(strict=False)
        return any(_path_inside_any_root(candidate, [root]) for root in roots)
    return False


def _explicit_command_path(executable: str) -> bool:
    path_text = executable.strip().strip("'\"")
    return _windows_explicit_path(path_text) or path_text.startswith(("/", "~", "./", "../"))


def _windows_explicit_path(path_text: str) -> bool:
    return bool(
        re.match(r"^[A-Za-z]:[\\/]", path_text) or path_text.startswith(("\\\\", ".\\", "..\\"))
    )


def _normalize_windows_path_text(path_text: str) -> str:
    return path_text.replace("\\", "/").rstrip("/").lower()


def _windows_path_inside(candidate: str, root: str) -> bool:
    return candidate == root or candidate.startswith(root + "/")


def _runtime_root_for_command(
    executable: str,
    workdir: str | None,
    roots: tuple[Path, ...],
) -> Path:
    path_text = executable.strip().strip("'\"")
    if _windows_explicit_path(path_text):
        candidate_text = _normalize_windows_path_text(path_text)
        for root in roots:
            if _windows_path_inside(candidate_text, _normalize_windows_path_text(str(root))):
                return root
        return roots[0]
    path = Path(path_text).expanduser()
    if path_text.startswith(("/", "~", "./", "../")):
        if not path.is_absolute():
            base = Path(workdir).expanduser() if workdir else Path.cwd()
            path = base / path
        candidate = path.resolve(strict=False)
        for root in roots:
            if _path_inside_any_root(candidate, [root]):
                return root
    return roots[0]


def _host_shell_env(env: dict[str, str]) -> dict[str, str]:
    cleaned = _dedupe_windows_env_keys(env)
    if cleaned.get(OPENSQUILLA_NETWORK_ENV_KEY) != "proxy_allowlist":
        return cleaned
    for key in (*PROXY_ENV_KEYS, *NO_PROXY_ENV_KEYS):
        cleaned.pop(key, None)
    for key, _value in PROXY_CONTROL_ENV:
        cleaned.pop(key, None)
    cleaned.pop(PROXY_ACTIVE_ENV_KEY, None)
    return cleaned


def _dedupe_windows_env_keys(env: dict[str, str]) -> dict[str, str]:
    if os.name != "nt":
        return dict(env)
    cleaned: dict[str, str] = {}
    normalized_to_key: dict[str, str] = {}
    for key, value in env.items():
        normalized = key.upper()
        output_key = _WINDOWS_ENV_CANONICAL_KEYS.get(normalized)
        if output_key is None:
            output_key = normalized_to_key.get(normalized, key)
        previous_key = normalized_to_key.get(normalized)
        if previous_key is not None and previous_key != output_key:
            cleaned.pop(previous_key, None)
        normalized_to_key[normalized] = output_key
        cleaned[output_key] = value
    return cleaned


def _workspace_write_deny_shell_block(
    tool_name: str,
    command: str,
    workdir: str | None,
    *,
    stdin: str | None = None,
) -> dict[str, object] | None:
    from opensquilla.tools.write_policy import (
        match_workspace_write_deny,
        workspace_write_deny_block,
    )

    ctx = current_tool_context.get()
    workspace = (
        Path(ctx.workspace_dir).expanduser().resolve(strict=False)
        if ctx is not None and ctx.workspace_dir
        else None
    )
    target_workdir = _shell_redirection_workdir(command, workdir)
    candidate_targets = list(_shell_write_targets_from_inputs(command, stdin))
    mutator_targets: set[str] = set()
    if _write_deny_lever_enabled("OPENSQUILLA_WORKSPACE_WRITE_DENY_COMMAND_TARGETS"):
        for extra_target in _mutating_command_write_targets_from_inputs(command, stdin):
            if extra_target not in candidate_targets:
                candidate_targets.append(extra_target)
                mutator_targets.add(extra_target)
    if _write_deny_lever_enabled("OPENSQUILLA_WORKSPACE_WRITE_DENY_INTERPRETER_TARGETS"):
        for extra_target in _interpreter_write_targets_from_inputs(command, stdin):
            if extra_target not in candidate_targets:
                candidate_targets.append(extra_target)
                mutator_targets.add(extra_target)
    for target in candidate_targets:
        resolved = _resolve_shell_write_target(target, target_workdir)
        deny_match = match_workspace_write_deny(
            resolved,
            original_path=target,
            workspace=workspace,
            ctx=ctx,
        )
        if deny_match is None and target in mutator_targets and resolved.is_dir():
            # A directory operand (rm -rf tests) mutates everything beneath
            # it; match it against dir/** style globs as well.
            deny_match = match_workspace_write_deny(
                resolved,
                original_path=target,
                workspace=workspace,
                ctx=ctx,
                as_directory=True,
            )
        if deny_match is not None:
            return workspace_write_deny_block(tool_name, deny_match, command=command)
    return None


def _workspace_scratch_artifact_shell_block(
    tool_name: str,
    command: str,
    workdir: str | None,
) -> dict[str, object] | None:
    from opensquilla.tools.write_policy import (
        match_workspace_scratch_artifact,
        workspace_scratch_artifact_block,
    )

    ctx = current_tool_context.get()
    workspace = (
        Path(ctx.workspace_dir).expanduser().resolve(strict=False)
        if ctx is not None and ctx.workspace_dir
        else None
    )
    target_workdir = _shell_redirection_workdir(command, workdir)
    for target in _shell_write_targets(command):
        resolved = _resolve_shell_write_target(target, target_workdir)
        scratch_match = match_workspace_scratch_artifact(
            resolved,
            original_path=target,
            workspace=workspace,
            ctx=ctx,
        )
        if scratch_match is not None:
            return workspace_scratch_artifact_block(
                tool_name,
                scratch_match,
                command=command,
            )
    return None


def _source_diff_preservation_shell_block(
    command: str,
    workdir: str | None,
    *,
    stdin: str | None = None,
) -> str | None:
    source_diff_block = source_diff_preservation_block_json(
        command=command,
        workdir=workdir,
    )
    if source_diff_block is not None:
        return source_diff_block
    if stdin is None:
        return None
    for stdin_chunk in _iter_stdin_guard_chunks(stdin):
        source_diff_block = source_diff_preservation_block_json(
            command=stdin_chunk,
            workdir=workdir,
        )
        if source_diff_block is not None:
            return source_diff_block
    return None


def _endgame_git_freeze_shell_block(
    command: str,
    *,
    stdin: str | None = None,
) -> str | None:
    freeze_block = endgame_git_freeze_block_json(command=command)
    if freeze_block is not None:
        return freeze_block
    if stdin is None:
        return None
    for stdin_chunk in _iter_stdin_guard_chunks(stdin):
        freeze_block = endgame_git_freeze_block_json(command=stdin_chunk)
        if freeze_block is not None:
            return freeze_block
    return None


def _resolve_exec_timeout(timeout: float | int | None) -> float:
    if timeout is None:
        return _DEFAULT_EXEC_TIMEOUT
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return _DEFAULT_EXEC_TIMEOUT
    return max(0.01, min(value, _MAX_EXEC_TIMEOUT))


def _resolve_background_timeout(timeout: float | int | None) -> float:
    if timeout is None:
        return _DEFAULT_BACKGROUND_TIMEOUT
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return _DEFAULT_BACKGROUND_TIMEOUT
    return max(0.01, min(value, _MAX_BACKGROUND_TIMEOUT))


def _process_wait_default() -> float:
    ctx = current_tool_context.get()
    if ctx is not None and getattr(ctx, "coding_mode", False):
        return _CODING_PROCESS_WAIT_TIMEOUT
    return _DEFAULT_PROCESS_WAIT_TIMEOUT


def _resolve_process_wait_timeout(timeout: float | int | None) -> float:
    default = _process_wait_default()
    if timeout is None:
        return default
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return default
    return max(0.01, min(value, _MAX_PROCESS_WAIT_TIMEOUT))


def _effective_workdir(workdir: str | None) -> str | None:
    ctx = current_tool_context.get()
    if workdir:
        translated = (
            _windows_translate_posix_tmp_path(workdir)
            if _windows_sandbox_backend_active()
            else workdir
        )
        reject_foreign_host_path(translated, platform=os.name)
        raw = Path(translated).expanduser()
        if not raw.is_absolute() and ctx and ctx.workspace_dir:
            return str((Path(ctx.workspace_dir).expanduser().resolve() / raw).resolve())
        return str(raw.resolve())
    if ctx and ctx.workspace_dir:
        return str(Path(ctx.workspace_dir).expanduser().resolve())
    return None


def _shell_elevation_required_envelope(
    command: str,
    cwd: str | None,
    profile: OperationProfile,
    *,
    stdin: str | None = None,
) -> dict[str, object] | None:
    """Identify sandbox-incompatible writes without granting any authority."""

    if not _sandbox_path_access_enabled():
        return None
    workspace = _workspace_root_for_path_access()
    if workspace is None:
        return None
    target = ""
    reason = ""
    leading_cd = _leading_cd_prefix(command)
    candidates = list(_shell_write_access_targets(command, profile, stdin=stdin))
    workdir_write = bool(cwd and _shell_workdir_requires_write(command, profile, stdin=stdin))
    relative_write_depends_on_cwd = workdir_write or any(
        _shell_target_is_relative(raw_path) for raw_path in candidates
    )
    if leading_cd.unsafe_reason and relative_write_depends_on_cwd:
        reason = leading_cd.unsafe_reason
    elif profile.host_effect:
        reason = profile.host_effect
    else:
        if workdir_write:
            assert cwd is not None
            candidates.insert(0, cwd)
        target_workdir = _shell_redirection_workdir(command, cwd)
        for raw_path in candidates:
            resolved = _resolve_shell_write_target(raw_path, target_workdir)
            decision = decide_path_access(
                resolved,
                workspace=workspace,
                mounts=_active_sandbox_mounts(),
                write=True,
                profile=active_file_system_profile(workspace),
                logical_path=_logical_shell_write_target(raw_path, target_workdir),
            )
            if decision.status == "blocked":
                return _path_access_blocked_envelope(decision)
            if decision.status == "request":
                target = decision.normalized_path
                reason = decision.reason
                break
    if not reason:
        return None
    payload: dict[str, object] = {
        "status": "elevation_required",
        "reason": reason,
        "message": (
            "This exact command needs capabilities outside the sandbox's writable "
            "roots. Retry only if the user's request warrants it, using "
            "sandbox_permissions=require_escalated and a precise justification."
        ),
    }
    if target:
        payload["target"] = target
    return payload


def _shell_elevation_hard_block(
    tool_name: str,
    command: str,
    cwd: str | None,
    *,
    stdin: str | None = None,
) -> dict[str, object] | None:
    lockdown = _workspace_lockdown_shell_block(tool_name, command, cwd, stdin=stdin)
    if lockdown is not None:
        return lockdown
    return _workspace_write_deny_shell_block(tool_name, command, cwd, stdin=stdin)


def _shell_elevation_action(
    *,
    tool_name: str,
    action_kind: str,
    command: str,
    cwd: str | None,
    profile: OperationProfile,
    justification: str,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    prefix_rule: list[str] | None = None,
) -> ElevationAction:
    target_paths: list[tuple[str, str]] = []

    def _add_target(raw_path: str, access: str) -> None:
        resolved = str(_resolve_shell_write_target(raw_path, cwd))
        candidate = (resolved, access)
        if candidate not in target_paths:
            target_paths.append(candidate)

    if cwd:
        _add_target(cwd, "execute")
    for raw_path in _shell_read_access_targets(command, profile):
        _add_target(raw_path, "read")
    for raw_path in _shell_write_access_targets(command, profile, stdin=stdin):
        _add_target(raw_path, "write")

    content_payload = {
        "env": dict(sorted((env or {}).items())),
        "stdin_sha256": (
            hashlib.sha256(stdin.encode("utf-8")).hexdigest() if stdin is not None else None
        ),
        "powershell_file": _referenced_powershell_file_digest(command, workdir=cwd),
        "script_files": _referenced_script_file_digests(command, workdir=cwd),
    }
    content_digest = hashlib.sha256(
        json.dumps(
            content_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    argv = ("cmd.exe", "/d", "/s", "/c", command) if os.name == "nt" else ("/bin/sh", "-c", command)
    return ElevationAction(
        tool_name=tool_name,
        action_kind=action_kind,
        argv=argv,
        cwd=cwd or str(Path.cwd()),
        sandbox_permissions="require_escalated",
        justification=justification,
        target_paths=tuple(target_paths),
        network_targets=tuple(profile.requested_domains),
        content_digest=content_digest,
        prefix_rule=tuple(prefix_rule) if prefix_rule is not None else None,
        display=ApprovalDisplay(kind="run_command", target=command),
    )


def _gate_shell_elevation(
    action: ElevationAction,
    *,
    approval_id: str | None,
) -> dict[str, object] | None:
    if not action.justification.strip():
        return {
            "status": "elevation_required",
            "reason": "justification_required",
            "message": "A precise justification is required for elevated execution.",
        }
    ctx = current_tool_context.get()
    gate = gate_elevated_action(
        action,
        approval_id=approval_id,
        session_key=ctx.session_key if ctx is not None else None,
    )
    return None if gate.allowed else gate.to_envelope()


def _bg_status(session: _BgSession) -> str:
    if session.killed:
        return "killed"
    if session.timed_out:
        return "timed_out"
    if session.done:
        return "done"
    return "running"


def _bg_session_payload(session: _BgSession) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": session.session_id,
        "command": session.command,
        "status": _bg_status(session),
        "returncode": session.returncode,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "killed": session.killed,
        "timed_out": session.timed_out,
    }
    if session.local_urls:
        payload["local_urls"] = list(session.local_urls)
    code_task = _code_task_status_payload(session)
    if code_task:
        payload["code_task"] = code_task
    return payload


def _code_task_status_payload(session: _BgSession) -> dict[str, object] | None:
    if "code-task" not in session.command:
        return None
    output = _bg_rendered_output(session)
    marker = _parse_code_task_marker(output)
    if marker is None:
        return None
    status_path = Path(marker["status_path"]).expanduser()
    payload: dict[str, object] = dict(marker)
    if status_path.is_file():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            status = {}
        if isinstance(status, dict):
            for key in (
                "phase",
                "updated",
                "pid",
                "current_command",
                "last_output_at",
                "quiet_for_seconds",
                "state",
                "verified",
                "error",
                "final_failure_reason",
                "installer_path",
                "log_paths",
            ):
                if key in status:
                    payload[key] = status[key]
    return payload


def _parse_code_task_marker(output: str) -> dict[str, str] | None:
    for line in output.splitlines():
        if "[code-task] run started:" not in line or "status=" not in line:
            continue
        status_tail = line.split("status=", 1)[1]
        status_end = status_tail.find("status.json")
        if status_end < 0:
            continue
        status_path = status_tail[: status_end + len("status.json")]
        payload = {"status_path": status_path}
        run_match = re.search(r"run_id=([^\s]+)", line)
        if run_match:
            payload["run_id"] = run_match.group(1)
        if "artifact_dir=" in line and " status=" in line:
            payload["artifact_dir"] = line.split("artifact_dir=", 1)[1].split(" status=", 1)[0]
        return payload
    return None


def _local_server_urls_from_command(command: str) -> list[str]:
    urls: list[str] = []
    url_pattern = r"https?://(?:127\.0\.0\.1|localhost):\d{2,5}(?:/[^\s\"']*)?"
    for match in re.finditer(url_pattern, command):
        urls.append(match.group(0).rstrip(".,;)"))

    http_server = re.search(
        r"(?:^|[\s;&|])python(?:3(?:\.\d+)?)?\s+-m\s+http\.server(?:\s+(?P<port>\d{2,5}))?",
        command,
    )
    if http_server is not None:
        port = http_server.group("port") or "8000"
        urls.append(f"http://127.0.0.1:{port}/")

    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _background_process_result(session: _BgSession) -> str:
    lines = [
        f"session_id={session.session_id}",
        f"command: {session.command}",
        "status: running",
    ]
    if session.local_urls:
        lines.append("local_urls:")
        lines.extend(f"- {url}" for url in session.local_urls)
        lines.append(
            "note: If the user asked to view this in a browser, include the local URL "
            "in your reply."
        )
    return "\n".join(lines)


def _current_bg_context_is_admin() -> bool:
    ctx = current_tool_context.get()
    if ctx is None or not ctx.is_owner:
        return False
    if ctx.caller_kind in {CallerKind.CLI, CallerKind.WEB}:
        return True
    # A verified channel administrator is the same operator principal as a
    # WebUI owner. The ingress-only marker prevents an arbitrary channel
    # context with ``is_owner=True`` from managing other sessions' processes.
    return ctx.caller_kind is CallerKind.CHANNEL and ctx.channel_admin_verified


def _current_bg_context_allows(session: _BgSession) -> bool:
    if _current_bg_context_is_admin():
        return True
    ctx = current_tool_context.get()
    if ctx is None or not ctx.session_key:
        return False
    return session.session_key is not None and session.session_key == ctx.session_key


def _iter_visible_bg_sessions() -> list[_BgSession]:
    visible: list[_BgSession] = []
    for session in _bg_sessions.values():
        if session.session_key is None:
            log.warning("shell.bg_session_untagged", session_id=session.session_id)
        if _current_bg_context_allows(session):
            visible.append(session)
    return visible


def _require_bg_session(session_id: str | None) -> _BgSession:
    if not session_id:
        raise ToolError("'session_id' required")
    session = _bg_sessions.get(session_id)
    if session is None:
        raise ToolError(f"Unknown process session: {session_id}")
    if not _current_bg_context_allows(session):
        raise ToolError(f"Process session not accessible: {session_id}")
    return session


async def _read_bg_output(session: _BgSession) -> None:
    stdout = session.process.stdout
    if stdout is None:
        return
    while chunk := await stdout.read(4096):
        # Accumulate raw bytes and decode the whole buffer at render time so a
        # multibyte character split across a 4 KB chunk boundary is not garbled,
        # and so Windows legacy-code-page output is decoded correctly (issue #336).
        session.output_bytes.extend(chunk)


def _bg_rendered_output(session: _BgSession) -> str:
    """Decode the collected process output and append any synthetic markers."""
    return decode_subprocess_output(bytes(session.output_bytes)) + "".join(session.output_lines)


def _finalize_bg_session(session: _BgSession) -> None:
    session.returncode = session.process.returncode
    if session.process_tree is not None:
        # Querying also closes an empty Windows Job handle. A still-live tree
        # remains owned and discoverable by task-scoped Stop after its leader
        # has exited.
        session.process_tree.is_active()
    if session.ended_at is None:
        session.ended_at = time.time()
    session.done = True
    callbacks = list(session.cleanup_callbacks)
    session.cleanup_callbacks.clear()
    for callback in callbacks:
        with contextlib.suppress(Exception):
            callback()


async def _finalize_bg_session_async(session: _BgSession) -> None:
    _finalize_bg_session(session)
    callbacks = list(session.async_cleanup_callbacks)
    session.async_cleanup_callbacks.clear()
    for callback in callbacks:
        with contextlib.suppress(Exception):
            await callback()


async def _wait_bg_process(session: _BgSession, timeout: float) -> bool:
    try:
        await asyncio.wait_for(session.process.wait(), timeout=timeout)
    except TimeoutError:
        return False
    return True


async def _terminate_bg_session(session: _BgSession) -> bool:
    process_tree = session.process_tree
    if process_tree is None:
        process_tree = capture_process_tree_owner(
            session.process,
            isolated=session.process.returncode is None,
        )
        session.process_tree = process_tree
    stopped = await process_tree.terminate(
        graceful_timeout=_BACKGROUND_TERMINATE_TIMEOUT,
        kill_timeout=_BACKGROUND_KILL_TIMEOUT,
    )
    if not stopped:
        log.warning("background_process_termination_timeout", session_id=session.session_id)
    return stopped


async def cancel_background_processes_for_task(session_key: str, task_id: str) -> int:
    """Terminate registered background process trees owned by one exact task."""

    sessions = [
        session
        for session in tuple(_bg_sessions.values())
        if session.session_key == session_key
        and session.task_id == task_id
        and session.process_tree is not None
        and session.process_tree.is_active()
    ]
    if not sessions:
        return 0

    async def terminate_one(session: _BgSession) -> None:
        session.killed = True
        try:
            await _terminate_bg_session(session)
        except Exception:
            log.warning(
                "background_process_task_cancel_failed",
                session_id=session.session_id,
                task_id=task_id,
                exc_info=True,
            )

    cleanup = asyncio.gather(*(terminate_one(session) for session in sessions))
    # An abort RPC has a shorter shared deadline than process-tree escalation.
    # Keep owned cleanup alive if that deadline cancels this waiter.
    await asyncio.shield(cleanup)
    return len(sessions)


def active_background_process_task_owners() -> tuple[tuple[str, str], ...]:
    """Snapshot exact durable identities for live registered background processes."""

    return tuple(
        dict.fromkeys(
            (session.session_key, session.task_id)
            for session in tuple(_bg_sessions.values())
            if isinstance(session.session_key, str)
            and session.session_key
            and isinstance(session.task_id, str)
            and session.task_id
            and session.process_tree is not None
            and session.process_tree.is_active()
        )
    )


async def _wait_exec_process(proc: Any, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
    while proc.returncode is None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return proc.returncode is not None
        await asyncio.sleep(min(0.01, remaining))
    return True


async def _terminate_exec_process_tree(
    proc: Any,
    process_tree: ProcessTreeOwner | None = None,
) -> None:
    owner = process_tree or capture_process_tree_owner(
        proc,
        isolated=getattr(proc, "returncode", None) is None,
    )
    stopped = await owner.terminate(
        graceful_timeout=_EXEC_TERMINATE_TIMEOUT,
        kill_timeout=_EXEC_KILL_TIMEOUT,
    )
    if not stopped:
        log.warning("exec_command_termination_timeout", pid=proc.pid)


async def _write_exec_stdin(proc: Any, stdin_bytes: bytes | None) -> None:
    if stdin_bytes is None or proc.stdin is None:
        return
    stdin = proc.stdin
    try:
        for offset in range(0, len(stdin_bytes), _EXEC_STDIN_WRITE_CHUNK_BYTES):
            stdin.write(stdin_bytes[offset : offset + _EXEC_STDIN_WRITE_CHUNK_BYTES])
            await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        if not stdin.is_closing():
            stdin.close()
        wait_closed = getattr(stdin, "wait_closed", None)
        if wait_closed is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await wait_closed()


def _use_windows_blocking_exec_stdin() -> bool:
    return os.name == "nt"


async def _wait_exec_stdin_writer(
    proc: Any, writer_task: asyncio.Task[None], timeout: float
) -> bool:
    """Wait for stdin closure without mistaking a completed process for a hang.

    Windows' proactor pipe transport can leave ``StreamWriter.wait_closed()``
    pending briefly after the child has consumed EOF and exited. The process exit
    is authoritative in that case; the caller cancels the stale writer task after
    observing the return code.
    """

    deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
    while not writer_task.done() and proc.returncode is None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return writer_task.done() or proc.returncode is not None
        await asyncio.wait({writer_task}, timeout=min(0.01, remaining))
    if not writer_task.done():
        return proc.returncode is not None
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        await writer_task
    return True


async def _cancel_exec_stdin_writer(proc: Any, writer_task: asyncio.Task[None] | None) -> None:
    if writer_task is None or writer_task.done():
        return
    if proc.stdin is not None and not proc.stdin.is_closing():
        proc.stdin.close()
    writer_task.cancel()
    done, _pending = await asyncio.wait({writer_task}, timeout=0.05)
    if writer_task not in done:
        return
    with contextlib.suppress(
        asyncio.CancelledError,
        BrokenPipeError,
        ConnectionResetError,
    ):
        await writer_task


async def _await_bg_output_task(output_task: asyncio.Task[None]) -> None:
    try:
        await asyncio.wait_for(output_task, timeout=_BACKGROUND_KILL_TIMEOUT)
    except TimeoutError:
        output_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await output_task


def _create_windows_host_shell_process(command: str, **kwargs: Any) -> Any:
    return create_owned_popen(_windows_direct_powershell_argv(command), **kwargs)


def _terminate_windows_host_shell_process(
    proc: Any,
    process_tree: ProcessTreeOwner,
) -> None:
    if process_tree.windows_job is not None:
        with contextlib.suppress(OSError):
            process_tree.windows_job.terminate()
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=_EXEC_TERMINATE_TIMEOUT)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    try:
        proc.wait(timeout=_EXEC_KILL_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.warning("exec_command_termination_timeout", pid=proc.pid)


async def _communicate_windows_host_shell_process(
    proc: Any,
    process_tree: ProcessTreeOwner,
    stdin_bytes: bytes,
    timeout: float,
) -> bool:
    """Write finite Windows stdin outside Proactor and bound the worker wait."""

    communicate_task = asyncio.create_task(asyncio.to_thread(proc.communicate, input=stdin_bytes))
    try:
        done, _pending = await asyncio.wait(
            {communicate_task},
            timeout=max(0.0, timeout),
        )
    except asyncio.CancelledError:
        await asyncio.shield(
            asyncio.to_thread(_terminate_windows_host_shell_process, proc, process_tree)
        )
        done, _pending = await asyncio.wait(
            {communicate_task},
            timeout=_EXEC_TERMINATE_TIMEOUT + _EXEC_KILL_TIMEOUT,
        )
        if communicate_task in done:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                communicate_task.result()
        raise
    if communicate_task in done:
        await communicate_task
        return True

    await asyncio.to_thread(_terminate_windows_host_shell_process, proc, process_tree)
    done, _pending = await asyncio.wait(
        {communicate_task},
        timeout=_EXEC_TERMINATE_TIMEOUT + _EXEC_KILL_TIMEOUT,
    )
    if communicate_task in done:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await communicate_task
    return False


async def _run_windows_host_shell_command_with_stdin(
    command: str,
    *,
    cwd: str | None,
    env: dict[str, str],
    stdin_bytes: bytes,
    effective_timeout: float,
) -> str:
    try:
        with tempfile.TemporaryFile() as output_file:
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            proc = _create_windows_host_shell_process(
                command,
                stdin=subprocess.PIPE,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=env,
                creationflags=creationflags,
            )
            process_tree = capture_process_tree_owner(proc, isolated=os.name == "nt")
            completed = await _communicate_windows_host_shell_process(
                proc,
                process_tree,
                stdin_bytes,
                effective_timeout,
            )
            await _terminate_exec_process_tree(proc, process_tree)
            output_file.flush()
            output_file.seek(0)
            raw_output = output_file.read()
            if not completed:
                return _exec_timeout_output(effective_timeout, command, raw_output)
            output = decode_subprocess_output(raw_output)
            return f"exit_code={proc.returncode}\n{output}"
    except Exception as exc:
        return f"[error] {exc}"


def _exec_timeout_output(effective_timeout: float, command: str, raw: bytes | str) -> str:
    """Build the exec-timeout result, preserving partial output captured so far.

    Keeps the ``[timeout after Ns]`` marker (asserted by tests and recognised by the
    model as a timeout signal) and appends a tail slice of whatever the command
    emitted before it was killed, so the model can see which test hung instead of a
    bare marker with no evidence.
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    base = f"[timeout after {effective_timeout}s]\ncommand: {command}"
    if not text:
        return base
    if len(text) > _EXEC_TIMEOUT_OUTPUT_TAIL_CHARS:
        text = "...[partial output truncated]...\n" + text[-_EXEC_TIMEOUT_OUTPUT_TAIL_CHARS:]
    return f"{base}\n--- partial output before timeout ---\n{text}"


async def _run_host_shell_command(
    command: str,
    *,
    cwd: str | None,
    env: dict[str, str],
    stdin_bytes: bytes | None,
    effective_timeout: float,
) -> str:
    if _use_windows_blocking_exec_stdin() and stdin_bytes is not None:
        return await _run_windows_host_shell_command_with_stdin(
            command,
            cwd=cwd,
            env=env,
            stdin_bytes=stdin_bytes,
            effective_timeout=effective_timeout,
        )
    try:
        with tempfile.TemporaryFile() as output_file:
            subprocess_kwargs: dict[str, Any] = {
                "stdin": asyncio.subprocess.PIPE if stdin_bytes is not None else None,
                "stdout": output_file,
                "stderr": asyncio.subprocess.STDOUT,
                "cwd": cwd,
                "env": env,
            }
            if os.name == "posix":
                subprocess_kwargs["start_new_session"] = True
            else:
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if creationflags:
                    subprocess_kwargs["creationflags"] = creationflags

            loop = asyncio.get_running_loop()
            deadline = loop.time() + effective_timeout

            def timeout_result() -> str:
                output_file.flush()
                output_file.seek(0)
                return _exec_timeout_output(effective_timeout, command, output_file.read())

            proc = await _create_host_shell_subprocess(command, **subprocess_kwargs)
            process_tree = capture_process_tree_owner(proc, isolated=True)
            stdin_writer: asyncio.Task[None] | None = None
            remaining = deadline - loop.time()
            if remaining <= 0:
                await _terminate_exec_process_tree(proc, process_tree)
                return timeout_result()
            try:
                if stdin_bytes is not None:
                    stdin_writer = asyncio.create_task(_write_exec_stdin(proc, stdin_bytes))
                    if not await _wait_exec_stdin_writer(proc, stdin_writer, remaining):
                        await _cancel_exec_stdin_writer(proc, stdin_writer)
                        await _terminate_exec_process_tree(proc, process_tree)
                        return timeout_result()
            except TimeoutError:
                await _cancel_exec_stdin_writer(proc, stdin_writer)
                await _terminate_exec_process_tree(proc, process_tree)
                return timeout_result()
            except asyncio.CancelledError:
                await _cancel_exec_stdin_writer(proc, stdin_writer)
                await asyncio.shield(_terminate_exec_process_tree(proc, process_tree))
                raise

            try:
                remaining = deadline - loop.time()
                if remaining <= 0 or not await _wait_exec_process(proc, remaining):
                    await _cancel_exec_stdin_writer(proc, stdin_writer)
                    await _terminate_exec_process_tree(proc, process_tree)
                    return timeout_result()
            except asyncio.CancelledError:
                await _cancel_exec_stdin_writer(proc, stdin_writer)
                await asyncio.shield(_terminate_exec_process_tree(proc, process_tree))
                raise
            await _cancel_exec_stdin_writer(proc, stdin_writer)
            await _terminate_exec_process_tree(proc, process_tree)

            output_file.flush()
            output_file.seek(0)
            output = decode_subprocess_output(output_file.read())
            return f"exit_code={proc.returncode}\n{output}"
    except Exception as e:
        return f"[error] {e}"


async def _run_full_host_shell_command(
    command: str,
    *,
    workdir: str | None,
    timeout: float,
    env: dict[str, str] | None,
    stdin: str | None,
) -> str:
    """Execute directly on the host without sandbox policy or safety preflight."""

    runtime = get_runtime()
    merged_env = _base_shell_environment()
    if env:
        merged_env.update(env)
    merged_env = _managed_skill_environment(
        _runtime_shell_environment(
            merged_env,
            require_bundled=_guest_requires_managed_runtime(),
        )
    )
    apply_utf8_child_env(merged_env)
    _append_windows_app_alias_path(merged_env, runtime=runtime)
    merged_env = _dedupe_windows_env_keys(_host_shell_env(merged_env))
    return await _run_host_shell_command(
        command,
        cwd=_effective_workdir(workdir),
        env=merged_env,
        stdin_bytes=stdin.encode("utf-8") if stdin is not None else None,
        effective_timeout=_resolve_exec_timeout(timeout),
    )


async def _create_host_shell_subprocess(
    command: str,
    *,
    windows_host: bool = False,
    **kwargs: Any,
) -> Any:
    if os.name == "nt" or windows_host:
        return await create_owned_subprocess_exec(
            *_windows_direct_powershell_argv(command),
            **kwargs,
        )
    return await create_owned_subprocess_shell(command, **kwargs)


@tool(
    name="exec_command",
    description=(
        "Execute a shell command and return stdout/stderr with exit code. Use for "
        "repository inspection, builds, tests, and command-line tools. For workspace "
        "source changes, prefer read_source followed by edit_source so edits stay "
        "revision-gated, structured, and reviewable. If a structured sandbox result "
        "says elevation_required, retry the exact command with "
        "sandbox_permissions=require_escalated and a precise justification only when "
        "the user's request warrants it; ordinary command failures are not grounds "
        "for elevation. "
        "On Windows, commands run in PowerShell; use PowerShell syntax such as "
        "Set-Location -LiteralPath, or wrap cmd.exe syntax such as cd /d with cmd /c."
    ),
    params={
        "command": {"type": "string", "description": "Shell command to execute."},
        "workdir": {"type": "string", "description": "Working directory (default: cwd)."},
        "timeout": {
            "type": "number",
            "description": "Timeout in seconds (default 60, max 600).",
        },
        "env": {
            "type": "object",
            "description": "Extra environment variable overrides.",
            "additionalProperties": {"type": "string"},
        },
        "stdin": {
            "type": "string",
            "description": "Data to write to the command's standard input.",
        },
        "sandbox_permissions": {
            "type": "string",
            "enum": ["use_default", "require_escalated"],
            "description": (
                "Use require_escalated only when this exact command needs host "
                "capabilities outside the sandbox."
            ),
        },
        "justification": {
            "type": "string",
            "description": "Short user-facing reason for the exact elevated action.",
        },
        "prefix_rule": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional narrow command-prefix suggestion. Automatic review never "
                "turns it into persistent authorization."
            ),
        },
        "approval_id": {
            "type": "string",
            "description": "Sandbox path approval record for shell path access.",
        },
    },
    required=["command"],
    runtime_only_arguments=("approval_id",),
    execution_timeout_seconds=_DEFAULT_EXEC_TIMEOUT + _EXEC_TOOL_TIMEOUT_PADDING,
    execution_timeout_argument="timeout",
    execution_timeout_padding=_EXEC_TOOL_TIMEOUT_PADDING,
    sandbox=SandboxToolDescriptor.process(
        kind="shell.exec",
        argv_factory=lambda a: ("exec_command", str(a.get("command", ""))),
        cwd_factory=lambda a: a.get("workdir") if isinstance(a.get("workdir"), str) else None,
        env_factory=lambda a: a.get("env") if isinstance(a.get("env"), dict) else None,
        enforce=False,
        record_payload=False,
    ),
)
async def exec_command(
    command: str,
    workdir: str | None = None,
    timeout: float = _DEFAULT_EXEC_TIMEOUT,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    approval_id: str | None = None,
    *,
    sandbox_permissions: str = "use_default",
    justification: str = "",
    prefix_rule: list[str] | None = None,
) -> str:
    runtime = get_runtime()
    reject_windows_guest_process(runtime)
    if full_host_access_active():
        cwd = _effective_workdir(workdir)
        mutation_before = snapshot_current_workspace_mutations()
        source_mutation_signal = (
            _shell_source_mutation_signal(command, cwd)
            if _shell_source_mutation_telemetry_enabled()
            else None
        )
        output = await _run_full_host_shell_command(
            command,
            workdir=workdir,
            timeout=timeout,
            env=env,
            stdin=stdin,
        )
        metadata: dict[str, Any] = {"command_hash": mutation_ledger_text_hash(command)}
        if source_mutation_signal is not None:
            metadata.update(source_mutation_signal)
            _emit_shell_source_mutation_signal(
                tool_name="exec_command",
                command=command,
                signal_payload=source_mutation_signal,
            )
        record_observed_workspace_mutations(
            tool_name="exec_command",
            before=mutation_before,
            metadata=metadata,
        )
        return output

    windows_process_sandbox = _windows_sandbox_backend_active(runtime)
    runtime_readonly_block = _runtime_readonly_shell_block(
        "exec_command", command, workdir, stdin=stdin, runtime=runtime
    )
    if runtime_readonly_block is not None:
        return json.dumps(runtime_readonly_block, ensure_ascii=False)
    if sandbox_permissions not in {"use_default", "require_escalated"}:
        return json.dumps(
            {
                "status": "invalid_request",
                "reason": "invalid_sandbox_permissions",
            }
        )
    guest_host_block = _guest_host_execution_block(sandbox_permissions)
    if guest_host_block is not None:
        return json.dumps(guest_host_block, ensure_ascii=False)
    original_profile = _profile_shell_command(command, workdir=workdir)
    host_execution = _host_execution_allowed()
    backend_retry_granted = False
    if windows_process_sandbox and not host_execution and sandbox_permissions == "use_default":
        command = _windows_translate_posix_tmp_references(command)
        if workdir:
            workdir = _windows_translate_posix_tmp_path(workdir)

    result = check_safe_bin(command)
    cwd = _effective_workdir(workdir)
    profile = original_profile

    # Denylist: hard-block, never bypassable
    if not result.allowed:
        raise ToolError(result.reason)
    result = _apply_safe_command_policy(command, result)

    sensitive_block = _sensitive_external_transfer_block(
        "exec_command", command, workdir=cwd, stdin=stdin
    )
    if sensitive_block is None:
        sensitive_block = _sensitive_shell_block("exec_command", command, workdir=cwd, stdin=stdin)
    if sensitive_block is not None:
        return sensitive_block
    approval_denial = _approval_policy_denial(
        "exec_command",
        command,
        result.reason or "Command denied by approval policy.",
    )
    if approval_denial is not None:
        return json.dumps(approval_denial, ensure_ascii=False)
    recursive_delete = await _gate_recursive_delete(
        command,
        cwd=cwd,
        approval_id=approval_id,
        require_exact=(result.needs_approval or sandbox_permissions == "require_escalated"),
    )
    if recursive_delete is not None:
        return recursive_delete
    if result.needs_approval:
        command_approval = _gate_safe_command_approval(
            command,
            cwd=cwd,
            reason=result.reason or "This high-risk command requires approval.",
            approval_id=approval_id,
        )
        if command_approval is not None:
            return command_approval
    scratch_block = _workspace_scratch_artifact_shell_block("exec_command", command, cwd)
    if scratch_block is not None:
        return json.dumps(scratch_block, ensure_ascii=False)
    # Freeze first: when both guards would fire, the source-diff decision's
    # candidate-lost marking and revert-observed events must not run for a
    # command the freeze block prevents from executing at all.
    endgame_freeze_block = _endgame_git_freeze_shell_block(command, stdin=stdin)
    if endgame_freeze_block is not None:
        return endgame_freeze_block
    source_diff_block = _source_diff_preservation_shell_block(command, cwd, stdin=stdin)
    if source_diff_block is not None:
        return source_diff_block
    if not host_execution:
        hard_block = _shell_elevation_hard_block(
            "exec_command",
            command,
            cwd,
            stdin=stdin,
        )
        if hard_block is not None:
            return json.dumps(hard_block, ensure_ascii=False)
    if not host_execution and sandbox_permissions == "require_escalated":
        elevation = _gate_shell_elevation(
            _shell_elevation_action(
                tool_name="exec_command",
                action_kind="shell.exec",
                command=command,
                cwd=cwd,
                profile=profile,
                justification=justification,
                env=env,
                stdin=stdin,
                prefix_rule=prefix_rule,
            ),
            approval_id=approval_id,
        )
        if elevation is not None:
            return json.dumps(elevation, ensure_ascii=False)
        host_execution = True
    elif not host_execution:
        elevation_required = _shell_elevation_required_envelope(
            command,
            cwd,
            profile,
            stdin=stdin,
        )
        if elevation_required is not None:
            return json.dumps(elevation_required, ensure_ascii=False)
    if not host_execution:
        path_access = _sandbox_workdir_access_envelope(
            cwd,
            write=_shell_workdir_requires_write(command, profile, stdin=stdin),
            approval_id=approval_id,
        )
        if path_access is not None:
            return json.dumps(path_access, ensure_ascii=False)
        path_access = _sandbox_read_path_access_envelope(
            profile,
            cwd,
            command=command,
            approval_id=approval_id,
        )
        if path_access is not None:
            return json.dumps(path_access, ensure_ascii=False)
        protected_block = _protected_metadata_write_block(
            "exec_command", command, cwd, profile, stdin=stdin
        )
        if protected_block is not None:
            return json.dumps(protected_block, ensure_ascii=False)
        path_access = _sandbox_write_path_access_envelope(
            profile,
            cwd,
            command,
            stdin=stdin,
            approval_id=approval_id,
        )
        if path_access is not None:
            return json.dumps(path_access, ensure_ascii=False)
        lockdown_block = _workspace_lockdown_shell_block("exec_command", command, cwd, stdin=stdin)
        if lockdown_block is not None:
            return json.dumps(lockdown_block, ensure_ascii=False)
        deny_block = _workspace_write_deny_shell_block("exec_command", command, cwd, stdin=stdin)
        if deny_block is not None:
            return json.dumps(deny_block, ensure_ascii=False)
    elif _write_deny_lever_enabled("OPENSQUILLA_WORKSPACE_WRITE_DENY_HOST_SHELL"):
        # Host execution skips the sandbox policy block above entirely, which
        # also skips deny-glob enforcement. This opt-in keeps just the deny
        # check active for host-executed shell commands.
        deny_block = _workspace_write_deny_shell_block("exec_command", command, cwd, stdin=stdin)
        if deny_block is not None:
            return json.dumps(deny_block, ensure_ascii=False)

    merged_env = _base_shell_environment()
    if env:
        merged_env.update(env)
    merged_env = _managed_skill_environment(
        _runtime_shell_environment(
            merged_env,
            require_bundled=_guest_requires_managed_runtime(),
        )
    )
    apply_utf8_child_env(merged_env)
    _append_windows_app_alias_path(merged_env, runtime=runtime)
    merged_env = _dedupe_windows_env_keys(merged_env)
    effective_timeout = _resolve_exec_timeout(timeout)
    stdin_bytes = stdin.encode("utf-8") if stdin is not None else None
    mutation_before = snapshot_current_workspace_mutations()
    source_mutation_signal = (
        _shell_source_mutation_signal(command, cwd)
        if _shell_source_mutation_telemetry_enabled()
        else None
    )

    def finish(output: str) -> str:
        metadata: dict[str, Any] = {"command_hash": mutation_ledger_text_hash(command)}
        if source_mutation_signal is not None:
            metadata.update(source_mutation_signal)
            _emit_shell_source_mutation_signal(
                tool_name="exec_command",
                command=command,
                signal_payload=source_mutation_signal,
            )
        record_observed_workspace_mutations(
            tool_name="exec_command",
            before=mutation_before,
            metadata=metadata,
        )
        # Effect enforcement runs after the ledger so the raw escape stays
        # honestly recorded before any revert rewrites the workspace.
        return enforce_workspace_write_deny_effects(
            tool_name="exec_command",
            before=mutation_before,
            output=output,
        )

    runtime_unavailable = _strict_runtime_unavailable_envelope(command, merged_env)
    if runtime_unavailable is not None:
        return finish(json.dumps(runtime_unavailable, ensure_ascii=False))

    if runtime is not None and runtime.effective.sandbox_enabled and not host_execution:
        if windows_process_sandbox:
            _apply_windows_session_tmp_env(merged_env)
        decision, policy, request = await gate_action(
            action_kind="shell.exec",
            argv=("exec_command", command),
            cwd=_sandbox_shell_policy_cwd(cwd),
            env=merged_env,
            hints=_level_hints_for_shell_profile(
                profile,
                warnlist_matched=result.needs_approval,
            ),
        )
        if isinstance(decision, DenialResult):
            return finish(json.dumps(decision.to_dict()))
        if isinstance(decision, ApprovedHostExecution):
            host_execution = True
            backend_retry_granted = True
        else:
            retry_gate = consume_backend_denial_retry(
                approval_id,
                request,
                policy,
                runtime=runtime,
            )
            if retry_gate is not None:
                if not retry_gate.allowed:
                    return finish(json.dumps(retry_gate.to_envelope(), ensure_ascii=False))
                host_execution = True
                backend_retry_granted = True
            else:
                backend_cwd = _sandbox_shell_backend_cwd(cwd, request)
                backend_policy = request.policy
                backend_policy = _policy_with_active_tool_mounts(backend_policy)
                backend_policy = _policy_with_managed_toolchain_mounts(backend_policy)
                backend_policy = _policy_with_windows_shell_runtime_mounts(backend_policy, runtime)
                backend_policy = _policy_with_wall_timeout(backend_policy, effective_timeout)
                backend_policy = _trusted_managed_network_policy(backend_policy, runtime)
                backend_request = SandboxRequest(
                    argv=_sandbox_shell_backend_argv(command, runtime, cwd=backend_cwd),
                    cwd=backend_cwd,
                    action_kind=request.action_kind,
                    policy=backend_policy,
                    stdin=stdin_bytes,
                    env=dict(getattr(request, "env", None) or merged_env),
                    reason=getattr(request, "reason", ""),
                    session_id=getattr(request, "session_id", ""),
                    run_mode=getattr(request, "run_mode", ""),
                )
                preflight = await preflight_subprocess_managed_network(backend_request, runtime)
                if isinstance(preflight, DenialResult):
                    return finish(json.dumps(preflight.to_dict()))
                if isinstance(preflight, dict):
                    return finish(json.dumps(preflight))
                try:
                    sandbox_result = await _run_backend_with_managed_network(
                        backend_request,
                        runtime=runtime,
                    )
                except SandboxBackendError as exc:
                    review_action = _shell_elevation_action(
                        tool_name="exec_command",
                        action_kind="shell.exec",
                        command=command,
                        cwd=cwd,
                        profile=profile,
                        justification=(
                            "Sandbox backend unavailable; retry this exact command on host."
                        ),
                        env=env,
                        stdin=stdin,
                        prefix_rule=prefix_rule,
                    )
                    escalation = await escalate_unavailable_backend_in_managed_mode(
                        exc,
                        request,
                        policy,
                        runtime=runtime,
                        review_action=review_action,
                    )
                    if escalation is not None:
                        if isinstance(escalation, DenialResult):
                            return finish(json.dumps(escalation.to_dict(), ensure_ascii=False))
                        return finish(json.dumps(escalation.to_envelope(), ensure_ascii=False))
                    raise
                except Exception as exc:
                    raise ToolError(f"Sandboxed shell execution failed: {exc}") from exc
                if is_likely_sandbox_denied(sandbox_result):
                    review_action = _shell_elevation_action(
                        tool_name="exec_command",
                        action_kind="shell.exec",
                        command=command,
                        cwd=cwd,
                        profile=profile,
                        justification=("Sandbox denied this exact command; retry it on host."),
                        env=env,
                        stdin=stdin,
                        prefix_rule=prefix_rule,
                    )
                    escalation = await escalate_backend_denial(
                        sandbox_result,
                        request,
                        policy,
                        runtime=runtime,
                        review_action=review_action,
                    )
                    if isinstance(escalation, DenialResult):
                        return finish(json.dumps(escalation.to_dict()))
                    return finish(json.dumps(escalation.to_envelope(), ensure_ascii=False))
                output = sandbox_result.stdout
                if sandbox_result.stderr:
                    output += sandbox_result.stderr
                output = _append_sandbox_network_hint(output)
                output = _append_patch_hygiene_warning(command, cwd, output)
                output = _append_masked_pipeline_failure_warning(
                    command,
                    sandbox_result.returncode,
                    output,
                )
                return finish(f"exit_code={sandbox_result.returncode}\n{output}")

    if host_execution:
        log.info(
            "shell_exec_host",
            command=_audit_command(command),
            run_mode=_context_run_mode(),
            elevation_grant=(sandbox_permissions == "require_escalated" or backend_retry_granted),
            host_effect=profile.host_effect,
        )
        merged_env = _host_shell_env(merged_env)

    host_output = await _run_host_shell_command(
        command,
        cwd=cwd,
        env=merged_env,
        stdin_bytes=stdin_bytes,
        effective_timeout=effective_timeout,
    )
    exit_code_match = re.match(r"exit_code=(-?\d+)\n", host_output)
    if exit_code_match is None:
        return finish(host_output)
    returncode = int(exit_code_match.group(1))
    output = host_output[exit_code_match.end() :]
    output = _append_patch_hygiene_warning(command, cwd, output)
    output = _append_masked_pipeline_failure_warning(
        command,
        returncode,
        output,
    )
    return finish(f"exit_code={returncode}\n{output}")


async def _start_host_background_process(
    command: str,
    *,
    cwd: str | None,
    effective_timeout: float,
    runtime: object | None,
    env: dict[str, str] | None = None,
) -> str:
    """Start a host background process without sandbox policy or safety preflight."""

    session_id = str(uuid.uuid4())[:8]
    base_env = dict(env) if env is not None else _base_shell_environment()
    host_env = _managed_skill_environment(
        _runtime_shell_environment(
            base_env,
            require_bundled=_guest_requires_managed_runtime(),
        )
    )
    apply_utf8_child_env(host_env)
    host_env = _host_shell_env(host_env)
    _append_windows_app_alias_path(host_env, runtime=runtime)
    host_env = _dedupe_windows_env_keys(host_env)

    process_kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
        "cwd": cwd,
        "env": host_env,
    }
    if os.name == "posix":
        process_kwargs["start_new_session"] = True
    else:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if creationflags:
            process_kwargs["creationflags"] = creationflags
    proc = await _create_host_shell_subprocess(
        command,
        windows_host=_windows_sandbox_backend_active(runtime),
        **process_kwargs,
    )
    process_tree = capture_process_tree_owner(proc, isolated=True)

    ctx = current_tool_context.get()
    session = _BgSession(
        session_id=session_id,
        command=command,
        process=proc,
        process_tree=process_tree,
        session_key=ctx.session_key if ctx is not None else None,
        task_id=ctx.task_id if ctx is not None else None,
        agent_id=ctx.agent_id if ctx is not None else None,
        is_owner_run=bool(ctx.is_owner) if ctx is not None else False,
        local_urls=_local_server_urls_from_command(command),
    )
    _bg_sessions[session_id] = session

    async def _collect_host() -> None:
        output_task = asyncio.create_task(_read_bg_output(session))
        try:
            await asyncio.wait_for(proc.wait(), timeout=effective_timeout)
        except TimeoutError:
            session.timed_out = True
            await _terminate_bg_session(session)
            session.output_lines.append(f"[timeout after {effective_timeout}s]\n")
        finally:
            await _await_bg_output_task(output_task)
            await _finalize_bg_session_async(session)

    session.collector_task = asyncio.create_task(_collect_host())
    return _background_process_result(session)


@tool(
    name="background_process",
    description=(
        "Run a shell command in the background. Returns a session_id for polling. "
        "If the sandbox reports elevation_required, use require_escalated with a "
        "precise justification only for the exact user-authorized command. "
        "On Windows, commands run in PowerShell; use PowerShell syntax such as "
        "Set-Location -LiteralPath, or wrap cmd.exe syntax such as cd /d with cmd /c."
    ),
    params={
        "command": {"type": "string", "description": "Shell command to run in background."},
        "workdir": {"type": "string", "description": "Working directory (default: cwd)."},
        "timeout": {
            "type": "number",
            "description": "Timeout in seconds (default 1800, max 3600).",
        },
        "sandbox_permissions": {
            "type": "string",
            "enum": ["use_default", "require_escalated"],
            "description": (
                "Use require_escalated only when this exact background command needs "
                "host capabilities outside the sandbox."
            ),
        },
        "justification": {
            "type": "string",
            "description": "Short user-facing reason for the exact elevated action.",
        },
        "prefix_rule": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional narrow command-prefix suggestion. Automatic review never "
                "turns it into persistent authorization."
            ),
        },
        "approval_id": {
            "type": "string",
            "description": "Sandbox path approval record for shell path access.",
        },
    },
    required=["command"],
    runtime_only_arguments=("approval_id",),
    sandbox=SandboxToolDescriptor.process(
        kind="shell.background",
        argv_factory=lambda a: ("background_process", str(a.get("command", ""))),
        cwd_factory=lambda a: a.get("workdir") if isinstance(a.get("workdir"), str) else None,
        enforce=False,
        record_payload=False,
    ),
)
async def background_process(
    command: str,
    workdir: str | None = None,
    timeout: float = _DEFAULT_BACKGROUND_TIMEOUT,
    approval_id: str | None = None,
    *,
    sandbox_permissions: str = "use_default",
    justification: str = "",
    prefix_rule: list[str] | None = None,
) -> str:
    runtime = get_runtime()
    reject_windows_guest_process(runtime)
    if full_host_access_active():
        return await _start_host_background_process(
            command,
            cwd=_effective_workdir(workdir),
            effective_timeout=_resolve_background_timeout(timeout),
            runtime=get_runtime(),
        )

    windows_process_sandbox = _windows_sandbox_backend_active(runtime)
    runtime_readonly_block = _runtime_readonly_shell_block(
        "background_process", command, workdir, runtime=runtime
    )
    if runtime_readonly_block is not None:
        return json.dumps(runtime_readonly_block, ensure_ascii=False)
    if sandbox_permissions not in {"use_default", "require_escalated"}:
        return json.dumps(
            {
                "status": "invalid_request",
                "reason": "invalid_sandbox_permissions",
            }
        )
    guest_host_block = _guest_host_execution_block(sandbox_permissions)
    if guest_host_block is not None:
        return json.dumps(guest_host_block, ensure_ascii=False)
    original_profile = _profile_shell_command(command, workdir=workdir)
    host_execution = _host_execution_allowed()
    backend_retry_granted = False
    if windows_process_sandbox and not host_execution and sandbox_permissions == "use_default":
        command = _windows_translate_posix_tmp_references(command)
        if workdir:
            workdir = _windows_translate_posix_tmp_path(workdir)

    result = check_safe_bin(command)
    cwd = _effective_workdir(workdir)
    profile = original_profile
    if not result.allowed:
        raise ToolError(result.reason)
    result = _apply_safe_command_policy(command, result)
    sensitive_block = _sensitive_external_transfer_block("background_process", command, workdir=cwd)
    if sensitive_block is None:
        sensitive_block = _sensitive_shell_block("background_process", command, workdir=cwd)
    if sensitive_block is not None:
        return sensitive_block
    approval_denial = _approval_policy_denial(
        "background_process",
        command,
        result.reason or "Command denied by approval policy.",
    )
    if approval_denial is not None:
        return json.dumps(approval_denial, ensure_ascii=False)
    recursive_delete = await _gate_recursive_delete(
        command,
        cwd=cwd,
        approval_id=approval_id,
        require_exact=(result.needs_approval or sandbox_permissions == "require_escalated"),
    )
    if recursive_delete is not None:
        return recursive_delete
    if result.needs_approval:
        command_approval = _gate_safe_command_approval(
            command,
            cwd=cwd,
            reason=result.reason or "This high-risk command requires approval.",
            approval_id=approval_id,
        )
        if command_approval is not None:
            return command_approval
    scratch_block = _workspace_scratch_artifact_shell_block(
        "background_process",
        command,
        cwd,
    )
    if scratch_block is not None:
        return json.dumps(scratch_block, ensure_ascii=False)
    # Freeze first, as in exec_command: no candidate-lost bookkeeping for a
    # command the freeze block prevents from executing.
    endgame_freeze_block = _endgame_git_freeze_shell_block(command)
    if endgame_freeze_block is not None:
        return endgame_freeze_block
    source_diff_block = _source_diff_preservation_shell_block(command, cwd)
    if source_diff_block is not None:
        return source_diff_block
    if not host_execution:
        hard_block = _shell_elevation_hard_block(
            "background_process",
            command,
            cwd,
        )
        if hard_block is not None:
            return json.dumps(hard_block, ensure_ascii=False)
    if not host_execution and sandbox_permissions == "require_escalated":
        elevation = _gate_shell_elevation(
            _shell_elevation_action(
                tool_name="background_process",
                action_kind="shell.background",
                command=command,
                cwd=cwd,
                profile=profile,
                justification=justification,
                prefix_rule=prefix_rule,
            ),
            approval_id=approval_id,
        )
        if elevation is not None:
            return json.dumps(elevation, ensure_ascii=False)
        host_execution = True
    elif not host_execution:
        elevation_required = _shell_elevation_required_envelope(command, cwd, profile)
        if elevation_required is not None:
            return json.dumps(elevation_required, ensure_ascii=False)
    if not host_execution:
        path_access = _sandbox_workdir_access_envelope(
            cwd,
            write=_shell_workdir_requires_write(command, profile),
            approval_id=approval_id,
        )
        if path_access is not None:
            return json.dumps(path_access, ensure_ascii=False)
        path_access = _sandbox_read_path_access_envelope(
            profile,
            cwd,
            command=command,
            approval_id=approval_id,
        )
        if path_access is not None:
            return json.dumps(path_access, ensure_ascii=False)
        protected_block = _protected_metadata_write_block(
            "background_process", command, cwd, profile
        )
        if protected_block is not None:
            return json.dumps(protected_block, ensure_ascii=False)
        path_access = _sandbox_write_path_access_envelope(
            profile,
            cwd,
            command,
            approval_id=approval_id,
        )
        if path_access is not None:
            return json.dumps(path_access, ensure_ascii=False)
        lockdown_block = _workspace_lockdown_shell_block("background_process", command, cwd)
        if lockdown_block is not None:
            return json.dumps(lockdown_block, ensure_ascii=False)
        deny_block = _workspace_write_deny_shell_block("background_process", command, cwd)
        if deny_block is not None:
            return json.dumps(deny_block, ensure_ascii=False)
    elif _write_deny_lever_enabled("OPENSQUILLA_WORKSPACE_WRITE_DENY_HOST_SHELL"):
        # Same opt-in as exec_command: keep deny-glob enforcement active for
        # host-executed background commands.
        deny_block = _workspace_write_deny_shell_block("background_process", command, cwd)
        if deny_block is not None:
            return json.dumps(deny_block, ensure_ascii=False)
    effective_timeout = _resolve_background_timeout(timeout)
    merged_env = _managed_skill_environment(_base_shell_environment())
    runtime_unavailable = _strict_runtime_unavailable_envelope(command, merged_env)
    if runtime_unavailable is not None:
        return json.dumps(runtime_unavailable, ensure_ascii=False)

    if runtime is not None and runtime.effective.sandbox_enabled and not host_execution:
        apply_utf8_child_env(merged_env)
        _append_windows_app_alias_path(merged_env, runtime=runtime)
        merged_env = _dedupe_windows_env_keys(merged_env)
        if windows_process_sandbox:
            _apply_windows_session_tmp_env(merged_env)
        decision, policy, request = await gate_action(
            action_kind="shell.background",
            argv=("background_process", command),
            cwd=_sandbox_shell_policy_cwd(cwd),
            env=merged_env,
            hints=_level_hints_for_shell_profile(
                profile,
                warnlist_matched=result.needs_approval,
            ),
        )
        if isinstance(decision, DenialResult):
            return json.dumps(decision.to_dict())
        if isinstance(decision, ApprovedHostExecution):
            log.info(
                "background_process_host",
                command=_audit_command(command),
                run_mode=_context_run_mode(),
                elevation_grant=True,
                host_effect=profile.host_effect,
            )
            return await _start_host_background_process(
                command,
                cwd=cwd,
                effective_timeout=effective_timeout,
                runtime=runtime,
                env=dict(getattr(request, "env", None) or merged_env),
            )
        retry_gate = consume_backend_denial_retry(
            approval_id,
            request,
            policy,
            runtime=runtime,
        )
        if retry_gate is not None:
            if not retry_gate.allowed:
                return json.dumps(retry_gate.to_envelope(), ensure_ascii=False)
            host_execution = True
            backend_retry_granted = True
        else:
            backend_cwd = _sandbox_shell_backend_cwd(cwd, request)
            backend_policy = policy
            backend_policy = _policy_with_active_tool_mounts(backend_policy)
            backend_policy = _policy_with_managed_toolchain_mounts(backend_policy)
            backend_policy = _policy_with_windows_shell_runtime_mounts(backend_policy, runtime)
            backend_policy = _policy_with_wall_timeout(backend_policy, effective_timeout)
            backend_policy = _trusted_managed_network_policy(backend_policy, runtime)
            backend_request = SandboxRequest(
                argv=_sandbox_shell_backend_argv(command, runtime, cwd=backend_cwd),
                cwd=backend_cwd,
                action_kind=request.action_kind,
                policy=backend_policy,
                env=dict(getattr(request, "env", None) or merged_env),
                session_id=getattr(request, "session_id", ""),
                run_mode=getattr(request, "run_mode", ""),
            )
            preflight = await preflight_subprocess_managed_network(backend_request, runtime)
            if isinstance(preflight, DenialResult):
                return json.dumps(preflight.to_dict())
            if isinstance(preflight, dict):
                return json.dumps(preflight)
            managed_network = await prepare_subprocess_managed_network_proxy(
                backend_request,
                runtime=runtime,
            )
            try:
                spawned = await _spawn_sandboxed_background_process(
                    runtime=runtime,
                    request=managed_network.request,
                )
            except SandboxBackendError as exc:
                await managed_network.cleanup()
                review_action = _shell_elevation_action(
                    tool_name="background_process",
                    action_kind="shell.background",
                    command=command,
                    cwd=cwd,
                    profile=profile,
                    justification=(
                        "Sandbox backend unavailable; retry this exact background command on host."
                    ),
                    prefix_rule=prefix_rule,
                )
                escalation = await escalate_unavailable_backend_in_managed_mode(
                    exc,
                    request,
                    policy,
                    runtime=runtime,
                    review_action=review_action,
                )
                if escalation is not None:
                    if isinstance(escalation, DenialResult):
                        return json.dumps(escalation.to_dict(), ensure_ascii=False)
                    return json.dumps(escalation.to_envelope(), ensure_ascii=False)
                raise
            except Exception:
                await managed_network.cleanup()
                raise
            session_id = str(uuid.uuid4())[:8]
            ctx = current_tool_context.get()
            session = _BgSession(
                session_id=session_id,
                command=command,
                process=spawned.process,
                process_tree=spawned.process_tree,
                session_key=ctx.session_key if ctx is not None else None,
                task_id=ctx.task_id if ctx is not None else None,
                agent_id=ctx.agent_id if ctx is not None else None,
                is_owner_run=bool(ctx.is_owner) if ctx is not None else False,
                local_urls=_local_server_urls_from_command(command),
                cleanup_callbacks=spawned.cleanup_callbacks,
                async_cleanup_callbacks=[
                    *spawned.async_cleanup_callbacks,
                    managed_network.cleanup,
                ],
            )
            _bg_sessions[session_id] = session

            async def _collect_restricted() -> None:
                output_task = asyncio.create_task(_read_bg_output(session))
                try:
                    await asyncio.wait_for(spawned.process.wait(), timeout=effective_timeout)
                except TimeoutError:
                    session.timed_out = True
                    await _terminate_bg_session(session)
                    session.output_lines.append(f"[timeout after {effective_timeout}s]\n")
                finally:
                    await _await_bg_output_task(output_task)
                    await _finalize_bg_session_async(session)

            session.collector_task = asyncio.create_task(_collect_restricted())
            return _background_process_result(session)

    if host_execution:
        log.info(
            "background_process_host",
            command=_audit_command(command),
            run_mode=_context_run_mode(),
            elevation_grant=(sandbox_permissions == "require_escalated" or backend_retry_granted),
            host_effect=profile.host_effect,
        )

    return await _start_host_background_process(
        command,
        cwd=cwd,
        effective_timeout=effective_timeout,
        runtime=runtime,
    )


async def _spawn_sandboxed_background_process(
    *,
    runtime,
    request: SandboxRequest,
) -> _SpawnedBackgroundProcess:
    backend = runtime.backend
    if isinstance(backend, BubblewrapBackend):
        bridge: LinuxProxyBridgeHost | None = None
        bridge_tmp: tempfile.TemporaryDirectory[str] | None = None
        wrapper_tmp: tempfile.TemporaryDirectory[str] | None = None
        async_cleanup_callbacks: list[Callable[[], Awaitable[None]]] = []
        cleanup_callbacks: list[Callable[[], None]] = []
        synthetic_registrations: tuple[SyntheticMountRegistration, ...] = ()
        protected_create_registrations: tuple[ProtectedCreateRegistration, ...] = ()
        try:
            bridge_uds_path: Path | None = None
            bridge_script_path: Path | None = None
            exec_wrapper_path: Path | None = None
            if request.policy.network is NetworkMode.PROXY_ALLOWLIST:
                proxy = request.policy.network_proxy
                if proxy is None:
                    raise ToolError(
                        "NetworkMode.PROXY_ALLOWLIST requires a network proxy "
                        "for bubblewrap background processes"
                    )
                bridge_tmp = tempfile.TemporaryDirectory(
                    prefix="opensquilla-bwrap-background-proxy-"
                )
                bridge_uds_path = Path(bridge_tmp.name) / "proxy.sock"
                bridge = LinuxProxyBridgeHost(
                    bridge_uds_path,
                    proxy.host,
                    proxy.port,
                )
                await bridge.start()
                bridge_uds_path = bridge.uds_path
                bridge_script_path = bridge.script_path
                exec_wrapper_path = bridge.exec_wrapper_path

                async def cleanup_bridge() -> None:
                    assert bridge is not None
                    assert bridge_tmp is not None
                    await bridge.stop()
                    bridge_tmp.cleanup()

                async_cleanup_callbacks.append(cleanup_bridge)
            else:
                wrapper_tmp = tempfile.TemporaryDirectory(
                    prefix="opensquilla-bwrap-background-exec-"
                )
                exec_wrapper_path = Path(wrapper_tmp.name) / "linux_exec_wrapper.py"
                materialize_linux_exec_wrapper(exec_wrapper_path)

                def cleanup_wrapper() -> None:
                    assert wrapper_tmp is not None
                    wrapper_tmp.cleanup()

                cleanup_callbacks.append(cleanup_wrapper)
            plan = build_bwrap_plan(
                request,
                bridge_uds_path=bridge_uds_path,
                bridge_script_path=bridge_script_path,
                exec_wrapper_path=exec_wrapper_path,
                mount_proc=bool(getattr(probe_bwrap(), "supports_proc", True)),
            )
            argv = plan.argv
            synthetic_registrations = register_synthetic_mount_targets(
                plan.synthetic_mount_targets,
            )
            protected_create_registrations = register_protected_create_targets(
                plan.protected_create_targets,
            )
            if plan.preserved_files:

                def cleanup_preserved_files() -> None:
                    for file in plan.preserved_files:
                        file.close()

                cleanup_callbacks.append(cleanup_preserved_files)
            if plan.synthetic_mount_targets:

                def cleanup_synthetic_mounts() -> None:
                    cleanup_synthetic_mount_registrations(synthetic_registrations)

                cleanup_callbacks.append(cleanup_synthetic_mounts)
            if plan.protected_create_targets:

                def cleanup_protected_create() -> None:
                    messages = cleanup_protected_create_registrations(
                        protected_create_registrations,
                    )
                    for message in messages:
                        log.warning("background_process_policy_violation", message=message)

                cleanup_callbacks.append(cleanup_protected_create)
            process = await create_owned_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=resource_preexec_from_limits(request.policy.limits),
                pass_fds=tuple(file.fileno() for file in plan.preserved_files),
            )
            return _SpawnedBackgroundProcess(
                process=process,
                process_tree=capture_process_tree_owner(process, isolated=True),
                cleanup_callbacks=cleanup_callbacks,
                async_cleanup_callbacks=async_cleanup_callbacks,
            )
        except Exception:
            cleanup_synthetic_mount_registrations(synthetic_registrations)
            cleanup_protected_create_registrations(protected_create_registrations)
            plan_obj = locals().get("plan")
            for file in getattr(plan_obj, "preserved_files", ()):
                file.close()
            if bridge is not None:
                await bridge.stop()
            if bridge_tmp is not None:
                bridge_tmp.cleanup()
            if wrapper_tmp is not None:
                wrapper_tmp.cleanup()
            raise
    if isinstance(backend, NoopBackend):
        process = await create_owned_subprocess_exec(
            *request.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(request.cwd),
            env=getattr(request, "env", None) or {},
            start_new_session=True,
        )
        return _SpawnedBackgroundProcess(
            process=process,
            process_tree=capture_process_tree_owner(process, isolated=True),
        )
    if isinstance(backend, SeatbeltBackend):
        tmp_ctx: tempfile.TemporaryDirectory[str] | None = None
        profile_path: Path | None = None

        def cleanup() -> None:
            if profile_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(profile_path)
            if tmp_ctx is not None:
                tmp_ctx.cleanup()

        try:
            tmp_dir: Path | None = None
            if request.policy.tmp_writable:
                tmp_ctx = tempfile.TemporaryDirectory(prefix="opensquilla-seatbelt-tmp-")
                tmp_dir = Path(tmp_ctx.name)
            profile = render_seatbelt_profile(request, tmp_dir=tmp_dir)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                prefix="opensquilla-seatbelt-",
                suffix=".sb",
                delete=False,
            ) as profile_file:
                profile_file.write(profile)
                profile_file.flush()
                profile_path = Path(profile_file.name)
            argv = build_seatbelt_argv(request, profile_path)
            env = seatbelt_env_for_policy(
                request.policy,
                getattr(request, "env", None) or {},
                tmp_dir=tmp_dir,
            )
            process = await create_owned_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(request.cwd),
                env=env,
                start_new_session=True,
            )
            return _SpawnedBackgroundProcess(
                process=process,
                process_tree=capture_process_tree_owner(process, isolated=True),
                cleanup_callbacks=[cleanup],
            )
        except Exception:
            cleanup()
            raise
    if isinstance(backend, UnavailableBackend):
        raise SandboxBackendError(f"sandbox backend unavailable: {backend.reason}")
    raise ToolError(f"Sandbox backend {backend.name!r} does not support background shell")


def get_bg_session(session_id: str) -> _BgSession | None:
    session = _bg_sessions.get(session_id)
    if session is None or not _current_bg_context_allows(session):
        return None
    return session


@tool(
    name="process",
    description=(
        "Manage background_process sessions created by OpenSquilla. To await a "
        "long-running background command, call action='wait' (blocks until it "
        "exits or the timeout elapses) instead of polling in a loop."
    ),
    params={
        "action": {
            "type": "string",
            "description": "Action: list, poll, wait, log, kill, remove, write, submit, eof.",
        },
        "session_id": {
            "type": "string",
            "description": "Target background_process session id.",
        },
        "sessionId": {
            "type": "string",
            "description": "Compatibility alias for session_id.",
        },
        "data": {
            "type": "string",
            "description": "Data to write to stdin. submit appends a newline.",
        },
        "offset": {
            "type": "integer",
            "description": "For log, character offset to start reading from.",
        },
        "limit": {
            "type": "integer",
            "description": "For log, maximum characters to return.",
        },
        "timeout": {
            "type": "number",
            "description": (
                "For wait: max seconds to block for the process to exit (default "
                "600, max 5400). On timeout, returns with the process still "
                "running so you can wait again."
            ),
        },
    },
    required=["action"],
    execution_timeout_seconds=_MAX_PROCESS_WAIT_TIMEOUT + _PROCESS_WAIT_TIMEOUT_PADDING,
    execution_timeout_argument="timeout",
    execution_timeout_padding=_PROCESS_WAIT_TIMEOUT_PADDING,
    sandbox=SandboxToolDescriptor.custom(kind="process", enforce=False),
)
async def process(
    action: str,
    session_id: str | None = None,
    sessionId: str | None = None,  # noqa: N803 - legacy camelCase alias.
    data: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    timeout: float | None = None,
) -> str:
    if action == "list":
        sessions = [_bg_session_payload(session) for session in _iter_visible_bg_sessions()]
        return json.dumps({"status": "ok", "action": action, "sessions": sessions})

    resolved_session_id = session_id or sessionId
    session = _require_bg_session(resolved_session_id)

    if action == "poll":
        return json.dumps(
            {"status": "ok", "action": action, "session": _bg_session_payload(session)}
        )

    if action == "wait":
        wait_timeout = _resolve_process_wait_timeout(timeout)
        exited = session.done or session.process.returncode is not None
        if not exited:
            exited = await _wait_bg_process(session, wait_timeout)
        exited = exited or session.done or session.process.returncode is not None
        if exited:
            if session.collector_task is not None and not session.collector_task.done():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(session.collector_task),
                        timeout=_BACKGROUND_KILL_TIMEOUT,
                    )
            if not session.done:
                await _finalize_bg_session_async(session)
        return json.dumps(
            {
                "status": "ok",
                "action": action,
                "exited": bool(session.done or session.process.returncode is not None),
                "session": _bg_session_payload(session),
            }
        )

    if action == "log":
        output = _bg_rendered_output(session)
        start = max(0, int(offset or 0))
        requested_limit = 20000 if limit is None else int(limit)
        max_chars = max(0, min(requested_limit, 100000))
        end = start + max_chars
        sliced = output[start:end]
        return json.dumps(
            {
                "status": "ok",
                "action": action,
                "session": _bg_session_payload(session),
                "output": sliced,
                "offset": start,
                "limit": max_chars,
                "truncated": start > 0 or end < len(output),
            }
        )

    if action == "kill":
        tree_active = bool(
            session.process_tree is not None and session.process_tree.is_active()
        )
        if not tree_active and (session.done or session.process.returncode is not None):
            if session.collector_task is not None and not session.collector_task.done():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(session.collector_task),
                        timeout=_BACKGROUND_KILL_TIMEOUT,
                    )
            if not session.done:
                await _finalize_bg_session_async(session)
            status = _bg_status(session)
            return json.dumps(
                {
                    "status": status,
                    "action": action,
                    "session_id": session.session_id,
                    "session": _bg_session_payload(session),
                }
            )

        session.killed = True
        await _terminate_bg_session(session)
        if session.collector_task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(session.collector_task),
                    timeout=_BACKGROUND_KILL_TIMEOUT,
                )
        if not session.done:
            await _finalize_bg_session_async(session)
        status = _bg_status(session)
        return json.dumps(
            {
                "status": status,
                "action": action,
                "session_id": session.session_id,
                "session": _bg_session_payload(session),
            }
        )

    if action == "remove":
        if not session.done:
            raise ToolError(f"Cannot remove running session: {session.session_id}")
        if session.process_tree is not None and session.process_tree.is_active():
            session.killed = True
            if not await _terminate_bg_session(session):
                raise ToolError(
                    f"Cannot remove session with a live process tree: {session.session_id}"
                )
        del _bg_sessions[session.session_id]
        return json.dumps({"status": "removed", "action": action, "session_id": session.session_id})

    if action in {"write", "submit"}:
        if data is None:
            raise ToolError("'data' required")
        if session.done:
            raise ToolError(f"Cannot write to completed session: {session.session_id}")
        stdin = session.process.stdin
        if stdin is None or stdin.is_closing():
            raise ToolError(f"Session stdin is closed: {session.session_id}")
        write_data = data if action == "write" else f"{data}\n"
        encoded = write_data.encode("utf-8")
        try:
            stdin.write(encoded)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ToolError(f"Session stdin is closed: {session.session_id}") from exc
        return json.dumps(
            {
                "status": "written" if action == "write" else "submitted",
                "action": action,
                "session_id": session.session_id,
                "bytes": len(encoded),
                "session": _bg_session_payload(session),
            }
        )

    if action == "eof":
        stdin = session.process.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()
            wait_closed = getattr(stdin, "wait_closed", None)
            if wait_closed is not None:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await wait_closed()
        return json.dumps(
            {
                "status": "eof",
                "action": action,
                "session_id": session.session_id,
                "session": _bg_session_payload(session),
            }
        )

    raise ToolError("Invalid action: list|poll|wait|log|kill|remove|write|submit|eof")
