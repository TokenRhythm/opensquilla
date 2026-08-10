#!/usr/bin/env python3
"""Snapshot and finalize authoritative DRACO B2 experiment artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opensquilla.eval.draco_experiment_config import load_draco_experiment_config
from opensquilla.provider.compat_policy import (
    compat_policy_for_kind,
    model_matches_policy_prefix,
)

SNAPSHOT_SCHEMA = "opensquilla.draco-b2-artifact-snapshot/v1"
SUCCESS_SCHEMA = "opensquilla.draco-b2-formal-success/v1"
ROUTE_PREFLIGHT_V1_SCHEMA = "opensquilla.openrouter-route-preflight/v1"
ROUTE_PREFLIGHT_V2_SCHEMA = "opensquilla.openrouter-route-preflight/v2"
ROUTE_PREFLIGHT_V3_SCHEMA = "opensquilla.openrouter-route-preflight/v3"
ROUTE_PREFLIGHT_SCHEMAS = frozenset(
    {ROUTE_PREFLIGHT_V1_SCHEMA, ROUTE_PREFLIGHT_V2_SCHEMA, ROUTE_PREFLIGHT_V3_SCHEMA}
)
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXPERIMENT_CONFIG_PATH = ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
FORMAL_GROUP_ORDER = ("B0", "B1", "B2", "B4", "G1")
B0_MODEL = "anthropic/claude-fable-5"
B4_MODEL = "openai/gpt-5.6-sol"
B2_EXPECTED_ROUTES = {
    "deepseek/deepseek-v4-pro": "deepseek",
    "z-ai/glm-5.2": "z-ai",
    "moonshotai/kimi-k2.7-code": "moonshotai",
    "qwen/qwen3.7-max": "alibaba",
    "google/gemini-3.1-pro-preview": "google-ai-studio",
}
FIXED_GROUP_ROUTES = {
    "B0": {B0_MODEL: "amazonbedrock"},
    "B4": {B4_MODEL: "azure"},
}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
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
OPENROUTER_COMPAT_POLICY = compat_policy_for_kind("openrouter")
B2_REQUIRED_PARAMETERS = {model: {"max_tokens", "reasoning"} for model in B2_EXPECTED_ROUTES}
B2_REQUIRED_PARAMETERS["deepseek/deepseek-v4-pro"].add("temperature")
B2_REQUIRED_PARAMETERS["z-ai/glm-5.2"] |= {"temperature", "tools"}
B2_REQUIRED_PARAMETERS["qwen/qwen3.7-max"].add("temperature")
B2_REQUIRED_PARAMETERS["google/gemini-3.1-pro-preview"].add("temperature")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def relative_file_record(root: Path, path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"artifact is not a regular non-symlink file: {path}")
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"artifact escapes snapshot root: {path}") from exc
    stat = resolved.stat()
    permissions = stat.st_mode & 0o777
    if permissions & 0o077:
        raise ValueError(f"artifact permissions expose benchmark data outside the owner: {path}")
    return {
        "path": relative.as_posix(),
        "sha256": file_sha256(resolved),
        "size_bytes": stat.st_size,
        "mode": oct(permissions),
    }


def load_snapshot(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError(f"invalid artifact snapshot: {path}")
    return value


def safe_relative_path(value: Any, *, label: str) -> Path:
    relative = Path(str(value or ""))
    if not str(relative) or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} is not a safe relative path: {value!r}")
    return relative


def recursive_artifact_paths(root: Path, *, excluded: set[Path]) -> list[Path]:
    artifacts: list[Path] = []
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise ValueError(f"artifact tree contains a symlink: {candidate}")
        if candidate.is_file() and candidate.resolve(strict=True) not in excluded:
            artifacts.append(candidate.resolve(strict=True))
    return artifacts


def verify_snapshot(path: Path) -> dict[str, Any]:
    snapshot = load_snapshot(path)
    root_reference = safe_relative_path(snapshot.get("root"), label="snapshot root")
    root = (path.resolve(strict=True).parent / root_reference).resolve(strict=True)
    if root != path.resolve(strict=True).parent:
        raise ValueError(f"snapshot root must be its containing directory: {path}")
    records = snapshot.get("artifacts")
    if not isinstance(records, list) or not records:
        raise ValueError(f"artifact snapshot has no files: {path}")
    recorded_paths: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"artifact snapshot contains a non-object record: {path}")
        relative = safe_relative_path(record.get("path"), label="artifact path")
        relative_key = relative.as_posix()
        if relative_key in recorded_paths:
            raise ValueError(f"artifact snapshot contains duplicate paths: {path}")
        recorded_paths.add(relative_key)
        artifact = root / relative
        actual = relative_file_record(root, artifact)
        if actual != record:
            raise ValueError(f"artifact changed after audit: {artifact}")
    if snapshot.get("closed_world") is True:
        allowed_values = snapshot.get("allowed_after_snapshot") or []
        if not isinstance(allowed_values, list):
            raise ValueError(f"snapshot allowed-after list is invalid: {path}")
        allowed = {
            safe_relative_path(value, label="allowed-after path").as_posix()
            for value in allowed_values
        }
        snapshot_relative = path.resolve(strict=True).relative_to(root).as_posix()
        excluded = {
            path.resolve(strict=True),
            *{(root / relative).resolve(strict=False) for relative in allowed},
        }
        actual_paths = {
            artifact.relative_to(root).as_posix()
            for artifact in recursive_artifact_paths(root, excluded=excluded)
        }
        if actual_paths != recorded_paths:
            missing = sorted(recorded_paths - actual_paths)
            extra = sorted(actual_paths - recorded_paths)
            raise ValueError(
                f"artifact set changed after audit: missing={missing}, extra={extra}, "
                f"snapshot={snapshot_relative}"
            )
    return snapshot


def _normalized_routes(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{label} expected_routes must be a non-empty object")
    routes: dict[str, str] = {}
    for raw_model, raw_provider in value.items():
        if not isinstance(raw_model, str) or not isinstance(raw_provider, str):
            raise ValueError(f"{label} expected_routes must contain string pairs")
        model = raw_model.strip().lower()
        provider = raw_provider.strip().lower()
        if model != raw_model or provider != raw_provider or "/" not in model or not provider:
            raise ValueError(f"{label} expected_routes are not canonical")
        routes[model] = provider
    if len(routes) != len(value):
        raise ValueError(f"{label} expected_routes contain duplicate identities")
    return routes


def _positive_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _formal_registry_snapshot(contract: Any) -> dict[str, Any]:
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
        raise ValueError("experiment config G1 registry snapshot version differs")
    raise ValueError("experiment config G1 registry snapshot hash differs")


def _resolved_g1_contract(
    config_path: Path,
    *,
    inline_overlay_json: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    experiment = load_draco_experiment_config(
        config_path,
        inline_overlay_json=inline_overlay_json,
    ).config
    contract = experiment.g1_routing
    if contract is None:
        raise ValueError("experiment config lacks the G1 contract")
    snapshot = _formal_registry_snapshot(contract)
    rows = snapshot.get("models")
    if not isinstance(rows, list):
        raise ValueError("experiment config G1 registry snapshot is malformed")
    models: set[str] = set()
    reasoning_ineligible_models: set[str] = set()
    for row in rows:
        facts = row.get("registry_facts") if isinstance(row, dict) else None
        if not isinstance(facts, dict):
            raise ValueError("experiment config G1 registry row is malformed")
        if str(facts.get("provider") or "").strip().lower() != "openrouter":
            continue
        model = str(facts.get("model_id") or "").strip().lower()
        if not model or model in models:
            raise ValueError("experiment config G1 registry identity is malformed")
        models.add(model)
        if facts.get("supports_reasoning") is not True:
            reasoning_ineligible_models.add(model)
    if contract.candidate_scope == "exact_routes":
        assert contract.expected_routes is not None
        routes = dict(contract.expected_routes)
        policy = "exact_openrouter_routes"
    else:
        routes = {model: "auto" for model in sorted(models)}
        policy = "all_registry_models"
    return contract, {
        "candidate_scope": contract.candidate_scope,
        "policy": policy,
        "expected_routes": routes,
        "expected_candidate_count": len(routes),
        "expected_routes_sha256": canonical_sha256(routes),
        "reasoning_ineligible_models": sorted(reasoning_ineligible_models),
    }


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


def _effective_ranking_resolution(experiment: Any) -> dict[str, Any]:
    """Resolve and authenticate the ranking policy used by runner/finalizer."""

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
        raise ValueError("route preflight lacks a frozen baseline ranking config")
    if (
        base.get("schema_version") != contract.expected_ranking_config_schema_version
        or base.get("config_version") != contract.expected_ranking_config_version
        or resolution.get("base_sha256") != contract.expected_ranking_config_sha256
    ):
        raise ValueError("baseline ranking config differs from the G1 contract")
    if _effective_proposer_max(base) != contract.expected_proposer_count_max:
        raise ValueError("baseline proposer maximum differs from the G1 contract")
    return resolution


def _resolved_task_analyzer_policy(
    ranking_resolution: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive analyzer identity from the authenticated effective ranking policy."""

    from opensquilla.provider.ranking_router import task_analyzer_policy

    effective = ranking_resolution.get("effective_config")
    if not isinstance(effective, Mapping):
        raise ValueError("route preflight lacks a frozen effective ranking config")
    policy = task_analyzer_policy(effective)
    if policy.get("provider") != "openrouter":
        raise ValueError("route preflight task analyzer must use OpenRouter")
    if policy.get("upstream_provider") == "auto":
        raise ValueError(
            "route preflight task analyzer upstream provider must be explicitly pinned"
        )
    return dict(policy)


_, _DEFAULT_RESOLVED_G1_CONTRACT = _resolved_g1_contract(DEFAULT_EXPERIMENT_CONFIG_PATH)
FORMAL_REASONING_INELIGIBLE_MODELS = frozenset(
    _DEFAULT_RESOLVED_G1_CONTRACT["reasoning_ineligible_models"]
)


def _formal_role_required_parameters(
    expected_routes: dict[str, str],
    *,
    include_tools: bool,
    reasoning_ineligible_models: set[str] | frozenset[str] = (FORMAL_REASONING_INELIGIBLE_MODELS),
) -> dict[str, list[str]]:
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
    for model in set(expected_routes) - reasoning_ineligible_models:
        required[model].add("reasoning")
    for model in expected_routes:
        if not model_matches_policy_prefix(
            model,
            OPENROUTER_COMPAT_POLICY.unsupported_temperature_model_prefixes,
        ):
            required[model].add("temperature")
    return {model: sorted(parameters) for model, parameters in required.items()}


def _formal_proposer_required_parameters(
    expected_routes: dict[str, str],
    *,
    reasoning_ineligible_models: set[str] | frozenset[str] = (FORMAL_REASONING_INELIGIBLE_MODELS),
) -> dict[str, list[str]]:
    return _formal_role_required_parameters(
        expected_routes,
        include_tools=False,
        reasoning_ineligible_models=reasoning_ineligible_models,
    )


def _formal_required_parameters(
    expected_routes: dict[str, str],
    *,
    reasoning_ineligible_models: set[str] | frozenset[str] = (FORMAL_REASONING_INELIGIBLE_MODELS),
) -> dict[str, list[str]]:
    return _formal_role_required_parameters(
        expected_routes,
        include_tools=True,
        reasoning_ineligible_models=reasoning_ineligible_models,
    )


def _tag_matches(tag: str, expected: str) -> bool:
    return tag == expected or tag.startswith(f"{expected}/")


def _recompute_endpoint_counts(
    *,
    model: str,
    expected_provider: str,
    required_parameters: list[str],
    endpoints: Any,
    label: str,
) -> tuple[int, int]:
    if not isinstance(endpoints, list) or not endpoints:
        raise ValueError(f"{label} route preflight v2 endpoint evidence is incomplete: {model}")
    provider_is_auto = expected_provider == "auto"
    expected_provider_name = EXPECTED_PROVIDER_NAMES.get(expected_provider)
    if not provider_is_auto and not expected_provider_name:
        raise ValueError(
            f"{label} route preflight v2 provider contract is unknown: {expected_provider}"
        )
    required = set(required_parameters)
    operational_count = 0
    compatible_count = 0
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            raise ValueError(f"{label} route preflight v2 endpoint row is invalid: {model}")
        if not provider_is_auto and not _tag_matches(
            str(endpoint.get("tag") or ""), expected_provider
        ):
            raise ValueError(f"{label} route preflight v2 endpoint provider tag differs: {model}")
        status = endpoint.get("status")
        operational = isinstance(status, int) and not isinstance(status, bool) and status == 0
        if not operational:
            continue
        operational_count += 1
        supported = endpoint.get("supported_parameters")
        if not isinstance(supported, list) or any(
            not isinstance(item, str) or not item for item in supported
        ):
            raise ValueError(f"{label} route preflight v2 endpoint parameters are invalid: {model}")
        supported_parameters = (
            {str(item) for item in supported} if isinstance(supported, list) else set()
        )
        if (
            (provider_is_auto or endpoint.get("provider_name") == expected_provider_name)
            and endpoint.get("model_id") == model
            and required <= supported_parameters
        ):
            compatible_count += 1
    if operational_count <= 0 or compatible_count <= 0:
        raise ValueError(f"{label} route preflight v2 model has no compatible route: {model}")
    return operational_count, compatible_count


def _parse_formal_groups(value: Any, *, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(group, str) for group in value)
    ):
        raise ValueError(f"{label} route preflight v3 groups are invalid")
    groups = tuple(value)
    try:
        indexes = [FORMAL_GROUP_ORDER.index(group) for group in groups]
    except ValueError as exc:
        raise ValueError(f"{label} route preflight v3 group is unsupported") from exc
    if indexes != sorted(set(indexes)):
        raise ValueError(f"{label} route preflight v3 groups are not canonical")
    return groups


def _required_fixed_route_specs(
    *,
    experiment: Any,
    groups: tuple[str, ...],
    proposer_required_parameters: dict[str, list[str]],
    ranking_resolution: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    routes: dict[str, str] = {}
    parameters: dict[str, set[str]] = {}

    def add(model: str, provider: str, required: list[str] | set[str]) -> None:
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
            add(model, provider, B2_REQUIRED_PARAMETERS[model])
    if "G1" in groups:
        judge_model = str(experiment.judge.model).strip().lower()
        analyzer_policy = _resolved_task_analyzer_policy(
            ranking_resolution or _effective_ranking_resolution(experiment)
        )
        for model, provider in {
            str(analyzer_policy["model"]): str(analyzer_policy["upstream_provider"]),
            judge_model: "google-ai-studio",
        }.items():
            if model not in proposer_required_parameters:
                raise ValueError(f"G1 fixed route is absent from the frozen registry: {model}")
            add(model, provider, proposer_required_parameters[model])
    return dict(sorted(routes.items())), {
        model: sorted(parameters[model]) for model in sorted(parameters)
    }


def _required_role_capacity(
    experiment: Any,
    groups: tuple[str, ...],
    *,
    ranking_resolution: Mapping[str, Any] | None = None,
) -> tuple[int, int]:
    if "G1" not in groups:
        return 0, 0
    g1 = experiment.g1_routing
    if g1 is None:
        raise ValueError("route preflight v3 requires a G1 routing contract")
    resolution = (
        dict(ranking_resolution)
        if ranking_resolution is not None
        else _effective_ranking_resolution(experiment)
    )
    ranking_config = resolution.get("effective_config")
    if not isinstance(ranking_config, Mapping):
        raise ValueError("route preflight lacks a frozen effective ranking config")
    return (
        _effective_proposer_max(ranking_config)
        + _effective_backup_count(ranking_config)
        + int(experiment.ensemble.aggregator_recovery_top_k),
        int(experiment.ensemble.aggregator_recovery_top_k),
    )


def _recompute_v3_endpoint_availability(
    *,
    model: str,
    expected_provider: str,
    proposer_required_parameters: list[str],
    aggregator_required_parameters: list[str],
    endpoints: Any,
    label: str,
) -> tuple[int, int, int, str]:
    if not isinstance(endpoints, list):
        raise ValueError(f"{label} route preflight v3 endpoint evidence is invalid: {model}")
    provider_is_auto = expected_provider == "auto"
    expected_provider_name = EXPECTED_PROVIDER_NAMES.get(expected_provider)
    if not provider_is_auto and not expected_provider_name:
        raise ValueError(
            f"{label} route preflight v3 provider contract is unknown: {expected_provider}"
        )
    proposer_required = set(proposer_required_parameters)
    aggregator_required = set(aggregator_required_parameters)
    operational_count = 0
    proposer_compatible_count = 0
    aggregator_compatible_count = 0
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            raise ValueError(f"{label} route preflight v3 endpoint row is invalid: {model}")
        if not provider_is_auto and not _tag_matches(
            str(endpoint.get("tag") or ""), expected_provider
        ):
            raise ValueError(f"{label} route preflight v3 endpoint provider tag differs: {model}")
        status = endpoint.get("status")
        if not (isinstance(status, int) and not isinstance(status, bool) and status == 0):
            continue
        operational_count += 1
        supported = endpoint.get("supported_parameters")
        if not isinstance(supported, list) or any(
            not isinstance(item, str) or not item for item in supported
        ):
            raise ValueError(f"{label} route preflight v3 endpoint parameters are invalid: {model}")
        supported_parameters = set(supported)
        identity_matches = (
            provider_is_auto or endpoint.get("provider_name") == expected_provider_name
        ) and endpoint.get("model_id") == model
        if identity_matches and proposer_required <= supported_parameters:
            proposer_compatible_count += 1
        if identity_matches and aggregator_required <= supported_parameters:
            aggregator_compatible_count += 1
    if not endpoints:
        status = "no_matching_endpoint"
    elif operational_count == 0:
        status = "no_operational_endpoint"
    elif proposer_compatible_count == 0:
        status = "no_compatible_request_surface"
    elif aggregator_compatible_count == 0:
        status = "proposer_only"
    else:
        status = "compatible"
    return operational_count, proposer_compatible_count, aggregator_compatible_count, status


def validate_route_preflight_payload(
    payload: Any,
    *,
    experiment_config_sha256: str,
    label: str,
) -> dict[str, Any]:
    """Validate one contract-bound formal/G1 v2 preflight payload."""

    if not isinstance(payload, dict):
        raise ValueError(f"{label} route preflight payload must be an object")
    schema = payload.get("schema")
    if schema not in ROUTE_PREFLIGHT_SCHEMAS:
        raise ValueError(f"{label} route preflight schema is unsupported: {schema!r}")
    if schema == ROUTE_PREFLIGHT_V1_SCHEMA:
        raise ValueError(
            f"{label} route preflight v1 lacks endpoint details required for "
            "fail-closed compatibility verification"
        )

    if payload.get("route_metadata_pass") is not True:
        raise ValueError(f"{label} route preflight v2 metadata did not pass")
    if payload.get("scope") != "formal":
        raise ValueError(f"{label} route preflight v2 scope must be formal")
    if payload.get("api_origin") != "https://openrouter.ai":
        raise ValueError(f"{label} route preflight v2 API origin differs")
    if payload.get("trust_env") is not False:
        raise ValueError(f"{label} route preflight v2 must disable trust_env")
    providers_hash = payload.get("providers_response_sha256")
    if not isinstance(providers_hash, str) or not HEX64.fullmatch(providers_hash):
        raise ValueError(f"{label} route preflight v2 providers hash is invalid")

    expected_routes = _normalized_routes(payload.get("expected_routes"), label=label)
    expected_routes_sha256 = payload.get("expected_routes_sha256")
    if (
        not isinstance(expected_routes_sha256, str)
        or not HEX64.fullmatch(expected_routes_sha256)
        or canonical_sha256(expected_routes) != expected_routes_sha256
    ):
        raise ValueError(f"{label} route preflight v2 expected-routes hash differs")

    experiment_evidence = payload.get("experiment_config")
    if not isinstance(experiment_evidence, dict):
        raise ValueError(f"{label} route preflight v2 lacks experiment config evidence")
    evidence_config_hash = experiment_evidence.get("sha256")
    if (
        evidence_config_hash != experiment_config_sha256
        or not isinstance(evidence_config_hash, str)
        or not HEX64.fullmatch(evidence_config_hash)
    ):
        raise ValueError(f"{label} route preflight v2 experiment config hash differs")
    config_path_raw = experiment_evidence.get("path")
    if not isinstance(config_path_raw, str) or not Path(config_path_raw).is_absolute():
        raise ValueError(f"{label} route preflight v2 experiment config path is invalid")
    config_path = Path(config_path_raw)
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError(f"{label} route preflight v2 experiment config is unavailable")
    if file_sha256(config_path) != experiment_config_sha256:
        raise ValueError(f"{label} route preflight v2 experiment config changed")
    inline_overlay = experiment_evidence.get("inline_overlay")
    inline_overlay_sha256 = experiment_evidence.get("inline_overlay_sha256")
    effective_evidence = experiment_evidence.get("effective_config")
    effective_config_path: Path | None = None
    if effective_evidence is not None:
        if not isinstance(effective_evidence, dict):
            raise ValueError(f"{label} route preflight effective config evidence is invalid")
        effective_path_raw = effective_evidence.get("path")
        effective_sha256 = effective_evidence.get("sha256")
        if (
            not isinstance(effective_path_raw, str)
            or not Path(effective_path_raw).is_absolute()
            or not isinstance(effective_sha256, str)
            or not HEX64.fullmatch(effective_sha256)
        ):
            raise ValueError(f"{label} route preflight effective config evidence is invalid")
        effective_config_path = Path(effective_path_raw)
        if effective_config_path.is_symlink() or not effective_config_path.is_file():
            raise ValueError(f"{label} route preflight effective config is unavailable")
        if effective_config_path.stat().st_mode & 0o077:
            raise ValueError(f"{label} route preflight effective config is not private")
        try:
            effective_document = json.loads(effective_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"{label} route preflight effective config is invalid"
            ) from exc
        if (
            not isinstance(effective_document, dict)
            or canonical_sha256(effective_document) != effective_sha256
        ):
            raise ValueError(f"{label} route preflight effective config hash differs")
    if inline_overlay is None:
        if inline_overlay_sha256 is not None and (
            not isinstance(inline_overlay_sha256, str)
            or not HEX64.fullmatch(inline_overlay_sha256)
        ):
            raise ValueError(f"{label} route preflight experiment overlay hash differs")
        inline_overlay_json = None
    else:
        if not isinstance(inline_overlay, dict):
            raise ValueError(f"{label} route preflight experiment overlay is invalid")
        if (
            not isinstance(inline_overlay_sha256, str)
            or not HEX64.fullmatch(inline_overlay_sha256)
            or canonical_sha256(inline_overlay) != inline_overlay_sha256
        ):
            raise ValueError(f"{label} route preflight experiment overlay hash differs")
        inline_overlay_json = json.dumps(
            inline_overlay,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    if (
        inline_overlay is None
        and inline_overlay_sha256 is not None
        and effective_config_path is None
    ):
        raise ValueError(f"{label} route preflight effective config evidence is missing")
    experiment_source_path = effective_config_path or config_path
    try:
        experiment = load_draco_experiment_config(
            experiment_source_path,
            inline_overlay_json=(inline_overlay_json if effective_config_path is None else None),
        ).config
        g1_contract, resolved_contract = _resolved_g1_contract(
            experiment_source_path,
            inline_overlay_json=(inline_overlay_json if effective_config_path is None else None),
        )
        ranking_resolution = _effective_ranking_resolution(experiment)
        analyzer_policy = _resolved_task_analyzer_policy(ranking_resolution)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} route preflight v2 experiment config is invalid: {exc}") from exc
    frozen_ranking_resolution = payload.get("ranking_config_resolution")
    requires_frozen_ranking_resolution = bool(
        inline_overlay_sha256 is not None or experiment.router_dynamic_ranking_override
    )
    if frozen_ranking_resolution is None:
        if requires_frozen_ranking_resolution:
            raise ValueError(f"{label} route preflight lacks frozen ranking resolution")
    elif (
        not isinstance(frozen_ranking_resolution, dict)
        or frozen_ranking_resolution != ranking_resolution
    ):
        raise ValueError(f"{label} route preflight frozen ranking resolution differs")
    effective_ranking_config = ranking_resolution.get("effective_config")
    effective_task_analyzer = (
        effective_ranking_config.get("task_analyzer")
        if isinstance(effective_ranking_config, Mapping)
        else None
    )
    current_analyzer_policy = isinstance(effective_task_analyzer, Mapping) and all(
        field in effective_task_analyzer
        for field in ("provider", "model", "upstream_provider", "stream_close_timeout_seconds")
    )
    frozen_analyzer_policy = payload.get("task_analyzer")
    if frozen_analyzer_policy is None:
        if current_analyzer_policy:
            raise ValueError(f"{label} route preflight lacks frozen task analyzer policy")
    elif (
        not isinstance(frozen_analyzer_policy, dict)
        or frozen_analyzer_policy != analyzer_policy
    ):
        raise ValueError(f"{label} route preflight frozen task analyzer policy differs")
    if g1_contract.selection_mode != "router_dynamic":
        raise ValueError(f"{label} route preflight v2 G1 selection mode differs")
    candidate_scope = str(resolved_contract["candidate_scope"])
    candidate_policy = str(resolved_contract["policy"])
    expected_policy = (
        "all_registry_models" if candidate_scope == "registry_all" else "exact_openrouter_routes"
    )
    if candidate_scope not in {"registry_all", "exact_routes"}:
        raise ValueError(f"{label} route preflight v2 G1 candidate scope differs")
    if candidate_policy != expected_policy:
        raise ValueError(f"{label} route preflight v2 G1 candidate policy differs")
    payload_candidate_scope = str(payload.get("candidate_scope") or "exact_routes")
    payload_candidate_policy = str(payload.get("candidate_policy") or "exact_openrouter_routes")
    if payload_candidate_scope != candidate_scope or payload_candidate_policy != candidate_policy:
        raise ValueError(f"{label} route preflight v2 G1 candidate scope differs")
    candidate_count = resolved_contract["expected_candidate_count"]
    if not _positive_int(candidate_count) or candidate_count != len(expected_routes):
        raise ValueError(f"{label} route preflight v2 G1 candidate count differs")
    config_routes = _normalized_routes(
        resolved_contract["expected_routes"],
        label=f"{label} G1 config",
    )
    if config_routes != expected_routes:
        raise ValueError(f"{label} route preflight v2 G1 routes differ")
    if resolved_contract["expected_routes_sha256"] != expected_routes_sha256:
        raise ValueError(f"{label} route preflight v2 G1 route hash differs")
    profile_id = experiment_evidence.get("g1_routing_profile_id")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError(f"{label} route preflight v2 G1 profile is missing")
    if profile_id != g1_contract.profile_id:
        raise ValueError(f"{label} route preflight v2 G1 profile differs")
    source_version = experiment_evidence.get("source_registry_snapshot_version")
    if not isinstance(source_version, str) or not source_version.strip():
        raise ValueError(f"{label} route preflight v2 registry version is missing")
    if source_version != g1_contract.source_registry_snapshot_version:
        raise ValueError(f"{label} route preflight v2 registry version differs")

    models = payload.get("models")
    if not isinstance(models, dict) or set(models) != set(expected_routes):
        raise ValueError(f"{label} route preflight v2 model evidence set differs")
    frozen_required_parameters = _formal_required_parameters(
        expected_routes,
        reasoning_ineligible_models=set(resolved_contract["reasoning_ineligible_models"]),
    )
    frozen_proposer_required_parameters = _formal_proposer_required_parameters(
        expected_routes,
        reasoning_ineligible_models=set(resolved_contract["reasoning_ineligible_models"]),
    )
    required_parameters: dict[str, list[str]] = {}
    proposer_required_parameters: dict[str, list[str]] = {}
    v3_counts: dict[str, tuple[int, int, int, str]] = {}
    for model, expected_provider in expected_routes.items():
        row = models.get(model)
        if not isinstance(row, dict):
            raise ValueError(f"{label} route preflight v2 model evidence is invalid")
        if row.get("expected_provider") != expected_provider:
            raise ValueError(f"{label} route preflight v2 model provider differs: {model}")
        response_sha256 = row.get("response_sha256")
        if not isinstance(response_sha256, str) or not HEX64.fullmatch(response_sha256):
            raise ValueError(
                f"{label} route preflight v2 endpoint response hash is invalid: {model}"
            )
        if schema == ROUTE_PREFLIGHT_V3_SCHEMA:
            if row.get("requested_model_id") != model:
                raise ValueError(f"{label} route preflight v3 requested model differs: {model}")
            fetch_outcome = row.get("endpoint_fetch_outcome")
            response_status = row.get("endpoint_http_status")
            response_hash_kind = row.get("response_sha256_kind")
            if fetch_outcome == "ok":
                if (
                    response_status != 200
                    or response_hash_kind != "canonical_json"
                    or row.get("response_model_id") != model
                ):
                    raise ValueError(
                        f"{label} route preflight v3 endpoint fetch evidence differs: {model}"
                    )
            elif fetch_outcome == "model_not_found":
                if (
                    candidate_scope != "registry_all"
                    or response_status != 404
                    or response_hash_kind != "raw_body"
                    or row.get("response_model_id") is not None
                    or row.get("matching_endpoints") != []
                ):
                    raise ValueError(
                        f"{label} route preflight v3 endpoint not-found evidence differs: {model}"
                    )
            else:
                raise ValueError(
                    f"{label} route preflight v3 endpoint fetch outcome is invalid: {model}"
                )
        elif row.get("response_model_id") != model:
            raise ValueError(f"{label} route preflight v2 endpoint response model differs: {model}")
        parameters = row.get("required_parameters")
        if (
            not isinstance(parameters, list)
            or not parameters
            or any(not isinstance(item, str) or not item for item in parameters)
            or parameters != sorted(set(parameters))
        ):
            raise ValueError(f"{label} route preflight v2 parameters are invalid: {model}")
        if parameters != frozen_required_parameters[model]:
            raise ValueError(f"{label} route preflight v2 frozen parameters differ: {model}")
        if schema == ROUTE_PREFLIGHT_V3_SCHEMA:
            proposer_parameters = row.get("proposer_required_parameters")
            aggregator_parameters = row.get("aggregator_required_parameters")
            if proposer_parameters != frozen_proposer_required_parameters[model]:
                raise ValueError(f"{label} route preflight v3 proposer parameters differ: {model}")
            if aggregator_parameters != frozen_required_parameters[model]:
                raise ValueError(
                    f"{label} route preflight v3 aggregator parameters differ: {model}"
                )
            if fetch_outcome == "model_not_found":
                counts = (0, 0, 0, "model_endpoint_not_found")
            else:
                counts = _recompute_v3_endpoint_availability(
                    model=model,
                    expected_provider=expected_provider,
                    proposer_required_parameters=proposer_parameters,
                    aggregator_required_parameters=aggregator_parameters,
                    endpoints=row.get("matching_endpoints"),
                    label=label,
                )
            operational_count, proposer_count, aggregator_count, status = counts
            expected_values = {
                "operational_match_count": operational_count,
                "compatible_operational_match_count": aggregator_count,
                "proposer_compatible_operational_match_count": proposer_count,
                "aggregator_compatible_operational_match_count": aggregator_count,
                "availability_status": status,
            }
            if any(row.get(field) != value for field, value in expected_values.items()):
                raise ValueError(
                    f"{label} route preflight v3 precomputed availability differs: {model}"
                )
            proposer_required_parameters[model] = proposer_parameters
            v3_counts[model] = counts
        else:
            operational_count, compatible_count = _recompute_endpoint_counts(
                model=model,
                expected_provider=expected_provider,
                required_parameters=parameters,
                endpoints=row.get("matching_endpoints"),
                label=label,
            )
            if (
                row.get("operational_match_count") != operational_count
                or row.get("compatible_operational_match_count") != compatible_count
            ):
                raise ValueError(
                    f"{label} route preflight v2 precomputed endpoint counts differ: {model}"
                )
        required_parameters[model] = parameters
    required_parameters_sha256 = payload.get("required_parameters_sha256")
    if (
        not isinstance(required_parameters_sha256, str)
        or not HEX64.fullmatch(required_parameters_sha256)
        or canonical_sha256(required_parameters) != required_parameters_sha256
    ):
        raise ValueError(f"{label} route preflight v2 required-parameters hash differs")

    v3_contract: dict[str, Any] = {}
    if schema == ROUTE_PREFLIGHT_V3_SCHEMA:
        proposer_parameters_sha256 = payload.get("proposer_required_parameters_sha256")
        if (
            not isinstance(proposer_parameters_sha256, str)
            or not HEX64.fullmatch(proposer_parameters_sha256)
            or canonical_sha256(proposer_required_parameters) != proposer_parameters_sha256
        ):
            raise ValueError(f"{label} route preflight v3 proposer-parameters hash differs")
        groups = _parse_formal_groups(payload.get("groups"), label=label)
        expected_availability_policy = (
            "registry_capacity" if candidate_scope == "registry_all" else "strict_all_routes"
        )
        if payload.get("availability_policy") != expected_availability_policy:
            raise ValueError(f"{label} route preflight v3 availability policy differs")

        proposer_models = sorted(
            model for model, (_, count, _, _) in v3_counts.items() if count > 0
        )
        aggregator_models = sorted(
            model for model, (_, _, count, _) in v3_counts.items() if count > 0
        )
        unavailable_models = sorted(set(expected_routes) - set(proposer_models))
        aggregator_ineligible_models = sorted(set(expected_routes) - set(aggregator_models))
        expected_summary = {
            "proposer_compatible_candidate_count": len(proposer_models),
            "aggregator_compatible_candidate_count": len(aggregator_models),
            "proposer_compatible_models": proposer_models,
            "aggregator_compatible_models": aggregator_models,
            "unavailable_models": unavailable_models,
            "aggregator_ineligible_models": aggregator_ineligible_models,
            "availability_status": (
                "complete"
                if not unavailable_models and not aggregator_ineligible_models
                else "degraded"
            ),
        }
        if any(payload.get(field) != value for field, value in expected_summary.items()):
            raise ValueError(f"{label} route preflight v3 availability summary differs")

        required_proposer_count, required_aggregator_count = _required_role_capacity(
            experiment,
            groups,
            ranking_resolution=ranking_resolution,
        )
        if candidate_scope == "exact_routes":
            required_proposer_count = len(expected_routes)
            required_aggregator_count = len(expected_routes)
        capacity_pass = (
            len(proposer_models) >= required_proposer_count
            and len(aggregator_models) >= required_aggregator_count
        )
        capacity_fields = {
            "required_proposer_compatible_candidate_count": required_proposer_count,
            "required_aggregator_compatible_candidate_count": required_aggregator_count,
            "candidate_capacity_pass": capacity_pass,
        }
        if any(payload.get(field) != value for field, value in capacity_fields.items()):
            raise ValueError(f"{label} route preflight v3 capacity evidence differs")
        if not capacity_pass:
            raise ValueError(f"{label} route preflight v3 candidate capacity did not pass")

        fixed_routes, fixed_parameters = _required_fixed_route_specs(
            experiment=experiment,
            groups=groups,
            proposer_required_parameters=proposer_required_parameters,
            ranking_resolution=ranking_resolution,
        )
        fixed_contract = {
            model: {
                "provider": fixed_routes[model],
                "required_parameters": fixed_parameters[model],
            }
            for model in fixed_routes
        }
        if payload.get("required_fixed_routes") != fixed_contract:
            raise ValueError(f"{label} route preflight v3 fixed-route contract differs")
        fixed_routes_sha256 = payload.get("required_fixed_routes_sha256")
        if (
            not isinstance(fixed_routes_sha256, str)
            or not HEX64.fullmatch(fixed_routes_sha256)
            or canonical_sha256(fixed_contract) != fixed_routes_sha256
        ):
            raise ValueError(f"{label} route preflight v3 fixed-route hash differs")
        fixed_checks = payload.get("fixed_route_checks")
        if not isinstance(fixed_checks, dict) or set(fixed_checks) != set(fixed_routes):
            raise ValueError(f"{label} route preflight v3 fixed-route evidence differs")
        for model, expected_provider in fixed_routes.items():
            row = fixed_checks.get(model)
            if not isinstance(row, dict):
                raise ValueError(f"{label} route preflight v3 fixed-route row is invalid: {model}")
            source_row = models[model]
            if source_row.get("endpoint_fetch_outcome") != "ok":
                raise ValueError(f"{label} route preflight v3 fixed route was not fetched: {model}")
            if row.get("expected_provider") != expected_provider:
                raise ValueError(
                    f"{label} route preflight v3 fixed-route provider differs: {model}"
                )
            for field in (
                "requested_model_id",
                "endpoint_fetch_outcome",
                "endpoint_http_status",
                "response_sha256_kind",
                "response_model_id",
                "response_sha256",
            ):
                if row.get(field) != source_row.get(field):
                    raise ValueError(
                        f"{label} route preflight v3 fixed-route response differs: {model}"
                    )
            if row.get("response_model_id") != model:
                raise ValueError(f"{label} route preflight v3 fixed-route model differs: {model}")
            response_hash = row.get("response_sha256")
            if not isinstance(response_hash, str) or not HEX64.fullmatch(response_hash):
                raise ValueError(
                    f"{label} route preflight v3 fixed-route response hash is invalid: {model}"
                )
            if row.get("required_parameters") != fixed_parameters[model]:
                raise ValueError(
                    f"{label} route preflight v3 fixed-route parameters differ: {model}"
                )
            source_endpoints = source_row.get("matching_endpoints")
            if not isinstance(source_endpoints, list):
                raise ValueError(
                    f"{label} route preflight v3 fixed-route source endpoints differ: {model}"
                )
            expected_fixed_endpoints = [
                endpoint
                for endpoint in source_endpoints
                if isinstance(endpoint, dict)
                and _tag_matches(str(endpoint.get("tag") or ""), expected_provider)
            ]
            if row.get("matching_endpoints") != expected_fixed_endpoints:
                raise ValueError(
                    f"{label} route preflight v3 fixed-route projection differs: {model}"
                )
            operational_count, compatible_count = _recompute_endpoint_counts(
                model=model,
                expected_provider=expected_provider,
                required_parameters=fixed_parameters[model],
                endpoints=expected_fixed_endpoints,
                label=label,
            )
            if (
                row.get("operational_match_count") != operational_count
                or row.get("compatible_operational_match_count") != compatible_count
            ):
                raise ValueError(f"{label} route preflight v3 fixed-route counts differ: {model}")
        if payload.get("fixed_routes_pass") is not True:
            raise ValueError(f"{label} route preflight v3 fixed routes did not pass")
        v3_contract = {
            "groups": list(groups),
            **({"task_analyzer": analyzer_policy} if frozen_analyzer_policy is not None else {}),
            "availability_policy": expected_availability_policy,
            "required_fixed_routes_sha256": fixed_routes_sha256,
            "required_proposer_compatible_candidate_count": required_proposer_count,
            "required_aggregator_compatible_candidate_count": required_aggregator_count,
        }

    return {
        "schema": schema,
        "scope": "formal",
        "candidate_scope": candidate_scope,
        "candidate_policy": candidate_policy,
        "expected_routes_sha256": expected_routes_sha256,
        "expected_candidate_count": candidate_count,
        "experiment_config_sha256": experiment_config_sha256,
        "experiment_config_inline_overlay_sha256": inline_overlay_sha256,
        "ranking_config_effective_sha256": ranking_resolution["effective_sha256"],
        "g1_routing_profile_id": profile_id,
        "source_registry_snapshot_version": source_version,
        **v3_contract,
    }


def validate_route_preflight_set(
    payloads: list[Any],
    *,
    experiment_config_sha256: str,
    labels: list[str],
) -> list[dict[str, Any]]:
    if len(payloads) != len(labels):
        raise ValueError("route preflight payload and label counts differ")
    validations = [
        validate_route_preflight_payload(
            payload,
            experiment_config_sha256=experiment_config_sha256,
            label=label,
        )
        for payload, label in zip(payloads, labels, strict=True)
    ]
    schemas = {validation["schema"] for validation in validations}
    if len(schemas) != 1:
        raise ValueError("route preflight evidence schemas differ")
    if schemas <= {ROUTE_PREFLIGHT_V2_SCHEMA, ROUTE_PREFLIGHT_V3_SCHEMA} and any(
        validation != validations[0] for validation in validations[1:]
    ):
        raise ValueError("route preflight G1 contracts differ")
    return validations


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("output", type=Path)
    snapshot_parser.add_argument("--root", type=Path, required=True)
    snapshot_parser.add_argument("--file", type=Path, action="append", default=[])
    snapshot_parser.add_argument("--recursive", action="store_true")
    snapshot_parser.add_argument("--allow-after", action="append", default=[])

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("snapshot", type=Path)

    success_parser = subparsers.add_parser("success")
    success_parser.add_argument("output", type=Path)
    success_parser.add_argument("--source-git-head", required=True)
    success_parser.add_argument("--input-sha256", required=True)
    success_parser.add_argument("--gateway-config-sha256", required=True)
    success_parser.add_argument("--experiment-config-sha256", required=True)
    success_parser.add_argument("--snapshot", type=Path, action="append", default=[])
    success_parser.add_argument("--evidence", type=Path, action="append", default=[])
    args = parser.parse_args()

    if args.command == "snapshot":
        if args.output.exists():
            parser.error(f"refusing to overwrite artifact snapshot: {args.output}")
        root = args.root.resolve(strict=True)
        output_resolved = args.output.resolve(strict=False)
        if output_resolved.parent != root:
            parser.error("artifact snapshot must be created directly inside --root")
        if args.recursive and args.file:
            parser.error("--recursive cannot be combined with --file")
        try:
            allowed_after = sorted(
                {
                    safe_relative_path(value, label="allowed-after path").as_posix()
                    for value in args.allow_after
                }
            )
        except ValueError as exc:
            parser.error(str(exc))
        excluded = {
            output_resolved,
            *{(root / relative).resolve(strict=False) for relative in allowed_after},
        }
        files = (
            recursive_artifact_paths(root, excluded=excluded)
            if args.recursive
            else sorted({path.resolve(strict=True) for path in args.file})
        )
        if not files:
            parser.error("artifact snapshot requires at least one --file")
        if output_resolved in files:
            parser.error("artifact snapshot cannot include itself")
        payload = {
            "schema": SNAPSHOT_SCHEMA,
            "created_at": datetime.now(UTC).isoformat(),
            "root": ".",
            "closed_world": bool(args.recursive),
            "allowed_after_snapshot": allowed_after,
            "artifacts": [relative_file_record(root, path) for path in files],
        }
        atomic_write_json(args.output, payload)
        return 0

    if args.command == "verify":
        verify_snapshot(args.snapshot)
        return 0

    if args.output.exists():
        parser.error(f"refusing to overwrite success sentinel: {args.output}")
    if not HEX40.fullmatch(args.source_git_head):
        parser.error("--source-git-head must be a 40-character lowercase hex commit")
    for field, value in (
        ("--input-sha256", args.input_sha256),
        ("--gateway-config-sha256", args.gateway_config_sha256),
        ("--experiment-config-sha256", args.experiment_config_sha256),
    ):
        if not HEX64.fullmatch(value):
            parser.error(f"{field} must be a 64-character lowercase hex digest")
    for path in args.snapshot:
        if path.is_symlink() or not path.is_file():
            parser.error(f"snapshot is not a regular non-symlink file: {path}")
        if path.stat().st_mode & 0o077:
            parser.error(f"snapshot permissions are not owner-only: {path}")
    resolved_snapshot_paths = [path.resolve(strict=True) for path in args.snapshot]
    if len(resolved_snapshot_paths) != 3 or len(set(resolved_snapshot_paths)) != 3:
        parser.error("formal success requires exactly three distinct static/canary/full snapshots")
    for path in args.evidence:
        if path.is_symlink() or not path.is_file():
            parser.error(f"evidence is not a regular non-symlink file: {path}")
        if path.stat().st_mode & 0o077:
            parser.error(f"evidence permissions are not owner-only: {path}")
    resolved_evidence_paths = [path.resolve(strict=True) for path in args.evidence]
    if len(resolved_evidence_paths) != 2 or len(set(resolved_evidence_paths)) != 2:
        parser.error("formal success requires exactly two distinct route preflight artifacts")
    success_root = args.output.resolve(strict=False).parent
    snapshots = []
    for snapshot_path in resolved_snapshot_paths:
        snapshot = verify_snapshot(snapshot_path)
        try:
            relative_snapshot = snapshot_path.relative_to(success_root).as_posix()
        except ValueError:
            parser.error(f"snapshot escapes success directory: {snapshot_path}")
        snapshots.append(
            {
                "path": relative_snapshot,
                "sha256": file_sha256(snapshot_path),
                "snapshot_schema": snapshot["schema"],
            }
        )
    route_payloads: list[dict[str, Any]] = []
    for resolved in resolved_evidence_paths:
        if resolved.is_symlink() or not resolved.is_file():
            parser.error(f"evidence is not a regular non-symlink file: {resolved}")
        try:
            route_payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"invalid route preflight evidence {resolved}: {exc}")
        route_payloads.append(route_payload)
    try:
        route_validations = validate_route_preflight_set(
            route_payloads,
            experiment_config_sha256=args.experiment_config_sha256,
            labels=[str(path) for path in resolved_evidence_paths],
        )
    except ValueError as exc:
        parser.error(str(exc))

    evidence = []
    for resolved, validation in zip(
        resolved_evidence_paths,
        route_validations,
        strict=True,
    ):
        try:
            relative_evidence = resolved.relative_to(success_root).as_posix()
        except ValueError:
            parser.error(f"evidence escapes success directory: {resolved}")
        evidence_record = {
            "path": relative_evidence,
            "sha256": file_sha256(resolved),
            "size_bytes": resolved.stat().st_size,
            "route_preflight_schema": validation["schema"],
        }
        if validation["schema"] in {
            ROUTE_PREFLIGHT_V2_SCHEMA,
            ROUTE_PREFLIGHT_V3_SCHEMA,
        }:
            evidence_record["formal_g1_contract"] = {
                key: validation[key]
                for key in (
                    "scope",
                    "candidate_scope",
                    "candidate_policy",
                    "expected_routes_sha256",
                    "expected_candidate_count",
                    "experiment_config_sha256",
                    "g1_routing_profile_id",
                    "source_registry_snapshot_version",
                )
            }
            if validation["schema"] == ROUTE_PREFLIGHT_V3_SCHEMA:
                evidence_record["formal_g1_contract"].update(
                    {
                        key: validation[key]
                        for key in (
                            "groups",
                            "availability_policy",
                            "required_fixed_routes_sha256",
                            "required_proposer_compatible_candidate_count",
                            "required_aggregator_compatible_candidate_count",
                        )
                    }
                )
        evidence.append(evidence_record)
    payload = {
        "schema": SUCCESS_SCHEMA,
        "status": "complete",
        "created_at": datetime.now(UTC).isoformat(),
        "source_git_head": args.source_git_head,
        "input_sha256": args.input_sha256,
        "gateway_config_sha256": args.gateway_config_sha256,
        "experiment_config_sha256": args.experiment_config_sha256,
        "artifact_snapshots": snapshots,
        "route_preflight_evidence": evidence,
    }
    atomic_write_json(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
