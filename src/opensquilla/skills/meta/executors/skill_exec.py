"""Executor for ``skill_exec`` meta-steps.

Runs a wrapped-CLI skill via its ``entrypoint`` manifest — no LLM, no
sub-Agent. Resolves ``skill.entrypoint`` from the injected ``skill_loader``,
renders ``command`` / ``args`` (and optional ``env`` / ``stdin`` / ``assemble``
templates) against ``inputs`` + ``outputs`` + ``with`` (the step's
rendered ``with_args``), then runs the subprocess in a task-owned tree.
Stdout is interpreted per ``parse`` (``text`` | ``json`` |
``lines``) and returned as the step output. Text output uses ``\n`` line
endings on every platform so downstream Meta steps can compare and persist it
deterministically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json as _json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path as _Path
from typing import Any

import jinja2
import structlog

from opensquilla.git_runtime import resolve_git_capability
from opensquilla.process_tree import (
    capture_process_tree_owner,
    create_owned_subprocess_exec,
)
from opensquilla.redaction import redact_error_text
from opensquilla.safety.secret_redaction import is_secret_key
from opensquilla.skills.capability_runtime import (
    META_CAPABILITY_API_KEY_ENV,
    META_CAPABILITY_BASE_URL_ENV,
    META_CAPABILITY_INTERNAL_CREDENTIAL_LEASE_TOKEN,
    META_CAPABILITY_INTERNAL_CREDENTIAL_SOURCE,
    META_CAPABILITY_INTERNAL_PROVIDER,
    META_CAPABILITY_INTERNAL_SESSION_KEY,
    META_CAPABILITY_PROVIDER_ENV,
    META_CAPABILITY_PROXY_ENV,
    META_OPENROUTER_API_KEY_ENV,
)
from opensquilla.skills.meta.replay_safety import (
    SAFE_NO_SUBMIT_EXIT_CODE,
    PaidReceiptProofError,
    PaidReceiptProofText,
    encode_paid_replay_safety,
    is_external_paid_step,
)
from opensquilla.skills.meta.templating import _JINJA_ENV, render_with_args
from opensquilla.skills.meta.types import MetaStep
from opensquilla.skills.runtime_env import MEDIA_FONTS_DIR_ENV, managed_skill_env
from opensquilla.skills.types import SkillLayer
from opensquilla.subprocess_encoding import apply_utf8_child_env, decode_subprocess_output

log = structlog.get_logger(__name__)

_ERROR_DETAIL_MAX_CHARS = 500
_ERROR_DETAIL_SCAN_CHARS = _ERROR_DETAIL_MAX_CHARS * 4
_REDACTED = "[REDACTED]"
_PAID_MEDIA_TRUSTED_ENV = frozenset(
    {
        META_CAPABILITY_PROVIDER_ENV,
        META_CAPABILITY_API_KEY_ENV,
        META_CAPABILITY_BASE_URL_ENV,
        META_CAPABILITY_PROXY_ENV,
        META_OPENROUTER_API_KEY_ENV,
    }
)
_TRUSTED_ENV_ALLOWLIST: dict[str, frozenset[str]] = {
    "audio-cog": _PAID_MEDIA_TRUSTED_ENV,
    "nano-banana-pro": _PAID_MEDIA_TRUSTED_ENV,
    "nano-banana-pro-openrouter": _PAID_MEDIA_TRUSTED_ENV,
    "openrouter-video-generator": _PAID_MEDIA_TRUSTED_ENV,
    "seedance-2-prompt": _PAID_MEDIA_TRUSTED_ENV,
}
_PAID_REPLAY_SAFE_EXIT_SKILLS = frozenset(_TRUSTED_ENV_ALLOWLIST)
_PROFILE_POOL_SOURCE = "profile_pool"
_BASE_ENV_KEYS = frozenset(
    {
        # Executable and platform discovery.
        "path",
        "pathext",
        "systemroot",
        "windir",
        "comspec",
        # Cross-platform temporary and home directories.
        "temp",
        "tmp",
        "tmpdir",
        "home",
        "userprofile",
        "homedrive",
        "homepath",
        # Basic user/terminal/locale compatibility.
        "user",
        "username",
        "logname",
        "shell",
        "term",
        "lang",
        "language",
        "tz",
        # Explicit Python child encoding choices are operator/runtime intent.
        # Preserve them so the Windows UTF-8 compatibility default remains a
        # default instead of silently overriding a declared legacy code page.
        "pythonioencoding",
        "pythonutf8",
        # Common TLS trust-store overrides. These are paths, not provider
        # credentials, and are needed in managed/corporate installations.
        "ssl_cert_file",
        "ssl_cert_dir",
        "requests_ca_bundle",
        "curl_ca_bundle",
        "node_extra_ca_certs",
        "git_ssl_cainfo",
        "aws_ca_bundle",
        # Operator-owned, non-secret media font selection. Presence matters:
        # an explicit empty value disables managed font injection.
        MEDIA_FONTS_DIR_ENV.casefold(),
    }
)


def _replace_base_dir(value: str, base_dir: str) -> str:
    """Substitute the path placeholder without leaving mixed separators."""
    stripped_base_dir = base_dir.rstrip("/\\")
    native_join = f"{stripped_base_dir}{os.sep}" if base_dir else ""
    return (
        value.replace("{baseDir}/", native_join)
        .replace("{baseDir}\\", native_join)
        .replace("{baseDir}", base_dir)
    )


def _normalize_base_dir_argument(value: str, base_dir: str) -> str:
    """Normalize every separator below a substituted ``{baseDir}`` prefix."""

    prefix = base_dir.rstrip("/\\")
    if not prefix or value == prefix:
        return value
    for separator in ("/", "\\"):
        if value.startswith(f"{prefix}{separator}"):
            suffix = value[len(prefix) + 1 :]
            normalized_suffix = re.sub(r"[/\\]+", lambda _match: os.sep, suffix)
            return f"{prefix}{os.sep}{normalized_suffix}"
    return value


_PROVIDER_FAILURE_EXIT_KINDS = {
    79: "auth_invalid",
    80: "insufficient_credits",
    81: "rate_limited",
}
_PAID_RECEIPT_STDOUT_LABELS = {
    "nano-banana-pro": "IMAGE_GENERATION_RECEIPT:",
    "seedance-2-prompt": "VIDEO_GENERATION_RECEIPT:",
}
_MAX_PAID_RECEIPT_BYTES = 64_000
# String sequences (OSC/DCS/SOS/PM/APC) tolerate a missing terminator so a
# diagnostic clipped by the subprocess cannot expose terminal control payloads.
_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b\x9c\n]*(?:\x07|\x1b\\|\x9c)?"
    r"|[PX^_][^\x1b\x9c\n]*(?:\x1b\\|\x9c)?"
    r"|[@-Z\\-_]"
    r")"
    r"|\x9b[0-?]*[ -/]*[@-~]"
    r"|\x9d[^\x07\x9c\x1b\n]*(?:\x07|\x9c|\x1b\\)?"
    r"|[\x90\x98\x9e\x9f][^\x9c\x1b\n]*(?:\x9c|\x1b\\)?"
)
# Keep tabs/newlines readable while removing executable C0/C1 controls.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _normalize_text_newlines(value: str) -> str:
    """Apply Python's universal-newline contract to Meta text output."""

    return value.replace("\r\n", "\n").replace("\r", "\n")


def _is_sensitive_env_key(key: str) -> bool:
    lowered = key.lower()
    return (
        is_secret_key(lowered)
        or lowered.endswith(("token", "_token", "-token", ".token"))
        or key == META_CAPABILITY_PROXY_ENV
    )


def _minimal_skill_base_env(source: Mapping[str, str]) -> dict[str, str]:
    """Copy only OS/runtime essentials, never the Gateway's full env."""

    env = {
        key: value
        for key, value in source.items()
        if key.casefold() in _BASE_ENV_KEYS or key.casefold().startswith("lc_")
    }
    if not any(key.casefold() == "path" for key in env):
        env["PATH"] = os.defpath
    return env


def _declared_ambient_env_keys(
    skill_spec: Any,
    *,
    effective_skill: str,
) -> frozenset[str]:
    """Return ambient variables an exact code-owned skill may receive.

    Workspace/project overrides receive no ambient variables beyond the
    minimal OS allowlist. Bundled skills retain exactly the names explicitly
    declared in eligibility metadata, including legacy opaque credential names
    that do not contain ``key`` or ``token``. Paid-media capability consumers
    receive none: a validated parent's volatile lease is injected later.
    """

    if getattr(skill_spec, "layer", None) != SkillLayer.BUNDLED:
        return frozenset()
    # Provider-capability consumers are never allowed to rediscover an
    # ambient key while running under MetaOrchestrator. Otherwise an
    # untrusted workspace parent could compose the genuine bundled child and
    # bypass the trusted top-level workflow gate. Direct CLI invocation keeps
    # its documented environment behavior because it does not use this
    # executor.
    if effective_skill in _TRUSTED_ENV_ALLOWLIST:
        return frozenset()
    metadata = getattr(skill_spec, "metadata", None)
    requires = getattr(metadata, "requires", None)
    declared = (
        *(getattr(requires, "env", ()) or ()),
        *(getattr(requires, "env_any", ()) or ()),
    )
    return frozenset(name for name in declared if isinstance(name, str) and name)


def _skill_requires_git(skill_spec: Any) -> bool:
    metadata = getattr(skill_spec, "metadata", None)
    requires = getattr(metadata, "requires", None)
    bins = getattr(requires, "bins", ()) or ()
    return any(isinstance(name, str) and name.casefold() == "git" for name in bins)


def _pin_safe_git_on_path(child_env: dict[str, str]) -> None:
    """Make a resolved real Git win over the macOS developer-tools shim."""

    capability = resolve_git_capability()
    if not capability.available or capability.executable is None:
        reason = capability.reason or "git_unavailable"
        raise RuntimeError(f"GIT_UNAVAILABLE: Git is unavailable ({reason}).")
    path_key = next((key for key in child_env if key.casefold() == "path"), "PATH")
    existing = child_env.get(path_key, "")
    safe_directory = str(capability.executable.parent)
    remaining = [
        value
        for value in existing.split(os.pathsep)
        if value and os.path.normcase(value) != os.path.normcase(safe_directory)
    ]
    child_env[path_key] = os.pathsep.join((safe_directory, *remaining))


def _ambient_value(source: Mapping[str, str], name: str) -> str | None:
    value = source.get(name)
    if value is not None or os.name != "nt":
        return value
    folded = name.casefold()
    return next(
        (candidate for key, candidate in source.items() if key.casefold() == folded),
        None,
    )


def _report_profile_pool_failure(
    *,
    returncode: int,
    trusted_env: Mapping[str, str] | None,
) -> None:
    """Park a failed pooled key for the next explicit run, never this run.

    Only reserved exits from exact bundled paid-media entrypoints reach this
    helper. The internal provenance fields are created by the parent runtime,
    ignored by child environment injection, and contain no credential bytes.
    """

    kind_value = _PROVIDER_FAILURE_EXIT_KINDS.get(returncode)
    if kind_value is None or not trusted_env:
        return
    if (
        trusted_env.get(META_CAPABILITY_INTERNAL_CREDENTIAL_SOURCE)
        != _PROFILE_POOL_SOURCE
    ):
        return
    provider_id = str(trusted_env.get(META_CAPABILITY_INTERNAL_PROVIDER) or "").strip().lower()
    session_key = str(trusted_env.get(META_CAPABILITY_INTERNAL_SESSION_KEY) or "").strip()
    lease_token = str(
        trusted_env.get(META_CAPABILITY_INTERNAL_CREDENTIAL_LEASE_TOKEN) or ""
    ).strip()
    if not provider_id or not session_key or not lease_token:
        return
    try:
        from opensquilla.engine.selector_override import (
            report_profile_credential_lease_failure,
        )
        from opensquilla.provider.failures import ProviderFailureKind

        # Paid media must never fall back to the compatibility session-only
        # reporter: a stale child result could otherwise park a newer key.
        report_profile_credential_lease_failure(
            provider_id,
            session_key,
            lease_token,
            ProviderFailureKind(kind_value),
        )
    except (ImportError, ValueError):
        # Credential bookkeeping is fail-soft and must never replace the
        # original paid-step failure or trigger an automatic retry.
        log.debug(
            "meta_orchestrator.profile_pool_failure_report_failed",
            provider=provider_id,
            failure_kind=kind_value,
        )


def _sanitize_failure_stream(
    text: str,
    *,
    child_env: Mapping[str, str],
    sensitive_values: set[str] | frozenset[str] = frozenset(),
) -> str:
    """Return a bounded, terminal-safe, secret-redacted failure stream."""

    if not text:
        return ""
    cleaned = _CONTROL_RE.sub(
        "",
        _ANSI_RE.sub("", text[:_ERROR_DETAIL_SCAN_CHARS]),
    ).strip()
    secret_values = {
        value
        for key, value in child_env.items()
        if value and _is_sensitive_env_key(key)
    }
    secret_values.update(value for value in sensitive_values if value)
    # Longest first prevents a shorter credential from exposing the remainder
    # of a longer credential that happens to contain it.
    for value in sorted(secret_values, key=len, reverse=True):
        cleaned = cleaned.replace(value, _REDACTED)
    return redact_error_text(cleaned, max_len=_ERROR_DETAIL_MAX_CHARS)


def _format_failure_detail(
    *,
    stderr_text: str,
    stdout_text: str,
    child_env: Mapping[str, str],
    sensitive_values: set[str] | frozenset[str] = frozenset(),
) -> str:
    """Prefer stderr, append stdout when it fits, and cap the total detail."""

    stderr = _sanitize_failure_stream(
        stderr_text,
        child_env=child_env,
        sensitive_values=sensitive_values,
    )
    stdout = _sanitize_failure_stream(
        stdout_text,
        child_env=child_env,
        sensitive_values=sensitive_values,
    )
    if stderr and stdout:
        detail = f"stderr: {stderr}\nstdout: {stdout}"
    else:
        detail = stderr or stdout
    if len(detail) <= _ERROR_DETAIL_MAX_CHARS:
        return detail
    return detail[: _ERROR_DETAIL_MAX_CHARS - 1].rstrip() + "…"


def _canonical_receipt_bytes(value: object) -> bytes | None:
    """Canonicalize one bounded JSON receipt without exposing its fields."""

    if not isinstance(value, dict):
        return None
    try:
        encoded = _json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    return encoded if len(encoded) <= _MAX_PAID_RECEIPT_BYTES else None


def _receipt_sidecar_path(argv: list[str], *, workdir: str | None) -> _Path | None:
    """Resolve the exact paid entrypoint's single ``--filename`` sidecar."""

    positions = [index for index, value in enumerate(argv) if value == "--filename"]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        return None
    raw_path = argv[positions[0] + 1]
    if not raw_path:
        return None
    output_path = _Path(raw_path)
    if not output_path.is_absolute():
        output_path = _Path(workdir or os.getcwd()) / output_path
    output_path = output_path.resolve()
    return output_path.with_suffix(output_path.suffix + ".receipt.json")


def _current_paid_receipt_proof(
    *,
    effective_skill: str,
    skill_spec: Any,
    argv: list[str],
    workdir: str | None,
    stdout_text: str,
) -> str | None:
    """Prove stdout and sidecar came from this exact bundled invocation.

    Workspace sidecars are never sufficient.  The exact bundled subprocess
    must emit one receipt line on the stdout captured for this invocation, and
    its bounded JSON object must equal the sidecar present after the process
    exits.  Only the canonical SHA-256 digest crosses the executor boundary.
    """

    label = _PAID_RECEIPT_STDOUT_LABELS.get(effective_skill)
    if label is None or getattr(skill_spec, "layer", None) != SkillLayer.BUNDLED:
        return None
    candidates = [
        line[len(label) :].strip()
        for line in stdout_text.splitlines()
        if line.startswith(label)
    ]
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    if not candidate or len(candidate.encode("utf-8", errors="ignore")) > _MAX_PAID_RECEIPT_BYTES:
        return None
    try:
        stdout_receipt = _json.loads(candidate)
    except _json.JSONDecodeError:
        return None
    canonical = _canonical_receipt_bytes(stdout_receipt)
    if canonical is None:
        return None

    receipt_path = _receipt_sidecar_path(argv, workdir=workdir)
    if receipt_path is None:
        return None
    try:
        if not receipt_path.is_file() or receipt_path.stat().st_size > _MAX_PAID_RECEIPT_BYTES:
            return None
        sidecar_receipt = _json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, _json.JSONDecodeError):
        return None
    if _canonical_receipt_bytes(sidecar_receipt) != canonical:
        return None
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


async def run_skill_exec_step(
    step: MetaStep,
    effective_skill: str,
    inputs: dict[str, Any],
    outputs: dict[str, str],
    *,
    skill_loader: Any,
    workspace_dir: str | None = None,
    trusted_env: Mapping[str, str] | None = None,
) -> str:
    """Run a wrapped-CLI skill via its ``entrypoint`` manifest — no LLM.

    Resolves ``skill.entrypoint`` from the loader, renders ``command`` /
    ``args`` against ``inputs`` + ``outputs`` + ``with`` (the step's
    rendered ``with_args``), then runs the subprocess in a worker thread.
    Stdout is interpreted per ``parse`` (``text`` |
    ``json`` | ``lines``) and returned as the step output.

    Optional features:

    * ``entrypoint.env`` — Jinja-rendered environment override keys and
      values. Empty rendered values are ignored so parent environment
      fallbacks survive.
    * ``entrypoint.stdin`` — Jinja-rendered template (with ``{baseDir}``
      substitution) piped to the subprocess's stdin.
    * ``entrypoint.assemble`` — a list of ``{into, from_template}``
      entries; each ``from_template`` is rendered and written to ``into``
      (resolved against ``workdir`` for relative paths) before the
      subprocess starts.
    * ``trusted_env`` — volatile parent-runtime values applied after manifest
      rendering. These values are never part of the Jinja context or argv and
      therefore cannot be persisted by the plan or redirected by workspace
      configuration.
    * Ambient secret-shaped variables are removed by default. An exact bundled
      skill may inherit only names declared in its eligibility requirements;
      project and workspace skills inherit none.

    Errors (missing entrypoint, non-zero exit, timeout, invalid JSON
    when ``parse=json``, invalid ``stdin``/``assemble`` shape) raise
    :class:`RuntimeError` so the orchestrator's step-failure path catches
    them and the meta-skill falls back to a normal turn instead of
    silently feeding garbage downstream.
    """

    skill_spec = skill_loader.get_by_name(effective_skill)
    if skill_spec is None:
        raise RuntimeError(
            f"step {step.id!r}: skill {effective_skill!r} not found in loader",
        )
    # Operator gate: a coding-mode / disabled skill stays unreachable even when
    # a meta-skill composes it as a step (codex review — every reach path).
    from opensquilla.skills.eligibility import is_skill_available_live

    if not is_skill_available_live(effective_skill):
        raise RuntimeError(
            f"step {step.id!r}: skill {effective_skill!r} is gated by operator config",
        )
    entrypoint = getattr(skill_spec, "entrypoint", None)
    if not isinstance(entrypoint, dict) or not entrypoint:
        raise RuntimeError(
            f"step {step.id!r}: skill {effective_skill!r} has no "
            f"entrypoint manifest — cannot run as skill_exec",
        )
    command_raw = entrypoint.get("command")
    if not isinstance(command_raw, str) or not command_raw.strip():
        raise RuntimeError(
            f"step {step.id!r}: skill {effective_skill!r} entrypoint "
            f"missing non-empty 'command'",
        )

    # Render with_args first so it becomes part of the Jinja context for
    # the entrypoint templates (lets the entrypoint reference ``with.q``
    # in addition to the global ``inputs`` / ``outputs``).
    rendered_with = render_with_args(step.with_args, inputs=inputs, outputs=outputs)
    base_dir = str(getattr(skill_spec, "base_dir", "") or "")
    context = {
        "inputs": inputs,
        "outputs": outputs,
        "with": rendered_with,
        "baseDir": base_dir,
    }

    def _render(value: str) -> str:
        try:
            return _JINJA_ENV.from_string(value).render(**context)
        except jinja2.UndefinedError as exc:
            raise RuntimeError(f"entrypoint template undefined: {exc}") from exc
        except jinja2.TemplateSyntaxError as exc:
            raise RuntimeError(f"entrypoint template syntax error: {exc}") from exc

    # `{baseDir}` is a static placeholder (not Jinja) — substitute before
    # rendering so it survives shlex.split() below.
    command_str = _replace_base_dir(command_raw, base_dir)
    command_str = _render(command_str)

    raw_args = entrypoint.get("args") or []
    if not isinstance(raw_args, list):
        raise RuntimeError(
            f"step {step.id!r}: entrypoint.args must be a list",
        )
    rendered_args: list[str] = []
    for index, item in enumerate(raw_args):
        if not isinstance(item, str):
            raise RuntimeError(
                f"step {step.id!r}: entrypoint.args[{index}] must be a string",
            )
        rendered_args.append(_render(_replace_base_dir(item, base_dir)))

    # Resolve cwd early so assemble's relative-path anchoring matches the
    # subprocess's working directory. Precedence:
    # 1. ``entrypoint.cwd`` — skill-author override, wins everything.
    # 2. orchestrator-level ``workspace_dir`` — shared workspace for the
    #    whole meta-skill so cross-skill files (results.csv → plot,
    #    references.bib → bibtex, etc.) land in the same tree.
    # 3. ``base_dir`` — fallback to the skill's own directory.
    cwd = entrypoint.get("cwd")
    if isinstance(cwd, str) and cwd:
        cwd = cwd.replace("{baseDir}", base_dir)
        workdir: str | None = cwd
    elif workspace_dir:
        workdir = workspace_dir
    else:
        workdir = base_dir or None
    allowed_workdir_root = workspace_dir or base_dir
    if workdir and allowed_workdir_root:
        allowed_root = _Path(allowed_workdir_root).expanduser().resolve()
        workdir_path = _Path(workdir).expanduser()
        if not workdir_path.is_absolute():
            workdir_path = allowed_root / workdir_path
        resolved_workdir = workdir_path.resolve()
        if (
            resolved_workdir != allowed_root
            and not resolved_workdir.is_relative_to(allowed_root)
        ):
            raise RuntimeError(
                f"step {step.id!r}: entrypoint.cwd path "
                f"{resolved_workdir!s} escapes allowed root "
                f"{allowed_root!s}",
            )
        resolved_workdir.mkdir(parents=True, exist_ok=True)
        workdir = str(resolved_workdir)

    # Optional assemble: render templated files to disk before exec.
    assemble_raw = entrypoint.get("assemble") or []
    if assemble_raw and not isinstance(assemble_raw, list):
        raise RuntimeError(
            f"step {step.id!r}: entrypoint.assemble must be a list of mappings",
        )
    for index, entry in enumerate(assemble_raw):
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"step {step.id!r}: entrypoint.assemble[{index}] must be a mapping",
            )
        into_raw = entry.get("into")
        template_raw = entry.get("from_template")
        if not isinstance(into_raw, str) or not into_raw:
            raise RuntimeError(
                f"step {step.id!r}: entrypoint.assemble[{index}] missing 'into'",
            )
        if not isinstance(template_raw, str):
            raise RuntimeError(
                f"step {step.id!r}: entrypoint.assemble[{index}] missing "
                f"'from_template'",
            )
        into_path_str = _render(into_raw.replace("{baseDir}", base_dir))
        template_body = _render(template_raw.replace("{baseDir}", base_dir))
        # Relative paths anchor to cwd (workdir), absolute paths pass through.
        target = _Path(into_path_str)
        if not target.is_absolute() and workdir:
            target = _Path(workdir) / target
        # Path-traversal defence: resolve to canonical form then ensure
        # the target stays within the allowed root. Precedence matches
        # the cwd resolution above:
        # 1. orchestrator-level ``workspace_dir`` — the shared meta-skill
        #    workspace tree (preferred root when set).
        # 2. ``base_dir`` — the skill's own directory.
        # An ``assemble.into`` of ``../../etc/passwd`` or an absolute path
        # outside the root would otherwise let a malicious or buggy
        # skill author write arbitrary files.
        allowed_root_str = workspace_dir or base_dir
        if allowed_root_str:
            allowed_root = _Path(allowed_root_str).resolve()
            resolved = target.resolve()
            if (
                resolved != allowed_root
                and not resolved.is_relative_to(allowed_root)
            ):
                raise RuntimeError(
                    f"step {step.id!r}: entrypoint.assemble[{index}] 'into' "
                    f"path {resolved!s} escapes allowed root "
                    f"{allowed_root!s}",
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(template_body, encoding="utf-8")
        log.info(
            "meta_orchestrator.skill_exec_assemble",
            step=step.id,
            into=str(target),
            bytes=len(template_body),
        )

    argv = [
        _normalize_base_dir_argument(item, base_dir)
        for item in shlex.split(command_str, posix=os.name != "nt") + rendered_args
    ]
    if not argv:
        raise RuntimeError(f"step {step.id!r}: empty argv after rendering")

    # Resolve bare interpreter names ("python", "python3") to the current
    # process's sys.executable so wrapped-CLI skills authored as
    # `command: python <script>` work regardless of whether the parent
    # process's PATH includes a "python" symlink (e.g. uv-managed venvs
    # ship only "python" inside .venv/bin but the gateway's runtime PATH
    # may not surface it). Absolute paths and other commands pass through
    # unchanged so authors can pin a specific interpreter when needed.
    if argv[0] in ("python", "python3"):
        argv[0] = sys.executable

    timeout_raw = entrypoint.get("timeout", 60.0)
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError):
        timeout = 60.0
    parse_mode = str(entrypoint.get("parse", "text"))

    raw_env = entrypoint.get("env") or {}
    if raw_env and not isinstance(raw_env, dict):
        raise RuntimeError(
            f"step {step.id!r}: entrypoint.env must be a mapping",
        )
    # Never start from the Gateway's complete environment: opaque credential
    # names such as ``OPENROUTER_PROD`` cannot be detected reliably by name.
    # Start with OS/runtime essentials, apply verified managed-toolchain
    # additions, then copy only manifest-declared variables for an exact
    # bundled skill. Paid-media consumers take the stricter lease-only path.
    ambient_env = dict(os.environ)
    declared_ambient_keys = _declared_ambient_env_keys(
        skill_spec,
        effective_skill=effective_skill,
    )
    declared_ambient_key_folds = {key.casefold() for key in declared_ambient_keys}
    child_env = managed_skill_env(_minimal_skill_base_env(ambient_env))
    sensitive_values: set[str] = set()
    for key in declared_ambient_keys:
        value = _ambient_value(ambient_env, key)
        if value:
            child_env[key] = value
            # A declared variable can be a credential regardless of its name.
            # Track the value by provenance so failures redact it reliably.
            sensitive_values.add(value)
    if isinstance(raw_env, dict):
        for key, value in raw_env.items():
            if not isinstance(key, str) or not key:
                raise RuntimeError(
                    f"step {step.id!r}: entrypoint.env keys must be non-empty strings",
                )
            if not isinstance(value, str):
                raise RuntimeError(
                    f"step {step.id!r}: entrypoint.env[{key!r}] must be a string template",
                )
            rendered_key = _render(key.replace("{baseDir}", base_dir))
            if not rendered_key:
                raise RuntimeError(
                    f"step {step.id!r}: entrypoint.env key rendered empty",
                )
            rendered_value = _render(value.replace("{baseDir}", base_dir))
            if rendered_value:
                child_env[rendered_key] = rendered_value
                if (
                    rendered_key in declared_ambient_keys
                    or rendered_key.casefold() in declared_ambient_key_folds
                    or _is_sensitive_env_key(rendered_key)
                ):
                    sensitive_values.add(rendered_value)

    # Parent-resolved credentials override manifest/ambient values, but only
    # for the exact skill selected by MetaOrchestrator. Validate the in-memory
    # boundary defensively and never render these values through Jinja.
    allowed_trusted_keys = (
        _TRUSTED_ENV_ALLOWLIST.get(effective_skill, frozenset())
        if getattr(skill_spec, "layer", None) == SkillLayer.BUNDLED
        else frozenset()
    )
    for key, value in (trusted_env or {}).items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise RuntimeError(f"step {step.id!r}: trusted child env is invalid")
        if key in allowed_trusted_keys and value:
            child_env[key] = value
            sensitive_values.add(value)
    if _skill_requires_git(skill_spec):
        # Eligibility and child execution must agree on the same executable.
        # In particular, a later Homebrew Git accepted by the resolver must be
        # placed ahead of an unusable /usr/bin/git shim in the child PATH.
        _pin_safe_git_on_path(child_env)
    apply_utf8_child_env(child_env)

    # Optional stdin: render Jinja template and pipe to the subprocess.
    stdin_raw = entrypoint.get("stdin")
    stdin_bytes: bytes | None = None
    if isinstance(stdin_raw, str) and stdin_raw:
        stdin_text = _render(stdin_raw.replace("{baseDir}", base_dir))
        try:
            stdin_bytes = stdin_text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RuntimeError(
                f"step {step.id!r}: entrypoint.stdin rendered to text that "
                f"cannot be encoded as UTF-8: {exc}",
            ) from exc
    elif stdin_raw not in (None, ""):
        raise RuntimeError(
            f"step {step.id!r}: entrypoint.stdin must be a string template",
        )

    log.info(
        "meta_orchestrator.skill_exec_spawn",
        step=step.id,
        skill=effective_skill,
        argv_head=argv[0],
        argc=len(argv),
        timeout=timeout,
        parse=parse_mode,
        stdin_bytes=len(stdin_bytes) if stdin_bytes is not None else 0,
    )

    try:
        process = await create_owned_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=child_env,
        )
    except FileNotFoundError as err:
        message = f"skill {effective_skill!r} command not found: {argv[0]!r}"
        if is_external_paid_step(step):
            message = encode_paid_replay_safety(message, safe_no_submit=True)
        raise RuntimeError(message) from err

    process_tree = capture_process_tree_owner(process, isolated=True)
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(input=stdin_bytes),
            timeout=timeout,
        )
    except asyncio.CancelledError:
        await asyncio.shield(
            process_tree.terminate(graceful_timeout=0.2, kill_timeout=1.0)
        )
        raise
    except TimeoutError as err:
        await process_tree.terminate(graceful_timeout=0.2, kill_timeout=1.0)
        message = f"skill {effective_skill!r} timed out after {timeout}s"
        if is_external_paid_step(step):
            # Once the child starts, a timeout cannot prove whether its
            # non-idempotent provider POST was accepted.
            message = encode_paid_replay_safety(message, safe_no_submit=False)
        raise RuntimeError(message) from err
    await process_tree.terminate(graceful_timeout=0.0, kill_timeout=1.0)

    completed = subprocess.CompletedProcess(
        argv,
        process.returncode if process.returncode is not None else -1,
        stdout_bytes,
        stderr_bytes,
    )

    returncode = completed.returncode
    stdout_bytes = completed.stdout
    stderr_bytes = completed.stderr
    stdout_text = decode_subprocess_output(stdout_bytes)
    stderr_text = decode_subprocess_output(stderr_bytes)
    receipt_proof = (
        _current_paid_receipt_proof(
            effective_skill=effective_skill,
            skill_spec=skill_spec,
            argv=argv,
            workdir=workdir,
            stdout_text=stdout_text,
        )
        if is_external_paid_step(step)
        else None
    )
    if returncode != 0:
        detail = _format_failure_detail(
            stderr_text=stderr_text,
            stdout_text=stdout_text,
            child_env=child_env,
            sensitive_values=sensitive_values,
        )
        if is_external_paid_step(step):
            if (
                effective_skill in _PAID_REPLAY_SAFE_EXIT_SKILLS
                and getattr(skill_spec, "layer", None) == SkillLayer.BUNDLED
            ):
                _report_profile_pool_failure(
                    returncode=returncode,
                    trusted_env=trusted_env,
                )
            detail = encode_paid_replay_safety(
                detail,
                # Do not trust stderr: provider-controlled diagnostics could
                # spoof text. Only audited bundled entrypoints may assert the
                # reserved pre-submit exit code.
                safe_no_submit=(
                    returncode == SAFE_NO_SUBMIT_EXIT_CODE
                    and effective_skill in _PAID_REPLAY_SAFE_EXIT_SKILLS
                    and getattr(skill_spec, "layer", None) == SkillLayer.BUNDLED
                ),
            )
        message = f"skill {effective_skill!r} exited {returncode}: {detail}"
        if receipt_proof is not None:
            raise PaidReceiptProofError(
                message,
                receipt_proof=receipt_proof,
            )
        raise RuntimeError(message)

    if parse_mode == "json":
        try:
            parsed = _json.loads(stdout_text)
        except _json.JSONDecodeError as exc:
            raise RuntimeError(
                f"skill {effective_skill!r} stdout was not valid JSON: {exc}",
            ) from exc
        result = _json.dumps(parsed, ensure_ascii=False)
    elif parse_mode == "lines":
        lines = [ln for ln in stdout_text.splitlines() if ln.strip()]
        result = _json.dumps(lines, ensure_ascii=False)
    else:
        result = _normalize_text_newlines(stdout_text).strip()
    if receipt_proof is not None:
        return PaidReceiptProofText(result, receipt_proof)
    return result


__all__ = ["run_skill_exec_step"]
