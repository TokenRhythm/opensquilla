from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.eval.draco_experiment_config import load_draco_experiment_config
from opensquilla.gateway.llm_runtime import OPENROUTER_DEFAULT_PROVIDER_ROUTING
from opensquilla.provider import ranking_router
from opensquilla.provider.compat_policy import (
    compat_policy_for_kind,
    model_matches_policy_prefix,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "experiments" / "validate_openrouter_b2_routes.py"
REGISTRY_PATH = ROOT / "src" / "opensquilla" / "provider" / "router_dynamic_model_profiles.json"


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_openrouter_b2_routes_under_test",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def _registry_contract(snapshot: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        source_registry_snapshot_version=snapshot["snapshot_version"],
        expected_source_registry_snapshot_sha256=validator.canonical_sha256(snapshot),
    )


def test_formal_registry_snapshot_selects_by_version_and_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {
        "schema_version": "raw",
        "snapshot_version": "same-version",
        "models": [{"raw": True}],
    }
    legacy = {
        "schema_version": "legacy",
        "snapshot_version": "same-version",
        "models": [{"raw": False}],
    }
    requested_versions: list[str | None] = []

    def load_snapshot(*, base_version: str | None = None):
        requested_versions.append(base_version)
        return raw

    monkeypatch.setattr(ranking_router, "load_model_registry_snapshot", load_snapshot)
    monkeypatch.setattr(
        ranking_router,
        "_legacy_registry_snapshot_projection",
        lambda snapshot: legacy,
    )

    assert validator.formal_registry_snapshot(_registry_contract(raw)) is raw
    assert validator.formal_registry_snapshot(_registry_contract(legacy)) is legacy
    assert requested_versions == ["same-version", "same-version"]


def test_formal_registry_snapshot_fails_closed_without_exact_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {"snapshot_version": "same-version", "models": [{"raw": True}]}
    legacy = {"snapshot_version": "same-version", "models": [{"raw": False}]}
    def load_snapshot(*, base_version: str | None = None):
        assert base_version is not None
        return raw

    monkeypatch.setattr(ranking_router, "load_model_registry_snapshot", load_snapshot)
    monkeypatch.setattr(
        ranking_router,
        "_legacy_registry_snapshot_projection",
        lambda snapshot: legacy,
    )
    wrong_hash = SimpleNamespace(
        source_registry_snapshot_version="same-version",
        expected_source_registry_snapshot_sha256="0" * 64,
    )
    wrong_version = SimpleNamespace(
        source_registry_snapshot_version="other-version",
        expected_source_registry_snapshot_sha256=validator.canonical_sha256(raw),
    )

    with pytest.raises(ValueError, match="hash differs"):
        validator.formal_registry_snapshot(wrong_hash)
    with pytest.raises(ValueError, match="version differs"):
        validator.formal_registry_snapshot(wrong_version)


def test_formal_scope_is_default(tmp_path: Path) -> None:
    args = validator.parse_args([str(tmp_path / "evidence.json")])

    assert args.scope == "formal"
    assert args.groups == validator.FORMAL_GROUP_ORDER
    assert args.experiment_config == validator.DEFAULT_EXPERIMENT_CONFIG_PATH
    assert args.experiment_config_override_json is None


def test_inline_experiment_override_reaches_frozen_ranking_resolution(
    tmp_path: Path,
) -> None:
    raw_overlay = json.dumps(
        {
            "router_dynamic_ranking_override": {
                "proposer_count": {"backup_count": 1}
            }
        }
    )
    args = validator.parse_args(
        [
            str(tmp_path / "evidence.json"),
            "--experiment-config-override-json",
            raw_overlay,
        ]
    )
    experiment, _ = validator.resolved_g1_contract(
        validator.DEFAULT_EXPERIMENT_CONFIG_PATH,
        inline_overlay_json=args.experiment_config_override_json,
    )
    resolution = validator.effective_ranking_resolution(experiment)

    assert resolution["effective_config"]["proposer_count"]["backup_count"] == 1
    assert validator.required_role_capacity(
        experiment,
        ("G1",),
        ranking_resolution=resolution,
    ) == (9, 3)


def test_preflight_experiment_evidence_uses_hashes_and_private_effective_config(
    tmp_path: Path,
) -> None:
    marker = "preflight-inline-secret-marker"
    bundle = load_draco_experiment_config(
        validator.DEFAULT_EXPERIMENT_CONFIG_PATH,
        inline_overlay_json=json.dumps(
            {
                "reference": {"repository": marker},
                "generation": {"max_attempts": 2},
            }
        ),
    )
    output = tmp_path / "route-preflight.json"
    effective_path = validator.effective_experiment_config_path(output)
    validator.atomic_write_json(
        effective_path,
        bundle.config.model_dump(mode="json"),
    )

    evidence = validator.experiment_config_evidence(
        bundle,
        effective_path=effective_path,
    )
    serialized = json.dumps(evidence, ensure_ascii=False)

    assert marker not in serialized
    assert "inline_overlay" not in evidence
    assert "inline_overlay_sha256" not in evidence
    assert bundle.inline_overlay_sha256 not in serialized
    assert evidence["inline_overlay_present"] is True
    assert evidence["inline_overlay_field_paths"] == [
        "generation.max_attempts",
        "reference.repository",
    ]
    assert evidence["inline_override_count"] == 0
    assert evidence["inline_override_paths"] == []
    assert evidence["effective_config"]["path"] == str(effective_path.resolve())
    assert effective_path.stat().st_mode & 0o777 == 0o600


def test_atomic_write_json_does_not_overwrite_a_racing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "route-preflight.json"
    sentinel = b"racing-writer-won\n"
    real_link = validator.os.link

    def racing_link(source, destination, *, follow_symlinks=True):
        Path(destination).write_bytes(sentinel)
        return real_link(
            source,
            destination,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(validator.os, "link", racing_link)

    with pytest.raises(FileExistsError):
        validator.atomic_write_json(output, {"new": "payload"})

    assert output.read_bytes() == sentinel
    assert not list(tmp_path.glob(f".{output.name}.*"))


def test_atomic_write_json_bundle_rolls_back_only_its_earlier_publication(
    tmp_path: Path,
) -> None:
    effective = tmp_path / "route-preflight.experiment-config.effective.json"
    evidence = tmp_path / "route-preflight.json"
    sentinel = b"preexisting-evidence\n"
    evidence.write_bytes(sentinel)

    with pytest.raises(FileExistsError):
        validator.atomic_write_json_bundle(
            [
                (effective, {"effective": True}),
                (evidence, {"evidence": True}),
            ]
        )

    assert not effective.exists()
    assert evidence.read_bytes() == sentinel
    assert not list(tmp_path.glob(".*"))


def test_atomic_write_json_bundle_does_not_remove_a_replaced_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effective = tmp_path / "route-preflight.experiment-config.effective.json"
    evidence = tmp_path / "route-preflight.json"
    replacement = b"replacement-from-racing-writer\n"
    real_atomic_write = validator.atomic_write_json
    calls = 0

    def replace_then_fail(path: Path, payload: dict[str, object]):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_atomic_write(path, payload)
        effective.unlink()
        effective.write_bytes(replacement)
        raise RuntimeError("injected second-publication failure")

    monkeypatch.setattr(validator, "atomic_write_json", replace_then_fail)

    with pytest.raises(RuntimeError, match="injected second-publication failure"):
        validator.atomic_write_json_bundle(
            [
                (effective, {"effective": True}),
                (evidence, {"evidence": True}),
            ]
        )

    assert effective.read_bytes() == replacement
    assert not evidence.exists()


def test_formal_route_contract_rejects_credentialed_member_url() -> None:
    marker = "url-secret-marker"
    with pytest.raises(ValueError, match=r"ensemble\.aggregator\.base_url") as error:
        validator.resolved_g1_contract(
            validator.DEFAULT_EXPERIMENT_CONFIG_PATH,
            inline_overlay_json=json.dumps(
                {
                    "ensemble": {
                        "aggregator": {
                            "base_url": (
                                f"https://user:{marker}@openrouter.ai/api/v1?token={marker}"
                            )
                        }
                    }
                }
            ),
        )

    assert marker not in str(error.value)


def test_formal_groups_require_a_canonical_subset() -> None:
    assert validator.parse_formal_groups("G1") == ("G1",)
    assert validator.parse_formal_groups("B0,B2,G1") == ("B0", "B2", "G1")
    with pytest.raises(ValueError, match="non-duplicated"):
        validator.parse_formal_groups("G1,G1")
    with pytest.raises(ValueError, match="canonical"):
        validator.parse_formal_groups("G1,B2")


def test_formal_routes_are_a_valid_subset_of_router_dynamic_registry() -> None:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    registry_models = {str(row["registry_facts"]["model_id"]) for row in payload["models"]}

    assert len(registry_models) == 82
    assert set(validator.FORMAL_EXPECTED_ROUTES) <= registry_models
    assert set(validator.B2_EXPECTED_ROUTES) <= set(validator.FORMAL_EXPECTED_ROUTES)
    experiment = load_draco_experiment_config(validator.DEFAULT_EXPERIMENT_CONFIG_PATH).config
    assert experiment.g1_routing is not None
    _, resolved = validator.resolved_g1_contract(validator.DEFAULT_EXPERIMENT_CONFIG_PATH)
    assert validator.FORMAL_EXPECTED_ROUTES == resolved["expected_routes"]
    assert (
        validator.canonical_sha256(validator.FORMAL_EXPECTED_ROUTES)
        == (resolved["expected_routes_sha256"])
    )


def test_formal_routes_match_runtime_pins_and_capability_contract() -> None:
    experiment = load_draco_experiment_config(validator.DEFAULT_EXPERIMENT_CONFIG_PATH).config
    assert experiment.g1_routing is not None
    candidate_scope = getattr(experiment.g1_routing, "candidate_scope", "exact_routes")
    policy = compat_policy_for_kind("openrouter")
    for model, provider in validator.FORMAL_EXPECTED_ROUTES.items():
        if candidate_scope == "registry_all":
            assert provider == "auto"
        else:
            assert OPENROUTER_DEFAULT_PROVIDER_ROUTING[model] == provider
        required = validator.FORMAL_REQUIRED_PARAMETERS[model]
        uses_max_completion_tokens = model_matches_policy_prefix(
            model,
            policy.max_completion_tokens_model_prefixes,
        )
        expected_token_parameter = (
            "max_completion_tokens" if uses_max_completion_tokens else "max_tokens"
        )
        other_token_parameter = (
            "max_tokens" if uses_max_completion_tokens else "max_completion_tokens"
        )
        assert {expected_token_parameter, "tools"} <= required
        assert other_token_parameter not in required
        assert ("reasoning" in required) is (
            model not in validator.FORMAL_REASONING_INELIGIBLE_MODELS
        )
        assert ("temperature" in required) is (
            not model_matches_policy_prefix(
                model,
                policy.unsupported_temperature_model_prefixes,
            )
        )
    if candidate_scope == "registry_all":
        payload = validator.formal_registry_snapshot(experiment.g1_routing)
        registry_models = {
            str(row["registry_facts"]["model_id"])
            for row in payload["models"]
            if row["registry_facts"]["provider"] == "openrouter"
        }
        assert validator.FORMAL_EXPECTED_ROUTES == dict.fromkeys(registry_models, "auto")
        assert validator.FORMAL_REASONING_INELIGIBLE_MODELS == {
            str(row["registry_facts"]["model_id"])
            for row in payload["models"]
            if row["registry_facts"]["provider"] == "openrouter"
            and row["registry_facts"]["supports_reasoning"] is not True
        }
        assert len(validator.FORMAL_REASONING_INELIGIBLE_MODELS) == 14
    else:
        assert validator.FORMAL_EXPECTED_ROUTES["google/gemini-3.5-flash"] == ("google-ai-studio")
        assert "openai/gpt-5.6-luna" not in validator.FORMAL_EXPECTED_ROUTES
    assert validator.FORMAL_REQUIRED_PARAMETERS["google/gemini-3.5-flash"] == {
        "max_tokens",
        "reasoning",
        "temperature",
        "tools",
    }
    assert validator.FORMAL_REQUIRED_PARAMETERS["anthropic/claude-fable-5"] == {
        "max_tokens",
        "reasoning",
        "tools",
    }
    assert validator.FORMAL_REQUIRED_PARAMETERS["openai/gpt-5.3-codex"] == {
        "max_tokens",
        "reasoning",
        "tools",
    }
    assert validator.FORMAL_REQUIRED_PARAMETERS["openai/gpt-5.6-terra"] == {
        "max_completion_tokens",
        "reasoning",
        "tools",
    }
    assert validator.FORMAL_REQUIRED_PARAMETERS["openai/gpt-5.6-sol"] == {
        "max_completion_tokens",
        "reasoning",
        "tools",
    }


def _saved_endpoint_evidence(
    *,
    model: str,
    provider: str,
    required_parameters: set[str],
) -> dict[str, object]:
    return {
        "expected_provider": provider,
        "response_model_id": model,
        "matching_endpoints": [
            {
                "tag": provider,
                "provider_name": validator.EXPECTED_PROVIDER_NAMES[provider],
                "model_id": model,
                "status": 0,
                "supported_parameters": sorted(required_parameters),
            },
            {
                "tag": f"{provider}/backup",
                "provider_name": validator.EXPECTED_PROVIDER_NAMES[provider],
                "model_id": model,
                "status": 1,
                "supported_parameters": sorted(required_parameters),
            },
        ],
        "operational_match_count": 1,
        "compatible_operational_match_count": 1,
        "required_parameters": sorted(required_parameters),
    }


def test_validator_recomputes_saved_endpoint_compatibility() -> None:
    model = "deepseek/deepseek-v4-pro"
    provider = "deepseek"
    evidence = _saved_endpoint_evidence(
        model=model,
        provider=provider,
        required_parameters=validator.FORMAL_REQUIRED_PARAMETERS[model],
    )

    assert validator.recompute_model_endpoint_compatibility(
        model=model,
        expected_provider=provider,
        required_parameters=validator.FORMAL_REQUIRED_PARAMETERS[model],
        evidence=evidence,
    ) == (1, 1)


def test_validator_rejects_tampered_precomputed_compatible_count() -> None:
    model = "deepseek/deepseek-v4-pro"
    provider = "deepseek"
    evidence = _saved_endpoint_evidence(
        model=model,
        provider=provider,
        required_parameters=validator.FORMAL_REQUIRED_PARAMETERS[model],
    )
    evidence["compatible_operational_match_count"] = 2

    try:
        validator.recompute_model_endpoint_compatibility(
            model=model,
            expected_provider=provider,
            required_parameters=validator.FORMAL_REQUIRED_PARAMETERS[model],
            evidence=evidence,
        )
    except ValueError as exc:
        assert "compatible endpoint count differs" in str(exc)
    else:
        raise AssertionError("tampered compatible count must fail closed")


def test_validator_auto_provider_accepts_matching_operational_endpoint() -> None:
    model = "vendor/model"
    required = {"max_tokens", "tools"}
    evidence = {
        "expected_provider": "auto",
        "response_model_id": model,
        "matching_endpoints": [
            {
                "tag": "any-upstream",
                "provider_name": "Any Upstream",
                "model_id": model,
                "status": 0,
                "supported_parameters": sorted(required),
            }
        ],
        "operational_match_count": 1,
        "compatible_operational_match_count": 1,
        "required_parameters": sorted(required),
    }

    assert validator.recompute_model_endpoint_compatibility(
        model=model,
        expected_provider="auto",
        required_parameters=required,
        evidence=evidence,
    ) == (1, 1)


def test_validator_auto_provider_rejects_wrong_serving_model() -> None:
    model = "vendor/model"
    required = {"max_tokens", "tools"}
    evidence = {
        "expected_provider": "auto",
        "response_model_id": model,
        "matching_endpoints": [
            {
                "tag": "any-upstream",
                "provider_name": "Any Upstream",
                "model_id": "vendor/other",
                "status": 0,
                "supported_parameters": sorted(required),
            }
        ],
        "operational_match_count": 1,
        "compatible_operational_match_count": 0,
        "required_parameters": sorted(required),
    }

    try:
        validator.recompute_model_endpoint_compatibility(
            model=model,
            expected_provider="auto",
            required_parameters=required,
            evidence=evidence,
        )
    except ValueError as exc:
        assert "no saved endpoint supports" in str(exc)
    else:
        raise AssertionError("auto provider must still bind the serving model")


def test_v3_registry_model_can_record_explicit_unavailability() -> None:
    model = "vendor/model"
    proposer_required = {"max_tokens", "reasoning"}
    aggregator_required = {*proposer_required, "tools"}
    evidence = {
        "expected_provider": "auto",
        "requested_model_id": model,
        "endpoint_fetch_outcome": "ok",
        "endpoint_http_status": 200,
        "response_sha256_kind": "canonical_json",
        "response_model_id": model,
        "matching_endpoints": [],
        "operational_match_count": 0,
        "compatible_operational_match_count": 0,
        "proposer_compatible_operational_match_count": 0,
        "aggregator_compatible_operational_match_count": 0,
        "availability_status": "no_matching_endpoint",
        "proposer_required_parameters": sorted(proposer_required),
        "aggregator_required_parameters": sorted(aggregator_required),
    }

    assert validator.recompute_model_endpoint_availability(
        model=model,
        expected_provider="auto",
        proposer_required_parameters=proposer_required,
        aggregator_required_parameters=aggregator_required,
        evidence=evidence,
    ) == (0, 0, 0, "no_matching_endpoint")

    evidence["availability_status"] = "compatible"
    with pytest.raises(ValueError, match="availability_status"):
        validator.recompute_model_endpoint_availability(
            model=model,
            expected_provider="auto",
            proposer_required_parameters=proposer_required,
            aggregator_required_parameters=aggregator_required,
            evidence=evidence,
        )


def test_v3_registry_model_can_record_endpoint_http_404() -> None:
    model = "vendor/model"
    proposer_required = {"max_tokens", "reasoning"}
    aggregator_required = {*proposer_required, "tools"}
    evidence = {
        "expected_provider": "auto",
        "requested_model_id": model,
        "endpoint_fetch_outcome": "model_not_found",
        "endpoint_http_status": 404,
        "response_sha256_kind": "raw_body",
        "response_model_id": None,
        "matching_endpoints": [],
        "operational_match_count": 0,
        "compatible_operational_match_count": 0,
        "proposer_compatible_operational_match_count": 0,
        "aggregator_compatible_operational_match_count": 0,
        "availability_status": "model_endpoint_not_found",
        "proposer_required_parameters": sorted(proposer_required),
        "aggregator_required_parameters": sorted(aggregator_required),
    }

    assert validator.recompute_model_endpoint_availability(
        model=model,
        expected_provider="auto",
        proposer_required_parameters=proposer_required,
        aggregator_required_parameters=aggregator_required,
        evidence=evidence,
    ) == (0, 0, 0, "model_endpoint_not_found")

    evidence["response_model_id"] = model
    with pytest.raises(ValueError, match="not-found evidence"):
        validator.recompute_model_endpoint_availability(
            model=model,
            expected_provider="auto",
            proposer_required_parameters=proposer_required,
            aggregator_required_parameters=aggregator_required,
            evidence=evidence,
        )


def test_get_json_accepts_404_only_when_explicitly_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b'{"error":"model missing"}'

    def handler(request):
        return validator.httpx.Response(404, content=body, request=request)

    transport = validator.httpx.MockTransport(handler)
    with validator.httpx.Client(transport=transport) as client:
        assert validator.get_json(
            client,
            "/api/v1/models/vendor/model/endpoints",
            allow_model_not_found=True,
        ) == (None, validator.hashlib.sha256(body).hexdigest(), 404, "raw_body")

        monkeypatch.setattr(validator.time, "sleep", lambda _seconds: None)
        with pytest.raises(RuntimeError, match="metadata request failed"):
            validator.get_json(client, "/api/v1/models/vendor/model/endpoints")


def test_g1_v3_capacity_and_fixed_routes_are_derived_from_config() -> None:
    experiment = load_draco_experiment_config(validator.DEFAULT_EXPERIMENT_CONFIG_PATH).config
    proposer_required = validator.formal_proposer_required_parameters(
        validator.FORMAL_EXPECTED_ROUTES
    )
    fixed_routes, fixed_parameters = validator.required_fixed_route_specs(
        experiment=experiment,
        groups=("G1",),
        proposer_required_parameters=proposer_required,
    )

    assert validator.required_role_capacity(experiment, ("G1",)) == (10, 3)
    assert fixed_routes == {
        "anthropic/claude-opus-4.8": "anthropic",
        "google/gemini-3.1-pro-preview": "google-ai-studio",
    }
    assert (
        fixed_parameters["anthropic/claude-opus-4.8"]
        == (proposer_required["anthropic/claude-opus-4.8"])
    )


def test_g1_fixed_analyzer_route_uses_effective_policy_and_b0_stays_fixed() -> None:
    experiment = load_draco_experiment_config(
        validator.DEFAULT_EXPERIMENT_CONFIG_PATH,
        inline_overlay_json=json.dumps(
            {
                "router_dynamic_ranking_override": {
                    "task_analyzer": {
                        "model": "z-ai/glm-5.2",
                        "upstream_provider": "z-ai",
                        "max_retries": 1,
                    }
                }
            }
        ),
    ).config
    resolution = validator.effective_ranking_resolution(experiment)
    proposer_required = validator.formal_proposer_required_parameters(
        validator.FORMAL_EXPECTED_ROUTES
    )

    g1_routes, _ = validator.required_fixed_route_specs(
        experiment=experiment,
        groups=("G1",),
        proposer_required_parameters=proposer_required,
        ranking_resolution=resolution,
    )
    b0_routes, _ = validator.required_fixed_route_specs(
        experiment=experiment,
        groups=("B0",),
        proposer_required_parameters=proposer_required,
        ranking_resolution=resolution,
    )

    assert validator.resolved_task_analyzer_policy(resolution) == {
        **validator.resolved_task_analyzer_policy(resolution),
        "model": "z-ai/glm-5.2",
        "upstream_provider": "z-ai",
        "max_retries": 1,
    }
    assert g1_routes["z-ai/glm-5.2"] == "z-ai"
    assert "anthropic/claude-opus-4.8" not in g1_routes
    assert b0_routes == {validator.B0_MODEL: "amazonbedrock"}


def test_formal_route_preflight_rejects_auto_task_analyzer_upstream() -> None:
    experiment = load_draco_experiment_config(
        validator.DEFAULT_EXPERIMENT_CONFIG_PATH,
        inline_overlay_json=json.dumps(
            {
                "router_dynamic_ranking_override": {
                    "task_analyzer": {"upstream_provider": "auto"}
                }
            }
        ),
    ).config
    resolution = validator.effective_ranking_resolution(experiment)

    with pytest.raises(ValueError, match="must be explicitly pinned"):
        validator.resolved_task_analyzer_policy(resolution)


def test_formal_route_preflight_rejects_custom_judge_without_explicit_pin_contract() -> None:
    experiment = load_draco_experiment_config(
        validator.DEFAULT_EXPERIMENT_CONFIG_PATH,
        inline_overlay_json=json.dumps({"judge": {"model": "openai/gpt-5.5"}}),
    ).config
    proposer_required = validator.formal_proposer_required_parameters(
        validator.FORMAL_EXPECTED_ROUTES
    )

    with pytest.raises(ValueError, match="frozen Gemini Judge model"):
        validator.required_fixed_route_specs(
            experiment=experiment,
            groups=("G1",),
            proposer_required_parameters=proposer_required,
            ranking_resolution=validator.effective_ranking_resolution(experiment),
        )


@pytest.mark.parametrize(
    ("backup_count", "required_proposer_count"),
    [(0, 8), (1, 9), (2, 10)],
)
def test_g1_v3_capacity_uses_effective_ranking_backup_override(
    backup_count: int,
    required_proposer_count: int,
) -> None:
    overlay = json.dumps(
        {
            "router_dynamic_ranking_override": {
                "proposer_count": {"backup_count": backup_count}
            }
        }
    )
    experiment = load_draco_experiment_config(
        validator.DEFAULT_EXPERIMENT_CONFIG_PATH,
        inline_overlay_json=overlay,
    ).config
    resolution = validator.effective_ranking_resolution(experiment)

    assert validator.required_role_capacity(
        experiment,
        ("G1",),
        ranking_resolution=resolution,
    ) == (required_proposer_count, 3)

    # The archived field is accepted only as matching loader compatibility.
    # It is deliberately not an authority for the frozen preflight capacity.
    object.__setattr__(experiment.ensemble, "proposer_backup_count", 2 - backup_count)
    assert validator.required_role_capacity(
        experiment,
        ("G1",),
        ranking_resolution=resolution,
    ) == (required_proposer_count, 3)


def test_g1_v3_capacity_accepts_matching_legacy_backup_but_uses_ranking() -> None:
    experiment = load_draco_experiment_config(
        validator.DEFAULT_EXPERIMENT_CONFIG_PATH,
        inline_overlay_json=json.dumps(
            {
                "router_dynamic_ranking_override": {
                    "proposer_count": {"backup_count": 1}
                },
                "ensemble": {"proposer_backup_count": 1},
            }
        ),
    ).config

    assert experiment.ensemble.proposer_backup_count == 1
    assert validator.required_role_capacity(experiment, ("G1",)) == (9, 3)
