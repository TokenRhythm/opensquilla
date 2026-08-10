#!/usr/bin/env python3
"""Fail-closed, read-only OpenRouter endpoint preflight for formal DRACO runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from opensquilla.eval.draco_experiment_config import (
    DracoExperimentConfigBundle,
    load_draco_experiment_config,
    validate_formal_draco_credential_bindings,
)
from opensquilla.provider.compat_policy import (
    compat_policy_for_kind,
    model_matches_policy_prefix,
)

API_ORIGIN = "https://openrouter.ai"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXPERIMENT_CONFIG_PATH = ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
ROUTE_PREFLIGHT_SCHEMA = "opensquilla.openrouter-route-preflight/v3"
FORMAL_GROUP_ORDER = ("B0", "B1", "B2", "B4", "G1")
B0_MODEL = "anthropic/claude-fable-5"
B4_MODEL = "openai/gpt-5.6-sol"
FIXED_GROUP_ROUTES = {
    "B0": {B0_MODEL: "amazonbedrock"},
    "B4": {B4_MODEL: "azure"},
}
B2_EXPECTED_ROUTES = {
    "deepseek/deepseek-v4-pro": "deepseek",
    "z-ai/glm-5.2": "z-ai",
    "moonshotai/kimi-k2.7-code": "moonshotai",
    "qwen/qwen3.7-max": "alibaba",
    "google/gemini-3.1-pro-preview": "google-ai-studio",
}


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def formal_registry_snapshot(contract: Any) -> dict[str, Any]:
    from opensquilla.provider.ranking_router import (
        _legacy_registry_snapshot_projection,
        load_model_registry_snapshot,
    )

    raw_snapshot = load_model_registry_snapshot(
        base_version=contract.source_registry_snapshot_version
    )
    candidates = (raw_snapshot, _legacy_registry_snapshot_projection(raw_snapshot))
    version_matches: list[dict[str, Any]] = []
    for snapshot in candidates:
        if str(snapshot.get("snapshot_version") or "") != contract.source_registry_snapshot_version:
            continue
        version_matches.append(snapshot)
        if canonical_sha256(snapshot) == contract.expected_source_registry_snapshot_sha256:
            return snapshot
    if not version_matches:
        raise ValueError("formal route preflight registry version differs")
    raise ValueError("formal route preflight registry hash differs")


def resolved_g1_contract(
    experiment_config: Path,
    *,
    inline_overlay_json: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    config = load_draco_experiment_config(
        experiment_config,
        inline_overlay_json=inline_overlay_json,
    ).config
    validate_formal_draco_credential_bindings(config)
    contract = config.g1_routing
    if contract is None:
        raise ValueError("formal route preflight requires g1_routing.expected_routes")
    if contract.candidate_scope == "exact_routes":
        assert contract.expected_routes is not None
        assert contract.expected_candidate_count is not None
        assert contract.expected_routes_sha256 is not None
        routes = dict(contract.expected_routes)
        policy = "exact_openrouter_routes"
    else:
        snapshot = formal_registry_snapshot(contract)
        rows = snapshot.get("models")
        if not isinstance(rows, list):
            raise ValueError("formal route preflight registry is malformed")
        models: set[str] = set()
        for row in rows:
            facts = row.get("registry_facts") if isinstance(row, dict) else None
            if not isinstance(facts, dict):
                raise ValueError("formal route preflight registry row is malformed")
            if str(facts.get("provider") or "").strip().lower() != "openrouter":
                continue
            model = str(facts.get("model_id") or "").strip().lower()
            if not model or model in models:
                raise ValueError("formal route preflight registry identity is malformed")
            models.add(model)
        routes = {model: "auto" for model in sorted(models)}
        policy = "all_registry_models"
    return config, {
        **contract.model_dump(mode="json", exclude_none=True),
        "candidate_scope": contract.candidate_scope,
        "policy": policy,
        "expected_candidate_count": len(routes),
        "expected_routes": routes,
        "expected_routes_sha256": canonical_sha256(routes),
    }


def formal_expected_routes(experiment_config: Path) -> dict[str, str]:
    _, contract = resolved_g1_contract(experiment_config)
    return dict(contract["expected_routes"])


FORMAL_EXPECTED_ROUTES = formal_expected_routes(DEFAULT_EXPERIMENT_CONFIG_PATH)
_FORMAL_CONFIG = load_draco_experiment_config(DEFAULT_EXPERIMENT_CONFIG_PATH).config
assert _FORMAL_CONFIG.g1_routing is not None
_FORMAL_REGISTRY_SNAPSHOT = formal_registry_snapshot(_FORMAL_CONFIG.g1_routing)
FORMAL_REASONING_INELIGIBLE_MODELS = frozenset(
    str(facts.get("model_id") or "").strip().lower()
    for row in _FORMAL_REGISTRY_SNAPSHOT.get("models") or []
    if isinstance(row, dict)
    and isinstance((facts := row.get("registry_facts")), dict)
    and str(facts.get("provider") or "").strip().lower() == "openrouter"
    and facts.get("supports_reasoning") is not True
)
EXPECTED_PROVIDER_NAMES = {
    "amazonbedrock": "Amazon Bedrock",
    "anthropic": "Anthropic",
    "azure": "Azure",
    "deepseek": "DeepSeek",
    "z-ai": "Z.AI",
    "moonshotai": "Moonshot AI",
    "alibaba": "Alibaba",
    "google-ai-studio": "Google AI Studio",
    "openai": "OpenAI",
    "xai": "xAI",
    "streamlake": "StreamLake",
    "groq": "Groq",
    "minimax": "Minimax",
    "mistral": "Mistral",
    "poolside": "Poolside",
    "tencent": "Tencent",
}
# Match the actual frozen request surface.  B2 proposers do not receive tool
# definitions; only the GLM aggregator can call the local tool surface.  The
# Gemini Judge is also text-only.  Over-requiring tool support on every
# proposer would reject an otherwise valid formal route before the canary.
B2_REQUIRED_PARAMETERS = {model: {"max_tokens", "reasoning"} for model in B2_EXPECTED_ROUTES}
B2_REQUIRED_PARAMETERS["deepseek/deepseek-v4-pro"].add("temperature")
B2_REQUIRED_PARAMETERS["z-ai/glm-5.2"] |= {
    "temperature",
    "tools",
}
B2_REQUIRED_PARAMETERS["qwen/qwen3.7-max"].add("temperature")
B2_REQUIRED_PARAMETERS["google/gemini-3.1-pro-preview"].add("temperature")
OPENROUTER_COMPAT_POLICY = compat_policy_for_kind("openrouter")
FORMAL_UNSUPPORTED_TEMPERATURE_MODELS = frozenset(
    model
    for model in FORMAL_EXPECTED_ROUTES
    if model_matches_policy_prefix(
        model,
        OPENROUTER_COMPAT_POLICY.unsupported_temperature_model_prefixes,
    )
)
FORMAL_MAX_COMPLETION_TOKENS_MODELS = frozenset(
    model
    for model in FORMAL_EXPECTED_ROUTES
    if model_matches_policy_prefix(
        model,
        OPENROUTER_COMPAT_POLICY.max_completion_tokens_model_prefixes,
    )
)


def _formal_role_required_parameters(
    expected_routes: dict[str, str],
    *,
    include_tools: bool,
) -> dict[str, set[str]]:
    required = {
        model: {
            (
                "max_completion_tokens"
                if model_matches_policy_prefix(
                    model,
                    OPENROUTER_COMPAT_POLICY.max_completion_tokens_model_prefixes,
                )
                else "max_tokens"
            ),
        }
        for model in expected_routes
    }
    if include_tools:
        for parameters in required.values():
            parameters.add("tools")
    for model in set(expected_routes) - FORMAL_REASONING_INELIGIBLE_MODELS:
        required[model].add("reasoning")
    for model in expected_routes:
        if not model_matches_policy_prefix(
            model,
            OPENROUTER_COMPAT_POLICY.unsupported_temperature_model_prefixes,
        ):
            required[model].add("temperature")
    return required


def formal_proposer_required_parameters(
    expected_routes: dict[str, str],
) -> dict[str, set[str]]:
    return _formal_role_required_parameters(expected_routes, include_tools=False)


def formal_required_parameters(expected_routes: dict[str, str]) -> dict[str, set[str]]:
    """Return the stricter aggregator request surface kept by the v2 contract."""

    return _formal_role_required_parameters(expected_routes, include_tools=True)


FORMAL_REQUIRED_PARAMETERS = formal_required_parameters(FORMAL_EXPECTED_ROUTES)


def get_json(
    client: httpx.Client,
    path: str,
    *,
    allow_model_not_found: bool = False,
) -> tuple[Any | None, str, int, str]:
    url = f"{API_ORIGIN}{path}"
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = client.get(url)
            if response.is_redirect:
                raise RuntimeError(f"redirect refused for {path}")
            if response.status_code == 404 and allow_model_not_found:
                return None, hashlib.sha256(response.content).hexdigest(), 404, "raw_body"
            response.raise_for_status()
            payload = response.json()
            return payload, canonical_sha256(payload), response.status_code, "canonical_json"
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(float(attempt))
    raise RuntimeError(f"OpenRouter metadata request failed for {path}: {last_error}")


def tag_matches(tag: str, expected: str) -> bool:
    return tag == expected or tag.startswith(f"{expected}/")


def parse_formal_groups(value: str) -> tuple[str, ...]:
    groups = tuple(value.split(","))
    if not groups:
        raise ValueError("formal route preflight requires at least one group")
    indexes: list[int] = []
    for group in groups:
        try:
            indexes.append(FORMAL_GROUP_ORDER.index(group))
        except ValueError as exc:
            raise ValueError(
                "formal route preflight groups must be a canonical subset of "
                + ",".join(FORMAL_GROUP_ORDER)
            ) from exc
    if indexes != sorted(set(indexes)):
        raise ValueError("formal route preflight groups must be non-duplicated and canonical")
    return groups


def resolved_task_analyzer_policy(
    ranking_resolution: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive the analyzer route only from the authenticated effective policy."""

    from opensquilla.provider.ranking_router import task_analyzer_policy

    effective = ranking_resolution.get("effective_config")
    if not isinstance(effective, Mapping):
        raise ValueError("formal route preflight lacks a frozen effective ranking config")
    policy = task_analyzer_policy(effective)
    if policy.get("provider") != "openrouter":
        raise ValueError("formal route preflight task analyzer must use OpenRouter")
    if policy.get("upstream_provider") == "auto":
        raise ValueError(
            "formal route preflight task analyzer upstream provider must be explicitly pinned"
        )
    return dict(policy)


def required_fixed_route_specs(
    *,
    experiment: Any,
    groups: tuple[str, ...],
    proposer_required_parameters: dict[str, set[str]],
    ranking_resolution: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, set[str]]]:
    routes: dict[str, str] = {}
    parameters: dict[str, set[str]] = {}

    def add(model: str, provider: str, required: set[str]) -> None:
        existing = routes.get(model)
        if existing is not None and existing != provider:
            raise ValueError(f"conflicting fixed providers for {model}: {existing}, {provider}")
        routes[model] = provider
        parameters.setdefault(model, set()).update(required)

    for group in groups:
        for model, provider in FIXED_GROUP_ROUTES.get(group, {}).items():
            if model not in proposer_required_parameters:
                raise ValueError(f"fixed route is absent from the frozen registry: {model}")
            add(model, provider, proposer_required_parameters[model])
    if "B2" in groups:
        for model, provider in B2_EXPECTED_ROUTES.items():
            add(model, provider, set(B2_REQUIRED_PARAMETERS[model]))
    if "G1" in groups:
        judge_model = str(experiment.judge.model).strip().lower()
        if judge_model != "google/gemini-3.1-pro-preview":
            raise ValueError(
                "formal route preflight currently requires the frozen Gemini Judge model"
            )
        analyzer_policy = resolved_task_analyzer_policy(
            ranking_resolution or effective_ranking_resolution(experiment)
        )
        for model, provider in {
            str(analyzer_policy["model"]): str(analyzer_policy["upstream_provider"]),
            judge_model: "google-ai-studio",
        }.items():
            if model not in proposer_required_parameters:
                raise ValueError(f"G1 fixed route is absent from the frozen registry: {model}")
            add(model, provider, proposer_required_parameters[model])
    return dict(sorted(routes.items())), {model: parameters[model] for model in sorted(parameters)}


def effective_ranking_resolution(experiment: Any) -> dict[str, Any]:
    """Resolve the immutable ranking policy used by the runner and finalizer."""

    from opensquilla.provider.ranking_router import ranking_config_resolution

    contract = experiment.g1_routing
    resolution = ranking_config_resolution(
        base_version=(
            contract.expected_ranking_config_version
            if contract is not None
            else None
        ),
        override=(experiment.router_dynamic_ranking_override or None),
    )
    if contract is None:
        return resolution
    base = resolution.get("base_config")
    if not isinstance(base, Mapping):
        raise ValueError("formal route preflight lacks a frozen baseline ranking config")
    if (
        base.get("schema_version") != contract.expected_ranking_config_schema_version
        or base.get("config_version") != contract.expected_ranking_config_version
        or resolution.get("base_sha256") != contract.expected_ranking_config_sha256
    ):
        raise ValueError(
            "formal route preflight baseline ranking config differs from the G1 contract"
        )
    if _effective_proposer_max(base) != contract.expected_proposer_count_max:
        raise ValueError(
            "formal route preflight baseline proposer maximum differs from the G1 contract"
        )
    return resolution


def _effective_proposer_max(ranking_config: Mapping[str, Any]) -> int:
    proposer_count = ranking_config.get("proposer_count")
    by_tier = proposer_count.get("by_tier") if isinstance(proposer_count, Mapping) else None
    high_risk = proposer_count.get("high_risk") if isinstance(proposer_count, Mapping) else None
    try:
        return max(
            *(int(row["max"]) for row in by_tier.values() if isinstance(row, Mapping)),
            int(high_risk["max"]),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("frozen ranking proposer bounds are malformed") from exc


def _effective_backup_count(ranking_config: Mapping[str, Any]) -> int:
    proposer_count = ranking_config.get("proposer_count")
    raw = proposer_count.get("backup_count") if isinstance(proposer_count, Mapping) else None
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 2:
        raise ValueError("frozen ranking proposer backup_count must be between 0 and 2")
    return raw


def required_role_capacity(
    experiment: Any,
    groups: tuple[str, ...],
    *,
    ranking_resolution: Mapping[str, Any] | None = None,
) -> tuple[int, int]:
    if "G1" not in groups:
        return 0, 0
    g1 = experiment.g1_routing
    if g1 is None:
        raise ValueError("formal route preflight requires a G1 routing contract")
    resolution = (
        dict(ranking_resolution)
        if ranking_resolution is not None
        else effective_ranking_resolution(experiment)
    )
    ranking_config = resolution.get("effective_config")
    if not isinstance(ranking_config, Mapping):
        raise ValueError("formal route preflight lacks a frozen effective ranking config")
    proposer_max = _effective_proposer_max(ranking_config)
    backup_count = _effective_backup_count(ranking_config)
    proposer_count = (
        proposer_max
        + backup_count
        + int(experiment.ensemble.aggregator_recovery_top_k)
    )
    return proposer_count, int(experiment.ensemble.aggregator_recovery_top_k)


def recompute_model_endpoint_availability(
    *,
    model: str,
    expected_provider: str,
    proposer_required_parameters: set[str],
    aggregator_required_parameters: set[str],
    evidence: dict[str, Any],
) -> tuple[int, int, int, str]:
    """Recompute v3 role-aware availability, including explicit 0/0 rows."""

    if evidence.get("expected_provider") != expected_provider:
        raise ValueError(f"endpoint evidence provider differs for {model}")
    if evidence.get("requested_model_id") != model:
        raise ValueError(f"endpoint requested model differs for {model}")
    fetch_outcome = evidence.get("endpoint_fetch_outcome")
    response_status = evidence.get("endpoint_http_status")
    response_hash_kind = evidence.get("response_sha256_kind")
    if fetch_outcome == "ok":
        if response_status != 200 or response_hash_kind != "canonical_json":
            raise ValueError(f"endpoint fetch evidence differs for {model}")
        if evidence.get("response_model_id") != model:
            raise ValueError(f"endpoint response model differs for {model}")
    elif fetch_outcome == "model_not_found":
        if (
            response_status != 404
            or response_hash_kind != "raw_body"
            or evidence.get("response_model_id") is not None
        ):
            raise ValueError(f"endpoint not-found evidence differs for {model}")
    else:
        raise ValueError(f"endpoint fetch outcome is invalid for {model}")
    endpoints = evidence.get("matching_endpoints")
    if not isinstance(endpoints, list):
        raise ValueError(f"endpoint evidence is invalid for {model}")
    provider_is_auto = expected_provider == "auto"
    expected_provider_name = EXPECTED_PROVIDER_NAMES.get(expected_provider)
    if not provider_is_auto and not expected_provider_name:
        raise ValueError(f"provider display-name contract is missing for {expected_provider}")

    operational_count = 0
    proposer_compatible_count = 0
    aggregator_compatible_count = 0
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            raise ValueError(f"endpoint evidence row is invalid for {model}")
        if not provider_is_auto and not tag_matches(
            str(endpoint.get("tag") or ""), expected_provider
        ):
            raise ValueError(f"endpoint tag differs for {model} -> {expected_provider}")
        status = endpoint.get("status")
        if not (isinstance(status, int) and not isinstance(status, bool) and status == 0):
            continue
        operational_count += 1
        supported = endpoint.get("supported_parameters")
        if not isinstance(supported, list) or any(
            not isinstance(item, str) or not item for item in supported
        ):
            raise ValueError(f"endpoint parameters are invalid for {model}")
        supported_parameters = set(supported)
        identity_matches = (
            provider_is_auto or endpoint.get("provider_name") == expected_provider_name
        ) and endpoint.get("model_id") == model
        if identity_matches and proposer_required_parameters <= supported_parameters:
            proposer_compatible_count += 1
        if identity_matches and aggregator_required_parameters <= supported_parameters:
            aggregator_compatible_count += 1

    if fetch_outcome == "model_not_found":
        if endpoints:
            raise ValueError(f"endpoint not-found evidence contains routes for {model}")
        availability_status = "model_endpoint_not_found"
    elif not endpoints:
        availability_status = "no_matching_endpoint"
    elif operational_count == 0:
        availability_status = "no_operational_endpoint"
    elif proposer_compatible_count == 0:
        availability_status = "no_compatible_request_surface"
    elif aggregator_compatible_count == 0:
        availability_status = "proposer_only"
    else:
        availability_status = "compatible"
    expected_values = {
        "operational_match_count": operational_count,
        "compatible_operational_match_count": aggregator_compatible_count,
        "proposer_compatible_operational_match_count": proposer_compatible_count,
        "aggregator_compatible_operational_match_count": aggregator_compatible_count,
        "availability_status": availability_status,
        "proposer_required_parameters": sorted(proposer_required_parameters),
        "aggregator_required_parameters": sorted(aggregator_required_parameters),
    }
    for field, expected in expected_values.items():
        if evidence.get(field) != expected:
            raise ValueError(f"saved {field} differs for {model}")
    return (
        operational_count,
        proposer_compatible_count,
        aggregator_compatible_count,
        availability_status,
    )


def recompute_model_endpoint_compatibility(
    *,
    model: str,
    expected_provider: str,
    required_parameters: set[str],
    evidence: dict[str, Any],
) -> tuple[int, int]:
    """Recompute route compatibility from the endpoint rows being persisted."""

    if evidence.get("expected_provider") != expected_provider:
        raise ValueError(f"endpoint evidence provider differs for {model}")
    if evidence.get("response_model_id") != model:
        raise ValueError(f"endpoint response model differs for {model}")
    endpoints = evidence.get("matching_endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        raise ValueError(f"endpoint evidence is missing for {model}")
    provider_is_auto = expected_provider == "auto"
    expected_provider_name = EXPECTED_PROVIDER_NAMES.get(expected_provider)
    if not provider_is_auto and not expected_provider_name:
        raise ValueError(f"provider display-name contract is missing for {expected_provider}")

    operational_count = 0
    compatible_count = 0
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            raise ValueError(f"endpoint evidence row is invalid for {model}")
        if not provider_is_auto and not tag_matches(
            str(endpoint.get("tag") or ""),
            expected_provider,
        ):
            raise ValueError(f"endpoint tag differs for {model} -> {expected_provider}")
        status = endpoint.get("status")
        operational = isinstance(status, int) and not isinstance(status, bool) and status == 0
        if not operational:
            continue
        operational_count += 1
        supported = endpoint.get("supported_parameters")
        if not isinstance(supported, list) or any(
            not isinstance(item, str) or not item for item in supported
        ):
            raise ValueError(f"endpoint parameters are invalid for {model}")
        supported_parameters = (
            {str(item) for item in supported} if isinstance(supported, list) else set()
        )
        if (
            (provider_is_auto or endpoint.get("provider_name") == expected_provider_name)
            and endpoint.get("model_id") == model
            and required_parameters <= supported_parameters
        ):
            compatible_count += 1

    if operational_count <= 0:
        raise ValueError(f"no operational endpoint remains for {model}")
    if compatible_count <= 0:
        raise ValueError(f"no saved endpoint supports the frozen request surface for {model}")
    if evidence.get("operational_match_count") != operational_count:
        raise ValueError(f"saved operational endpoint count differs for {model}")
    if evidence.get("compatible_operational_match_count") != compatible_count:
        raise ValueError(f"saved compatible endpoint count differs for {model}")
    if evidence.get("required_parameters") != sorted(required_parameters):
        raise ValueError(f"saved required parameters differ for {model}")
    return operational_count, compatible_count


def atomic_write_json(path: Path, payload: dict[str, Any]) -> tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_raw)
    identity: tuple[int, int] | None = None
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_stat = os.stat(temporary, follow_symlinks=False)
        identity = (temporary_stat.st_dev, temporary_stat.st_ino)
        os.link(temporary, path, follow_symlinks=False)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    if identity is None:
        raise RuntimeError("atomic JSON writer did not capture its temporary file identity")
    return identity


def atomic_write_json_bundle(entries: list[tuple[Path, dict[str, Any]]]) -> None:
    published: list[tuple[Path, tuple[int, int]]] = []
    try:
        for path, payload in entries:
            identity = atomic_write_json(path, payload)
            published.append((path, identity))
    except BaseException:
        for path, identity in reversed(published):
            try:
                current = os.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (current.st_dev, current.st_ino) == identity:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
        raise


def effective_experiment_config_path(output: Path) -> Path:
    return output.with_suffix(".experiment-config.effective.json")


def experiment_config_evidence(
    bundle: DracoExperimentConfigBundle,
    *,
    effective_path: Path,
) -> dict[str, Any]:
    effective_config = bundle.config.model_dump(mode="json")
    provenance = bundle.provenance()
    base_provenance = provenance["base"]
    inline_overlay = provenance["inline_overlay"]
    inline_overrides = provenance["inline_overrides"]
    return {
        "path": str(base_provenance["path"]),
        "sha256": str(base_provenance["sha256"]),
        "provenance": provenance,
        "inline_overlay_present": bool(inline_overlay["present"]),
        "inline_overlay_field_paths": list(inline_overlay["field_paths"]),
        "inline_override_count": int(inline_overrides["count"]),
        "inline_override_paths": list(inline_overrides["paths"]),
        "effective_config": {
            "path": str(effective_path.expanduser().resolve()),
            "sha256": canonical_sha256(effective_config),
        },
        "g1_routing_profile_id": (
            bundle.config.g1_routing.profile_id
            if bundle.config.g1_routing is not None
            else None
        ),
        "source_registry_snapshot_version": (
            bundle.config.g1_routing.source_registry_snapshot_version
            if bundle.config.g1_routing is not None
            else None
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--scope", choices=("b2", "formal"), default="formal")
    parser.add_argument("--groups", default=",".join(FORMAL_GROUP_ORDER))
    parser.add_argument(
        "--experiment-config",
        type=Path,
        default=DEFAULT_EXPERIMENT_CONFIG_PATH,
    )
    parser.add_argument(
        "--experiment-config-override-json",
        help="Sparse experiment JSON merged after the base config before preflight.",
    )
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"refusing to overwrite route preflight evidence: {args.output}")
    effective_path = effective_experiment_config_path(args.output)
    if effective_path.exists():
        parser.error(
            "refusing to overwrite route preflight effective config: "
            f"{effective_path}"
        )
    try:
        args.groups = parse_formal_groups(args.groups)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def saved_endpoint_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "tag": row.get("tag"),
            "provider_name": row.get("provider_name"),
            "model_id": row.get("model_id"),
            "status": row.get("status"),
            "supported_parameters": sorted(
                str(item) for item in (row.get("supported_parameters") or [])
            ),
            "pricing": row.get("pricing"),
            "max_completion_tokens": row.get("max_completion_tokens"),
        }
        for row in rows
    ]


def main() -> int:
    args = parse_args()

    experiment_bundle = load_draco_experiment_config(
        args.experiment_config,
        inline_overlay_json=args.experiment_config_override_json,
    )
    experiment = experiment_bundle.config
    validate_formal_draco_credential_bindings(experiment)
    effective_config_path = effective_experiment_config_path(args.output)
    resolved_contract = (
        resolved_g1_contract(
            args.experiment_config,
            inline_overlay_json=args.experiment_config_override_json,
        )[1]
        if args.scope == "formal"
        else None
    )
    ranking_resolution = effective_ranking_resolution(experiment)
    expected_routes = (
        dict(resolved_contract["expected_routes"])
        if resolved_contract is not None
        else B2_EXPECTED_ROUTES
    )
    if args.scope == "formal" and experiment.g1_routing is None:
        raise SystemExit("formal route preflight requires g1_routing.expected_routes")
    candidate_scope = (
        str(resolved_contract["candidate_scope"])
        if resolved_contract is not None
        else "exact_routes"
    )
    candidate_policy = (
        str(resolved_contract["policy"])
        if resolved_contract is not None
        else "exact_openrouter_routes"
    )
    aggregator_required_parameters = (
        formal_required_parameters(expected_routes)
        if args.scope == "formal"
        else B2_REQUIRED_PARAMETERS
    )
    proposer_required_parameters = (
        formal_proposer_required_parameters(expected_routes)
        if args.scope == "formal"
        else aggregator_required_parameters
    )
    fixed_routes: dict[str, str] = {}
    fixed_parameters: dict[str, set[str]] = {}
    analyzer_policy: dict[str, Any] | None = None
    if args.scope == "formal":
        fixed_routes, fixed_parameters = required_fixed_route_specs(
            experiment=experiment,
            groups=args.groups,
            proposer_required_parameters=proposer_required_parameters,
            ranking_resolution=ranking_resolution,
        )
        if "G1" in args.groups:
            analyzer_policy = resolved_task_analyzer_policy(ranking_resolution)

    with httpx.Client(
        timeout=httpx.Timeout(20.0),
        trust_env=False,
        follow_redirects=False,
        headers={"Accept": "application/json"},
    ) as client:
        providers_payload, providers_sha256, providers_status, providers_hash_kind = get_json(
            client, "/api/v1/providers"
        )
        if providers_status != 200 or providers_hash_kind != "canonical_json":
            raise SystemExit("OpenRouter providers response status is invalid")
        provider_rows = (
            providers_payload.get("data") if isinstance(providers_payload, dict) else None
        )
        if not isinstance(provider_rows, list):
            raise SystemExit("OpenRouter providers response has an invalid schema")
        provider_slugs = {
            str(row.get("slug"))
            for row in provider_rows
            if isinstance(row, dict) and row.get("slug")
        }
        required_provider_slugs = set(expected_routes.values()) | set(fixed_routes.values())
        missing_slugs = sorted(required_provider_slugs - {"auto"} - provider_slugs)
        if missing_slugs:
            raise SystemExit(f"OpenRouter provider slug(s) unavailable: {missing_slugs}")

        model_evidence: dict[str, Any] = {}
        endpoint_payloads: dict[str, tuple[list[dict[str, Any]], str, str | None, int, str]] = {}
        for model, expected_provider in expected_routes.items():
            encoded_model = "/".join(quote(part, safe="") for part in model.split("/"))
            allow_model_not_found = (
                args.scope == "formal"
                and candidate_scope == "registry_all"
                and model not in fixed_routes
            )
            payload, response_sha256, response_status, response_hash_kind = get_json(
                client,
                f"/api/v1/models/{encoded_model}/endpoints",
                allow_model_not_found=allow_model_not_found,
            )
            if response_status == 404:
                data = None
                endpoints: list[dict[str, Any]] = []
                response_model_id = None
                fetch_outcome = "model_not_found"
            else:
                data = payload.get("data") if isinstance(payload, dict) else None
                raw_endpoints = data.get("endpoints") if isinstance(data, dict) else None
                if not isinstance(raw_endpoints, list):
                    raise SystemExit(f"OpenRouter endpoint schema invalid for {model}")
                endpoints = raw_endpoints
                if data.get("id") != model:
                    raise SystemExit(f"OpenRouter endpoint response model differs for {model}")
                response_model_id = str(data.get("id"))
                fetch_outcome = "ok"
            endpoint_payloads[model] = (
                endpoints,
                response_sha256,
                response_model_id,
                response_status,
                response_hash_kind,
            )
            provider_is_auto = expected_provider == "auto"
            matches = [
                row
                for row in endpoints
                if isinstance(row, dict)
                and (provider_is_auto or tag_matches(str(row.get("tag") or ""), expected_provider))
            ]
            operational = [row for row in matches if row.get("status") == 0]
            proposer_compatible = [
                row
                for row in operational
                if proposer_required_parameters[model]
                <= {str(item) for item in (row.get("supported_parameters") or [])}
                and (
                    provider_is_auto
                    or row.get("provider_name") == EXPECTED_PROVIDER_NAMES[expected_provider]
                )
                and row.get("model_id") == model
            ]
            aggregator_compatible = [
                row
                for row in operational
                if aggregator_required_parameters[model]
                <= {str(item) for item in (row.get("supported_parameters") or [])}
                and (
                    provider_is_auto
                    or row.get("provider_name") == EXPECTED_PROVIDER_NAMES[expected_provider]
                )
                and row.get("model_id") == model
            ]
            saved_evidence = {
                "expected_provider": expected_provider,
                "requested_model_id": model,
                "endpoint_fetch_outcome": fetch_outcome,
                "endpoint_http_status": response_status,
                "response_sha256_kind": response_hash_kind,
                "response_model_id": response_model_id,
                "response_sha256": response_sha256,
                "matching_endpoints": saved_endpoint_rows(matches),
                "operational_match_count": len(operational),
                "compatible_operational_match_count": len(aggregator_compatible),
                "required_parameters": sorted(aggregator_required_parameters[model]),
                "proposer_compatible_operational_match_count": len(proposer_compatible),
                "aggregator_compatible_operational_match_count": len(aggregator_compatible),
                "proposer_required_parameters": sorted(proposer_required_parameters[model]),
                "aggregator_required_parameters": sorted(aggregator_required_parameters[model]),
            }
            if fetch_outcome == "model_not_found":
                availability_status = "model_endpoint_not_found"
            elif not matches:
                availability_status = "no_matching_endpoint"
            elif not operational:
                availability_status = "no_operational_endpoint"
            elif not proposer_compatible:
                availability_status = "no_compatible_request_surface"
            elif not aggregator_compatible:
                availability_status = "proposer_only"
            else:
                availability_status = "compatible"
            saved_evidence["availability_status"] = availability_status
            if args.scope != "formal" or candidate_scope == "exact_routes":
                recompute_model_endpoint_compatibility(
                    model=model,
                    expected_provider=expected_provider,
                    required_parameters=aggregator_required_parameters[model],
                    evidence=saved_evidence,
                )
            else:
                recompute_model_endpoint_availability(
                    model=model,
                    expected_provider=expected_provider,
                    proposer_required_parameters=proposer_required_parameters[model],
                    aggregator_required_parameters=aggregator_required_parameters[model],
                    evidence=saved_evidence,
                )
            model_evidence[model] = saved_evidence

        fixed_route_checks: dict[str, Any] = {}
        for model, expected_provider in fixed_routes.items():
            (
                endpoints,
                response_sha256,
                response_model_id,
                response_status,
                response_hash_kind,
            ) = endpoint_payloads[model]
            matches = [
                row
                for row in endpoints
                if isinstance(row, dict)
                and tag_matches(str(row.get("tag") or ""), expected_provider)
            ]
            operational = [row for row in matches if row.get("status") == 0]
            compatible = [
                row
                for row in operational
                if fixed_parameters[model]
                <= {str(item) for item in (row.get("supported_parameters") or [])}
                and row.get("provider_name") == EXPECTED_PROVIDER_NAMES[expected_provider]
                and row.get("model_id") == model
            ]
            fixed_evidence = {
                "expected_provider": expected_provider,
                "requested_model_id": model,
                "endpoint_fetch_outcome": "ok",
                "endpoint_http_status": response_status,
                "response_sha256_kind": response_hash_kind,
                "response_model_id": response_model_id,
                "response_sha256": response_sha256,
                "matching_endpoints": saved_endpoint_rows(matches),
                "operational_match_count": len(operational),
                "compatible_operational_match_count": len(compatible),
                "required_parameters": sorted(fixed_parameters[model]),
            }
            recompute_model_endpoint_compatibility(
                model=model,
                expected_provider=expected_provider,
                required_parameters=fixed_parameters[model],
                evidence=fixed_evidence,
            )
            fixed_route_checks[model] = fixed_evidence

    proposer_compatible_models = sorted(
        model
        for model, row in model_evidence.items()
        if int(row["proposer_compatible_operational_match_count"]) > 0
    )
    aggregator_compatible_models = sorted(
        model
        for model, row in model_evidence.items()
        if int(row["aggregator_compatible_operational_match_count"]) > 0
    )
    unavailable_models = sorted(set(expected_routes) - set(proposer_compatible_models))
    aggregator_ineligible_models = sorted(set(expected_routes) - set(aggregator_compatible_models))
    required_proposer_count, required_aggregator_count = required_role_capacity(
        experiment,
        args.groups,
        ranking_resolution=ranking_resolution,
    )
    if candidate_scope == "exact_routes":
        required_proposer_count = len(expected_routes)
        required_aggregator_count = len(expected_routes)
    candidate_capacity_pass = (
        len(proposer_compatible_models) >= required_proposer_count
        and len(aggregator_compatible_models) >= required_aggregator_count
    )
    schema = (
        ROUTE_PREFLIGHT_SCHEMA
        if args.scope == "formal"
        else ("opensquilla.openrouter-route-preflight/v2")
    )
    evidence: dict[str, Any] = {
        "schema": schema,
        "captured_at": datetime.now(UTC).isoformat(),
        "api_origin": API_ORIGIN,
        "scope": args.scope,
        "trust_env": False,
        "providers_response_sha256": providers_sha256,
        "candidate_scope": candidate_scope,
        "candidate_policy": candidate_policy,
        "expected_routes": expected_routes,
        "expected_routes_sha256": canonical_sha256(expected_routes),
        "experiment_config": experiment_config_evidence(
            experiment_bundle,
            effective_path=effective_config_path,
        ),
        "ranking_config_resolution": ranking_resolution,
        "required_parameters_sha256": canonical_sha256(
            {
                model: sorted(parameters)
                for model, parameters in aggregator_required_parameters.items()
            }
        ),
        "models": model_evidence,
        "route_metadata_pass": candidate_capacity_pass,
        "non_byok_verified": None,
        "billing_verified": None,
        "reasoning_ineligible_models": (
            sorted(FORMAL_REASONING_INELIGIBLE_MODELS) if args.scope == "formal" else []
        ),
        "scope_note": (
            "Public metadata availability only; per-request router metadata, non-BYOK "
            "usage evidence, canary, and account reconciliation remain mandatory."
        ),
    }
    if args.scope == "formal":
        fixed_contract = {
            model: {
                "provider": fixed_routes[model],
                "required_parameters": sorted(fixed_parameters[model]),
            }
            for model in fixed_routes
        }
        evidence.update(
            {
                "task_analyzer": analyzer_policy,
                "groups": list(args.groups),
                "availability_policy": (
                    "registry_capacity"
                    if candidate_scope == "registry_all"
                    else "strict_all_routes"
                ),
                "availability_status": (
                    "complete"
                    if not unavailable_models and not aggregator_ineligible_models
                    else "degraded"
                ),
                "proposer_required_parameters_sha256": canonical_sha256(
                    {
                        model: sorted(parameters)
                        for model, parameters in proposer_required_parameters.items()
                    }
                ),
                "proposer_compatible_candidate_count": len(proposer_compatible_models),
                "aggregator_compatible_candidate_count": len(aggregator_compatible_models),
                "proposer_compatible_models": proposer_compatible_models,
                "aggregator_compatible_models": aggregator_compatible_models,
                "unavailable_models": unavailable_models,
                "aggregator_ineligible_models": aggregator_ineligible_models,
                "required_proposer_compatible_candidate_count": (required_proposer_count),
                "required_aggregator_compatible_candidate_count": (required_aggregator_count),
                "candidate_capacity_pass": candidate_capacity_pass,
                "required_fixed_routes": fixed_contract,
                "required_fixed_routes_sha256": canonical_sha256(fixed_contract),
                "fixed_route_checks": fixed_route_checks,
                "fixed_routes_pass": True,
            }
        )
    atomic_write_json_bundle(
        [
            (effective_config_path, experiment.model_dump(mode="json")),
            (args.output, evidence),
        ]
    )
    if not candidate_capacity_pass:
        raise SystemExit(
            "OpenRouter dynamic route capacity is insufficient: "
            f"proposer={len(proposer_compatible_models)}/{required_proposer_count}, "
            f"aggregator={len(aggregator_compatible_models)}/{required_aggregator_count}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
