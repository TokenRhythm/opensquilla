from __future__ import annotations

import shlex
from typing import Any

from opensquilla.health.model import FixStep, HealthFinding, HealthSeverity
from opensquilla.router_runtime_diagnostics import (
    MACOS_LIBOMP_MISSING,
    ROUTER_ASSETS_MISSING,
    ROUTER_NATIVE_DEPENDENCY_MISSING,
    ROUTER_PYTHON_DEPENDENCY_MISSING,
    ROUTER_RUNTIME_UNAVAILABLE,
    WINDOWS_VC_RUNTIME_MISSING,
    classify_router_runtime_error,
    router_runtime_hint,
)

_LEGACY_PROVIDER_REPLACEMENTS = {
    "zai": "zhipu",
}
_API_KEY_PLACEHOLDER = "YOUR_API_KEY"
_ONNX_DIR_PLACEHOLDER = "PATH_TO_ONNX_MODELS"


def _known_provider_ids(rows: list[dict[str, Any]]) -> list[str]:
    return [
        provider_id
        for row in rows
        if (provider_id := str(row.get("providerId") or ""))
    ]


def _replacement_provider(active: str, known_provider_ids: list[str]) -> str:
    replacement = _LEGACY_PROVIDER_REPLACEMENTS.get(active)
    if replacement in known_provider_ids:
        return replacement
    for preferred in ("tokenrhythm", "openrouter"):
        if preferred in known_provider_ids:
            return preferred
    return known_provider_ids[0] if known_provider_ids else "tokenrhythm"


def _int_from_payload(payload: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            return int(str(value))
        except (TypeError, ValueError):
            continue
    return 0


def _command_arg(value: str) -> str:
    return shlex.quote(value)


def _diagnostic_incomplete(
    surface: str,
    *,
    expected_key: str,
    inspect_command: str,
) -> list[HealthFinding]:
    return [
        HealthFinding(
            id=f"{surface}.diagnostic.incomplete",
            severity="warn",
            readiness_impact="degrades",
            surface=surface,
            title=f"{surface.replace('_', ' ').title()} diagnostics are incomplete",
            detail=(
                f"{surface.replace('_', ' ').title()} diagnostics did not include "
                f"{expected_key}, so the state could not be interpreted."
            ),
            evidence={"expectedKey": expected_key},
            fix_steps=[
                FixStep(label="Inspect diagnostics", command=inspect_command),
                FixStep(label="Restart gateway", command="opensquilla gateway restart"),
            ],
            restart_required=True,
        )
    ]


def _channel_last_error(row: dict[str, Any]) -> dict[str, Any] | None:
    diagnostics = row.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return None
    last_error = diagnostics.get("last_error")
    return last_error if isinstance(last_error, dict) else None


def _channel_outbox_unknown(row: dict[str, Any]) -> dict[str, Any] | None:
    """The outbox 'unknown' bucket for a channel row, if it has entries."""
    diagnostics = row.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return None
    delivery = diagnostics.get("delivery")
    if not isinstance(delivery, dict):
        return None
    outbox = delivery.get("outbox")
    if not isinstance(outbox, dict):
        return None
    unknown = outbox.get("unknown")
    if not isinstance(unknown, dict):
        return None
    count = unknown.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return None
    return unknown


def evaluate_provider(payload: dict[str, Any]) -> list[HealthFinding]:
    raw_rows = payload.get("providers")
    if not isinstance(raw_rows, list):
        return [
            HealthFinding(
                id="provider.diagnostic.incomplete",
                severity="error",
                readiness_impact="blocks_ready",
                surface="provider",
                title="Provider diagnostics are incomplete",
                detail=(
                    "Provider diagnostics did not include providers, "
                    "so active provider readiness could not be interpreted."
                ),
                evidence={"expectedKey": "providers"},
                fix_steps=[
                    FixStep(
                        label="Inspect providers",
                        command="opensquilla providers status --json",
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]

    findings: list[HealthFinding] = []
    active = str(payload.get("activeProvider") or "")
    rows = raw_rows
    active_row = next((row for row in rows if row.get("active")), None)
    known_provider_ids = _known_provider_ids(rows)
    if not active_row:
        if active and active not in known_provider_ids:
            provider_id = _replacement_provider(active, known_provider_ids)
            return [
                HealthFinding(
                    id="provider.active.unknown",
                    severity="error",
                    surface="provider",
                    title="Active provider is unknown",
                    detail=(
                        f"{active} is configured as the active provider, but this "
                        "OpenSquilla build does not recognize it."
                    ),
                    evidence={
                        "activeProvider": active,
                        "knownProviders": known_provider_ids,
                    },
                    fix_steps=[
                        FixStep(
                            label="List supported providers",
                            command="opensquilla providers list --json",
                        ),
                        FixStep(
                            label="Configure a supported provider",
                            command=(
                                "opensquilla providers configure "
                                f"{provider_id} --api-key {_API_KEY_PLACEHOLDER}"
                            ),
                        ),
                        FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                    ],
                    restart_required=True,
                )
            ]

        provider_id = active or _replacement_provider(active, known_provider_ids)
        return [
            HealthFinding(
                id="provider.active.missing",
                severity="error",
                surface="provider",
                title="No active provider is available",
                detail="The gateway did not report a buildable active LLM provider.",
                evidence={"activeProvider": active},
                fix_steps=[
                    FixStep(
                        label="Configure a provider",
                        command=(
                            "opensquilla providers configure "
                            f"{provider_id} --api-key {_API_KEY_PLACEHOLDER}"
                        ),
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]

    provider_id = str(active_row.get("providerId") or active or "unknown")
    if provider_id == "unknown":
        return [
            HealthFinding(
                id="provider.active.unidentified",
                severity="error",
                surface="provider",
                title="Active provider is unidentified",
                detail="The active provider row did not include a provider id.",
                evidence={
                    "activeProvider": active,
                    "knownProviders": known_provider_ids,
                    "model": active_row.get("model"),
                },
                fix_steps=[
                    FixStep(
                        label="Inspect provider status",
                        command="opensquilla providers status --json",
                    ),
                    FixStep(
                        label="Configure a provider",
                        command=(
                            "opensquilla providers configure openrouter "
                            f"--api-key {_API_KEY_PLACEHOLDER}"
                        ),
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    if not active_row.get("configured"):
        requires_api_key = bool(active_row.get("requiresApiKey"))
        api_key_configured = bool(active_row.get("apiKeyConfigured"))
        api_key_env = str(active_row.get("apiKeyEnv") or "")
        evidence = {
            "providerId": provider_id,
            "requiresApiKey": requires_api_key,
            "apiKeyEnv": api_key_env,
            "apiKeyConfigured": api_key_configured,
            "baseUrlConfigured": bool(active_row.get("baseUrlConfigured")),
        }
        detail = f"{provider_id} is active but missing required configuration."
        fix_steps = [
            FixStep(
                label="Configure provider",
                command=(
                    "opensquilla providers configure "
                    f"{provider_id} --api-key {_API_KEY_PLACEHOLDER}"
                ),
            ),
            FixStep(label="Restart gateway", command="opensquilla gateway restart"),
        ]
        if requires_api_key and api_key_env and not api_key_configured:
            detail = (
                f"{provider_id} is active, but environment variable {api_key_env} "
                "is not set or is not visible to the gateway."
            )
            fix_steps.insert(
                0,
                FixStep(
                    label="Set provider environment variable",
                    detail=(
                        f"Set {api_key_env} in the gateway environment, then restart "
                        "OpenSquilla."
                    ),
                ),
            )
        findings.append(
            HealthFinding(
                id="provider.active.not_configured",
                severity="error",
                surface="provider",
                title="Active provider is not configured",
                detail=detail,
                evidence=evidence,
                fix_steps=fix_steps,
                restart_required=True,
            )
        )
    elif not active_row.get("buildable"):
        findings.append(
            HealthFinding(
                id="provider.active.not_buildable",
                severity="error",
                surface="provider",
                title="Active provider cannot be built",
                detail=str(active_row.get("error") or "Provider construction failed."),
                evidence={"providerId": provider_id, "model": active_row.get("model")},
                fix_steps=[
                    FixStep(
                        label="Inspect provider status",
                        command="opensquilla providers status --json",
                    ),
                    FixStep(
                        label="Update provider config",
                        command=f"opensquilla providers configure {provider_id}",
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        )
    else:
        # Rows from older gateways / offline doctor payloads carry no
        # apiKeyShape; default to "ok" so they behave exactly as before.
        api_key_shape = active_row.get("apiKeyShape", "ok")
        if api_key_shape not in ("ok", "", None):
            shape = str(api_key_shape)
            if shape == "looks_like_url":
                problem = (
                    "the configured API key looks like a URL. The key is sent "
                    "verbatim as the request credential, so the upstream "
                    "provider rejects it with 401."
                )
            elif shape == "looks_like_env_name":
                problem = (
                    "the configured API key looks like an environment variable "
                    "NAME, not its value. Requests authenticate with the "
                    "literal name and are rejected."
                )
            else:
                problem = f"the configured API key has a suspicious shape ({shape})."
            findings.append(
                HealthFinding(
                    id="provider.active.api_key_shape",
                    severity="warn",
                    surface="provider",
                    title="Active provider API key looks misconfigured",
                    detail=f"{provider_id} is configured and buildable, but {problem}",
                    evidence={"providerId": provider_id, "shape": shape},
                    fix_steps=[
                        FixStep(
                            label="Reconfigure provider key",
                            command=(
                                "opensquilla providers configure "
                                f"{provider_id} --api-key {_API_KEY_PLACEHOLDER}"
                            ),
                        ),
                        FixStep(
                            label="Update the key in the console",
                            detail=(
                                "Open Settings → Chat Model and paste the "
                                "provider's real API key value."
                            ),
                        ),
                    ],
                )
            )
        else:
            findings.append(
                HealthFinding(
                    id="provider.active.ready",
                    severity="ok",
                    surface="provider",
                    title="Active provider ready",
                    detail=f"{provider_id} is configured and buildable.",
                    evidence={"providerId": provider_id, "model": active_row.get("model")},
                )
            )
    return findings


def evaluate_memory(payload: dict[str, Any]) -> list[HealthFinding]:
    if "status" not in payload:
        return _diagnostic_incomplete(
            "memory",
            expected_key="status",
            inspect_command="opensquilla memory status --deep --json",
        )

    findings: list[HealthFinding] = []
    status = str(payload.get("status") or "unknown")
    if status in {"error", "unavailable"}:
        # Core turns can still run without memory; treat this as capability loss
        # rather than global readiness failure.
        findings.append(
            HealthFinding(
                id="memory.status.error",
                severity="error",
                readiness_impact="degrades",
                surface="memory",
                title="Memory backend unavailable",
                detail=str(payload.get("error") or "Memory backend is not usable."),
                evidence={"backend": payload.get("backend"), "status": status},
                fix_steps=[
                    FixStep(
                        label="Inspect memory",
                        command="opensquilla memory status --deep --json",
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        )
    elif status == "degraded":
        findings.append(
            HealthFinding(
                id="memory.status.degraded",
                severity="warn",
                surface="memory",
                title="Memory is degraded",
                detail="Memory is available but one or more retrieval components are degraded.",
                evidence={
                    "backend": payload.get("backend"),
                    "vecAvailable": bool(payload.get("vecAvailable")),
                    "ftsAvailable": bool(payload.get("ftsAvailable")),
                },
                fix_steps=[
                    FixStep(
                        label="Inspect memory",
                        command="opensquilla memory status --deep --json",
                    )
                ],
            )
        )
    elif status in {"ok", "ready", "healthy"}:
        findings.append(
            HealthFinding(
                id="memory.status.ready",
                severity="ok",
                surface="memory",
                title="Memory ready",
                detail="Memory backend reported a healthy status.",
                evidence={"backend": payload.get("backend"), "status": status},
            )
        )
    else:
        findings.append(
            HealthFinding(
                id="memory.status.unknown",
                severity="warn",
                surface="memory",
                title="Memory status is unknown",
                detail="Memory diagnostics returned an unrecognized status.",
                evidence={"backend": payload.get("backend"), "status": status},
                fix_steps=[
                    FixStep(
                        label="Inspect memory",
                        command="opensquilla memory status --deep --json",
                    )
                ],
            )
        )

    pending = _int_from_payload(payload, "pendingRepairCount", "pendingRepairs")
    if pending:
        findings.append(
            HealthFinding(
                id="memory.repair.pending",
                severity="warn",
                surface="memory",
                title="Memory repair work is pending",
                detail=f"{pending} compaction repair item(s) require attention.",
                evidence={"pendingRepairCount": pending},
                fix_steps=[
                    FixStep(label="List repairs", command="opensquilla memory repair list --json"),
                    FixStep(label="Run repairs", command="opensquilla memory repair run --json"),
                ],
            )
        )
    return findings


def evaluate_logs(payload: dict[str, Any]) -> list[HealthFinding]:
    raw_file_log = payload.get("gateway_file_log")
    if not isinstance(raw_file_log, dict):
        return [
            HealthFinding(
                id="logs.diagnostic.incomplete",
                severity="warn",
                readiness_impact="degrades",
                surface="logs",
                title="Log diagnostics are incomplete",
                detail=(
                    "Log diagnostics did not include gateway_file_log, "
                    "so the logging state could not be interpreted."
                ),
                evidence={"keys": sorted(str(key) for key in payload.keys())},
                fix_steps=[
                    FixStep(label="Inspect diagnostics", command="opensquilla diagnostics status"),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]

    file_log = raw_file_log
    if not file_log.get("enabled"):
        return [
            HealthFinding(
                id="logs.gateway_file_log.disabled",
                severity="info",
                surface="logs",
                title="Gateway file logging is disabled",
                detail=(
                    "Persistent gateway file logging is optional, but it makes runtime "
                    "failures easier to diagnose after the fact."
                ),
                evidence={
                    "enabled": False,
                    "path": file_log.get("path"),
                },
                fix_steps=[
                    FixStep(
                        label="Persist file logging",
                        command="opensquilla config set log_file_enabled true",
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    if (
        file_log.get("enabled")
        and not file_log.get("exists")
        and not file_log.get("active_tail_path_exists")
    ):
        return [
            HealthFinding(
                id="logs.gateway_file_log.missing",
                severity="warn",
                surface="logs",
                title="Gateway file log is not present",
                detail="Debug logging is configured, but no active log file was found.",
                evidence={"path": file_log.get("path")},
                fix_steps=[
                    FixStep(label="Inspect diagnostics", command="opensquilla diagnostics status"),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    return [
        HealthFinding(
            id="logs.gateway_file_log.ready",
            severity="ok",
            surface="logs",
            title="Gateway logs available",
            detail="Gateway log configuration is readable.",
            evidence={"path": file_log.get("path"), "enabled": bool(file_log.get("enabled"))},
        )
    ]


def evaluate_search(payload: dict[str, Any]) -> list[HealthFinding]:
    if "provider" not in payload and "activeProvider" not in payload:
        return _diagnostic_incomplete(
            "search",
            expected_key="provider or activeProvider",
            inspect_command="opensquilla search status --json",
        )

    configured_provider = str(payload.get("provider") or payload.get("activeProvider") or "")
    provider = configured_provider or "unknown"
    if configured_provider and not payload.get("unknownProvider"):
        missing_keys = [
            key
            for key in ("configured", "runtimeSupported", "buildable")
            if key not in payload
        ]
        if missing_keys:
            return _diagnostic_incomplete(
                "search",
                expected_key=", ".join(missing_keys),
                inspect_command="opensquilla search status --json",
            )
    configured = bool(payload.get("configured"))
    buildable = bool(payload.get("buildable"))
    runtime_supported = bool(payload.get("runtimeSupported"))
    requires_api_key = bool(payload.get("requiresApiKey"))
    api_key_configured = bool(payload.get("apiKeyConfigured"))
    api_key_env = str(payload.get("apiKeyEnv") or "")
    evidence = {
        "provider": provider,
        "activeProvider": payload.get("activeProvider"),
        "runtimeSupported": runtime_supported,
        "requiresApiKey": requires_api_key,
        "apiKeyEnv": api_key_env,
        "apiKeyConfigured": api_key_configured,
        "fallbackPolicy": payload.get("fallbackPolicy"),
        "maxResults": payload.get("maxResults"),
        "proxyConfigured": payload.get("proxyConfigured"),
        "useEnvProxy": payload.get("useEnvProxy"),
        "diagnostics": payload.get("diagnostics"),
    }
    configure_command = f"opensquilla configure search --search-provider {provider}"
    if requires_api_key:
        configure_command = f"{configure_command} --api-key {_API_KEY_PLACEHOLDER}"

    if not configured_provider:
        return [
            HealthFinding(
                id="search.provider.disabled",
                severity="info",
                surface="search",
                title="Search provider is not configured",
                detail=(
                    "Web search is not configured. OpenSquilla can run, but web "
                    "research tools are unavailable until a provider is selected."
                ),
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Configure search",
                        command="opensquilla configure search --search-provider duckduckgo",
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]

    if payload.get("unknownProvider"):
        return [
            HealthFinding(
                id="search.provider.unknown",
                severity="warn",
                surface="search",
                title="Search provider is unknown",
                detail=(
                    f"{provider} is selected for web search, but this OpenSquilla "
                    "build does not recognize it."
                ),
                evidence={**evidence, "error": payload.get("error")},
                fix_steps=[
                    FixStep(
                        label="List search providers",
                        command="opensquilla search list --json",
                    ),
                    FixStep(
                        label="Choose supported provider",
                        command="opensquilla configure search --search-provider duckduckgo",
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]

    if not runtime_supported:
        return [
            HealthFinding(
                id="search.provider.unsupported",
                severity="warn",
                surface="search",
                title="Search provider is not supported by this runtime",
                detail=f"{provider} is selected, but it is not supported in the current runtime.",
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="List search providers",
                        command="opensquilla search list --json",
                    ),
                    FixStep(
                        label="Choose supported provider",
                        command="opensquilla configure search --search-provider duckduckgo",
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    if not configured:
        detail = (
            f"{provider} is selected for web search but is missing required "
            "configuration."
        )
        fix_steps = [
            FixStep(label="Configure search", command=configure_command),
            FixStep(
                label="Inspect search status",
                command=f"opensquilla search status {provider} --json",
            ),
            FixStep(label="Restart gateway", command="opensquilla gateway restart"),
        ]
        if requires_api_key and api_key_env and not api_key_configured:
            detail = (
                f"{provider} is selected for web search, but environment "
                f"variable {api_key_env} is not set or is not visible to the gateway."
            )
            fix_steps.insert(
                0,
                FixStep(
                    label="Set search environment variable",
                    detail=(
                        f"Set {api_key_env} in the gateway environment, then restart "
                        "OpenSquilla."
                    ),
                ),
            )
        return [
            HealthFinding(
                id="search.provider.not_configured",
                severity="warn",
                surface="search",
                title="Search provider is not configured",
                detail=detail,
                evidence=evidence,
                fix_steps=fix_steps,
                restart_required=True,
            )
        ]
    if not buildable:
        return [
            HealthFinding(
                id="search.provider.not_buildable",
                severity="warn",
                surface="search",
                title="Search provider cannot be built",
                detail=str(payload.get("error") or "Search provider construction failed."),
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Inspect search status",
                        command=f"opensquilla search status {provider} --json",
                    ),
                    FixStep(label="Reconfigure search", command=configure_command),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    return [
        HealthFinding(
            id="search.provider.ready",
            severity="ok",
            surface="search",
            title="Search provider ready",
            detail=f"{provider} is configured and buildable.",
            evidence=evidence,
        )
    ]


def evaluate_image_generation(payload: dict[str, Any]) -> list[HealthFinding]:
    if "enabled" not in payload:
        return _diagnostic_incomplete(
            "image_generation",
            expected_key="enabled",
            inspect_command="opensquilla onboard status --json",
        )

    enabled = bool(payload.get("enabled"))
    if enabled:
        missing_keys = [key for key in ("configured", "status") if key not in payload]
        if missing_keys:
            return _diagnostic_incomplete(
                "image_generation",
                expected_key=", ".join(missing_keys),
                inspect_command="opensquilla onboard status --json",
            )
    configured = bool(payload.get("configured"))
    status = str(payload.get("status") or "unknown")
    provider = str(payload.get("provider") or "")
    primary = str(payload.get("primary") or "")
    if not provider and "/" in primary:
        provider = primary.split("/", 1)[0]
    provider = provider or "openai"
    api_key_env = str(payload.get("apiKeyEnv") or "")
    evidence = {
        "enabled": enabled,
        "configured": configured,
        "status": status,
        "provider": payload.get("provider"),
        "primary": primary,
        "source": payload.get("source"),
        "apiKeyEnv": api_key_env,
        "configPath": payload.get("configPath"),
    }

    if not enabled:
        return [
            HealthFinding(
                id="image_generation.disabled",
                severity="info",
                surface="image_generation",
                title="Image generation is disabled",
                detail="Image generation is optional and is currently disabled.",
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Enable image generation",
                        command=(
                            "opensquilla configure image-generation "
                            f"--image-provider {provider} --api-key {_API_KEY_PLACEHOLDER}"
                        ),
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    if configured and status == "ok":
        return [
            HealthFinding(
                id="image_generation.ready",
                severity="ok",
                surface="image_generation",
                title="Image generation ready",
                detail=f"{provider} image generation is configured.",
                evidence=evidence,
            )
        ]
    if status == "unknown":
        finding_id = "image_generation.provider.unknown"
        title = "Image generation provider is unknown"
        detail = "Image generation is enabled, but the provider reference is not recognized."
        recovery_provider = "openai"
        fix_steps = [
            FixStep(
                label="Configure image generation",
                command=(
                    "opensquilla configure image-generation "
                    f"--image-provider {recovery_provider} --api-key {_API_KEY_PLACEHOLDER}"
                ),
            ),
            FixStep(label="Inspect onboarding", command="opensquilla onboard status --json"),
            FixStep(label="Restart gateway", command="opensquilla gateway restart"),
        ]
    else:
        finding_id = "image_generation.credentials.missing"
        title = "Image generation credentials are missing"
        detail = "Image generation is enabled but missing usable provider credentials."
        recovery_provider = provider
        fix_steps = [
            FixStep(
                label="Configure image generation",
                command=(
                    "opensquilla configure image-generation "
                    f"--image-provider {recovery_provider} --api-key {_API_KEY_PLACEHOLDER}"
                ),
            ),
            FixStep(label="Inspect onboarding", command="opensquilla onboard status --json"),
            FixStep(label="Restart gateway", command="opensquilla gateway restart"),
        ]
        if api_key_env:
            detail = (
                "Image generation is enabled, but environment variable "
                f"{api_key_env} is not set or is not visible to the gateway."
            )
            fix_steps.insert(
                0,
                FixStep(
                    label="Set image environment variable",
                    detail=(
                        f"Set {api_key_env} in the gateway environment, then restart "
                        "OpenSquilla."
                    ),
                ),
            )
    return [
        HealthFinding(
            id=finding_id,
            severity="warn",
            surface="image_generation",
            title=title,
            detail=detail,
            evidence=evidence,
            fix_steps=fix_steps,
            restart_required=True,
        )
    ]


def _router_runtime_missing_guidance(
    payload: dict[str, Any],
) -> tuple[str, str, list[FixStep]]:
    error = str(payload.get("error") or "The configured router runtime is unavailable.")
    kind = str(payload.get("runtimeErrorKind") or classify_router_runtime_error(error))
    if kind == MACOS_LIBOMP_MISSING:
        return (
            "Router native dependency is missing",
            (
                "LightGBM could not load macOS OpenMP runtime libomp.dylib. "
                f"{error}"
            ),
            [
                FixStep(label="Install macOS OpenMP runtime", command="brew install libomp"),
                FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                FixStep(
                    label="Disable local router",
                    command="opensquilla configure router --router disabled",
                ),
            ],
        )
    if kind == WINDOWS_VC_RUNTIME_MISSING:
        return (
            "Router native dependency is missing",
            (
                "ONNX Runtime could not load on Windows. Install the Visual C++ "
                f"Redistributable for Visual Studio 2015-2022 (x64). {error}"
            ),
            [
                FixStep(
                    label="Download Visual C++ Redistributable",
                    command="https://aka.ms/vs/17/release/vc_redist.x64.exe",
                ),
                FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                FixStep(
                    label="Disable local router",
                    command="opensquilla configure router --router disabled",
                ),
            ],
        )
    if kind == ROUTER_ASSETS_MISSING:
        return (
            "Router runtime assets are missing",
            error,
            [
                FixStep(
                    label="Disable local router",
                    command="opensquilla configure router --router disabled",
                ),
                FixStep(
                    label="Reconfigure recommended router",
                    command="opensquilla configure router --router recommended",
                ),
                FixStep(label="Restart gateway", command="opensquilla gateway restart"),
            ],
        )
    if kind in {ROUTER_NATIVE_DEPENDENCY_MISSING, ROUTER_PYTHON_DEPENDENCY_MISSING}:
        return (
            "Router runtime dependency is missing",
            error,
            [
                FixStep(
                    label="Reinstall recommended dependencies",
                    detail=(
                        "Reinstall using the same release URL or source install command, "
                        "including the recommended extra."
                    ),
                ),
                FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                FixStep(
                    label="Disable local router",
                    command="opensquilla configure router --router disabled",
                ),
            ],
        )
    return (
        "Router runtime is unavailable",
        error,
        [
            FixStep(
                label="Disable local router",
                command="opensquilla configure router --router disabled",
            ),
            FixStep(
                label="Reconfigure recommended router",
                command="opensquilla configure router --router recommended",
            ),
            FixStep(label="Restart gateway", command="opensquilla gateway restart"),
        ],
    )


def evaluate_router(payload: dict[str, Any]) -> list[HealthFinding]:
    findings = _evaluate_router_readiness(payload)
    findings.extend(_router_tier_provider_mismatch_findings(payload))
    return findings


def _router_tier_provider_mismatch_findings(payload: dict[str, Any]) -> list[HealthFinding]:
    """Advisory: enabled router tiers whose provider will silently misroute.

    Emitted only for the risky combination — a configured tier names a
    provider other than the active one, ``cross_provider_tiers`` is off, and
    ``squilla_router.tier_provider_mismatch`` is ``"route"`` — where the
    tier's model id is sent to the active provider's credentials. Aligned
    tiers, cross-provider execution, or veto mode produce no finding.
    """
    if not payload.get("enabled"):
        return []
    if bool(payload.get("crossProviderTiers")):
        return []
    mode = str(payload.get("tierProviderMismatch") or "route")
    if mode != "route":
        return []
    raw_mismatched = payload.get("mismatchedTierProviders")
    if not isinstance(raw_mismatched, dict) or not raw_mismatched:
        return []
    active = str(payload.get("activeProvider") or "")
    if not active:
        return []
    mismatched = {str(tier): str(provider) for tier, provider in raw_mismatched.items()}
    tier_list = ", ".join(
        f"{tier} -> {provider}" for tier, provider in sorted(mismatched.items())
    )
    return [
        HealthFinding(
            id="router.tier_provider.mismatch",
            severity="warn",
            surface="router",
            title="Router tiers will silently misroute",
            detail=(
                f"Router tier(s) {tier_list} name a provider other than the active "
                f"provider {active}, and cross-provider tier execution is disabled. "
                'With squilla_router.tier_provider_mismatch = "route" these tiers '
                "misroute: the tier's model id is sent to the active provider's "
                'credentials. Set squilla_router.tier_provider_mismatch = "veto" to '
                "rebind such turns to the nearest same-provider tier, enable "
                "cross_provider_tiers, or align the tier providers."
            ),
            evidence={
                "activeProvider": active,
                "mismatchedTierProviders": mismatched,
                "crossProviderTiers": False,
                "tierProviderMismatch": mode,
            },
            fix_steps=[
                FixStep(
                    label="Veto mismatched tiers",
                    command=(
                        "opensquilla config set squilla_router.tier_provider_mismatch veto"
                    ),
                ),
                FixStep(
                    label="Inspect router config",
                    command="opensquilla diagnostics status",
                ),
                FixStep(label="Restart gateway", command="opensquilla gateway restart"),
            ],
            restart_required=True,
        )
    ]


def _evaluate_router_readiness(payload: dict[str, Any]) -> list[HealthFinding]:
    if "enabled" not in payload:
        return _diagnostic_incomplete(
            "router",
            expected_key="enabled",
            inspect_command="opensquilla diagnostics status",
        )

    enabled = bool(payload.get("enabled"))
    if enabled:
        missing_keys = [
            key
            for key in ("runtimeValid", "rolloutPhase", "requireRouterRuntime")
            if key not in payload
        ]
        if missing_keys:
            return _diagnostic_incomplete(
                "router",
                expected_key=", ".join(missing_keys),
                inspect_command="opensquilla diagnostics status",
            )
    rollout_phase = str(payload.get("rolloutPhase") or "unknown")
    strategy = str(payload.get("strategy") or "unknown")
    tier_profile = str(payload.get("tierProfile") or "custom")
    runtime_valid = bool(payload.get("runtimeValid"))
    require_runtime = bool(payload.get("requireRouterRuntime"))
    runtime_error_kind = payload.get("runtimeErrorKind")
    if not runtime_valid and not runtime_error_kind:
        runtime_error_kind = classify_router_runtime_error(
            str(payload.get("error") or "The configured router runtime is unavailable.")
        )
    evidence = {
        "enabled": enabled,
        "rolloutPhase": rollout_phase,
        "strategy": strategy,
        "tierProfile": tier_profile,
        "defaultTier": payload.get("defaultTier"),
        "runtimeValid": runtime_valid,
        "requireRouterRuntime": require_runtime,
        "runtimeErrorKind": runtime_error_kind,
    }

    if not enabled:
        return [
            HealthFinding(
                id="router.disabled",
                severity="info",
                surface="router",
                title="Router is disabled",
                detail="Local model routing is optional and is currently disabled.",
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Enable recommended router",
                        command="opensquilla configure router --router recommended",
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    if not runtime_valid:
        severity: HealthSeverity = "error" if require_runtime else "warn"
        title, detail, fix_steps = _router_runtime_missing_guidance(payload)
        return [
            HealthFinding(
                id="router.runtime.missing",
                severity=severity,
                surface="router",
                title=title,
                detail=detail,
                evidence=evidence,
                fix_steps=fix_steps,
                restart_required=True,
            )
        ]
    if rollout_phase not in {"full", "observe"}:
        return [
            HealthFinding(
                id="router.rollout_phase.unknown",
                severity="warn",
                surface="router",
                title="Router rollout phase needs review",
                detail=f"Router rollout phase {rollout_phase} is not recognized.",
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Reconfigure recommended router",
                        command="opensquilla configure router --router recommended",
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    if rollout_phase != "full":
        return [
            HealthFinding(
                id="router.observe_only",
                severity="info",
                surface="router",
                title="Router is not active for turns",
                detail=(
                    f"Router rollout phase is {rollout_phase}; turns use the configured "
                    "provider path."
                ),
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Enable router for turns",
                        command="opensquilla configure router --router recommended",
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    return [
        HealthFinding(
            id="router.ready",
            severity="ok",
            surface="router",
            title="Router ready",
            detail=f"{strategy} router is active with {tier_profile} profile.",
            evidence=evidence,
        )
    ]


def evaluate_memory_embedding(payload: dict[str, Any]) -> list[HealthFinding]:
    if "status" not in payload:
        return _diagnostic_incomplete(
            "memory_embedding",
            expected_key="status",
            inspect_command="opensquilla memory status --deep --json",
        )

    status = str(payload.get("status") or "unknown")
    if status in {"ok", "ready", "healthy", "fts_only"} and "effectiveProvider" not in payload:
        return _diagnostic_incomplete(
            "memory_embedding",
            expected_key="effectiveProvider",
            inspect_command="opensquilla memory status --deep --json",
        )
    requested = str(payload.get("requestedProvider") or "auto")
    effective = str(payload.get("effectiveProvider") or "none")
    model = str(payload.get("model") or "")
    evidence = {
        "status": status,
        "requestedProvider": requested,
        "effectiveProvider": effective,
        "model": model,
        "retrievalMode": payload.get("retrievalMode"),
        "reason": payload.get("reason"),
    }

    if status in {"error", "config_error", "invalid"}:
        return [
            HealthFinding(
                id="memory_embedding.config.error",
                severity="warn",
                surface="memory_embedding",
                title="Memory embedding configuration needs attention",
                detail=str(payload.get("error") or "Memory embedding configuration is invalid."),
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Configure memory embeddings",
                        command=(
                            "opensquilla configure memory-embedding "
                            f"--memory-provider openai --api-key {_API_KEY_PLACEHOLDER}"
                        ),
                    ),
                    FixStep(
                        label="Inspect memory deeply",
                        command="opensquilla memory status --deep --json",
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    if effective == "none":
        return [
            HealthFinding(
                id="memory_embedding.fts_only",
                severity="info",
                surface="memory_embedding",
                title="Memory embeddings are using FTS-only mode",
                detail="Vector memory is optional; retrieval is currently limited to text search.",
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Configure local embeddings",
                        command=(
                            "opensquilla configure memory-embedding "
                            f"--memory-provider local --onnx-dir {_ONNX_DIR_PLACEHOLDER}"
                        ),
                    ),
                    FixStep(
                        label="Configure remote embeddings",
                        command=(
                            "opensquilla configure memory-embedding "
                            f"--memory-provider openai --api-key {_API_KEY_PLACEHOLDER}"
                        ),
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    if status not in {"ok", "ready", "healthy"}:
        return [
            HealthFinding(
                id="memory_embedding.status.unknown",
                severity="warn",
                surface="memory_embedding",
                title="Memory embedding status needs review",
                detail=(
                    f"Memory embeddings reported status {status}; vector retrieval "
                    "should be inspected before treating it as ready."
                ),
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Inspect memory deeply",
                        command="opensquilla memory status --deep --json",
                    ),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    return [
        HealthFinding(
            id="memory_embedding.ready",
            severity="ok",
            surface="memory_embedding",
            title="Memory embeddings ready",
            detail=f"{effective} embeddings are selected for memory retrieval.",
            evidence=evidence,
        )
    ]


def evaluate_channels(payload: dict[str, Any]) -> list[HealthFinding]:
    if "channels" not in payload:
        return _diagnostic_incomplete(
            "channels",
            expected_key="channels",
            inspect_command="opensquilla channels status --json",
        )

    raw_rows = payload.get("channels")
    if not isinstance(raw_rows, list):
        return _diagnostic_incomplete(
            "channels",
            expected_key="channels",
            inspect_command="opensquilla channels status --json",
        )
    if any(not isinstance(row, dict) for row in raw_rows):
        return _diagnostic_incomplete(
            "channels",
            expected_key="channel rows",
            inspect_command="opensquilla channels status --json",
        )

    findings: list[HealthFinding] = []
    rows = raw_rows
    if not rows:
        return [
            HealthFinding(
                id="channels.none_configured",
                severity="info",
                surface="channels",
                title="No channels are configured",
                detail=(
                    "No channel entrypoints are configured. OpenSquilla can run locally, "
                    "but external chat surfaces are unavailable."
                ),
                evidence={"channelCount": 0},
                fix_steps=[
                    FixStep(
                        label="Configure channels",
                        command="opensquilla configure --section channels",
                    )
                ],
            )
        ]

    for row in rows:
        name = str(row.get("name") or "unnamed")
        name_arg = _command_arg(name)
        status = str(row.get("status") or "unknown")
        last_error = _channel_last_error(row)
        if isinstance(last_error, dict) and last_error.get("error_class") == "auth_invalid":
            message = str(last_error.get("message") or "Channel credentials are invalid.")
            provider_code = str(last_error.get("provider_code") or "")
            channel_type = str(row.get("type") or "channel")
            findings.append(
                HealthFinding(
                    id=f"channel.{name}.auth_invalid",
                    severity="error",
                    readiness_impact="degrades",
                    surface="channels",
                    title=f"Channel {name} credentials are invalid",
                    detail=(
                        f"{message}. {channel_type} rejected the configured credentials. "
                        "For DingTalk Stream Mode, check that client_id is the AppKey "
                        "and client_secret is the matching AppSecret from the same "
                        "DingTalk app or robot."
                    ),
                    evidence={
                        "name": name,
                        "channelName": name,
                        "status": status,
                        "type": row.get("type"),
                        "errorClass": "auth_invalid",
                        "providerCode": provider_code,
                    },
                    fix_steps=[
                        FixStep(
                            label="Inspect channels",
                            command=f"opensquilla channels status {name_arg} --json",
                        ),
                        FixStep(
                            label="Check DingTalk credentials",
                            detail=(
                                "Update the channel config with the AppKey/AppSecret "
                                "from the same DingTalk app or robot."
                            ),
                        ),
                        FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                    ],
                    restart_required=True,
                )
            )
        elif row.get("enabled") is False:
            findings.append(
                HealthFinding(
                    id=f"channel.{name}.disabled",
                    severity="info",
                    surface="channels",
                    title=f"Channel {name} is disabled",
                    detail="The channel is configured but disabled on disk.",
                    evidence={
                        "name": name,
                        "channelName": name,
                        "status": status,
                        "type": row.get("type"),
                    },
                    fix_steps=[
                        FixStep(
                            label="Enable channel",
                            command=f"opensquilla channels enable {name_arg}",
                        ),
                        FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                    ],
                    restart_required=True,
                )
            )
        elif status in {"dead", "exhausted"}:
            findings.append(
                HealthFinding(
                    id=f"channel.{name}.{status}",
                    severity="error",
                    readiness_impact="degrades",
                    surface="channels",
                    title=f"Channel {name} is {status}",
                    detail="The configured channel is not able to receive or send messages.",
                    evidence={
                        "name": name,
                        "channelName": name,
                        "status": status,
                        "type": row.get("type"),
                    },
                    fix_steps=[
                        FixStep(
                            label="Restart channel",
                            command=f"opensquilla channels restart {name_arg} --yes",
                        ),
                        FixStep(
                            label="Inspect channels",
                            command=f"opensquilla channels status {name_arg} --json",
                        ),
                    ],
                )
            )
        elif status == "stopped":
            findings.append(
                HealthFinding(
                    id=f"channel.{name}.stopped",
                    severity="warn",
                    surface="channels",
                    title=f"Channel {name} is stopped",
                    detail="The channel is configured and enabled but is not connected.",
                    evidence={
                        "name": name,
                        "channelName": name,
                        "status": status,
                        "type": row.get("type"),
                    },
                    fix_steps=[
                        FixStep(
                            label="Inspect channels",
                            command=f"opensquilla channels status {name_arg} --json",
                        ),
                        FixStep(
                            label="Restart channel",
                            command=f"opensquilla channels restart {name_arg} --yes",
                        ),
                    ],
                )
            )
        elif status == "restarting":
            findings.append(
                HealthFinding(
                    id=f"channel.{name}.restarting",
                    severity="warn",
                    surface="channels",
                    title=f"Channel {name} is restarting",
                    detail="The channel is recovering after dispatch errors.",
                    evidence={
                        "name": name,
                        "channelName": name,
                        "status": status,
                        "type": row.get("type"),
                    },
                    fix_steps=[
                        FixStep(
                            label="Inspect channels",
                            command=f"opensquilla channels status {name_arg} --json",
                        )
                    ],
                )
            )
        elif status not in {"connected", "running", "ready", "healthy"}:
            findings.append(
                HealthFinding(
                    id=f"channel.{name}.unknown_status",
                    severity="warn",
                    surface="channels",
                    title=f"Channel {name} status needs review",
                    detail=(
                        f"The channel reported status {status}, which is not recognized "
                        "as a ready state."
                    ),
                    evidence={
                        "name": name,
                        "channelName": name,
                        "status": status,
                        "type": row.get("type"),
                    },
                    fix_steps=[
                        FixStep(
                            label="Inspect channels",
                            command=f"opensquilla channels status {name_arg} --json",
                        ),
                        FixStep(
                            label="Restart channel",
                            command=f"opensquilla channels restart {name_arg} --yes",
                        ),
                    ],
                )
            )
        # Action items ride alongside the status finding: a connected channel
        # can still be silently gating senders or losing replies, and "ready"
        # must not paper over either.
        pending_pairings = row.get("pendingPairings")
        if (
            isinstance(pending_pairings, int)
            and not isinstance(pending_pairings, bool)
            and pending_pairings > 0
        ):
            findings.append(
                HealthFinding(
                    id=f"channel.{name}.pending_pairings",
                    severity="warn",
                    # Awaiting a human decision, not a malfunction: surface it
                    # without downgrading overall readiness.
                    readiness_impact="optional",
                    surface="channels",
                    title=f"Channel {name} has pairing requests awaiting approval",
                    detail=(
                        f"{pending_pairings} sender(s) asked to pair and were told to "
                        "wait for operator approval. Until someone approves or revokes "
                        "the requests, their messages get no replies."
                    ),
                    evidence={
                        "name": name,
                        "channelName": name,
                        "pendingPairings": pending_pairings,
                    },
                    fix_steps=[
                        FixStep(
                            label="Review pending pairings",
                            command=f"opensquilla channels pairings list {name_arg}",
                            detail=(
                                "Approve or revoke each request from this list or "
                                "the control console Channels page."
                            ),
                        ),
                        FixStep(
                            label="Inspect channels",
                            command=f"opensquilla channels status {name_arg} --json",
                        ),
                    ],
                )
            )
        outbox_unknown = _channel_outbox_unknown(row)
        if outbox_unknown is not None:
            unknown_count = int(outbox_unknown["count"])
            findings.append(
                HealthFinding(
                    id=f"channel.{name}.sends_unknown_outcome",
                    severity="warn",
                    # Unknown-outcome rows are terminal until a human resolves
                    # them; letting them degrade readiness would flag the
                    # channel forever after a single lost send.
                    readiness_impact="optional",
                    surface="channels",
                    title=f"Channel {name} has sends with unknown outcome",
                    detail=(
                        f"{unknown_count} outbound send(s) raised mid-delivery, so "
                        "whether they reached the recipient is unknown. These rows "
                        "are kept for a human decision; nothing retries them "
                        "automatically."
                    ),
                    evidence={
                        "name": name,
                        "channelName": name,
                        "unknownSends": unknown_count,
                        "oldestAt": outbox_unknown.get("oldest_at"),
                    },
                    fix_steps=[
                        FixStep(
                            label="Inspect channels",
                            command=f"opensquilla channels status {name_arg} --json",
                        ),
                        FixStep(
                            label="Confirm delivery with the recipient",
                            detail=(
                                "Check the conversation on the provider side; the "
                                "outbox diagnostics carry an error class per "
                                "affected send."
                            ),
                        ),
                    ],
                )
            )
    if findings:
        return findings
    status_counts: dict[str, int] = {}
    types: set[str] = set()
    enabled_count = 0
    for row in rows:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        types.add(str(row.get("type") or "unknown"))
        if row.get("enabled") is not False:
            enabled_count += 1
    return [
        HealthFinding(
            id="channels.ready",
            severity="ok",
            surface="channels",
            title="Channels ready",
            detail=f"{len(rows)} configured channel entrypoints require no attention.",
            evidence={
                "channelCount": len(rows),
                "enabledCount": enabled_count,
                "statuses": status_counts,
                "types": sorted(types),
            },
        )
    ]


def evaluate_sandbox(payload: dict[str, Any]) -> list[HealthFinding]:
    posture = str(payload.get("posture") or "unknown")
    evidence = {
        key: value
        for key, value in payload.items()
        if key not in {"restart_required", "restartRequired"}
    }
    if posture == "unknown":
        return [
            HealthFinding(
                id="sandbox.posture.unknown",
                severity="warn",
                surface="sandbox",
                title="Sandbox posture is unknown",
                detail="OpenSquilla could not determine the current sandbox posture.",
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Inspect sandbox",
                        command="opensquilla sandbox status --json",
                    )
                ],
            )
        ]
    if posture == "bypass":
        return [
            HealthFinding(
                id="sandbox.posture.bypass",
                severity="info",
                surface="sandbox",
                title="Sandbox posture is bypass",
                detail="OpenSquilla is configured for maximum convenience, not strict isolation.",
                evidence=evidence,
                fix_steps=[
                    FixStep(label="Enable sandbox", command="opensquilla sandbox on"),
                    FixStep(label="Enable full posture", command="opensquilla sandbox full"),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    if posture == "custom":
        return [
            HealthFinding(
                id="sandbox.posture.custom",
                severity="warn",
                surface="sandbox",
                title="Sandbox posture is custom",
                detail="Sandbox and permission settings do not match a standard posture.",
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Inspect sandbox",
                        command="opensquilla sandbox status --json",
                    ),
                    FixStep(label="Enable sandbox", command="opensquilla sandbox on"),
                    FixStep(label="Restart gateway", command="opensquilla gateway restart"),
                ],
                restart_required=True,
            )
        ]
    return [
        HealthFinding(
            id="sandbox.posture.ready",
            severity="ok",
            surface="sandbox",
            title="Sandbox posture configured",
            detail=f"Sandbox posture is {posture}.",
            evidence=evidence,
        )
    ]


# selection_mode → (member-provider label, env-key fallback) for the static
# B5 profiles. Payload-driven mirror of the gateway's static-B5 mode table.
_STATIC_B5_MODE_DETAILS = {
    "static_openrouter_b5": ("OpenRouter", "OPENROUTER_API_KEY"),
    "static_tokenrhythm_b5": ("TokenRhythm", "TOKENRHYTHM_API_KEY"),
}


def evaluate_llm_ensemble(payload: dict[str, Any]) -> list[HealthFinding]:
    enabled = bool(payload.get("enabled"))
    selection_mode = str(payload.get("selectionMode") or "")
    if enabled and selection_mode == "custom_b5":
        return _evaluate_custom_b5_ensemble(payload)
    mode_details = _STATIC_B5_MODE_DETAILS.get(selection_mode)
    if not enabled or mode_details is None:
        return []
    provider_label, env_key_fallback = mode_details
    api_key_env = str(payload.get("apiKeyEnv") or env_key_fallback)
    credential_available = bool(payload.get("credentialAvailable"))
    evidence = {
        "enabled": enabled,
        "selectionMode": selection_mode,
        "activeProvider": payload.get("activeProvider"),
        "apiKeyEnv": api_key_env,
        "credentialAvailable": credential_available,
    }
    if credential_available:
        return [
            HealthFinding(
                id=f"llm_ensemble.{selection_mode}.ready",
                severity="ok",
                surface="llm_ensemble",
                title="LLM ensemble ready",
                detail=(
                    f"The static {provider_label} B5 ensemble resolves a "
                    f"{provider_label} credential and is active for turns."
                ),
                evidence=evidence,
            )
        ]
    return [
        HealthFinding(
            id=f"llm_ensemble.{selection_mode}.credentials.missing",
            severity="warn",
            surface="llm_ensemble",
            title="LLM ensemble is enabled but cannot run",
            detail=(
                f"LLM ensemble (static {provider_label} B5) is enabled but no "
                f"{provider_label} credential resolves — the ensemble is inactive and "
                f"every turn falls back to the single configured provider. Set "
                f"{api_key_env}, switch llm_ensemble.selection_mode, or disable the "
                "ensemble."
            ),
            evidence=evidence,
            fix_steps=[
                FixStep(
                    label=f"Set {provider_label} API key",
                    detail=(
                        f"Set {api_key_env} in the gateway environment, then restart "
                        "the gateway."
                    ),
                ),
                FixStep(
                    label="Disable the ensemble",
                    command="opensquilla config set llm_ensemble.enabled false",
                ),
                FixStep(label="Restart gateway", command="opensquilla gateway restart"),
            ],
            restart_required=True,
        )
    ]


def _evaluate_custom_b5_ensemble(payload: dict[str, Any]) -> list[HealthFinding]:
    """Readiness finding for the explicit custom lineup.

    ``lineupReady``/``lineupBlockedReason`` come from the doctor collector
    (``custom_b5_lineup_ready``): False means the turn-time wrap is being
    skipped — either the lineup has no enabled proposers or a member's
    provider resolves no API key.
    """
    ready = bool(payload.get("lineupReady"))
    reason = str(payload.get("lineupBlockedReason") or "")
    evidence = {
        "enabled": True,
        "selectionMode": "custom_b5",
        "activeProvider": payload.get("activeProvider"),
        "lineupReady": ready,
        "lineupBlockedReason": reason,
    }
    if ready:
        return [
            HealthFinding(
                id="llm_ensemble.custom_b5.ready",
                severity="ok",
                surface="llm_ensemble",
                title="LLM ensemble ready",
                detail=(
                    "The custom ensemble lineup resolves credentials for every "
                    "member and is active for turns."
                ),
                evidence=evidence,
            )
        ]
    if reason.startswith("missing_credential:"):
        provider_id = reason.split(":", 1)[1]
        detail = (
            "LLM ensemble (custom lineup) is enabled but the "
            f"{provider_id} member resolves no API key — the ensemble is "
            "inactive and every turn falls back to the single configured "
            "provider. Set the provider key, remove the member, or disable "
            "the ensemble."
        )
    elif reason == "no_proposers":
        detail = (
            "LLM ensemble (custom lineup) is enabled but has no enabled "
            "proposer candidates — add candidates or disable the ensemble."
        )
    else:
        detail = (
            "LLM ensemble (custom lineup) is enabled but cannot run "
            f"({reason or 'unknown reason'}) — every turn falls back to the "
            "single configured provider."
        )
    return [
        HealthFinding(
            id="llm_ensemble.custom_b5.not_ready",
            severity="warn",
            surface="llm_ensemble",
            title="LLM ensemble is enabled but cannot run",
            detail=detail,
            evidence=evidence,
            fix_steps=[
                FixStep(
                    label="Review the ensemble lineup",
                    detail=(
                        "Open Settings → Model routing and fix the flagged "
                        "member, or remove it from the lineup."
                    ),
                ),
                FixStep(
                    label="Disable the ensemble",
                    command="opensquilla config set llm_ensemble.enabled false",
                ),
            ],
            restart_required=False,
        )
    ]


def evaluate_squilla_router_runtime(payload: dict[str, Any]) -> list[HealthFinding]:
    """Persistent surfacing of the router runtime load outcome.

    Unlike ``evaluate_router`` (which re-validates config and bundle assets),
    this reads the live strategy singleton from the turn loop: which
    classifier is actually serving turns right now.

    Finding matrix:

    * runtime loaded → ok ``squilla_router.runtime.ready``.
    * runtime failed to load and ``require_router_runtime`` is true → warn
      ``squilla_router.runtime.unavailable`` (readiness degrades). This is
      how the flag gets teeth without refusing to start: the operator asked
      for the ML runtime, so its absence must stay loudly visible.
    * runtime failed to load and the flag is false → the same finding at
      info severity: the operator explicitly accepted degraded routing, so
      it stays visible as optional context but does not degrade readiness.
    * router disabled, or the strategy singleton not yet constructed (the
      gateway preloads it in a background task at boot) → no findings.
    """
    if not bool(payload.get("enabled")):
        return []
    if not bool(payload.get("initialized")):
        return []
    require_runtime = bool(payload.get("requireRouterRuntime"))
    strategy = str(payload.get("strategy") or "unavailable")
    code = str(payload.get("code") or ROUTER_RUNTIME_UNAVAILABLE)
    evidence = {
        "requireRouterRuntime": require_runtime,
        "strategy": strategy,
        "loaded": bool(payload.get("loaded")),
        "runtimeErrorKind": None if payload.get("loaded") else code,
        "error": payload.get("error"),
    }
    if bool(payload.get("loaded")):
        return [
            HealthFinding(
                id="squilla_router.runtime.ready",
                severity="ok",
                surface="squilla_router",
                title="Router ML runtime loaded",
                detail="The local ML router runtime is loaded and classifying turns.",
                evidence=evidence,
            )
        ]

    if strategy == "heuristic":
        degradation = (
            "Turns are tiered by the built-in heuristic fallback "
            '(routing source "heuristic") until the ML runtime loads.'
        )
    else:
        degradation = "Every turn is routed to the default tier until the ML runtime loads."
    _, _, fix_steps = _router_runtime_missing_guidance(
        {"error": payload.get("error"), "runtimeErrorKind": code}
    )
    severity: HealthSeverity = "warn" if require_runtime else "info"
    return [
        HealthFinding(
            id="squilla_router.runtime.unavailable",
            severity=severity,
            surface="squilla_router",
            title="Router ML runtime is unavailable",
            detail=(
                f"The local ML router runtime failed to load ({code}). "
                f"{degradation} {router_runtime_hint(code)}"
            ),
            evidence=evidence,
            fix_steps=fix_steps,
            restart_required=True,
        )
    ]
