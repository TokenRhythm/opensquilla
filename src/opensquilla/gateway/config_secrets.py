"""Single home for config-mutation runtime-secret inheritance (D19).

Every config-mutation surface must treat runtime-secret markers identically:
the RPC ``config.set`` / ``config.patch`` / ``config.apply`` handlers and the
onboarding ``upsert_*`` mutations. This module owns that one implementation so
the rule can never drift between surfaces.

Two concerns live here, both part of the same secret-inheritance contract:

* **Redaction round-trip.** A control surface reads config back with every
  secret masked to the public marker (``"[redacted]"``). When the client
  re-submits that public object, :func:`restore_redacted_values` swaps each
  marker at a secret-named path back to the currently-stored secret value and
  reports the set of paths it restored, so a re-save never wipes a secret the
  client only ever saw redacted.

* **D19 inherit-then-clear-explicit.** :func:`inherit_then_clear_explicit`
  first inherits the currently-stored runtime-secret markers onto the candidate
  config, then clears the marker at every path the mutation set with an
  *explicit* new value. A path whose value was the marker (restored/inherited)
  keeps its marker; an explicit new value replaces the secret and un-marks the
  path so a later persist writes it to disk.

The marker constant here is the write-side twin of ``config._REDACTED`` (the
read-side redaction value); both are the literal ``"[redacted]"``. It is kept
as a local literal so this module has no import-time dependency on
``opensquilla.gateway.config`` (matching the lazy-import discipline the config
RPC/onboarding surfaces rely on during boot).
"""

from __future__ import annotations

from typing import Any

# Write-side twin of ``opensquilla.gateway.config._REDACTED``. A client that
# submits a public config object carries this sentinel in place of every secret
# it only ever saw redacted.
REDACTED_PUBLIC_VALUE = "[redacted]"


def is_sensitive_redacted_path(path: str) -> bool:
    """Return True when ``path``'s final segment names a secret field."""
    if not path:
        return False
    from opensquilla.gateway.config import is_sensitive_config_key

    return is_sensitive_config_key(path.rsplit(".", 1)[-1])


def _has_existing_redacted_source(source: Any) -> bool:
    return source is not None and source != ""


def collect_paths(payload: Any, prefix: str = "") -> set[str]:
    """Collect every dotted key path present in a (possibly nested) dict payload."""
    paths: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            current = f"{prefix}.{key}" if prefix else key
            paths.add(current)
            paths.update(collect_paths(value, current))
    return paths


def restore_redacted_values(
    payload: Any, source: Any, prefix: str = ""
) -> tuple[Any, set[str]]:
    """Swap redaction markers back to stored secrets and report restored paths.

    Walks ``payload`` in lockstep with ``source`` (the currently-stored config
    view). Wherever a secret-named path carries the redaction marker, the stored
    value is substituted and the path is recorded in the returned set — those
    paths inherited (rather than replaced) their secret, so the caller must not
    clear their runtime-secret marker. A marker at a secret path with no stored
    secret is a hard error (nothing to preserve). A literal ``"[redacted]"`` at a
    non-secret path passes through untouched.
    """
    if payload == REDACTED_PUBLIC_VALUE and is_sensitive_redacted_path(prefix):
        if not _has_existing_redacted_source(source):
            raise ValueError(f"Cannot preserve redacted secret at {prefix}: no existing secret")
        return source, {prefix} if prefix else set()
    if isinstance(payload, dict):
        source_dict = source if isinstance(source, dict) else {}
        restored: dict[str, Any] = {}
        redacted_paths: set[str] = set()
        for key, value in payload.items():
            current = f"{prefix}.{key}" if prefix else key
            child, child_paths = restore_redacted_values(
                value,
                source_dict.get(key),
                current,
            )
            restored[key] = child
            redacted_paths.update(child_paths)
        return restored, redacted_paths
    if isinstance(payload, list):
        source_list = source if isinstance(source, list) else []
        restored_list: list[Any] = []
        list_redacted_paths: set[str] = set()
        for index, value in enumerate(payload):
            current = f"{prefix}.{index}" if prefix else str(index)
            source_value = source_list[index] if index < len(source_list) else None
            child, child_paths = restore_redacted_values(value, source_value, current)
            restored_list.append(child)
            list_redacted_paths.update(child_paths)
        return restored_list, list_redacted_paths
    return payload, set()


def inherit_runtime_secrets(source: Any, target: Any) -> None:
    """Copy the runtime-secret path markers from ``source`` onto ``target``."""
    if hasattr(target, "inherit_runtime_secrets") and source is not None:
        target.inherit_runtime_secrets(source)


def clear_runtime_secret_paths(config: Any, paths: set[str]) -> None:
    """Clear the runtime-secret marker at each path in ``paths`` on ``config``."""
    if not hasattr(config, "clear_runtime_secret"):
        return
    for path in paths:
        config.clear_runtime_secret(path)


def forget_secret_provenance_paths(config: Any, paths: set[str]) -> None:
    """Forget all authorship markers for secrets removed from the config."""

    if not hasattr(config, "forget_secret_provenance"):
        return
    for path in paths:
        config.forget_secret_provenance(path)


def inherit_then_clear_explicit(
    source: Any, target: Any, explicit_secret_paths: set[str]
) -> None:
    """Apply the D19 inherit-then-clear-explicit rule to ``target``.

    Inherit the currently-stored runtime-secret markers from ``source``, then
    clear the marker at every path in ``explicit_secret_paths`` — the paths the
    mutation set with an explicit new value (marker/inherited paths are excluded
    by the caller so they keep their marker).
    """
    inherit_runtime_secrets(source, target)
    clear_runtime_secret_paths(target, explicit_secret_paths)
