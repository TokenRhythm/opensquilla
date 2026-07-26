"""Environment-only live certification for channel adapters.

The certification runner is deliberately separate from gateway configuration:
credentials are read from the process environment, used to construct an
ephemeral adapter, and never written to disk or included in evidence output.
Safe, non-mutating provider probes are the default and the only operation run
without an explicit side-effect opt-in and per-provider target.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from opensquilla.channels.registry import build_managed_channel, parse_channel_entry
from opensquilla.channels.types import OutgoingMessage
from opensquilla.onboarding.channel_specs import (
    ChannelSetupField,
    get_channel_setup_spec,
    list_channel_setup_specs,
)

CERT_ENV_PREFIX = "OPENSQUILLA_CHANNEL_CERT_"
EVIDENCE_SCHEMA_VERSION = 1
_COMMON_FIELDS = frozenset(
    {
        "name",
        "type",
        "enabled",
        "agent_id",
        "group_session_scope",
        "busy_input_mode",
        "dm_access",
        "allowed_senders",
    }
)
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:access_?token|api_?key|app_?secret|authorization|bot_?secret|"
    r"cookie|credential|client_?secret|secret|ticket|"
    r"corp_?secret|encrypt_?key|encoding_?aes_?key|password|private_?key|"
    r"signing_?secret|token)(?:$|_)",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"(?i)\b(authorization|access[_ -]?token|api[_ -]?key|app[_ -]?secret|"
    r"bot[_ -]?token|client[_ -]?secret|cookie|credential|password|"
    r"private[_ -]?key|secret|signing[_ -]?secret|ticket)\b"
    r"(\s*[=:]\s*|\s+)([^\s,;]+)"
)
_TELEGRAM_URL_TOKEN = re.compile(r"(?i)(/bot)[^/\s]+")
_DELIVERY_UNSUPPORTED: dict[str, str] = {
    "dingtalk": (
        "DingTalk robot delivery requires an inbound sessionWebhook context; "
        "ephemeral certification intentionally does not start ingress."
    ),
}


class CertificationUsageError(ValueError):
    """The requested certification mode is unsafe or malformed."""


def supported_certification_providers() -> tuple[str, ...]:
    """Return channel types exposed by the onboarding/runtime catalog."""

    return tuple(spec.type for spec in list_channel_setup_specs())


def certification_env_name(provider: str, field: str) -> str:
    """Return the environment variable used for one provider field."""

    normalized_provider = re.sub(r"[^A-Z0-9]+", "_", provider.upper()).strip("_")
    normalized_field = re.sub(r"[^A-Z0-9]+", "_", field.upper()).strip("_")
    return f"{CERT_ENV_PREFIX}{normalized_provider}_{normalized_field}"


def certification_environment(provider: str) -> dict[str, dict[str, Any]]:
    """Describe accepted environment variables without exposing their values."""

    spec = get_channel_setup_spec(provider)
    return {
        field.name: {
            "environment": certification_env_name(provider, field.name),
            "required": field.required,
            "secret": field.secret,
        }
        for field in spec.fields
        if field.name not in _COMMON_FIELDS
    }


def parse_targets(values: Sequence[str]) -> dict[str, str]:
    """Parse repeatable ``provider=target`` CLI values."""

    targets: dict[str, str] = {}
    for raw in values:
        provider, separator, target = raw.partition("=")
        provider = provider.strip().lower()
        target = target.strip()
        if not separator or not provider or not target:
            raise CertificationUsageError(
                "targets must use provider=destination (for example telegram=12345)"
            )
        if provider in targets:
            raise CertificationUsageError(f"duplicate target for provider: {provider}")
        targets[provider] = target
    return targets


def _coerce_environment_value(field: ChannelSetupField, value: str) -> Any:
    if field.field_type == "bool":
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{field.name} must be true or false")
    if field.field_type == "int":
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{field.name} must be an integer") from None
    if field.field_type == "float":
        try:
            return float(value)
        except ValueError:
            raise ValueError(f"{field.name} must be a number") from None
    return value


def _visible(field: ChannelSetupField, payload: Mapping[str, Any]) -> bool:
    if not field.show_when:
        return True
    return all(str(payload.get(key, "")) == expected for key, expected in field.show_when.items())


def _ephemeral_entry(
    provider: str,
    environ: Mapping[str, str],
) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    spec = get_channel_setup_spec(provider)
    payload: dict[str, Any] = {
        "type": provider,
        "name": f"cert-{provider}",
        "enabled": False,
        "agent_id": "main",
    }
    supplied: list[str] = []
    secret_values: list[str] = []

    for field in spec.fields:
        if field.name in _COMMON_FIELDS:
            continue
        env_name = certification_env_name(provider, field.name)
        raw = environ.get(env_name)
        if raw is not None and raw != "":
            payload[field.name] = _coerce_environment_value(field, raw)
            supplied.append(field.name)
            if field.secret:
                secret_values.append(raw)
        elif field.default is not None:
            payload[field.name] = field.default

    missing = [
        certification_env_name(provider, field.name)
        for field in spec.fields
        if field.name not in _COMMON_FIELDS
        and field.required
        and _visible(field, payload)
        and not str(payload.get(field.name, "")).strip()
    ]
    return payload, sorted(supplied), sorted(missing), secret_values


def _redact_string(value: str, secrets: Sequence[str]) -> str:
    redacted = value
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _INLINE_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    return _TELEGRAM_URL_TOKEN.sub(r"\1[REDACTED]", redacted)


def redact_evidence(value: Any, secrets: Sequence[str]) -> Any:
    """Recursively redact credential-shaped keys and known secret values."""

    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_KEY.search(str(key))
                else redact_evidence(item, secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_evidence(item, secrets) for item in value]
    if isinstance(value, str):
        return _redact_string(value, secrets)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_string(str(value), secrets)


async def _close_adapter(adapter: Any) -> None:
    close = getattr(adapter, "close", None)
    stop = getattr(adapter, "stop", None)
    # Managed adapters own background work through stop(). Prefer it over a
    # lower-level SDK/client close() method when both exist.
    operation = stop if callable(stop) else close if callable(close) else None
    if operation is None:
        return
    try:
        result = operation()
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        # Cleanup failures must not replace the provider probe evidence. They
        # are also intentionally not rendered because SDK errors may contain
        # request details.
        return


def _error_detail(exc: BaseException, secrets: Sequence[str]) -> str:
    detail = redact_evidence(str(exc), secrets)
    if not isinstance(detail, str) or not detail:
        return type(exc).__name__
    return detail


def _delivery_message(provider: str, target: str) -> OutgoingMessage:
    """Build the narrowest provider-valid envelope for one delivery check."""

    metadata: dict[str, Any] = {}
    if provider == "slack":
        # Slack treats a bare non-conversation reply_to as a thread timestamp.
        # Certification targets are destinations, so pin it as a channel.
        metadata["channel"] = target
    elif provider == "telegram":
        metadata["chat_id"] = target
    elif provider == "matrix":
        metadata["room_id"] = target
    elif provider == "wecom":
        metadata["touser"] = target
    elif provider == "qq":
        chat_type, separator, destination = target.partition(":")
        if separator and chat_type == "c2c" and destination:
            metadata.update({"chat_type": "c2c", "openid": destination})
        elif separator and chat_type == "group" and destination:
            metadata.update({"chat_type": "group", "group_openid": destination})
        else:
            raise CertificationUsageError(
                "QQ delivery targets must use c2c:<openid> or group:<group_openid>"
            )
    elif provider == "dingtalk":
        metadata["conversation_id"] = target

    return OutgoingMessage(
        content="OpenSquilla channel certification test.",
        reply_to=target,
        metadata=metadata,
    )


async def _certify_one(
    provider: str,
    *,
    environ: Mapping[str, str],
    timeout: float,
    send_test_message: bool,
    target: str | None,
) -> dict[str, Any]:
    started = time.perf_counter()
    base: dict[str, Any] = {
        "provider": provider,
        "credentialSource": "environment",
        "operation": "send_test_message" if send_test_message else "safe_auth_probe",
    }
    try:
        payload, supplied, missing, secrets = _ephemeral_entry(provider, environ)
    except (KeyError, TypeError, ValueError) as exc:
        return {
            **base,
            "status": "invalid_environment",
            "authenticated": False,
            "latencyMs": 0,
            "detail": _error_detail(exc, ()),
        }

    base["suppliedFields"] = supplied
    if missing:
        return {
            **base,
            "status": "missing_credentials",
            "authenticated": False,
            "latencyMs": 0,
            "missingEnvironment": missing,
        }

    try:
        entry = parse_channel_entry(payload)
        adapter = build_managed_channel(entry)
    except Exception as exc:  # noqa: BLE001 - provider SDK construction boundary
        return {
            **base,
            "status": "invalid_config",
            "authenticated": False,
            "latencyMs": round((time.perf_counter() - started) * 1000),
            "detail": _error_detail(exc, secrets),
        }
    if adapter is None:
        return {
            **base,
            "status": "unsupported",
            "authenticated": False,
            "latencyMs": round((time.perf_counter() - started) * 1000),
            "detail": "No runtime adapter is registered.",
        }

    try:
        probe = getattr(adapter, "probe_connection", None)
        if not callable(probe):
            return {
                **base,
                "status": "unsupported",
                "authenticated": False,
                "latencyMs": round((time.perf_counter() - started) * 1000),
                "detail": "Adapter has no safe non-mutating authentication probe.",
            }
        try:
            raw_result = await asyncio.wait_for(probe(), timeout=timeout)
        except TimeoutError:
            return {
                **base,
                "status": "timeout",
                "authenticated": False,
                "latencyMs": round((time.perf_counter() - started) * 1000),
                "timedOutOperation": "safe_auth_probe",
                "detail": f"Provider operation exceeded {timeout:g} seconds.",
            }
        except Exception as exc:
            return {
                **base,
                "status": "failed",
                "authenticated": False,
                "latencyMs": round((time.perf_counter() - started) * 1000),
                "detail": _error_detail(exc, secrets),
            }

        result = raw_result if isinstance(raw_result, dict) else {}
        supported = bool(result.get("supported", True))
        authenticated = bool(result.get("authenticated", False))
        evidence = redact_evidence(result, secrets)
        if not supported or not authenticated or not send_test_message:
            status = (
                "unsupported"
                if not supported
                else "verified"
                if authenticated
                else "failed"
            )
            return {
                **base,
                "status": status,
                "authenticated": authenticated,
                "latencyMs": round((time.perf_counter() - started) * 1000),
                "result": evidence,
            }

        delivery_limitation = _DELIVERY_UNSUPPORTED.get(provider)
        if delivery_limitation is not None:
            return {
                **base,
                "status": "delivery_unsupported",
                "authenticated": True,
                "latencyMs": round((time.perf_counter() - started) * 1000),
                "targetConfigured": True,
                "deliveryAttempted": False,
                "detail": delivery_limitation,
                "result": evidence,
            }

        try:
            message = _delivery_message(provider, str(target or ""))
            await asyncio.wait_for(adapter.send(message), timeout=timeout)
        except TimeoutError:
            return {
                **base,
                "status": "delivery_timeout",
                "authenticated": True,
                "latencyMs": round((time.perf_counter() - started) * 1000),
                "timedOutOperation": "send_test_message",
                "targetConfigured": True,
                "deliveryAttempted": True,
                "detail": f"Provider operation exceeded {timeout:g} seconds.",
                "result": evidence,
            }
        except Exception as exc:
            return {
                **base,
                "status": "delivery_failed",
                "authenticated": True,
                "latencyMs": round((time.perf_counter() - started) * 1000),
                "targetConfigured": True,
                "deliveryAttempted": True,
                "detail": _error_detail(exc, (*secrets, str(target or ""))),
                "result": evidence,
            }
        return {
            **base,
            "status": "verified_with_delivery",
            "authenticated": True,
            "latencyMs": round((time.perf_counter() - started) * 1000),
            "targetConfigured": True,
            "deliveryAttempted": True,
            "result": evidence,
        }
    finally:
        await _close_adapter(adapter)


async def certify_channels(
    providers: Sequence[str],
    *,
    environ: Mapping[str, str],
    timeout: float = 15.0,
    send_test_message: bool = False,
    allow_side_effects: bool = False,
    targets: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Certify selected channel adapters and return secret-free JSON evidence."""

    if timeout <= 0:
        raise CertificationUsageError("timeout must be greater than zero")
    supported = set(supported_certification_providers())
    selected = list(dict.fromkeys(provider.strip().lower() for provider in providers))
    if not selected:
        selected = sorted(supported)
    unknown = sorted(set(selected) - supported)
    if unknown:
        raise CertificationUsageError(f"unsupported channel type(s): {', '.join(unknown)}")

    resolved_targets = dict(targets or {})
    if send_test_message:
        if not allow_side_effects:
            raise CertificationUsageError(
                "--send-test-message requires the explicit --allow-side-effects flag"
            )
        missing_targets = [provider for provider in selected if not resolved_targets.get(provider)]
        if missing_targets:
            raise CertificationUsageError(
                "side-effecting tests require --target provider=destination for: "
                + ", ".join(missing_targets)
            )
    elif allow_side_effects or resolved_targets:
        raise CertificationUsageError(
            "--allow-side-effects and --target are only valid with --send-test-message"
        )

    rows = [
        await _certify_one(
            provider,
            environ=environ,
            timeout=timeout,
            send_test_message=send_test_message,
            target=resolved_targets.get(provider),
        )
        for provider in selected
    ]
    passed = sum(
        row["status"] in {"verified", "verified_with_delivery"} for row in rows
    )
    return {
        "schemaVersion": EVIDENCE_SCHEMA_VERSION,
        "generatedAt": datetime.now(UTC).isoformat(),
        "mode": "send_test_message" if send_test_message else "safe_auth_probe",
        "summary": {"total": len(rows), "passed": passed, "failed": len(rows) - passed},
        "providers": rows,
    }


def evidence_passed(evidence: Mapping[str, Any]) -> bool:
    """Return whether every selected provider passed certification."""

    summary = evidence.get("summary")
    return isinstance(summary, Mapping) and summary.get("failed") == 0
