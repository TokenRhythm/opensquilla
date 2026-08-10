from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from opensquilla.eval.draco_experiment_config import (
    FORMAL_DRACO_OPENROUTER_BASE_URL,
    DracoRunnerConfig,
    load_draco_experiment_config,
    validate_formal_draco_credential_bindings,
    validate_formal_draco_gateway_credential_binding,
    validate_reference_input,
)
from opensquilla.provider.ranking_router import (
    TASK_ANALYZER_VERSION,
    load_model_registry_snapshot,
    ranking_config_resolution,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "configs/benchmarks/draco_b2_g12.json"


def _frozen_router_fallback_replay_payload(
    task_ids: list[str],
) -> dict[str, object]:
    profile = {"task_type": "analysis"}
    profile_sha256 = hashlib.sha256(
        json.dumps(
            profile,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    source_config = dict(
        ranking_config_resolution()["effective_config"]["task_analyzer"]
    )
    source_config_sha256 = hashlib.sha256(
        json.dumps(
            source_config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    entries = {
        task_id: {
            "task_input_sha256": f"sha256:{'a' * 64}",
            "prompt_sha256": "b" * 64,
            "task_profile_pre_escalation": profile,
            "task_profile_pre_escalation_sha256": profile_sha256,
            "origin_outcome": "deterministic_router_fallback",
            "task_analyzer": {
                "source": "frozen_replay",
                "schema_valid": False,
                "confidence": 0.0,
                "analyzer_version": TASK_ANALYZER_VERSION,
                "provider": source_config["provider"],
                "model": source_config["model"],
                "fallback_reason": "task_analysis_not_supplied",
                "usage": {},
                "normalization_warnings": [],
            },
        }
        for task_id in task_ids
    }
    entries_sha256 = hashlib.sha256(
        json.dumps(
            entries,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema": "opensquilla.draco.frozen-task-analysis/v2",
        "mode": "frozen_replay",
        "source_experiment": "legacy-e0",
        "source_manifest_sha256": "c" * 64,
        "source_results_sha256": "d" * 64,
        "source_task_analyzer_config": source_config,
        "source_task_analyzer_config_sha256": source_config_sha256,
        "entries": entries,
        "entries_sha256": entries_sha256,
    }


def test_default_b2_config_is_g12_derived_quality_first_profile() -> None:
    config = load_draco_experiment_config(DEFAULT_CONFIG).config
    assert config.profile_id == "opensquilla_b2_quality_first_v2"

    assert config.reference.source_commit == ("153e5ff267950b0e285efcdb180cea8724c0471d")
    assert config.reference.group == "G12"
    assert config.reference.profile == "g12_k2_replace_gemini"
    assert config.benchmark_input.sha256 == (
        "1eb4e618c8df8e7f68bded3d2b6f77a541744aa1072eb338835b776183188a8d"
    )
    assert config.benchmark_input.task_count == 10
    assert config.routing.selection_mode == "static_openrouter_b5"
    assert config.routing.skip_single_model_router is True
    assert config.g1_routing is not None
    assert config.router_dynamic_ranking_override == {}
    assert config.g1_routing.profile_id == "draco_g1_formal_registry_all_20260729"
    assert config.g1_routing.selection_mode == "router_dynamic"
    assert config.g1_routing.user_profile_enabled is False
    assert config.g1_routing.candidate_scope == "registry_all"
    assert config.g1_routing.expected_source_registry_snapshot_sha256 == (
        "9f76c7f96e5cb22c05b615f69b71ca633965e5039fbec9673f0a5edf9b45078a"
    )
    assert config.g1_routing.expected_ranking_config_schema_version == "step2-ranking-config-v3"
    assert config.g1_routing.expected_ranking_config_version == "step2-ranking-2026-08-02.2"
    assert config.g1_routing.expected_ranking_config_sha256 == (
        "71be283f94095bc3ced34d39ae9ed58abbaa7e4d273b0a074e7e8a4a6e4b5fc6"
    )
    assert config.g1_routing.expected_proposer_count_max == 5
    assert config.g1_routing.expected_candidate_count is None
    assert config.g1_routing.expected_routes is None
    assert config.g1_routing.expected_routes_sha256 is None
    assert config.g1_routing.live_task_analyzer_chain is not None
    assert [
        (route.provider, route.model, route.upstream_provider, route.max_attempts)
        for route in config.g1_routing.live_task_analyzer_chain
    ] == [
        ("openrouter", "anthropic/claude-opus-4.8", "anthropic", 1),
        ("openrouter", "openai/gpt-5.6-sol", "azure", 1),
        (
            "openrouter",
            "google/gemini-3.1-pro-preview",
            "google-ai-studio",
            1,
        ),
    ]
    analyzer_fallback = config.g1_routing.analyzer_failure_fallback_ensemble
    assert analyzer_fallback is not None
    assert [(route.model, route.upstream_provider) for route in analyzer_fallback.proposers] == [
        ("z-ai/glm-5.2", "z-ai"),
        ("moonshotai/kimi-k2.7-code", "moonshotai"),
        ("qwen/qwen3.8-max", "alibaba"),
        ("deepseek/deepseek-v4-pro", "deepseek"),
    ]
    assert analyzer_fallback.aggregator.model == "z-ai/glm-5.2"
    assert analyzer_fallback.aggregator.upstream_provider == "z-ai"
    assert analyzer_fallback.min_successful_proposers == 1
    assert analyzer_fallback.complete_proposers_only is True
    assert analyzer_fallback.aggregator_max_recovery_actions == 1
    assert [member.model for member in config.ensemble.proposers] == [
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        "qwen/qwen3.7-max",
    ]
    assert config.ensemble.aggregator.model == "z-ai/glm-5.2"
    assert [member.thinking for member in config.ensemble.proposers] == [
        "xhigh",
        "xhigh",
        "high",
        "high",
    ]
    assert config.ensemble.aggregator.thinking == "xhigh"
    assert config.generation.require_highest_thinking is True
    assert config.generation.thinking_budget_tokens == 50_000
    assert config.generation.model_thinking_levels == {
        "anthropic/claude-fable-5": "max",
        "anthropic/claude-opus-4.8": "max",
        "anthropic/claude-sonnet-5": "max",
        "deepseek/deepseek-v4-flash": "xhigh",
        "deepseek/deepseek-v4-pro": "xhigh",
        "google/gemini-3.1-pro-preview": "high",
        "google/gemini-3-flash-preview": "high",
        "google/gemini-3.5-flash": "high",
        "kwaipilot/kat-coder-air-v2.5": "off",
        "kwaipilot/kat-coder-pro-v2.5": "off",
        "meta-llama/llama-4-scout": "off",
        "minimax/minimax-m3": "high",
        "mistralai/mistral-medium-3-5": "high",
        "moonshotai/kimi-k2.7-code": "high",
        "openai/gpt-5.5": "xhigh",
        "openai/gpt-5.5-pro": "xhigh",
        "openai/gpt-5.6-sol": "max",
        "poolside/laguna-xs-2.1": "high",
        "qwen/qwen3.7-max": "high",
        "qwen/qwen3.7-plus": "high",
        "qwen/qwen3.8-max": "high",
        "sakana/fugu-ultra": "max",
        "tencent/hy3": "high",
        "x-ai/grok-4.5": "high",
        "z-ai/glm-5.2": "xhigh",
    }
    assert all(member.max_tokens == 16_384 for member in config.ensemble.proposers)
    assert all(member.temperature == 0.0 for member in config.ensemble.proposers)
    assert config.ensemble.aggregator.max_tokens == 16_384
    assert config.ensemble.aggregator.temperature == 0.0
    assert config.ensemble.min_successful_proposers == 2
    assert config.ensemble.all_failed_policy == "fallback_single"
    assert config.ensemble.candidate_max_chars == 24_000
    assert config.ensemble.shuffle_candidates is False
    assert config.ensemble.candidate_order_seed is None
    assert config.ensemble.record_candidates is True
    assert config.ensemble.proposer_tools is False
    assert config.ensemble.aggregator_tools is True
    assert config.ensemble.proposer_backup_count == 0
    assert "proposer_backup_count" not in config.ensemble.model_dump(mode="json")
    assert config.ensemble.proposer_recovery_max_additional_calls == 3
    assert config.ensemble.proposer_max_tokens_cap == 65_536
    assert config.ensemble.proposer_visible_answer_reserve_tokens == 4_096
    assert config.ensemble.wait_for_all_proposers is True
    assert config.ensemble.quorum_grace_seconds == 0.0
    assert config.tools.web_search.provider == "brave"
    assert config.tools.sandbox_enabled is False
    assert config.tools.web_search.max_results == 5
    assert config.tools.web_fetch.max_content_tokens == 50_000
    assert config.timeouts.proposer_seconds == pytest.approx(907.5)
    assert config.timeouts.aggregator_seconds == pytest.approx(2662.5)
    assert config.timeouts.task_seconds == 10800.0
    assert config.timeouts.task_margin_seconds == 30.0
    assert config.runner.mode == "agent_loop"
    assert config.runner.agent_max_iterations == 20
    assert config.runner.concurrency == 2
    assert config.runner.deadline_wrapup_margin_seconds == 300
    assert config.runner.deadline_wrapup_disable_tools is True
    assert config.runner.deadline_thinking_off_margin_seconds == 0
    assert config.runner.max_iterations_includes_finalization is False
    assert config.runner.retrieval_loop_finalization_threshold == 0
    assert config.runner.finalization_aggregator_only is False
    assert config.runner.finalization_disable_thinking is False
    assert config.generation.max_attempts == 3
    assert config.generation.retry_backoff_seconds == 2.0
    assert config.judge.model == "google/gemini-3.1-pro-preview"
    assert config.judge.repeats == 3
    assert config.judge.concurrency == 6
    assert config.judge.max_attempts == 3


@pytest.mark.parametrize("seed", [0, (1 << 64) - 1])
def test_draco_ensemble_accepts_uint64_candidate_order_seed(seed: int) -> None:
    config = load_draco_experiment_config(
        DEFAULT_CONFIG,
        inline_overlay_json=json.dumps({"ensemble": {"candidate_order_seed": seed}}),
    ).config

    assert config.ensemble.candidate_order_seed == seed


@pytest.mark.parametrize("seed", [True, -1, 1 << 64])
def test_draco_ensemble_rejects_invalid_candidate_order_seed(seed: object) -> None:
    with pytest.raises(ValidationError, match="candidate_order_seed"):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_overlay_json=json.dumps({"ensemble": {"candidate_order_seed": seed}}),
        )


def test_draco_ensemble_rejects_invalid_aggregator_output_budget() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 2"):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=["ensemble.aggregator_max_tokens_cap=1"],
        )

    with pytest.raises(
        ValidationError,
        match="aggregator_visible_answer_reserve_tokens must be smaller",
    ):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=["ensemble.aggregator_max_tokens_cap=8192"],
        )


def test_draco_ensemble_rejects_invalid_proposer_recovery_budget() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 3"):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=["ensemble.proposer_recovery_max_additional_calls=4"],
        )

    with pytest.raises(
        ValidationError,
        match="proposer_visible_answer_reserve_tokens must be smaller",
    ):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=["ensemble.proposer_max_tokens_cap=4096"],
        )


@pytest.mark.parametrize(
    ("path", "secret_value"),
    [
        ("ensemble.proposers.0.api_key_env", "sk-live-secret"),
        ("ensemble.aggregator.api_key_env", "actual secret"),
        ("tools.web_search.api_key_env", "brave-api-key-value"),
    ],
)
def test_api_key_env_fields_accept_only_environment_variable_names(
    path: str,
    secret_value: str,
) -> None:
    with pytest.raises(ValidationError) as error:
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=[f"{path}={json.dumps(secret_value)}"],
        )

    assert secret_value not in str(error.value)


def test_api_key_env_fields_accept_lowercase_environment_variable_names() -> None:
    bundle = load_draco_experiment_config(
        DEFAULT_CONFIG,
        inline_sets=[
            'tools.web_search.api_key_env="brave_search_key"',
            'ensemble.proposers.0.api_key_env="openrouter_key"',
        ],
    )

    assert bundle.config.tools.web_search.api_key_env == "brave_search_key"
    assert bundle.config.ensemble.proposers[0].api_key_env == "openrouter_key"


@pytest.mark.parametrize(
    ("inline_sets", "message"),
    [
        (
            ['ensemble.proposers.0.provider="anthropic"'],
            r"ensemble\.proposers\.0\.provider=openrouter",
        ),
        (
            ['ensemble.proposers.0.base_url="https://attacker.invalid/v1"'],
            r"ensemble\.proposers\.0\.base_url",
        ),
        (
            ['ensemble.proposers.0.api_key_env="UNRELATED_AMBIENT_SECRET"'],
            r"ensemble\.proposers\.0\.api_key_env",
        ),
        (
            ['ensemble.aggregator.api_key_env="UNRELATED_AMBIENT_SECRET"'],
            r"ensemble\.aggregator\.api_key_env",
        ),
        (
            ['tools.web_search.api_key_env="UNRELATED_AMBIENT_SECRET"'],
            r"tools\.web_search\.api_key_env=BRAVE_SEARCH_API_KEY",
        ),
        (
            [
                'tools.web_search.provider="duckduckgo"',
                'tools.web_search.api_key_env="BRAVE_SEARCH_API_KEY"',
            ],
            r"tools\.web_search\.api_key_env=an empty api_key_env",
        ),
    ],
)
def test_formal_credential_bindings_reject_redirects_without_echoing_values(
    inline_sets: list[str],
    message: str,
) -> None:
    experiment = load_draco_experiment_config(
        DEFAULT_CONFIG,
        inline_sets=inline_sets,
    ).config

    with pytest.raises(ValueError, match=message) as error:
        validate_formal_draco_credential_bindings(experiment)

    assert "attacker.invalid" not in str(error.value)
    assert "UNRELATED_AMBIENT_SECRET" not in str(error.value)


@pytest.mark.parametrize(
    "inline_sets",
    [
        [],
        [
            f'ensemble.proposers.0.base_url="{FORMAL_DRACO_OPENROUTER_BASE_URL}/"',
            'ensemble.proposers.0.api_key_env=""',
        ],
        [
            'tools.web_search.provider="duckduckgo"',
            'tools.web_search.api_key_env=""',
        ],
    ],
    ids=["frozen-default", "official-url-and-inherited-key", "keyless-search"],
)
def test_formal_credential_bindings_accept_only_frozen_safe_alternatives(
    inline_sets: list[str],
) -> None:
    experiment = load_draco_experiment_config(
        DEFAULT_CONFIG,
        inline_sets=inline_sets,
    ).config

    validate_formal_draco_credential_bindings(experiment)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {
                "provider": "anthropic",
                "base_url": FORMAL_DRACO_OPENROUTER_BASE_URL,
                "api_key_env": "OPENROUTER_API_KEY",
            },
            r"config\.llm\.provider=openrouter",
        ),
        (
            {
                "provider": "openrouter",
                "base_url": "https://attacker.invalid/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            },
            r"config\.llm\.base_url",
        ),
        (
            {
                "provider": "openrouter",
                "base_url": FORMAL_DRACO_OPENROUTER_BASE_URL,
                "api_key_env": "UNRELATED_AMBIENT_SECRET",
            },
            r"config\.llm\.api_key_env",
        ),
    ],
    ids=["provider", "base-url", "api-key-env"],
)
def test_formal_gateway_credential_binding_rejects_redirects_without_echoing_values(
    kwargs: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message) as error:
        validate_formal_draco_gateway_credential_binding(**kwargs)

    assert "attacker.invalid" not in str(error.value)
    assert "UNRELATED_AMBIENT_SECRET" not in str(error.value)


@pytest.mark.parametrize("api_key_env", ["", "OPENROUTER_API_KEY"])
def test_formal_gateway_credential_binding_accepts_only_approved_key_sources(
    api_key_env: str,
) -> None:
    validate_formal_draco_gateway_credential_binding(
        provider="openrouter",
        base_url=FORMAL_DRACO_OPENROUTER_BASE_URL,
        api_key_env=api_key_env,
    )


def test_runner_finalization_fields_default_off_for_legacy_configs() -> None:
    config = DracoRunnerConfig(
        mode="agent_loop",
        agent_max_iterations=12,
        concurrency=2,
    )

    assert config.deadline_wrapup_margin_seconds == 0
    assert config.deadline_wrapup_disable_tools is False
    assert config.deadline_thinking_off_margin_seconds == 0
    assert config.max_iterations_includes_finalization is False
    assert config.retrieval_loop_finalization_threshold == 0
    assert config.finalization_aggregator_only is False
    assert config.finalization_disable_thinking is False


def test_override_files_and_inline_paths_apply_in_documented_order(tmp_path: Path) -> None:
    override_path = tmp_path / "override.json"
    override_path.write_text(
        json.dumps(
            {
                "runner": {"concurrency": 3},
                "ensemble": {"candidate_max_chars": 32000},
            }
        ),
        encoding="utf-8",
    )

    bundle = load_draco_experiment_config(
        DEFAULT_CONFIG,
        override_paths=[override_path],
        inline_sets=[
            "runner.concurrency=4",
            "ensemble.proposers.0.max_tokens=8192",
        ],
    )

    assert bundle.config.runner.concurrency == 4
    assert bundle.config.ensemble.candidate_max_chars == 32_000
    assert bundle.config.ensemble.proposers[0].max_tokens == 8192
    assert bundle.provenance()["overrides"][0]["path"] == str(override_path.resolve())


def test_file_provenance_is_frozen_from_the_bytes_loaded_through_symlinks(
    tmp_path: Path,
) -> None:
    base_bytes = DEFAULT_CONFIG.read_bytes()
    base_target = tmp_path / "base-target.json"
    base_target.write_bytes(base_bytes)
    base_link = tmp_path / "base-link.json"
    base_link.symlink_to(base_target)
    override_bytes = b'{\n  "runner": {"concurrency": 3}\n}\n'
    override_target = tmp_path / "override-target.json"
    override_target.write_bytes(override_bytes)
    override_link = tmp_path / "override-link.json"
    override_link.symlink_to(override_target)

    bundle = load_draco_experiment_config(
        base_link,
        override_paths=[override_link],
    )
    frozen = bundle.provenance()

    assert frozen["base"] == {
        "path": str(base_target.resolve()),
        "sha256": hashlib.sha256(base_bytes).hexdigest(),
    }
    assert frozen["overrides"] == [
        {
            "path": str(override_target.resolve()),
            "sha256": hashlib.sha256(override_bytes).hexdigest(),
        }
    ]

    replacement_base = tmp_path / "replacement-base.json"
    replacement_base.write_bytes(DEFAULT_CONFIG.read_bytes())
    replacement_override = tmp_path / "replacement-override.json"
    replacement_override.write_text('{"runner":{"concurrency":9}}\n', encoding="utf-8")
    base_link.unlink()
    base_link.symlink_to(replacement_base)
    override_link.unlink()
    override_link.symlink_to(replacement_override)
    base_target.write_text('{"changed":true}\n', encoding="utf-8")
    override_target.write_text('{"runner":{"concurrency":8}}\n', encoding="utf-8")

    assert bundle.provenance() == frozen


def test_inline_json_overlay_precedes_dotted_sets_and_records_canonical_sha(
    tmp_path: Path,
) -> None:
    override_path = tmp_path / "override.json"
    override_path.write_text(
        json.dumps({"runner": {"concurrency": 3}}),
        encoding="utf-8",
    )
    inline_overlay = {
        "runner": {"concurrency": 4},
        "ensemble": {"candidate_max_chars": 36_000},
        "reference": {"repository": "overlay-private-marker"},
        "router_dynamic_ranking_override": {"penalties": {"task_cost_weights": {"high": 0.5}}},
    }
    dotted_marker = "dotted-private-marker"

    bundle = load_draco_experiment_config(
        DEFAULT_CONFIG,
        override_paths=[override_path],
        inline_overlay_json=json.dumps(inline_overlay),
        inline_sets=[
            "runner.concurrency=5",
            f"reference.run_directory={json.dumps(dotted_marker)}",
        ],
    )

    expected_sha = hashlib.sha256(
        json.dumps(
            inline_overlay,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert bundle.config.runner.concurrency == 5
    assert bundle.config.ensemble.candidate_max_chars == 36_000
    assert bundle.config.reference.repository == "overlay-private-marker"
    assert bundle.config.reference.run_directory == dotted_marker
    assert bundle.config.router_dynamic_ranking_override == {
        "penalties": {"task_cost_weights": {"high": 0.5}}
    }
    assert bundle.inline_overlay_document == inline_overlay
    assert bundle.inline_overlay_sha256 == expected_sha
    assert bundle.provenance()["precedence"] == [
        "base_json",
        "override_json_in_cli_order",
        "inline_json_object",
        "inline_path_overrides_in_cli_order",
    ]
    assert bundle.provenance()["inline_overlay"] == {
        "present": True,
        "field_paths": [
            "ensemble.candidate_max_chars",
            "reference.repository",
            "router_dynamic_ranking_override.penalties.task_cost_weights.high",
            "runner.concurrency",
        ],
    }
    assert bundle.provenance()["inline_overrides"] == {
        "count": 2,
        "paths": ["runner.concurrency", "reference.run_directory"],
    }
    dotted_hash = hashlib.sha256(
        json.dumps(
            dotted_marker,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    serialized_provenance = json.dumps(bundle.provenance(), ensure_ascii=False)
    assert "overlay-private-marker" not in serialized_provenance
    assert dotted_marker not in serialized_provenance
    assert expected_sha not in serialized_provenance
    assert dotted_hash not in serialized_provenance


def test_public_provenance_omits_inline_values_and_per_value_hashes() -> None:
    overlay_marker = "public-provenance-overlay-marker"
    dotted_marker = "public-provenance-dotted-marker"
    bundle = load_draco_experiment_config(
        DEFAULT_CONFIG,
        inline_overlay_json=json.dumps({"reference": {"repository": overlay_marker}}),
        inline_sets=[f"reference.run_directory={json.dumps(dotted_marker)}"],
    )

    provenance = bundle.provenance()
    serialized = json.dumps(provenance, ensure_ascii=False)
    assert provenance["inline_overlay"] == {
        "present": True,
        "field_paths": ["reference.repository"],
    }
    assert provenance["inline_overrides"] == {
        "count": 1,
        "paths": ["reference.run_directory"],
    }
    assert overlay_marker not in serialized
    assert dotted_marker not in serialized
    assert bundle.inline_overlay_sha256 not in serialized
    dotted_hash = hashlib.sha256(
        json.dumps(
            dotted_marker,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert dotted_hash not in serialized


@pytest.mark.parametrize("raw", ["[]", "null", "42", '"value"'])
def test_inline_json_overlay_requires_an_object(raw: str) -> None:
    with pytest.raises(ValueError, match="must contain a JSON object"):
        load_draco_experiment_config(DEFAULT_CONFIG, inline_overlay_json=raw)


def test_inline_json_overlay_rejects_invalid_json_without_echoing_value() -> None:
    raw = '{"private_token":"do-not-repeat"'
    with pytest.raises(ValueError, match="must contain valid JSON") as exc_info:
        load_draco_experiment_config(DEFAULT_CONFIG, inline_overlay_json=raw)
    assert "do-not-repeat" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"api_key": "do-not-record"}, "secret-like field"),
        (
            {"penalties": {"not_a_real_weight": 0.5}},
            "unknown or missing keys",
        ),
    ],
)
def test_ranking_override_fails_during_config_load_before_any_artifact(
    override: dict[str, object],
    message: str,
) -> None:
    raw = json.dumps({"router_dynamic_ranking_override": override})

    with pytest.raises(ValidationError, match=message) as exc_info:
        load_draco_experiment_config(DEFAULT_CONFIG, inline_overlay_json=raw)

    assert "do-not-record" not in str(exc_info.value)


def test_unknown_override_path_fails_instead_of_silently_missing() -> None:
    with pytest.raises(ValueError, match="unknown experiment config override path"):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=["ensemble.typo_timeout=12"],
        )


def test_highest_thinking_invariant_rejects_accidental_downgrade() -> None:
    with pytest.raises(ValidationError, match="highest configured setting"):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=["ensemble.proposers.2.thinking=medium"],
        )


def test_formal_g1_thinking_map_uses_each_registry_models_highest_level() -> None:
    config = load_draco_experiment_config(DEFAULT_CONFIG).config
    assert config.g1_routing is not None
    registry = load_model_registry_snapshot()
    highest_by_model = {
        str(row["registry_facts"]["model_id"]): str(
            row["registry_facts"]["supported_thinking_levels"][0]
        )
        for row in registry["models"]
    }

    configured_registry_models = set(config.generation.model_thinking_levels) & set(
        highest_by_model
    )
    assert {
        model: config.generation.model_thinking_levels[model]
        for model in configured_registry_models
    } == {model: highest_by_model[model] for model in configured_registry_models}


def test_g1_registry_all_requires_no_explicit_route_fields(tmp_path: Path) -> None:
    config = load_draco_experiment_config(DEFAULT_CONFIG).config
    assert config.g1_routing is not None
    assert config.g1_routing.candidate_scope == "registry_all"

    for index, partial_contract in enumerate(
        (
            {"expected_candidate_count": 20},
            {"expected_routes": {"deepseek/deepseek-v4-pro": "deepseek"}},
            {"expected_routes_sha256": "0" * 64},
        )
    ):
        payload = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        payload["g1_routing"].update(partial_contract)
        partial_config = tmp_path / f"partial-{index}.json"
        partial_config.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValidationError, match="must be specified together"):
            load_draco_experiment_config(partial_config)


def test_live_task_analyzer_chain_is_strictly_nonempty_and_unique() -> None:
    with pytest.raises(ValidationError):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_overlay_json=json.dumps({"g1_routing": {"live_task_analyzer_chain": []}}),
        )

    with pytest.raises(ValidationError, match="must not contain duplicate routes"):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=['g1_routing.live_task_analyzer_chain.1.model="anthropic/claude-opus-4.8"'],
        )


@pytest.mark.parametrize(
    "override",
    [
        "g1_routing.live_task_analyzer_chain.0.max_attempts=2",
        'g1_routing.live_task_analyzer_chain.0.provider="anthropic"',
        "g1_routing.analyzer_failure_fallback_ensemble.min_successful_proposers=2",
        "g1_routing.analyzer_failure_fallback_ensemble.complete_proposers_only=false",
        "g1_routing.analyzer_failure_fallback_ensemble.aggregator_max_recovery_actions=2",
    ],
)
def test_live_analyzer_and_failure_fallback_literals_fail_closed(
    override: str,
) -> None:
    with pytest.raises(ValidationError):
        load_draco_experiment_config(DEFAULT_CONFIG, inline_sets=[override])


def test_analyzer_failure_fallback_rejects_duplicate_proposer_routes() -> None:
    with pytest.raises(ValidationError, match="must not contain duplicate routes"):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=[
                'g1_routing.analyzer_failure_fallback_ensemble.proposers.1.model="z-ai/glm-5.2"'
            ],
        )


@pytest.mark.parametrize(
    "missing_field",
    [
        "live_task_analyzer_chain",
        "analyzer_failure_fallback_ensemble",
    ],
)
def test_live_analyzer_chain_and_failure_fallback_must_be_configured_together(
    tmp_path: Path,
    missing_field: str,
) -> None:
    payload = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    payload["g1_routing"].pop(missing_field)
    invalid_config = tmp_path / f"missing-{missing_field}.json"
    invalid_config.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="must be configured together"):
        load_draco_experiment_config(invalid_config)


def test_historical_g1_configs_may_omit_new_live_analyzer_fields(
    tmp_path: Path,
) -> None:
    payload = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    payload["g1_routing"].pop("live_task_analyzer_chain")
    payload["g1_routing"].pop("analyzer_failure_fallback_ensemble")
    historical_config = tmp_path / "historical.json"
    historical_config.write_text(json.dumps(payload), encoding="utf-8")

    config = load_draco_experiment_config(historical_config).config

    assert config.g1_routing is not None
    assert config.g1_routing.live_task_analyzer_chain is None
    assert config.g1_routing.analyzer_failure_fallback_ensemble is None


def test_frozen_replay_may_coexist_with_inactive_live_analyzer_pair(
    tmp_path: Path,
) -> None:
    payload = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    payload["g1_routing"]["task_analysis_execution"] = (
        _frozen_router_fallback_replay_payload(
            payload["benchmark_input"]["task_ids"]
        )
    )
    coexist_config = tmp_path / "replay-with-inactive-live-pair.json"
    coexist_config.write_text(json.dumps(payload), encoding="utf-8")

    config = load_draco_experiment_config(coexist_config).config

    assert config.g1_routing is not None
    assert config.g1_routing.task_analysis_execution is not None
    assert config.g1_routing.live_task_analyzer_chain is not None
    assert config.g1_routing.analyzer_failure_fallback_ensemble is not None


def test_g1_explicit_routes_remain_hash_and_count_fail_closed(tmp_path: Path) -> None:
    payload = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    routes = {
        "deepseek/deepseek-v4-pro": "deepseek",
        "z-ai/glm-5.2": "z-ai",
    }
    routes_hash = hashlib.sha256(
        json.dumps(
            routes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload["g1_routing"].update(
        {
            "expected_proposer_count_max": len(routes),
            "expected_candidate_count": len(routes),
            "expected_routes": routes,
            "expected_routes_sha256": routes_hash,
        }
    )
    explicit_config = tmp_path / "explicit.json"
    explicit_config.write_text(json.dumps(payload), encoding="utf-8")

    config = load_draco_experiment_config(explicit_config).config
    assert config.g1_routing is not None
    assert config.g1_routing.candidate_scope == "exact_routes"

    with pytest.raises(ValidationError, match="expected_routes_sha256"):
        load_draco_experiment_config(
            explicit_config,
            inline_sets=[f"g1_routing.expected_routes_sha256={'0' * 64}"],
        )
    with pytest.raises(ValidationError, match="expected_candidate_count"):
        load_draco_experiment_config(
            explicit_config,
            inline_sets=["g1_routing.expected_candidate_count=3"],
        )


@pytest.mark.parametrize(
    "override",
    [
        "g1_routing.user_profile_enabled=true",
        "tools.sandbox_enabled=true",
    ],
)
def test_formal_runtime_false_flags_cannot_be_overridden(override: str) -> None:
    with pytest.raises(ValidationError):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=[override],
        )


def test_aggregator_rejects_unsupported_multiple_samples() -> None:
    with pytest.raises(ValidationError, match="ensemble.aggregator.k must be 1"):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=["ensemble.aggregator.k=2"],
        )


def test_reference_input_validation_checks_bytes_count_ids_and_order(tmp_path: Path) -> None:
    path = tmp_path / "mini.jsonl"
    path.write_text('{"id":"task-a","prompt":"hello"}\n', encoding="utf-8")
    default = load_draco_experiment_config(DEFAULT_CONFIG).config.benchmark_input
    input_config = default.model_copy(
        update={
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "task_count": 1,
            "task_ids": ["task-a"],
        }
    )

    trace = validate_reference_input(
        path,
        task_ids=["task-a"],
        config=input_config,
    )

    assert trace["status"] == "matched"
    with pytest.raises(ValueError, match="task_ids_or_order"):
        validate_reference_input(path, task_ids=["task-b"], config=input_config)
