"""Durable per-provider record of the last saved-deployment probe.

The control console's configured-provider rows verify a *saved* deployment
with a small live request. The result used to live only in the Web UI's
memory, so any settings reload silently reset a just-verified row to "not
tested". This module keeps a bounded, secret-free record of the last probe
per provider — outcome, timestamp, and a fingerprint of the saved
credential/endpoint identity that was verified — so ``onboarding.status``
can distinguish "verified and unchanged", "verified but the config changed
since", and "never verified".

Records never contain key material: inline keys enter the fingerprint only
as a SHA-256 digest, and environment credentials by variable *name*. A
rotated value behind the same environment variable is deliberately out of
scope — the record answers "did the saved config change", not "is the remote
account still valid".
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from opensquilla import paths

logger = structlog.get_logger(__name__)

_HISTORY_FILENAME = "probe_history.json"
_SCHEMA_VERSION = 1


def _history_path(cfg: Any) -> Path:
    state_root = str(getattr(cfg, "state_dir", "") or "").strip()
    root = Path(os.path.expanduser(state_root)) if state_root else paths.state_dir()
    return root / "onboarding" / _HISTORY_FILENAME


def _normalize_provider(provider_id: str) -> str:
    return str(provider_id or "").strip().lower()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def saved_deployment_fingerprint(cfg: Any, provider_id: str) -> str:
    """Fingerprint the saved credential/endpoint identity for one provider.

    Inputs come from the persisted config only (``[llm]`` for the active
    provider, ``[llm_profiles.<id>]`` otherwise), so the value computed when a
    probe is recorded and the value computed when status is rendered agree by
    construction. The model is intentionally excluded: a row probe validates
    credentials and endpoint, both of which stay valid across a model switch.
    """
    provider = _normalize_provider(provider_id)
    llm = getattr(cfg, "llm", None)
    active = _normalize_provider(str(getattr(llm, "provider", "") or ""))
    identity: dict[str, Any] = {"provider": provider}
    source: Any = None
    if provider and provider == active:
        source = llm
    else:
        profiles = getattr(cfg, "llm_profiles", None) or {}
        for key, profile in profiles.items():
            if _normalize_provider(str(key)) == provider:
                source = profile
                break
    if source is not None:
        api_key = str(getattr(source, "api_key", "") or "")
        identity.update(
            {
                "apiKeyDigest": _digest(api_key) if api_key else "",
                "apiKeyEnv": str(getattr(source, "api_key_env", "") or "").strip(),
                "apiKeyEnvPool": [
                    str(name or "").strip()
                    for name in (getattr(source, "api_key_env_pool", None) or [])
                    if str(name or "").strip()
                ],
                "baseUrl": str(getattr(source, "base_url", "") or "").strip(),
                "proxy": str(getattr(source, "proxy", "") or "").strip(),
            }
        )
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return _digest(canonical)


def load_probe_history(cfg: Any) -> dict[str, dict[str, Any]]:
    """Read the history map; corruption or absence degrades to empty."""
    path = _history_path(cfg)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    records = raw.get("providers") if isinstance(raw, dict) else None
    if not isinstance(records, dict):
        return {}
    history: dict[str, dict[str, Any]] = {}
    for key, value in records.items():
        provider = _normalize_provider(str(key))
        if not provider or not isinstance(value, dict):
            continue
        history[provider] = {
            "ok": bool(value.get("ok")),
            "at": str(value.get("at") or ""),
            "fingerprint": str(value.get("fingerprint") or ""),
            "failureKind": str(value.get("failureKind") or ""),
        }
    return history


def record_probe(cfg: Any, provider_id: str, *, ok: bool, failure_kind: str = "") -> None:
    """Persist the outcome of one saved-deployment probe (best effort).

    A recording failure must never fail the probe RPC that produced the
    result, so filesystem errors are logged and swallowed.
    """
    provider = _normalize_provider(provider_id)
    if not provider:
        return
    try:
        history = load_probe_history(cfg)
        history[provider] = {
            "ok": bool(ok),
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "fingerprint": saved_deployment_fingerprint(cfg, provider),
            "failureKind": str(failure_kind or ""),
        }
        path = _history_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schemaVersion": _SCHEMA_VERSION, "providers": history}
        fd, tmp_name = tempfile.mkstemp(
            prefix=_HISTORY_FILENAME, dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning("probe_history.record_failed", provider=provider, error=str(exc))


def last_probe_payload(
    record: dict[str, Any] | None, current_fingerprint: str
) -> dict[str, Any] | None:
    """Shape one stored record for the status payload; ``None`` when absent."""
    if not record or not str(record.get("at") or ""):
        return None
    stored = str(record.get("fingerprint") or "")
    return {
        "ok": bool(record.get("ok")),
        "at": str(record.get("at") or ""),
        "configChanged": bool(not stored or stored != str(current_fingerprint or "")),
        "failureKind": str(record.get("failureKind") or ""),
    }
