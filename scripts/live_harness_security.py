"""Security boundaries shared by live provider harness scripts.

The live harness is intentionally conservative: credential files are parsed as
data (never evaluated by a shell), provider endpoints come only from the
registry, and every report is scrubbed before it can be printed or written.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from opensquilla.provider.registry import get_provider_spec, list_provider_specs

_ASSIGNMENT_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$")
_SENSITIVE_FIELD_RE = re.compile(
    r"(?:api[_-]?key|apikey|access[_-]?token|accesstoken|secret|password|authorization|"
    r"credential)",
    re.IGNORECASE,
)
_TEXT_REDACTIONS = (
    re.compile(
        r"(?i)(\bauthorization\s*[:=]\s*(?:bearer\s+)?)([^\s,;\"'}]+)"
    ),
    re.compile(r"(?i)(\bx-api-key\s*[:=]\s*)([^\s,;\"'}]+)"),
    re.compile(
        r"(?i)([\"'](?:api[_-]?key|apikey|access[_-]?token|accesstoken|secret|password|"
        r"credential)"
        r"[\"']\s*:\s*[\"'])(.*?)([\"'])"
    ),
)

# Model ids are operator-controlled metadata, not credentials.  Keep the
# allowlist explicit so a provider.keys file cannot smuggle endpoint or shell
# configuration into a live child process.  Aliases cover the names used by
# existing local provider-key bundles without accepting arbitrary ``*_MODEL``
# variables.
_MODEL_METADATA_NAMES = frozenset(
    {
        "AIHUBMIX_MODEL",
        "BYTEPLUS_MODEL",
        "DASHSCOPE_MODEL",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_REASONER_MODEL",
        "GEMINI_MODEL",
        "GROQ_MODEL",
        "KIMI_MODEL",
        "MINIMAX_MODEL",
        "MISTRAL_MODEL",
        "MOONSHOT_MODEL",
        "OPENAI_MODEL",
        "OPENROUTER_MODEL",
        "QIANFAN_MODEL",
        "SILICONFLOW_MODEL",
        "VOLCENGINE_MODEL",
        "ZAI_MODEL",
        "ZHIPU_MODEL",
    }
)
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")

# A subprocess gets only process-launch essentials.  In particular, proxy
# variables, HOME, arbitrary tokens, Python injection variables, and ambient
# OPENSQUILLA_* overrides are not inherited.  Harness-owned overrides are
# added explicitly by the caller after this boundary.
_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "WINDIR",
    }
)

_OWNED_TEMP_TREE_PREFIX = "opensquilla-"
_SECRET_SCAN_CHUNK_BYTES = 64 * 1024

# Provider request ids can be rolling aliases while the upstream response
# identifies the concrete model that served the call.  This is intentionally
# provider-scoped and exact: callers must still validate decision, request, and
# provider identity independently, so an alias can never bridge providers or
# hide a foreign model request.
_PROVIDER_RESPONSE_MODEL_EQUIVALENTS: dict[tuple[str, str], frozenset[str]] = {
    ("deepseek", "deepseek-chat"): frozenset({"deepseek-v4-flash"}),
    ("deepseek", "deepseek-reasoner"): frozenset({"deepseek-v4-pro"}),
}


def provider_response_model_matches(
    provider: str,
    requested_model: str,
    response_model: str,
) -> bool:
    """Match an exact response id or one explicit provider-scoped rolling alias."""

    expected = str(requested_model or "").strip()
    actual = str(response_model or "").strip()
    if not expected or not actual:
        return False
    if actual == expected:
        return True
    return actual in _PROVIDER_RESPONSE_MODEL_EQUIVALENTS.get(
        (str(provider or "").strip().lower(), expected),
        (),
    )


def is_premium_model(model: str) -> bool:
    """Return whether a model is outside the bounded live-test budget."""

    lowered = str(model or "").strip().lower()
    leaf = lowered.rsplit("/", 1)[-1]
    if re.match(r"^o(?:1|3|4)(?:[-_.]|$)", leaf):
        return True
    if leaf.startswith(("gpt-4", "gpt-5")):
        return not any(marker in leaf for marker in ("mini", "nano"))
    if "claude" in lowered and any(marker in lowered for marker in ("sonnet", "opus")):
        return True
    if "gemini" in lowered and any(marker in leaf for marker in ("-pro", "_pro", "ultra")):
        return True
    return leaf.startswith("qwen") and any(
        marker in leaf for marker in ("-max", "_max")
    )


def classify_failure(text: Any) -> str:
    """Map external and harness failures to the one bounded live taxonomy."""

    lowered = str(text or "").lower()
    if any(
        marker in lowered
        for marker in (
            "401",
            "unauthorized",
            "authentication",
            "api key invalid",
            "invalid api key",
            "please pass a valid api key",
        )
    ):
        return "auth"
    if any(
        marker in lowered
        for marker in (
            "insufficient balance",
            "insufficient_balance",
            "insufficient quota",
            "insufficient_quota",
            "billing hard limit",
            "credit balance",
            "payment required",
        )
    ):
        return "balance"
    if any(
        marker in lowered
        for marker in (
            "not entitled",
            "not-entitled",
            "entitlement",
            "permission denied",
            "403",
            "forbidden",
        )
    ):
        return "not-entitled"
    if "model" in lowered and any(
        marker in lowered
        for marker in (
            "not found",
            "not available",
            "unavailable",
            "does not exist",
            "invalid model",
            "unknown model",
            "404",
        )
    ):
        return "model-unavailable"
    if any(marker in lowered for marker in ("429", "rate limit", "too many requests")):
        return "rate-limit"
    if re.search(r"\b5\d\d\b", lowered) or any(
        marker in lowered
        for marker in (
            "timeout",
            "timed out",
            "transport",
            "connection",
            "connecterror",
            "dns",
            "network",
            "tls",
            "certificate",
            "stage_spawn_failed",
        )
    ):
        return "transport"
    return "implementation"


@dataclass(frozen=True)
class ProviderKeysInventory:
    """Safely parsed provider-key data plus non-secret hygiene metadata."""

    secrets: dict[str, str]
    models: dict[str, str]
    ignored_line_numbers: tuple[int, ...]
    file_mode: int
    permission_warning: str | None


def provider_model_metadata_names() -> frozenset[str]:
    """Return model metadata names accepted from a provider-key data file."""

    return _MODEL_METADATA_NAMES


def _parse_literal_assignment(raw_line: str) -> tuple[str, str] | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export"):
        export_match = re.match(r"^export\s+(.*)$", line)
        if export_match is None:
            return None
        line = export_match.group(1).strip()
    match = _ASSIGNMENT_RE.fullmatch(line)
    if match is None:
        return None
    value = match.group("value").strip()
    if "\x00" in value:
        return None
    if value.startswith(("'", '"')):
        quote = value[0]
        if len(value) < 2 or not value.endswith(quote):
            return None
        value = value[1:-1]
    return match.group("name"), value


def provider_secret_names() -> frozenset[str]:
    """Return the registry-declared API-key environment names."""

    return frozenset(
        spec.env_key
        for spec in list_provider_specs()
        if spec.env_key and spec.env_key != "OAuth"
    )


def parse_secrets_file(
    path: Path | str,
    *,
    allowed_names: Iterable[str] | None = None,
) -> dict[str, str]:
    """Parse allowlisted ``NAME=VALUE`` entries without shell evaluation.

    ``export NAME=VALUE`` is accepted for operator convenience. Invalid and
    non-allowlisted lines are ignored silently so neither their contents nor
    accidental secret material can leak into terminal output.
    """

    allowed = frozenset(allowed_names) if allowed_names is not None else provider_secret_names()
    parsed: dict[str, str] = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        assignment = _parse_literal_assignment(raw_line)
        if assignment is None:
            continue
        name, value = assignment
        if name not in allowed:
            continue
        parsed[name] = value
    return parsed


def parse_provider_keys_file(path: Path | str) -> ProviderKeysInventory:
    """Parse the fixed live-test credential bundle as inert data.

    Only registry credential names and the explicit model-metadata allowlist
    are accepted.  In particular, ``*_BASE_URL`` assignments are ignored so
    live tests always use the checked-in registry endpoint.  Ignored content
    is represented only by line number and is never returned or logged.
    """

    source = Path(path)
    secret_names = provider_secret_names()
    secrets: dict[str, str] = {}
    models: dict[str, str] = {}
    ignored: list[int] = []
    for line_number, raw_line in enumerate(
        source.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assignment = _parse_literal_assignment(raw_line)
        if assignment is None:
            ignored.append(line_number)
            continue
        name, value = assignment
        if name in secret_names:
            secrets[name] = value
            continue
        if name in _MODEL_METADATA_NAMES and (not value or _MODEL_ID_RE.fullmatch(value)):
            models[name] = value
            continue
        ignored.append(line_number)

    file_mode = stat.S_IMODE(source.stat().st_mode)
    permission_warning = None
    # POSIX permission bits do not describe Windows ACLs. Keep the numeric
    # mode as diagnostics on every platform, but only make a security claim
    # where group/other bits have their POSIX meaning.
    if os.name != "nt" and file_mode & 0o077:
        permission_warning = (
            f"credential file permissions are {file_mode:04o}; expected 0600 or stricter"
        )
    return ProviderKeysInventory(
        secrets=secrets,
        models=models,
        ignored_line_numbers=tuple(ignored),
        file_mode=file_mode,
        permission_warning=permission_warning,
    )


def registry_endpoint(provider: str, requested: str | None = None) -> str:
    """Return the provider's registered endpoint and reject endpoint overrides."""

    spec = get_provider_spec(provider)
    endpoint = spec.default_base_url.strip().rstrip("/")
    if not spec.runtime_supported or not endpoint:
        raise ValueError(f"provider {provider!r} has no runnable registry endpoint")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"provider {provider!r} has an invalid registry endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"provider {provider!r} has an unsafe registry endpoint")
    if requested is not None and requested.strip().rstrip("/") != endpoint:
        raise ValueError(f"endpoint override rejected for provider {provider!r}")
    return endpoint


def _secret_values(secrets: Mapping[str, str] | Iterable[str]) -> tuple[str, ...]:
    values = secrets.values() if isinstance(secrets, Mapping) else secrets
    return tuple(sorted({str(value) for value in values if value}, key=len, reverse=True))


def redact_text(text: Any, secrets: Mapping[str, str] | Iterable[str] = ()) -> str:
    """Remove known credentials and common credential-bearing header shapes."""

    redacted = str(text)
    for secret in _secret_values(secrets):
        redacted = redacted.replace(secret, "[REDACTED]")
    for pattern in _TEXT_REDACTIONS:
        if pattern.groups == 3:
            redacted = pattern.sub(r"\1[REDACTED]\3", redacted)
        else:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


def sanitize_report(
    value: Any,
    secrets: Mapping[str, str] | Iterable[str] = (),
) -> Any:
    """Recursively redact a JSON-compatible report value."""

    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_FIELD_RE.search(key_text) and key_text.lower() != "env_key":
                sanitized[key_text] = "[REDACTED]" if item not in (None, "") else item
            else:
                sanitized[key_text] = sanitize_report(item, secrets)
        return sanitized
    if isinstance(value, list):
        return [sanitize_report(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [sanitize_report(item, secrets) for item in value]
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


def report_contains_secret(
    report: Any,
    secrets: Mapping[str, str] | Iterable[str],
) -> bool:
    """Return whether a serialized report still contains a known credential."""

    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    return any(secret in serialized for secret in _secret_values(secrets))


def write_safe_report(
    path: Path | str,
    report: Any,
    secrets: Mapping[str, str] | Iterable[str],
) -> Any:
    """Atomically write a redacted, credential-scanned JSON report.

    The temporary file is mode 0600.  If either the in-memory or on-disk scan
    detects a credential, the temporary artifact is removed and the operation
    fails without replacing the requested report path.
    """

    output = require_temporary_report_path(path)
    safe_report = sanitize_report(report, secrets)
    if report_contains_secret(safe_report, secrets):
        output.unlink(missing_ok=True)
        raise RuntimeError("refusing to write a report containing provider credentials")
    serialized = json.dumps(safe_report, ensure_ascii=False, indent=2) + "\n"
    if any(secret in serialized for secret in _secret_values(secrets)):
        output.unlink(missing_ok=True)
        raise RuntimeError("refusing to write a report containing provider credentials")

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.tmp-",
        dir=output.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(serialized)
        written = temporary.read_text(encoding="utf-8")
        if any(secret in written for secret in _secret_values(secrets)):
            raise RuntimeError("credential detected after report write")
        os.replace(temporary, output)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    return safe_report


def _temporary_roots() -> tuple[Path, ...]:
    roots = {Path(tempfile.gettempdir()).resolve()}
    if os.name != "nt":
        roots.update({Path("/tmp").resolve(), Path("/private/tmp").resolve()})
    return tuple(sorted(roots, key=str))


def _is_strict_temporary_child(path: Path) -> bool:
    for root in _temporary_roots():
        if path == root:
            continue
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _secret_needles(secrets: Mapping[str, str] | Iterable[str]) -> tuple[bytes, ...]:
    return tuple(value.encode("utf-8") for value in _secret_values(secrets))


def _bytes_contain_secret(value: bytes, needles: tuple[bytes, ...]) -> bool:
    return any(needle in value for needle in needles)


def _regular_file_contains_secret(path: Path, needles: tuple[bytes, ...]) -> bool:
    overlap = max((len(needle) for needle in needles), default=1) - 1
    tail = b""
    with path.open("rb") as stream:
        while chunk := stream.read(_SECRET_SCAN_CHUNK_BYTES):
            value = tail + chunk
            if _bytes_contain_secret(value, needles):
                return True
            tail = value[-overlap:] if overlap else b""
    return False


def _is_link_or_junction(path: Path, mode: int) -> bool:
    if stat.S_ISLNK(mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _raise_walk_error(error: OSError) -> None:
    raise error


def _temporary_tree_contains_secret(root: Path, needles: tuple[bytes, ...]) -> bool:
    if not needles:
        return False
    if _bytes_contain_secret(root.name.encode("utf-8"), needles):
        return True

    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        onerror=_raise_walk_error,
        followlinks=False,
    ):
        current_path = Path(current)
        traversable_directories: list[str] = []
        for name in directory_names:
            if _bytes_contain_secret(name.encode("utf-8"), needles):
                return True
            entry = current_path / name
            mode = entry.lstat().st_mode
            if _is_link_or_junction(entry, mode):
                if _bytes_contain_secret(str(entry.readlink()).encode("utf-8"), needles):
                    return True
                continue
            if not stat.S_ISDIR(mode):
                raise OSError("unexpected non-directory entry in temporary live artifacts")
            traversable_directories.append(name)
        directory_names[:] = traversable_directories

        for name in file_names:
            if _bytes_contain_secret(name.encode("utf-8"), needles):
                return True
            entry = current_path / name
            mode = entry.lstat().st_mode
            if _is_link_or_junction(entry, mode):
                if _bytes_contain_secret(str(entry.readlink()).encode("utf-8"), needles):
                    return True
                continue
            if not stat.S_ISREG(mode):
                raise OSError("unexpected non-file entry in temporary live artifacts")
            if _regular_file_contains_secret(entry, needles):
                return True
    return False


def scan_and_remove_temporary_tree(
    path: Path | str,
    secrets: Mapping[str, str] | Iterable[str],
) -> None:
    """Scan known secrets, then strictly remove one harness-owned temporary tree.

    Symlink roots, non-directories, broad temporary roots, and directories not
    created with the harness prefix are refused before deletion.  Interior
    symlinks are inspected as metadata but never followed.  A detected secret
    or scan failure is reported only after the tree has been removed; deletion
    failures are never suppressed.
    """

    target = Path(path)
    try:
        mode = target.lstat().st_mode
    except FileNotFoundError:
        return
    if _is_link_or_junction(target, mode) or not stat.S_ISDIR(mode):
        raise ValueError(
            "refusing to remove a temporary tree through a link, junction, or non-directory"
        )

    resolved = target.resolve(strict=True)
    if not resolved.name.startswith(_OWNED_TEMP_TREE_PREFIX) or not _is_strict_temporary_child(
        resolved
    ):
        raise ValueError("refusing to remove a non-owned temporary tree")

    scan_error: Exception | None = None
    contains_secret = False
    try:
        contains_secret = _temporary_tree_contains_secret(
            resolved,
            _secret_needles(secrets),
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must still run after any scan failure
        scan_error = exc

    shutil.rmtree(resolved)
    if scan_error is not None:
        raise RuntimeError(
            "unable to scan temporary live artifacts before deletion"
        ) from scan_error
    if contains_secret:
        raise RuntimeError("credential detected in temporary live artifacts")


def is_temporary_report_path(path: Path | str) -> bool:
    """Return whether a report resolves below an OS temporary root."""

    resolved = Path(path).resolve()
    for root in _temporary_roots():
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def require_temporary_report_path(path: Path | str) -> Path:
    """Resolve and validate a report destination before any filesystem write."""

    output = Path(path)
    if not is_temporary_report_path(output):
        raise ValueError("live report output must be inside the system temporary directory")
    return output


def minimal_child_environment(
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy only portable, non-secret process-launch environment entries."""

    source = os.environ if base_environment is None else base_environment
    env = {
        str(name): str(value)
        for name, value in source.items()
        if str(name).upper() in _CHILD_ENV_ALLOWLIST and value is not None
    }
    if not any(name.upper() == "PATH" for name in env):
        env["PATH"] = os.defpath
    return env


def child_environment(
    provider: str,
    secrets: Mapping[str, str],
    *,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a least-privilege environment for one live-provider subprocess."""

    spec = get_provider_spec(provider)
    registry_endpoint(provider)
    env = minimal_child_environment(base_environment)
    env["OPENSQUILLA_LIVE_DISABLE_DOTENV"] = "1"
    secret = secrets.get(spec.env_key, "")
    if spec.env_key and secret:
        env[spec.env_key] = secret
    return env
