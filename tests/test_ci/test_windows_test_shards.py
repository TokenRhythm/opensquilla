from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SHARD_SCRIPT = Path(".github/scripts/windows_test_shards.py")
SHARD_MODULE: dict[str, Any] = runpy.run_path(
    SHARD_SCRIPT.as_posix(), run_name="windows_test_shards"
)
SHARD_NAMES: tuple[str, ...] = SHARD_MODULE["SHARD_NAMES"]
discover_test_files = SHARD_MODULE["discover_test_files"]
files_for_shard = SHARD_MODULE["files_for_shard"]
historical_test_weights = SHARD_MODULE["historical_test_weights"]
matching_specialized_shards = SHARD_MODULE["matching_specialized_shards"]
assignment_governance = SHARD_MODULE["assignment_governance"]
assignment_governance_summary = SHARD_MODULE["assignment_governance_summary"]
assignment_snapshot_fingerprint = SHARD_MODULE["assignment_snapshot_fingerprint"]
shard_for_test = SHARD_MODULE["shard_for_test"]
shard_weight_summary = SHARD_MODULE["shard_weight_summary"]
validate_assignment_payload = SHARD_MODULE["validate_assignment_payload"]

OFFLINE_MARKER_EXCLUSIONS = {
    "tests/functional/test_agent_synthetic_golden.py",
    "tests/functional/test_gateway_llm_e2e.py",
    "tests/functional/test_live_agent_context_boundary_e2e.py",
    "tests/functional/test_live_channel_telegram_smoke.py",
    "tests/functional/test_live_openrouter_compaction.py",
    "tests/functional/test_llm_smoke.py",
    "tests/functional/test_webui_browser_e2e.py",
    "tests/integration/cli/tui_real_terminal/test_architecture_prompt.py",
    "tests/integration/cli/tui_real_terminal/test_completion_menu.py",
    "tests/integration/cli/tui_real_terminal/test_complex_ui_state.py",
    "tests/integration/cli/tui_real_terminal/test_exit_restoration.py",
    "tests/integration/cli/tui_real_terminal/test_framebuffer.py",
    "tests/integration/cli/tui_real_terminal/test_framebuffer_recovery.py",
    "tests/integration/cli/tui_real_terminal/test_gateway_empty_bootstrap_startup.py",
    "tests/integration/cli/tui_real_terminal/test_idle_resize_round_trip.py",
    "tests/integration/cli/tui_real_terminal/test_launch_input_loop.py",
    "tests/integration/cli/tui_real_terminal/test_live_opentui_real_cli.py",
    "tests/integration/cli/tui_real_terminal/test_long_streaming.py",
    "tests/integration/cli/tui_real_terminal/test_mouse_scroll_stability.py",
    "tests/integration/cli/tui_real_terminal/test_packaged_gateway_e2e.py",
    "tests/integration/cli/tui_real_terminal/test_source_gateway_bootstrap_startup.py",
    "tests/integration/cli/tui_real_terminal/test_terminal_changes.py",
    "tests/live/test_search_api_matrix_live.py",
    "tests/live/test_skill_hub_canary_live.py",
    "tests/live/test_multi_provider_matrix_live.py",
    "tests/live/test_search_retrieval_live.py",
    "tests/live/test_tokenrhythm_catalog_live.py",
    "tests/live/test_web_search_agent_e2e.py",
    "tests/test_skills/test_meta_router_live.py",
    "tests/test_skills/test_meta_skill_creator_smoke_live.py",
}
RECENTLY_ADDED_ACTIVE_TESTS = {
    "tests/test_scripts/test_bench_skill_integrity.py",
    "tests/test_skills_hash_consumers.py",
    "tests/test_skills/test_loader_turn_snapshot.py",
    "tests/test_skills_tree.py",
    "tests/test_recovery/test_config_recovery.py",
    "tests/unit/cli/tui/test_keys_cheatsheet.py",
    "tests/unit/cli/tui/test_opentui_prefs.py",
    "tests/test_cli/test_gateway_client_steer.py",
    "tests/test_cli/test_skills_search_cmd.py",
    "tests/test_channels/test_admission_reason_persistence.py",
    "tests/test_channels/test_channel_admission.py",
    "tests/test_channels/test_channel_certification.py",
    "tests/test_channels/test_channel_delivery_store.py",
    "tests/test_channels/test_channel_mock_certification.py",
    "tests/test_channels/test_channel_pairing.py",
    "tests/test_channels/test_discord_gateway_lifecycle.py",
    "tests/test_channels/test_length_declaration_conformance.py",
    "tests/test_channels/test_manager_status_telemetry.py",
    "tests/test_channels/test_matrix_contract_repairs.py",
    "tests/test_channels/test_pairing_store_bounds.py",
    "tests/test_channels/test_qq_lifecycle.py",
    "tests/test_channels/test_send_error_classification.py",
    "tests/test_channels/test_util_length.py",
    "tests/test_gateway/test_channel_dispatch_chunking.py",
    "tests/test_gateway/test_channel_reply_delivery_guard.py",
    "tests/test_gateway/test_channel_session_and_busy_policy.py",
    "tests/test_gateway/test_capability_runtime.py",
    "tests/test_gateway/test_meta_setup_launch_e2e.py",
    "tests/test_gateway/test_rpc_meta_setup.py",
    "tests/test_artifact_validation.py",
    "tests/test_ci/test_dockerignore_context.py",
    "tests/test_ci/test_migration_v022.py",
    "tests/test_ci/test_session_storage_connection_contract.py",
    "tests/test_desktop/test_onboarding_main_process_flow_contract.py",
    "tests/test_channels/test_stream_terminal_routing.py",
    "tests/test_engine/test_agent_canonical_text_contract.py",
    "tests/test_engine/test_agent_transactional_tool_publication.py",
    "tests/test_engine/test_attachment_aware_routing.py",
    "tests/test_engine/test_done_text_snapshot_consumers.py",
    "tests/test_engine/test_provider_request_correlation.py",
    "tests/test_engine/test_provider_activity.py",
    "tests/test_engine/test_long_task_backend_boundaries.py",
    "tests/test_engine/test_route_plan.py",
    "tests/test_engine/test_stream_repetition_guard.py",
    "tests/test_engine/turn_runner/test_canonical_text_contract.py",
    "tests/test_engine/turn_runner/test_turn_identity_finalizer.py",
    "tests/test_gateway/test_api_chat.py",
    "tests/test_gateway/test_channel_turn_ingress.py",
    "tests/test_gateway/test_config_persist_corruption.py",
    "tests/test_gateway/test_config_profile_paths.py",
    "tests/test_gateway/test_cron_result_payload.py",
    "tests/test_gateway/test_memory_repair_storage_gate.py",
    "tests/test_gateway/test_p1a_exact_abort_contract.py",
    "tests/test_gateway/test_rpc_ingress_validation.py",
    "tests/test_gateway/test_rpc_llm_profiles.py",
    "tests/test_gateway/test_rpc_capability_reset.py",
    "tests/test_gateway/test_rpc_provider_credential_clear.py",
    "tests/test_gateway/test_rpc_migration.py",
    "tests/test_gateway/test_rpc_memory_import.py",
    "tests/test_gateway/test_rpc_storage_busy.py",
    "tests/test_gateway/test_steer_restart_recovery.py",
    "tests/test_gateway/test_task_runtime_reservations.py",
    "tests/test_gateway/test_turn_ingress_fork.py",
    "tests/test_gateway/test_turn_ingress_intents.py",
    "tests/test_gateway/test_turn_ingress_rpc.py",
    "tests/test_memory/test_store_vec_extension_cleanup.py",
    "tests/test_memory/test_profile_import.py",
    "tests/test_migration/test_import_receipt_verification_cli.py",
    "tests/test_migration/test_source_snapshot_windows.py",
    "tests/test_migrations/test_migrator_diagnostics.py",
    "tests/test_migrations/test_v020_turn_ingress_receipts.py",
    "tests/test_observability/test_usage_telemetry.py",
    "tests/test_migrations/test_v023_router_deployment_telemetry.py",
    "tests/test_migrations/test_v024_usage_native_billing_receipts.py",
    "tests/test_migrations/test_v030_meta_control_intents.py",
    "tests/test_migrations/test_v031_meta_launch_drafts.py",
    "tests/test_migrations/test_v032_meta_launch_discard_tombstones.py",
    "tests/test_live_mixed_provider_gateway.py",
    "tests/test_live_long_task_case_driver.py",
    "tests/test_live_long_task_release_gate.py",
    "tests/test_live_multi_provider_matrix.py",
    "tests/test_live_tokenrhythm_billing_audit.py",
    "tests/test_onboarding/test_llm_profiles.py",
    "tests/test_onboarding/test_image_generation_model_discovery.py",
    "tests/test_packaging/test_webui_build_contract.py",
    "tests/test_provider/test_error_secret_boundary.py",
    "tests/test_provider_candidate_artifact.py",
    "tests/test_provider_correlation_context.py",
    "tests/test_provider_native_response_guards.py",
    "tests/test_provider_terminal_evidence.py",
    "tests/test_provider_terminal_evidence_anthropic_codex.py",
    "tests/test_provider_text_tool_normalization.py",
    "tests/test_provider_tokenrhythm_correlation.py",
    "tests/test_long_task_fault_proxy.py",
    "tests/test_recovery/test_atomic_and_locking.py",
    "tests/test_recovery/test_cleanup.py",
    "tests/test_recovery/test_engine.py",
    "tests/test_recovery/test_historical_upgrades.py",
    "tests/test_recovery/test_recovery_cmd.py",
    "tests/test_recovery/test_restore.py",
    "tests/test_recovery/test_runtime_writer_guard.py",
    "tests/test_recovery/test_settings_transaction.py",
    "tests/test_recovery/test_transaction.py",
    "tests/test_scripts/test_release_channel_manifest.py",
    "tests/test_scripts/test_verify_webui_artifact.py",
    "tests/test_scheduler/test_job_lifecycle.py",
    "tests/test_session/test_storage_session_list_pagination.py",
    "tests/test_session/test_storage_transactions.py",
    "tests/test_session/test_meta_launch_drafts.py",
    "tests/test_session/test_pending_chat_inputs.py",
    "tests/test_session/test_turn_acceptance_storage.py",
    "tests/test_session/test_assistant_message_identity.py",
    "tests/test_skills/test_hub_deps_subprocess.py",
    "tests/test_skills/test_managed_toolchains.py",
    "tests/test_skills/test_meta_readiness.py",
    "tests/test_skills/test_meta_short_drama_delivery_audit.py",
    "tests/test_skills/test_paper_citation_integrity_gate.py",
    "tests/test_skills/test_paper_delivery_summary.py",
    "tests/test_skills/test_paper_latex_sanitizer.py",
    "tests/test_skills/test_paper_length_gate.py",
    "tests/test_skills/test_paper_quality_gate.py",
    "tests/test_skills/test_paper_refbib_metadata.py",
    "tests/test_skills/test_paper_source_readiness_gate.py",
    "tests/test_skills/test_short_drama_review_normalizer.py",
    "tests/test_skills/test_subtitle_burner.py",
    "tests/test_skills/test_title_card_image.py",
    "tests/test_skills/test_toolchain_runtime_integration.py",
    "tests/test_skills/test_toolchain_state_scope.py",
    "tests/test_tools/test_shell_managed_toolchains.py",
    "tests/test_envelope_policy_deny_cap.py",
    "tests/test_request_proof_levers.py",
    "tests/test_toolcomp_matcher_levers.py",
    "tests/test_toolcomp_matcher_safety.py",
    "tests/test_toolcomp_reducer_semantics.py",
    "tests/test_engine/test_agent_patch_hygiene_block.py",
    "tests/test_engine/test_agent_submit_review.py",
    "tests/test_engine/test_agent_verify_mirror_and_variant_challenge.py",
    "tests/test_engine/test_endgame_directive_and_cap_levers.py",
    "tests/test_engine/test_plan_run_reconciliation.py",
    "tests/test_engine/test_runtime_submit_surfacing.py",
    "tests/test_engine/test_submit_review.py",
    "tests/test_engine/test_tool_surface_levers.py",
    "tests/test_engine/turn_runner/test_tool_surface_levers_bootstrap_unit.py",
    "tests/test_gateway/test_plan_rpc.py",
    "tests/test_gateway/test_user_input_broker.py",
    "tests/test_session/test_plan_storage.py",
    "tests/test_tools/test_description_overrides.py",
    "tests/test_tools/test_edit_file_closest_hint.py",
    "tests/test_tools/test_patch_classification.py",
    "tests/test_tools/test_plan_access.py",
    "tests/test_tools/test_repeated_call_notice.py",
    "tests/test_tools/test_admin_audio_config.py",
    "tests/test_tools/test_admin_gateway_contract.py",
    "tests/test_tools/test_shell_self_kill_policy.py",
    "tests/test_tools/test_run_mode_full_host_fallback.py",
    "tests/test_tools/test_workspace_write_deny_effects.py",
    "tests/test_engine/test_goal_context_prompt.py",
    "tests/test_engine/test_goal_routing_hint.py",
    "tests/test_gateway/test_goal_rpc.py",
    "tests/test_migrations/test_v033_goal_runs.py",
    "tests/test_migrations/test_v034_goal_message_anchor.py",
    "tests/test_session/test_goal_storage.py",
    "tests/test_session/test_goals.py",
    "tests/test_contracts/test_ensemble_fallback_event_wire.py",
    "tests/test_contracts/test_turn_execution.py",
    "tests/test_engine/test_turn_control_terminal.py",
}


def test_every_pytest_file_belongs_to_exactly_one_windows_shard() -> None:
    discovered = set(discover_test_files(Path.cwd()))
    by_shard = {
        shard: set(files_for_shard(Path.cwd(), shard)) for shard in SHARD_NAMES
    }

    assert set(SHARD_NAMES) == {
        "core",
        "gateway-sqlite",
        "recovery-migration",
        "desktop-installer-contracts",
    }
    assert all(by_shard.values())
    assert set().union(*by_shard.values()) == discovered
    assert sum(len(paths) for paths in by_shard.values()) == len(discovered)
    assert all(len(matching_specialized_shards(path)) <= 1 for path in discovered)
    assert "tests/fixtures/meta_skill_inputs/code_review_dirty_repo/tests/test_app.py" not in (
        discovered
    )


def test_windows_shard_responsibilities_cover_high_risk_surfaces() -> None:
    expected = {
        "tests/test_ci/test_router_artifact_manifest.py": "core",
        "tests/test_gateway/test_task_runtime_terminal_cleanup.py": "gateway-sqlite",
        "tests/test_persistence/test_migrator.py": "gateway-sqlite",
        "tests/test_session/test_manager.py": "gateway-sqlite",
        "tests/test_migration/test_opensquilla_home_migration.py": "recovery-migration",
        "tests/test_recovery/test_fixture_contracts.py": "recovery-migration",
        "tests/test_cli/test_migrate_cmd.py": "recovery-migration",
        "tests/test_desktop/test_electron_startup_contract.py": (
            "desktop-installer-contracts"
        ),
        "tests/test_uninstall/test_safety.py": "desktop-installer-contracts",
        "tests/test_install_scripts.py": "desktop-installer-contracts",
        "tests/test_scripts/test_bench_skill_integrity.py": "recovery-migration",
        "tests/test_skills_hash_consumers.py": "recovery-migration",
        "tests/test_skills_tree.py": "recovery-migration",
    }

    assert {path: shard_for_test(path) for path in expected} == expected


def test_windows_shards_are_balanced_by_historical_duration() -> None:
    discovered = set(discover_test_files(Path.cwd()))
    weights = historical_test_weights()
    summary = shard_weight_summary(Path.cwd())

    # Stale duration entries would distort the balance after a test is deleted.
    assert set(weights) <= discovered
    estimated_seconds = [summary[shard][1] for shard in SHARD_NAMES]
    assert min(estimated_seconds) > 0
    assert max(estimated_seconds) / min(estimated_seconds) < 1.05


def test_windows_assignment_snapshot_governs_reviewed_rebalancing() -> None:
    baseline, assignments, guardrails, overrides = assignment_governance()
    report = assignment_governance_summary(Path.cwd())

    expected_moved_paths = {
        "tests/test_gateway/test_goal_rpc.py",
        "tests/test_gateway/test_project_workspace_execution.py",
        "tests/test_gateway/test_rpc_meta_runs.py",
        "tests/test_gateway/test_rpc_router_decisions.py",
        "tests/test_live_long_task_case_driver.py",
        "tests/test_live_multi_provider_matrix.py",
        "tests/test_observability/test_bundle.py",
        "tests/test_persistence/test_router_decision_writer.py",
        "tests/test_sandbox/test_windows_default_capability.py",
        "tests/test_skills/test_meta_resume.py",
    }
    moved_paths = {
        path for path, shard in assignments.items() if baseline[path] != shard
    }

    assert moved_paths == expected_moved_paths
    assert set(assignments) == set(historical_test_weights())
    assert {str(override["path"]) for override in overrides} == expected_moved_paths
    assert sum(override.get("affinity_exception") is True for override in overrides) == 6
    assert guardrails == {
        "max_moved_files": 10,
        "max_moved_fraction": 0.02,
        "minimum_predicted_max_shard_improvement_seconds": 60.0,
    }
    assert len(moved_paths) <= guardrails["max_moved_files"]
    assert len(moved_paths) / len(baseline) <= guardrails["max_moved_fraction"]
    assert report["predicted_max_shard_improvement_seconds"] >= (
        guardrails["minimum_predicted_max_shard_improvement_seconds"]
    )
    proposed_seconds = list(report["current_predicted_seconds"].values())
    assert max(proposed_seconds) / min(proposed_seconds) < 1.05
    assert report["assignment_sha256"] == assignment_snapshot_fingerprint()
    assert len(str(report["assignment_sha256"])) == 64


def _synthetic_assignment_payload(
    baseline_assignments: dict[str, list[str]], overrides: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "guardrails": {
            "max_moved_files": 10,
            "max_moved_fraction": 1.0,
            "minimum_predicted_max_shard_improvement_seconds": 60.0,
        },
        "baseline_assignments": baseline_assignments,
        "overrides": overrides,
    }


def test_windows_assignment_snapshot_rejects_hard_pin_movement() -> None:
    baseline = {
        "core": ["tests/test_ci/test_router_artifact_manifest.py"],
        "gateway-sqlite": ["tests/test_gateway/test_rpc.py"],
        "recovery-migration": ["tests/test_recovery/test_restore.py"],
        "desktop-installer-contracts": ["tests/test_desktop/test_startup.py"],
    }
    weights = {path: 100.0 for paths in baseline.values() for path in paths}
    payload = _synthetic_assignment_payload(
        baseline,
        [
            {
                "path": "tests/test_ci/test_router_artifact_manifest.py",
                "from": "core",
                "to": "desktop-installer-contracts",
                "reason": "synthetic invalid movement",
            }
        ],
    )

    with pytest.raises(ValueError, match="hard-pinned"):
        validate_assignment_payload(payload, weights)


def test_windows_assignment_snapshot_rejects_low_value_movement() -> None:
    baseline = {
        "core": ["tests/test_core_big.py", "tests/test_core_small.py"],
        "gateway-sqlite": ["tests/test_gateway_other.py"],
        "recovery-migration": ["tests/test_recovery_other.py"],
        "desktop-installer-contracts": ["tests/test_desktop_other.py"],
    }
    weights = {
        "tests/test_core_big.py": 100.0,
        "tests/test_core_small.py": 1.0,
        "tests/test_gateway_other.py": 100.0,
        "tests/test_recovery_other.py": 100.0,
        "tests/test_desktop_other.py": 100.0,
    }
    payload = _synthetic_assignment_payload(
        baseline,
        [
            {
                "path": "tests/test_core_small.py",
                "from": "core",
                "to": "gateway-sqlite",
                "reason": "synthetic low-value movement",
            }
        ],
    )

    with pytest.raises(ValueError, match="minimum predicted improvement"):
        validate_assignment_payload(payload, weights)


def test_windows_assignment_snapshot_rejects_excessive_movement() -> None:
    core_paths = [f"tests/test_core_{index:02d}.py" for index in range(11)]
    baseline = {
        "core": core_paths,
        "gateway-sqlite": ["tests/test_gateway_other.py"],
        "recovery-migration": ["tests/test_recovery_other.py"],
        "desktop-installer-contracts": ["tests/test_desktop_other.py"],
    }
    weights = {path: 100.0 for paths in baseline.values() for path in paths}
    payload = _synthetic_assignment_payload(
        baseline,
        [
            {
                "path": path,
                "from": "core",
                "to": "gateway-sqlite",
                "reason": "synthetic excessive movement",
            }
            for path in core_paths
        ],
    )

    with pytest.raises(ValueError, match="movement budget"):
        validate_assignment_payload(payload, weights)


def test_active_unweighted_fallback_stays_within_refresh_budget() -> None:
    discovered = set(discover_test_files(Path.cwd()))
    weighted = set(historical_test_weights())
    unweighted = discovered - weighted
    unexpected_active = unweighted - OFFLINE_MARKER_EXCLUSIONS
    active = discovered - OFFLINE_MARKER_EXCLUSIONS

    assert OFFLINE_MARKER_EXCLUSIONS <= unweighted
    assert RECENTLY_ADDED_ACTIVE_TESTS <= weighted
    # A small number of newly added tests can run immediately through the core
    # fail-safe. Crossing either threshold signals that the history should be
    # refreshed before the original shard imbalance can materially return.
    assert len(unexpected_active) <= 4
    assert len(unexpected_active) / len(active) < 0.01


def test_unmatched_or_unweighted_tests_fail_safe_to_core() -> None:
    weights = historical_test_weights()
    for path in discover_test_files(Path.cwd()):
        if path not in weights and not matching_specialized_shards(path):
            assert shard_for_test(path) == "core"

    assert shard_for_test("tests/test_new_unclassified_surface.py") == "core"
    assert shard_for_test("tests/test_gateway/test_new_rpc_surface.py") == (
        "gateway-sqlite"
    )


def test_tests_requiring_core_only_setup_remain_pinned() -> None:
    assert shard_for_test("tests/test_ci/test_router_artifact_manifest.py") == "core"
    assert shard_for_test("tests/unit/cli/tui/test_opentui_fuzzy_rank.py") == "core"


def test_affinity_overflow_moves_only_environment_independent_tests() -> None:
    weights = historical_test_weights()
    moved = {
        path: shard_for_test(path)
        for path in weights
        if (matches := matching_specialized_shards(path))
        and shard_for_test(path) != matches[0]
    }

    # These reviewed files need no shard-specific setup. Releasing them keeps
    # environment-dependent tests pinned while restoring an even critical path.
    assert moved == {
        "tests/test_ci/test_migrations_packaged.py": "core",
        "tests/test_gateway/test_goal_rpc.py": "desktop-installer-contracts",
        "tests/test_gateway/test_project_workspace_execution.py": (
            "desktop-installer-contracts"
        ),
        "tests/test_gateway/test_rpc_meta_runs.py": "desktop-installer-contracts",
        "tests/test_gateway/test_rpc_router_decisions.py": (
            "desktop-installer-contracts"
        ),
        "tests/test_observability/test_bundle.py": "desktop-installer-contracts",
        "tests/test_persistence/test_meta_run_writer.py": (
            "desktop-installer-contracts"
        ),
        "tests/test_persistence/test_router_decision_writer.py": "core",
    }
    assert shard_for_test("tests/test_recovery/test_atomic_and_locking.py") == (
        "recovery-migration"
    )


def test_windows_shard_runner_preserves_failure_exit_and_summary(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nnorecursedirs = ["tests/fixtures"]\n',
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_failure.py").write_text(
        "def test_failure():\n    assert False, 'synthetic shard failure'\n",
        encoding="utf-8",
    )
    junit = tmp_path / "reports" / "junit.xml"
    summary = tmp_path / "reports" / "first-failure.txt"
    metadata = tmp_path / "reports" / "windows-shard-metadata.json"
    env = os.environ.copy()
    env.update(
        {
            "GITHUB_RUN_ID": "1234",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_SHA": "a" * 40,
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            SHARD_SCRIPT.resolve().as_posix(),
            "run",
            "core",
            "--root",
            tmp_path.as_posix(),
            "--junit",
            junit.as_posix(),
            "--summary",
            summary.as_posix(),
            "--metadata",
            metadata.as_posix(),
            "--",
            "-q",
            "--maxfail=3",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 1
    assert "CI shard core (historical weight: 0.0s; unweighted: 1)" in result.stdout
    assert junit.is_file()
    metadata_payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert metadata_payload["run_id"] == 1234
    assert metadata_payload["run_attempt"] == 2
    assert metadata_payload["sha"] == "a" * 40
    assert metadata_payload["shard"] == "core"
    assert metadata_payload["test_files"] == ["tests/test_failure.py"]
    assert len(metadata_payload["assignment_sha256"]) == 64
    text = summary.read_text(encoding="utf-8")
    assert "pytest_exit_code=1" in text
    assert "junit_status=failed" in text
    assert "synthetic shard failure" in text
