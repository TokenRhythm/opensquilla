from __future__ import annotations

import ast
import hashlib
import itertools
import json
import os
import runpy
import subprocess
import sys
import threading
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SHARD_SCRIPT = Path(".github/scripts/windows_test_shards.py")
SHARD_MODULE: dict[str, Any] = runpy.run_path(
    SHARD_SCRIPT.as_posix(), run_name="windows_test_shards"
)
SHARD_NAMES: tuple[str, ...] = SHARD_MODULE["SHARD_NAMES"]
WINDOWS_SHARD_NAMES: tuple[str, ...] = SHARD_MODULE["WINDOWS_SHARD_NAMES"]
SHARED_SHARD_NAMES: tuple[str, ...] = SHARD_MODULE["SHARED_SHARD_NAMES"]
discover_test_files = SHARD_MODULE["discover_test_files"]
files_for_shard = SHARD_MODULE["files_for_shard"]
files_for_planned_windows_shards = SHARD_MODULE["files_for_planned_windows_shards"]
historical_test_weights = SHARD_MODULE["historical_test_weights"]
matching_specialized_shards = SHARD_MODULE["matching_specialized_shards"]
assignment_governance = SHARD_MODULE["assignment_governance"]
assignment_governance_summary = SHARD_MODULE["assignment_governance_summary"]
assignment_snapshot_fingerprint = SHARD_MODULE["assignment_snapshot_fingerprint"]
shard_for_test = SHARD_MODULE["shard_for_test"]
shard_weight_summary = SHARD_MODULE["shard_weight_summary"]
validate_assignment_payload = SHARD_MODULE["validate_assignment_payload"]
validated_files_for_shard = SHARD_MODULE["validated_files_for_shard"]
requires_isolated_core_wheel = SHARD_MODULE["_requires_isolated_core_wheel"]
combined_pytest_exit_code = SHARD_MODULE["_combined_pytest_exit_code"]
pytest_file_selection_arg = SHARD_MODULE["_pytest_file_selection_arg"]
windows_shard_for_test = SHARD_MODULE["windows_shard_for_test"]
shard_family = SHARD_MODULE["shard_family"]
partition_assignments = SHARD_MODULE["partition_assignments"]
partition_snapshot_fingerprint = SHARD_MODULE["partition_snapshot_fingerprint"]
validate_partition_payload = SHARD_MODULE["validate_partition_payload"]

OFFLINE_MARKER_EXCLUSIONS = SHARD_MODULE["OFFLINE_MARKER_EXCLUSIONS"]
RECENTLY_ADDED_ACTIVE_TESTS = {
    # Cancellation settlement uses the provisional floor until a Windows refresh.
    "tests/test_gateway/test_task_runtime_cancellation_settlement.py",
    # Generator provenance checks use the provisional floor pending Windows samples.
    "tests/contracts/test_codegen_versions.py",
    # Security inventory, rendering, and functional probes use measured Windows
    # testcase totals until the next comparable three-run duration refresh.
    "tests/test_desktop/test_gateway_functional_probes.py",
    "tests/test_scripts/test_release_dependency_inventory.py",
    "tests/test_security/test_weasyprint_presentational_hints.py",
    # Telemetry regressions use provisional weights until the next comparable
    # Windows duration refresh supplies measured timings.
    "tests/test_engine/test_runtime_usage_telemetry.py",
    "tests/test_observability/test_usage_telemetry_identity.py",
    # Primary-provider and validator coverage use the declared provisional
    # floor until a comparable three-run Windows refresh supplies measured timings.
    "tests/contracts/test_gateway_validator_profiles.py",
    "tests/test_desktop/test_router_provider_bridge.py",
    "tests/test_gateway/test_router_recommended_reset.py",
    "tests/test_scripts/test_gateway_ux.py",
    "tests/test_engine/test_agent_autonomous_tool_recovery.py",
    "tests/test_engine/test_agent_connection_recovery.py",
    "tests/test_engine/test_selector_provider_recovery.py",
    "tests/test_provider_connection_failure.py",
    "tests/test_tools/test_bounded_output_capture.py",
    # Execution identity suites use the provisional floor until a Windows refresh.
    "tests/test_engine/test_request_execution_identity.py",
    "tests/test_tools/test_execution_status.py",
    "tests/test_live_execution_identity_acceptance.py",
    # Execution-log suites use the declared provisional floor until a Windows refresh.
    "tests/test_gateway/test_rpc_execution_logs.py",
    "tests/test_tools/test_execution_log_queries.py",

    # Local-first workspace files use the 0.01s provisional floor until a
    # comparable three-run Windows refresh supplies measured timings.
    "tests/test_gateway/test_execution_workspace_preparation.py",
    "tests/test_gateway/test_local_first_workspaces.py",
    "tests/test_gateway/test_working_file_actions.py",
    "tests/test_gateway/test_workspace_config_provenance.py",
    "tests/test_gateway/test_workspace_preview_registration.py",
    "tests/test_live_deliverable_acceptance.py",
    "tests/test_tools/test_memory_workspace_ownership.py",
    # Image budget suites use the provisional floor until a Windows duration refresh.
    "tests/test_engine/test_agent_image_compaction_budget.py",
    "tests/test_provider_request_proof_images.py",
    "tests/test_session/test_compaction_media_budget.py",
    # New compaction recovery suites use the provisional floor until a
    # comparable three-run Windows duration refresh supplies timings.
    "tests/test_engine/test_request_window.py",
    "tests/test_engine/test_runtime_request_window.py",
    "tests/test_session/test_compaction_integrity.py",
    # Artifact source/version regressions use the declared provisional floor.
    "tests/test_engine/test_artifact_delivery_sources.py",
    "tests/test_engine/test_runtime_artifact_context.py",
    # New connection-stability suites use the declared 0.01s provisional floor
    # until a comparable three-run Windows refresh supplies measured timings.
    "tests/test_gateway/test_connection_stability_socket.py",
    "tests/test_gateway/test_snapshot_transfer.py",
    "tests/test_gateway/test_snapshot_transfer_rpc.py",
    "tests/test_gateway/test_transport_diagnostics.py",
    "tests/test_gateway/test_transport_flow.py",
    "tests/test_gateway/test_websocket_connection_stability.py",
    # Custom-provider request extensions use the provisional floor until the
    # next comparable three-run Windows duration refresh.
    "tests/test_gateway/test_custom_extra_body.py",
    "tests/test_ci/test_windows_signed_update_audit.py",
    # New replay files use the documented provisional floor until a Windows refresh.
    "tests/functional/test_reasoning_replay_persistence_e2e.py",
    "tests/test_engine/test_assistant_replay.py",
    "tests/test_engine/test_assistant_replay_lifecycle.py",
    "tests/test_engine/test_assistant_replay_tool_boundaries.py",
    "tests/test_engine/test_reasoning_replay_compat.py",
    "tests/test_live_reasoning_replay_e2e.py",
    "tests/test_migrations/test_v042_assistant_replay.py",
    "tests/test_provider_replay_state.py",
    "tests/test_session/test_session_assistant_replay.py",
    "tests/test_engine/test_attachment_replay_ownership.py",
    "tests/test_engine/test_router_configured_image_policy.py",
    "tests/test_provider/test_image_projection.py",
    "tests/test_session/test_attachment_manifest.py",
    # Title refusal and archived first-message regressions use the declared
    # provisional floor until a comparable three-run Windows duration refresh.
    "tests/test_gateway/test_compacted_title_recovery.py",
    "tests/test_gateway/test_session_title_recovery.py",
    "tests/test_session/test_canonical_title_inputs.py",
    "tests/test_session/test_naming_refusal.py",
    "tests/test_session/test_title_quality.py",
    "tests/contracts/test_approval_center_contract.py",
    "tests/test_gateway/test_chat_history_characterization.py",
    "tests/contracts/test_conversation_events_contract.py",
    "tests/contracts/test_gateway_contract_runner.py",
    "tests/contracts/test_gateway_contract_toolchain_integration.py",
    "tests/test_gateway/test_rpc_retired_surface.py",
    "tests/contracts/test_goals_contract.py",
    "tests/contracts/test_sandbox_runtime_contract.py",
    "tests/contracts/test_sessions_changed_contract.py",
    "tests/contracts/test_sessions_list_contract.py",
    "tests/contracts/test_sessions_resolve_contract.py",
    "tests/contracts/test_sessions_search_contract.py",
    "tests/test_application/test_app_settings.py",
    "tests/test_application/test_provider_configuration.py",
    "tests/test_application/test_sandbox_runtime.py",
    "tests/test_application/test_session_lifecycle.py",
    "tests/test_application/test_setup_workflow.py",
    "tests/test_gateway/test_session_preview_adapter.py",
    "tests/test_gateway/test_session_history_adapter.py",
    "tests/test_gateway/test_sessions_bootstrap_history_characterization.py",
    "tests/test_application/test_session_history.py",
    "tests/test_application/test_session_read.py",
    "tests/test_application/test_session_transcript.py",
    "tests/test_artifact_session/test_retirement.py",
    "tests/test_artifact_session/test_working_files.py",
    "tests/test_engine/test_agent_file_context.py",
    "tests/test_gateway/test_desktop_browser.py",
    "tests/test_live_tokenrhythm_budget.py",
    "tests/test_ci/test_plan_ci.py",
    "tests/test_git_runtime.py",
    "tests/test_tools/test_gitless_write_tracking.py",
    "tests/test_gateway/test_artifact_product_errors.py",
    "tests/test_gateway/test_websocket_close_coordination.py",
    "tests/test_scripts/test_bench_skill_integrity.py",
    "tests/test_skills_hash_consumers.py",
    "tests/test_skills/test_loader_turn_snapshot.py",
    "tests/test_skills_tree.py",
    "tests/test_recovery/test_config_recovery.py",
    "tests/unit/cli/tui/test_keys_cheatsheet.py",
    "tests/unit/cli/tui/test_opentui_prefs.py",
    "tests/test_cli/test_gateway_client_steer.py",
    "tests/test_cli/test_gateway_client_sessions_contract.py",
    "tests/test_cli/test_sessions_cmd.py",
    "tests/test_cli/test_skills_search_cmd.py",
    "tests/test_channels/test_admission_reason_persistence.py",
    "tests/test_channels/test_channel_admission.py",
    "tests/test_channels/test_channel_certification.py",
    "tests/test_channels/test_channel_delivery_store.py",
    "tests/test_channels/test_channel_mock_certification.py",
    "tests/test_channels/test_channel_pairing.py",
    "tests/test_channels/test_discord_gateway_lifecycle.py",
    # Real Feishu SDK coverage uses the provisional floor until a comparable
    # three-run Windows refresh supplies measured timings.
    "tests/test_channels/test_feishu_sdk_websocket.py",
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
    "tests/test_gateway/test_contract_method_adapter.py",
    "tests/test_gateway/test_session_read_adapter.py",
    "tests/test_gateway/test_session_read_contract_registration.py",
    "tests/test_gateway/test_session_lifecycle_adapter.py",
    "tests/test_gateway/test_sandbox_runtime_contract_registration.py",
    "tests/contracts/test_turn_commands_contract.py",
    "tests/test_application/test_conversation_runtime.py",
    "tests/test_gateway/test_conversation_runtime_adapter.py",
    "tests/test_gateway/test_goal_plan_contract_adapters.py",
    "tests/test_scripts/test_stage_webui_artifact.py",
    "tests/test_artifact_validation.py",
    "tests/test_ci/test_dockerignore_context.py",
    "tests/test_ci/test_migration_v022.py",
    "tests/test_ci/test_session_storage_connection_contract.py",
    "tests/test_ci/test_rpc_architecture_contracts.py",
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
    "tests/test_gateway/test_p1a_exact_abort_contract.py",
    "tests/test_gateway/test_rpc_ingress_validation.py",
    "tests/test_gateway/test_sessions_list_contract_adapter.py",
    "tests/test_gateway/test_sessions_resolve_contract_adapter.py",
    "tests/test_gateway/test_sessions_search_contract_adapter.py",
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
    "tests/test_session/test_pending_chat_inputs.py",
    "tests/test_session/test_turn_acceptance_storage.py",
    "tests/test_session/test_assistant_message_identity.py",
    "tests/test_skills/test_hub_deps_subprocess.py",
    "tests/test_skills/test_managed_toolchains.py",
    "tests/test_skills/test_toolchain_runtime_integration.py",
    "tests/test_skills/test_toolchain_state_scope.py",
    "tests/test_tools/test_shell_managed_toolchains.py",
    "tests/test_envelope_policy_deny_cap.py",
    "tests/test_request_proof_levers.py",
    "tests/test_toolcomp_matcher_levers.py",
    "tests/test_toolcomp_matcher_safety.py",
    "tests/test_toolcomp_reducer_semantics.py",
    "tests/test_engine/test_agent_verify_mirror_and_variant_challenge.py",
    "tests/test_engine/test_endgame_directive_and_cap_levers.py",
    "tests/test_engine/test_plan_run_reconciliation.py",
    "tests/test_engine/test_runtime_submit_surfacing.py",
    "tests/test_engine/test_tool_surface_levers.py",
    "tests/test_engine/turn_runner/test_tool_surface_levers_bootstrap_unit.py",
    "tests/test_gateway/test_plan_rpc.py",
    "tests/test_gateway/test_user_input_broker.py",
    "tests/test_session/test_plan_storage.py",
    "tests/test_tools/test_edit_file_closest_hint.py",
    "tests/test_tools/test_patch_classification.py",
    "tests/test_tools/test_plan_access.py",
    # Agent transcript search uses the provisional floor until a Windows refresh.
    "tests/test_tools/test_session_search.py",
    "tests/test_tools/test_admin_audio_config.py",
    "tests/test_tools/test_admin_gateway_contract.py",
    "tests/test_tools/test_shell_self_kill_policy.py",
    "tests/test_tools/test_run_mode_full_host_fallback.py",
    "tests/test_tools/test_workspace_write_deny_effects.py",
    "tests/test_engine/test_goal_context_prompt.py",
    "tests/test_gateway/test_goal_rpc.py",
    "tests/test_migrations/test_v033_goal_runs.py",
    "tests/test_migrations/test_v034_goal_message_anchor.py",
    "tests/test_session/test_goal_storage.py",
    "tests/test_session/test_goals.py",
    "tests/test_contracts/test_ensemble_fallback_event_wire.py",
    "tests/test_contracts/test_turn_execution.py",
    "tests/test_engine/test_turn_control_terminal.py",
    # New runtime-notice and telemetry pipeline suites use the provisional floor
    # until a comparable three-run Windows duration refresh supplies timings.
    "tests/test_engine/turn_runner/test_runtime_notices.py",
    "tests/test_telemetry_server/test_product_active_pipeline.py",
    "tests/test_telemetry_server/test_product_activity_pipeline.py",
    "tests/test_telemetry_server/test_protocol_upgrade_pipeline.py",
    # Workspace MD retirement suites use the declared provisional floor until
    # a comparable three-run Windows refresh supplies measured timings.
    "tests/test_gateway/test_workspace_md_retirement_rpc.py",
    "tests/test_identity/test_workspace_md_retirement.py",
    "tests/test_scheduler/test_heartbeat_retirement.py",
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
    assert "tests/fixtures/synthetic_project/tests/test_app.py" not in (
        discovered
    )
    assert set(validated_files_for_shard(Path.cwd(), "core")) == by_shard["core"]


def test_parallel_ci_contract_registers_xdist_and_serial_marker() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dev_dependencies = data["project"]["optional-dependencies"]["dev"]
    markers = data["tool"]["pytest"]["ini_options"]["markers"]

    assert any(dependency.startswith("pytest-xdist>=") for dependency in dev_dependencies)
    assert any(marker.startswith("ci_serial:") for marker in markers)


@pytest.mark.parametrize(
    ("parallel_exit_code", "serial_exit_code", "expected"),
    [
        (5, 0, 0),
        (0, 5, 0),
        (5, 5, 5),
        (5, 1, 1),
        (1, 5, 1),
    ],
)
def test_split_phase_exit_codes_allow_one_empty_successful_phase(
    parallel_exit_code: int,
    serial_exit_code: int,
    expected: int,
) -> None:
    assert (
        combined_pytest_exit_code(
            parallel_exit_code,
            serial_exit_code,
            no_tests_collected=5,
        )
        == expected
    )


@pytest.mark.parametrize("shard_names", [SHARD_NAMES, WINDOWS_SHARD_NAMES])
def test_only_fixture_consuming_shards_prebuild_the_core_wheel(
    shard_names: tuple[str, ...],
) -> None:
    root = Path.cwd()
    consumers = {
        shard_family(shard)
        for shard in shard_names
        if requires_isolated_core_wheel(root, files_for_shard(root, shard, platform="windows"))
    }

    assert consumers == {"core", "desktop-installer-contracts"}


@pytest.mark.parametrize("encoding", ["utf-8-sig", "latin-1"])
@pytest.mark.parametrize("consumes_wheel", [False, True])
def test_core_wheel_prescan_honors_python_source_encodings(
    tmp_path: Path, encoding: str, consumes_wheel: bool,
) -> None:
    path = tmp_path / "test_encoded.py"
    cookie = "# coding: latin-1\n" if encoding == "latin-1" else ""
    argument = "isolated_core_wheel" if consumes_wheel else ""
    source = f"{cookie}# caf\u00e9\ndef test_encoded({argument}):\n    pass\n"
    path.write_bytes(source.encode(encoding))

    assert requires_isolated_core_wheel(tmp_path, (path.name,)) is consumes_wheel


@pytest.mark.parametrize(
    "source",
    [
        b"\xef\xbb\xbfdef test_invalid(:\n    pass\n",
        b"# coding: unknown-source-codec\ndef test_invalid():\n    pass\n",
        b"\xef\xbb\xbf# coding: latin-1\ndef test_invalid():\n    pass\n",
    ],
)
def test_core_wheel_prescan_keeps_invalid_python_sources_fail_closed(
    tmp_path: Path, source: bytes,
) -> None:
    path = tmp_path / "test_invalid.py"
    path.write_bytes(source)

    with pytest.raises(SyntaxError):
        requires_isolated_core_wheel(tmp_path, (path.name,))


def _function_decorators(path: Path, function_name: str) -> set[str]:
    parsed = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(parsed):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return {ast.unparse(decorator) for decorator in node.decorator_list}
    raise AssertionError(f"missing test function: {path}:{function_name}")


@pytest.mark.parametrize(
    "test_file",
    [
        "tests/test_ci/test_windows_signatures.py",
        "tests/test_sandbox/test_windows_shell_process_runtime.py",
        "tests/test_scripts/test_gateway_ux.py",
    ],
)
def test_windows_process_harnesses_are_marked_ci_serial(test_file: str) -> None:
    path = Path(test_file)
    parsed = ast.parse(path.read_text(encoding="utf-8"))

    assert any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in node.targets
        )
        and ast.unparse(node.value) == "pytest.mark.ci_serial"
        for node in parsed.body
    )


def test_known_process_tree_flakes_are_marked_ci_serial() -> None:
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_process_tree.py"),
        "test_owner_registry_supports_concurrent_process_writers",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/functional/test_gateway_stop_process_tree_e2e.py"),
        "test_stop_kills_leaderless_descendant_and_gateway_accepts_next_task",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_recovery/test_cleanup.py"),
        "test_cleanup_apply_refuses_running_legacy_gateway",
    )


def test_task_runtime_leak_smoke_is_marked_ci_serial() -> None:
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_gateway/test_task_runtime_terminal_cleanup.py"),
        "test_no_leak_under_load",
    )


@pytest.mark.parametrize("function_name", [
    "test_real_node_metadata_imports_unchanged_image_on_python",
    "test_electron_embedded_node_metadata_imports_unchanged_image",
])
def test_native_attachment_runtime_parity_is_marked_ci_serial(function_name: str) -> None:
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_gateway/test_native_attachment_import.py"), function_name,
    )


def test_runner_saturated_subprocess_contracts_are_marked_ci_serial() -> None:
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_desktop/test_gateway_functional_probes.py"),
        "test_mcp_probe_uses_real_stdio_server_and_gateway",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_live_provider_profile_gateway_e2e.py"),
        "test_attachment_capacity_runner_reaches_provider_through_real_gateway",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_live_provider_profile_gateway_e2e.py"),
        "test_attachment_capacity_runner_bounds_provider_http_failures_to_one_call",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_ci/test_windows_signed_update_audit.py"),
        "test_real_node_and_frozen_python_complete_only_in_new_temporary_parent",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_gateway/test_goal_rpc.py"),
        "test_continuation_authority_loss_after_accept_compensates_before_activation",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_scripts/test_verify_webui_artifact.py"),
        "test_node_and_python_source_fingerprints_share_order_and_line_endings",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_live_long_task_case_driver.py"),
        "test_fault_case_executes_through_isolated_gateway_without_real_provider",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_live_long_task_case_driver.py"),
        "test_fault_429_case_proves_retry_after_was_not_violated",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_recovery/test_atomic_and_locking.py"),
        "test_moved_legacy_lock_can_be_rebound_without_dropping_exclusion",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_recovery/test_transaction.py"),
        "test_transaction_recovery_locks_parked_backup_before_restoring_target",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/functional/test_gateway_session_routing_e2e.py"),
        "test_real_gateway_websocket_session_routing_contract",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/functional/test_gateway_silent_reply_process_e2e.py"),
        "test_real_gateway_suppresses_goal_sentinel_everywhere",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/functional/test_gateway_silent_reply_process_e2e.py"),
        "test_default_timing_sample_has_one_provider_call_and_no_goal",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_tools/test_shell_process_isolation.py"),
        "test_exec_command_writes_optional_stdin",
    )
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_skills/test_hub_transaction_process_gates.py"),
        "test_unleased_build_services_does_not_sweep_another_process_reservation",
    )


@pytest.mark.parametrize(
    ("test_file", "function_name"),
    [
        (
            "tests/test_tools/test_shell_blocking_launch.py",
            "test_slow_native_launch_preserves_loop_and_ownership",
        ),
        (
            "tests/test_tools/test_shell_blocking_launch.py",
            "test_quiet_native_reader_is_reclaimed_after_initialization",
        ),
        (
            "tests/test_tools/test_shell_blocking_launch.py",
            "test_runner_shutdown_waits_for_native_launch_cleanup",
        ),
        (
            "tests/test_gateway/test_plan_rpc.py",
            "test_interrupted_plan_can_deliver_existing_artifact_in_a_new_turn",
        ),
        (
            "tests/test_gateway/test_plan_rpc.py",
            "test_explicit_implement_mode_is_pinned_at_queued_execution_boundary",
        ),
        (
            "tests/test_engine/test_tool_concurrency.py",
            "test_image_analysis_calls_have_dedicated_inflight_cap",
        ),
        (
            "tests/test_gateway/test_manual_compaction_contract.py",
            "test_absolute_deadline_closes_provider_and_emits_one_skipped_terminal",
        ),
        (
            "tests/test_engine/test_compaction_parent_turn_deadline.py",
            "test_parent_deadline_cancels_inflight_preflight_without_auxiliary_failure",
        ),
        (
            "tests/test_engine/test_compaction_parent_turn_deadline.py",
            "test_parent_deadline_after_sqlite_commit_does_not_rollback_checkpoint",
        ),
        (
            "tests/test_engine/test_compaction_runtime_boundaries.py",
            "test_parent_expiry_cancels_inflight_summary_without_failure_or_window",
        ),
        (
            "tests/test_engine/test_compaction_runtime_boundaries.py",
            "test_real_turn_reports_parent_timeout_without_auxiliary_failure",
        ),
        (
            "tests/test_gateway/test_rpc_sessions.py",
            "test_direct_send_rejects_dropping_durable_runner",
        ),
        (
            "tests/test_engine/test_interactive_approval_retry.py",
            "test_pending_approval_ignores_legacy_timeout_and_waits_for_decision",
        ),
        (
            "tests/test_skills/test_staging_io_worker.py",
            "test_scan_keeps_status_responsive_and_cancel_waits_for_cleanup",
        ),
        (
            "tests/test_observability/test_http_client.py",
            "test_custom_ca_and_hostname_validation_are_preserved",
        ),
        (
            "tests/test_observability/test_http_client.py",
            "test_idle_tls_connection_does_not_block_certificate_validation",
        ),
        (
            "tests/test_recovery/test_runtime_writer_guard.py",
            "test_runtime_writer_lock_keeps_read_only_cli_available_and_rejects_agent",
        ),
        (
            "tests/test_recovery/test_runtime_writer_guard.py",
            "test_distinct_profile_and_gateway_state_dirs_run_concurrently_across_processes",
        ),
    ],
)
def test_bounded_latency_contracts_are_marked_ci_serial(
    test_file: str, function_name: str,
) -> None:
    assert "pytest.mark.ci_serial" in _function_decorators(Path(test_file), function_name)


@pytest.mark.parametrize("function_name", [
    "test_close_releases_stalled_send_lock_before_draining_accepted_record",
    "test_prepare_shutdown_releases_send_lock_and_keeps_producer_records_open",
    "test_shutdown_probe_rejects_broken_contracts",
    "test_shutdown_probe_bounds_noncooperative_cleanup",
])
def test_telemetry_process_probes_are_explicitly_serial(function_name: str) -> None:
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_telemetry/test_client_runtime.py"), function_name,
    )


@pytest.mark.parametrize("function_name", [
    "test_close_drains_native_before_reconnect",
    "test_close_probe_rejects_broken_resource_contracts",
    "test_close_probe_watchdog_terminates_noncooperative_close",
])
def test_recovery_close_probes_are_explicitly_serial(function_name: str) -> None:
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_session/test_recovery_reads.py"), function_name,
    )


@pytest.mark.ci_serial
@pytest.mark.parametrize(("target", "serial_count"), [
    ("tests/test_telemetry/test_client_runtime.py", 5),
    ("tests/test_session/test_recovery_reads.py", 9),
    ("tests/test_gateway/test_native_attachment_import.py", 3),
    ("tests/test_observability/test_http_client.py", 2),
    ("tests/test_recovery/test_runtime_writer_guard.py", 3),
])
def test_process_probe_collection_is_complete_and_disjoint(target: str, serial_count: int) -> None:
    """Exercise actual pytest collection, not fixture-setup-time marker changes."""
    def collect(marker: str | None) -> set[str]:
        command = [sys.executable, "-m", "pytest", target, "--collect-only", "-q"]
        if marker is not None:
            command += ["-m", marker]
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        nodes = [line for line in result.stdout.splitlines() if line.startswith(f"{target}::")]
        assert nodes and len(nodes) == len(set(nodes)), result.stdout
        return set(nodes)

    all_nodes = collect(None)
    parallel = collect("not ci_serial")
    serial = collect("ci_serial")
    assert not parallel & serial
    assert parallel | serial == all_nodes
    assert len(serial) == serial_count


def test_real_skill_install_cancellation_is_marked_ci_serial() -> None:
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_engine/test_skill_install_turn.py"),
        "test_explicit_turn_deadline_cancels_install_and_preserves_receipt",
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process and mapped-file lifecycle")
@pytest.mark.ci_serial
def test_gateway_cleanup_waits_for_writer_after_launcher_exit(tmp_path: Path) -> None:
    harness = runpy.run_path("tests/functional/test_gateway_silent_reply_process_e2e.py")
    writer = tmp_path / "writer.py"
    writer.write_text(
        "import datetime, json, mmap, os, pathlib, sys, time\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "with (root / 'mapped.db').open('w+b') as stream:\n"
        "    stream.truncate(32768)\n"
        "    with mmap.mmap(stream.fileno(), 0) as mapping:\n"
        "        mapping[:4] = b'live'\n"
        "        (root / 'gateway.pid').write_text(json.dumps({\n"
        "            'pid': os.getpid(),\n"
        "            'start_ts': datetime.datetime.now(datetime.UTC).isoformat(),\n"
        "        }), encoding='utf-8')\n"
        "        (root / 'ready').touch()\n"
        "        deadline = time.monotonic() + 15\n"
        "        while not (root / 'release').exists():\n"
        "            if time.monotonic() >= deadline:\n"
        "                raise TimeoutError('parent did not release mapped file')\n"
        "            time.sleep(0.01)\n",
        encoding="utf-8",
    )
    launcher_code = (
        "import pathlib, subprocess, sys, time\n"
        "root = pathlib.Path(sys.argv[2])\n"
        "child = subprocess.Popen([sys.executable, sys.argv[1], str(root)],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "while not (root / 'ready').exists():\n"
        "    if child.poll() is not None:\n"
        "        raise RuntimeError('writer exited before ready')\n"
        "    time.sleep(0.01)\n"
    )
    # A direct interpreter gives the fixture a real launcher that can finish
    # while its child still owns mappings, without depending on uv teardown timing.
    launcher = subprocess.Popen(
        [getattr(sys, "_base_executable", sys.executable), "-c", launcher_code,
         str(writer), str(tmp_path)],
    )
    identity = None
    release = tmp_path / "release"
    timer = threading.Timer(0.2, release.touch)
    try:
        assert launcher.wait(timeout=10) == 0
        identity = harness["_open_gateway_process_handle"](tmp_path)
        assert identity is not None
        kernel32, handle = identity
        assert kernel32.WaitForSingleObject(handle, 0) == 258
        with (tmp_path / "mapped.db").open("r+b") as stream, pytest.raises(OSError):
            stream.truncate(0)
        timer.start()
        harness["_stop_process"](launcher, tmp_path)
        assert kernel32.WaitForSingleObject(handle, 0) == 0
        with (tmp_path / "mapped.db").open("r+b") as stream:
            stream.truncate(0)
    finally:
        release.touch()
        timer.cancel()
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=10)
        if identity is not None:
            kernel32, handle = identity
            try:
                assert kernel32.WaitForSingleObject(handle, 10_000) == 0
            finally:
                kernel32.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process birth identity")
def test_gateway_cleanup_ignores_reused_pid(tmp_path: Path) -> None:
    harness = runpy.run_path("tests/functional/test_gateway_silent_reply_process_e2e.py")
    (tmp_path / "gateway.pid").write_text(
        json.dumps({"pid": os.getpid(), "start_ts": "1970-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    assert harness["_open_gateway_process_handle"](tmp_path) is None


@pytest.mark.parametrize(
    "function_name",
    [
        "test_native_move_moves_a_regular_tree_between_real_parents",
        "test_windows_real_legacy_lock_survives_profile_move_and_rebind",
        "test_windows_real_replacement_locks_survive_two_profile_moves",
        "test_windows_real_recent_locked_profile_tree_moves_without_metadata_false_positive",
        "test_windows_primitive_collision_preserves_both_trees",
        "test_windows_native_move_refuses_real_cross_volume_move",
        "test_windows_native_move_handles_real_path_longer_than_260_characters",
        "test_windows_native_move_rejects_real_junction_in_source_tree",
        "test_windows_no_replace_pins_both_parents_during_real_mutation_window",
    ],
)
def test_native_recovery_global_state_contracts_are_marked_ci_serial(
    function_name: str,
) -> None:
    assert "pytest.mark.ci_serial" in _function_decorators(
        Path("tests/test_recovery/test_atomic_and_locking.py"), function_name
    )


def test_xdist_runtime_roots_are_worker_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conftest_module = runpy.run_path(
        Path("tests/conftest.py").as_posix(),
        run_name="pytest_conftest_contract",
    )
    state_root = tmp_path / "state"
    log_root = tmp_path / "logs"
    user_state_root = tmp_path / "user-state"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(state_root))
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(log_root))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(user_state_root))
    monkeypatch.delenv("OPENSQUILLA_PYTEST_XDIST_SCOPE", raising=False)

    conftest_module["pytest_configure"](
        SimpleNamespace(workerinput={"workerid": "gw2", "testrunuid": "run/unsafe"})
    )

    expected_suffix = Path(".pytest-xdist") / "run_unsafe" / "gw2"
    for env_key, root in (
        ("OPENSQUILLA_STATE_DIR", state_root),
        ("OPENSQUILLA_LOG_DIR", log_root),
        ("OPENSQUILLA_USER_STATE_DIR", user_state_root),
    ):
        scoped = Path(os.environ[env_key])
        assert scoped == root / expected_suffix
        assert scoped.is_dir()
    assert os.environ["OPENSQUILLA_PYTEST_XDIST_SCOPE"] == "run_unsafe/gw2"


def test_approval_queue_default_path_uses_worker_scoped_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.application import approval_queue as approval_queue_module

    conftest_module = runpy.run_path(
        Path("tests/conftest.py").as_posix(),
        run_name="pytest_conftest_approval_queue_contract",
    )
    state_root = tmp_path / "state-root"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(state_root))
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    monkeypatch.delenv("OPENSQUILLA_PYTEST_XDIST_SCOPE", raising=False)
    monkeypatch.setattr(approval_queue_module, "_DEFAULT_APPROVAL_QUEUE_PATH", None)

    conftest_module["pytest_configure"](
        SimpleNamespace(workerinput={"workerid": "gw3", "testrunuid": "queue-run"})
    )
    queue = approval_queue_module.ApprovalQueue()
    try:
        assert queue._db_path == (
            state_root
            / ".pytest-xdist"
            / "queue-run"
            / "gw3"
            / "state"
            / "approval_queue.sqlite"
        )
    finally:
        queue.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".", "unknown"),
        ("..", "unknown"),
        ("...", "unknown"),
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("LPT9", "_LPT9"),
        ("run/unsafe", "run_unsafe"),
    ],
)
def test_xdist_runtime_component_is_cross_platform_path_safe(
    raw: str,
    expected: str,
) -> None:
    conftest_module = runpy.run_path(
        Path("tests/conftest.py").as_posix(),
        run_name="pytest_conftest_component_contract",
    )

    assert conftest_module["_safe_xdist_component"](raw) == expected


def test_live_xdist_worker_uses_isolated_runtime_roots() -> None:
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker_id:
        pytest.skip("contract is exercised inside an xdist worker")

    for env_key in (
        "OPENSQUILLA_STATE_DIR",
        "OPENSQUILLA_LOG_DIR",
        "OPENSQUILLA_USER_STATE_DIR",
    ):
        parts = Path(os.environ[env_key]).parts
        assert ".pytest-xdist" in parts
        assert parts[-1] == worker_id


def test_prebuilt_core_wheel_environment_is_content_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conftest_module = runpy.run_path(
        Path("tests/conftest.py").as_posix(),
        run_name="pytest_core_wheel_contract",
    )
    wheel = tmp_path / "opensquilla-0-py3-none-any.whl"
    wheel.write_bytes(b"immutable wheel contract")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    monkeypatch.setenv("OPENSQUILLA_TEST_CORE_WHEEL", str(wheel))
    monkeypatch.setenv("OPENSQUILLA_TEST_CORE_WHEEL_SHA256", digest)

    assert conftest_module["_prebuilt_core_wheel_from_environment"]() == wheel.resolve()

    wheel.write_bytes(b"changed")
    with pytest.raises(AssertionError, match="SHA-256 mismatch"):
        conftest_module["_prebuilt_core_wheel_from_environment"]()


def test_windows_shard_responsibilities_cover_high_risk_surfaces() -> None:
    expected = {
        "tests/test_ci/test_router_artifact_manifest.py": "core",
        "tests/test_channels/test_feishu_sdk_websocket.py": "gateway-sqlite",
        "tests/test_gateway/test_task_runtime_cancellation_settlement.py": "gateway-sqlite",
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


def test_windows_execution_partitions_cover_every_file_once_within_its_family() -> None:
    root = Path.cwd()
    discovered = set(discover_test_files(root))
    assignments = partition_assignments()
    assert set(assignments) <= discovered
    physical_files = {
        shard: set(validated_files_for_shard(root, shard, platform="windows"))
        for shard in WINDOWS_SHARD_NAMES
    }
    assert len(physical_files) == 23
    catalog = json.loads(Path(".github/ci/suites.v1.json").read_text(encoding="utf-8"))
    assert tuple(catalog["full_python_matrix"]["windows"]) == WINDOWS_SHARD_NAMES
    assert tuple(catalog["full_python_matrix"]["ubuntu"]) == SHARED_SHARD_NAMES
    assert tuple(catalog["python_shard_families"]) == SHARD_NAMES
    assert all(physical_files.values())
    assert set().union(*physical_files.values()) == discovered
    assert sum(map(len, physical_files.values())) == len(discovered)
    for family in SHARD_NAMES:
        family_files = [paths for shard, paths in physical_files.items()
                        if shard_family(shard) == family]
        assert len(family_files) == (11 if family == "gateway-sqlite" else 4)
        assert set().union(*family_files) == set(files_for_shard(root, family))
    for path, physical in assignments.items():
        assert shard_family(physical) == shard_for_test(path)
        expected = SHARD_MODULE["windows_partition_overrides"]().get(path, physical)
        assert windows_shard_for_test(path) == expected


def test_planned_windows_inventory_preserves_owners_for_matrix_subsets(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    # Retain every nonempty plan over the original ten physical labels, then
    # add every singleton, pair, omitted-owner boundary, and full matrix
    # plan. This preserves the original exhaustive check without expanding to
    # a million inventory scans over all matrix combinations.
    effective = {**partition_assignments(), **SHARD_MODULE["windows_partition_overrides"]()}
    samples = {
        shard: next(path for path, owner in effective.items() if owner == shard)
        for shard in WINDOWS_SHARD_NAMES
    }
    for path in samples.values():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    legacy_shards = tuple(
        f"{family}-{partition}"
        for family in SHARD_NAMES
        for partition in range(1, 5 if family == "gateway-sqlite" else 3)
    )
    plans = {
        planned
        for size in range(1, len(legacy_shards) + 1)
        for planned in itertools.combinations(legacy_shards, size)
    }
    for size in (1, 2, len(WINDOWS_SHARD_NAMES) - 1, len(WINDOWS_SHARD_NAMES)):
        plans.update(itertools.combinations(WINDOWS_SHARD_NAMES, size))
    for planned in sorted(plans):
        selected = files_for_planned_windows_shards(tmp_path, planned[0], json.dumps(planned))
        assert set(selected) == {samples[shard] for shard in planned}, planned


@pytest.mark.parametrize("planned", [
    "", "not-json", "null", "{}", "[]", '["core-1", "core-1"]',
    '["core-1", "unknown"]', '["core-1", 1]', '["core-2"]', '["core"]',
])
def test_planned_windows_inventory_cli_rejects_invalid_proof_before_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], planned: str,
) -> None:
    args = SHARD_MODULE["_parser"]().parse_args([
        "list", "core-1", "--root", str(tmp_path), "--planned-windows-shards", planned,
    ])
    with pytest.raises(ValueError):
        args.handler(args)
    assert capsys.readouterr().out == ""


def test_planned_windows_inventory_cli_propagates_collection_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    args = SHARD_MODULE["_parser"]().parse_args([
        "list", "core-1", "--root", str(tmp_path),
        "--planned-windows-shards", '["core-1"]',
    ])
    with pytest.raises(ValueError, match="cannot read pytest collection contract"):
        args.handler(args)
    assert capsys.readouterr().out == ""


def test_windows_partitions_stay_fixed_when_duration_weights_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignments = {**partition_assignments(), **SHARD_MODULE["windows_partition_overrides"]()}
    before = partition_snapshot_fingerprint()

    def unexpected_weight_access() -> dict[str, float]:
        raise AssertionError("partition scheduling must not consult duration weights")

    monkeypatch.setitem(
        windows_shard_for_test.__globals__, "historical_test_weights", unexpected_weight_access
    )
    assert {path: windows_shard_for_test(path) for path in assignments} == assignments
    assert partition_snapshot_fingerprint() == before


def test_windows_new_file_fallback_is_stable_and_keeps_environment_family() -> None:
    paths = (
        "tests/test_new_partition_fallback.py",
        "tests/test_gateway/test_new_partition_fallback.py",
        "tests/test_recovery/test_new_partition_fallback.py",
        "tests/test_desktop/test_new_partition_fallback.py",
    )
    assert not set(paths).intersection(partition_assignments())
    first = {path: windows_shard_for_test(path) for path in paths}
    assert {path: windows_shard_for_test(path) for path in reversed(paths)} == first
    for path, shard in first.items():
        assert shard in WINDOWS_SHARD_NAMES
        assert shard_family(shard) == shard_for_test(path)


def test_gateway_new_file_fallback_uses_all_eight_partitions() -> None:
    paths = [f"tests/test_gateway/test_new_partition_fallback_{index}.py"
             for index in range(64)]
    assert not set(paths).intersection(partition_assignments())
    assert {windows_shard_for_test(path) for path in paths} == {
        f"gateway-sqlite-{partition}" for partition in range(1, 9)
    }
    assert all(
        windows_shard_for_test(path)
        == SHARD_MODULE["execution_shard_for_test"](path, platform="ubuntu")
        for path in paths
    )


def test_windows_gateway_overlay_preserves_source_inventory_and_ubuntu_snapshot() -> None:
    root = Path.cwd()
    source = "gateway-sqlite-5"
    extras = SHARD_MODULE["WINDOWS_EXTRA_SHARD_NAMES"]
    original = set(validated_files_for_shard(root, source, platform="ubuntu"))
    groups = {shard: set(validated_files_for_shard(root, shard, platform="windows"))
              for shard in (source, *extras)}
    source_snapshot = {path for path, shard in partition_assignments().items() if shard == source}
    assert len(source_snapshot) == 92
    assert [len(groups[shard] & source_snapshot) for shard in (source, *extras)] == [25, 22, 22, 23]
    assert set().union(*groups.values()) == original
    assert sum(map(len, groups.values())) == len(original)
    serial_files = {
        "tests/functional/test_gateway_stop_process_tree_e2e.py",
        "tests/test_gateway/test_manual_compaction_contract.py",
        "tests/test_gateway/test_plan_rpc.py",
    }
    assert serial_files <= groups[source]
    # This is the actual round-5 partition hash, before the Windows-only move.
    assert partition_snapshot_fingerprint(platform="ubuntu") == (
        "0591c1457e43e3cb61ae8d367ff02757692644f1cd4356754282cca9a9dc4f71"
    )
    assert partition_snapshot_fingerprint() != partition_snapshot_fingerprint(platform="ubuntu")
    for shard in SHARED_SHARD_NAMES:
        if shard != source:
            assert files_for_shard(root, shard, platform="windows") == files_for_shard(
                root, shard, platform="ubuntu",
            )


@pytest.mark.parametrize("mutation", [
    "missing_target", "unknown_target", "wrong_source", "new_file", "duplicate",
    "duplicate_within", "unsorted", "not_list", "non_string",
])
def test_windows_gateway_overlay_rejects_invalid_ownership(mutation: str) -> None:
    payload = json.loads(Path(".github/scripts/windows_test_partitions.json").read_text())
    overrides = payload["windows_overrides"]
    if mutation == "missing_target":
        del overrides["gateway-sqlite-9"]
    elif mutation == "unknown_target":
        overrides["gateway-sqlite-12"] = overrides.pop("gateway-sqlite-9")
    elif mutation == "wrong_source":
        overrides["gateway-sqlite-9"] = payload["partitions"]["gateway-sqlite-4"][:1]
    elif mutation == "new_file":
        overrides["gateway-sqlite-9"] = ["tests/test_gateway/test_new_unreviewed.py"]
    elif mutation == "duplicate":
        overrides["gateway-sqlite-10"] = overrides["gateway-sqlite-9"][:1]
    elif mutation == "duplicate_within":
        overrides["gateway-sqlite-9"] = overrides["gateway-sqlite-9"][:1] * 2
    elif mutation == "unsorted":
        overrides["gateway-sqlite-9"].reverse()
    elif mutation == "not_list":
        overrides["gateway-sqlite-9"] = {}
    elif mutation == "non_string":
        overrides["gateway-sqlite-9"] = [None]
    with pytest.raises(ValueError, match="windows_overrides"):
        validate_partition_payload(payload)


@pytest.mark.parametrize("host,target", [("Windows", "windows"), ("Linux", "ubuntu")])
def test_execution_inventory_uses_native_or_explicit_platform(
    host: str, target: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = SHARD_MODULE["execution_shard_for_test"]
    monkeypatch.setattr(selector.__globals__["platform"], "system", lambda: host)
    path, destination = next(iter(SHARD_MODULE["windows_partition_overrides"]().items()))
    assert selector(path) == (destination if target == "windows" else "gateway-sqlite-5")
    assert windows_shard_for_test(path) == destination
    assert selector(path, platform="ubuntu") == "gateway-sqlite-5"
    assert selector(path, platform="windows") == destination
    for getter in (files_for_shard, validated_files_for_shard):
        with pytest.raises(ValueError, match="Windows-only shard"):
            getter(Path.cwd(), destination, platform="ubuntu")


@pytest.mark.parametrize("target", ["windows", "ubuntu"])
def test_planned_cli_uses_target_platform_for_overlay_owners(
    target: str, capsys: pytest.CaptureFixture[str],
) -> None:
    path, destination = next(iter(SHARD_MODULE["windows_partition_overrides"]().items()))
    shard = destination if target == "windows" else "gateway-sqlite-5"
    args = SHARD_MODULE["_parser"]().parse_args([
        "list", shard, "--platform", target, "--planned-windows-shards", json.dumps([shard]),
    ])
    assert args.handler(args) == 0
    assert path in capsys.readouterr().out.splitlines()


def test_overlay_change_is_bound_to_windows_partition_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = partition_snapshot_fingerprint()
    ubuntu = partition_snapshot_fingerprint(platform="ubuntu")
    overlay = dict(SHARD_MODULE["windows_partition_overrides"]())
    overlay.pop(next(iter(overlay)))
    monkeypatch.setitem(
        partition_snapshot_fingerprint.__globals__, "windows_partition_overrides", lambda: overlay,
    )
    assert partition_snapshot_fingerprint() != before
    assert partition_snapshot_fingerprint(platform="ubuntu") == ubuntu


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing_partition", "every Windows execution shard"),
        ("duplicate_file", "duplicate Windows partition"),
        ("cross_family", "crosses responsibility families"),
        ("path_escape", "invalid Windows partition test path"),
        ("unsorted", "is not sorted"),
    ],
)
def test_windows_partition_snapshot_rejects_invalid_coverage(case: str, message: str) -> None:
    partitions: dict[str, list[str]] = {shard: [] for shard in SHARED_SHARD_NAMES}
    if case == "missing_partition":
        del partitions["core-2"]
    elif case == "duplicate_file":
        partitions["core-1"] = ["tests/test_partition_fixture.py"]
        partitions["core-2"] = ["tests/test_partition_fixture.py"]
    elif case == "cross_family":
        partitions["core-1"] = ["tests/test_gateway/test_partition_fixture.py"]
    elif case == "path_escape":
        partitions["core-1"] = ["tests/../test_partition_fixture.py"]
    elif case == "unsorted":
        partitions["core-1"] = ["tests/test_partition_z.py", "tests/test_partition_a.py"]
    with pytest.raises(ValueError, match=message):
        validate_partition_payload({"schema_version": 1, "partitions": partitions})


def test_windows_physical_metadata_binds_both_family_and_partition_snapshots(
    tmp_path: Path,
) -> None:
    write_metadata = SHARD_MODULE["_write_run_metadata"]
    path = tmp_path / "metadata.json"
    write_metadata(path, "gateway-sqlite-2", (), parallel_workers=3, execution_platform="windows")
    physical = json.loads(path.read_text(encoding="utf-8"))
    assert physical["shard"] == "gateway-sqlite-2"
    assert physical["family"] == "gateway-sqlite"
    assert physical["partition_sha256"] == partition_snapshot_fingerprint()
    assert physical["assignment_sha256"] == assignment_snapshot_fingerprint()
    assert physical["execution"]["parallel"]["workers"] == 3
    assert physical["execution_shards"] == list(WINDOWS_SHARD_NAMES)

    write_metadata(path, "gateway-sqlite", (), parallel_workers=2)
    family = json.loads(path.read_text(encoding="utf-8"))
    assert family["assignment_sha256"] == physical["assignment_sha256"]
    assert "partition_sha256" not in family
    assert "family" not in family


def _bundle_platform_matrix() -> list[dict[str, str]]:
    return [
        {"suite": "windows-high-risk", "os": "windows-latest", "shard": shard}
        for shard in (SHARD_MODULE["BUNDLE_OWNER"], *SHARD_MODULE["BUNDLE_SHARDS"])
    ]


def test_bundle_delegation_is_explicit_and_preserves_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegate = SHARD_MODULE["_delegate_bundle_files"]
    monkeypatch.setattr(delegate.__globals__["platform"], "system", lambda: "Windows")
    owner = SHARD_MODULE["BUNDLE_OWNER"]
    original = validated_files_for_shard(Path.cwd(), owner)
    bundle = SHARD_MODULE["BUNDLE_TEST_FILE"]
    matrix = _bundle_platform_matrix()
    # The full platform plan also contains Ubuntu and unrelated platform suites.
    matrix.append({"suite": "python-full", "os": "ubuntu-latest", "shard": owner})
    executed = delegate(original, owner, json.dumps(matrix))
    assert set(original) - set(executed) == {bundle}
    assert len(executed) == len(original) - 1
    assert validated_files_for_shard(Path.cwd(), owner) == original
    assert bundle in files_for_shard(Path.cwd(), owner)

    metadata_path = tmp_path / "metadata.json"
    SHARD_MODULE["_write_run_metadata"](
        metadata_path, owner, executed, parallel_workers=2, delegate_bundle=True,
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert bundle not in metadata["test_files"]
    assert metadata["execution"]["parallel"]["item_parallel_files"] == []
    assert metadata["delegated_case_files"] == [{
        "path": bundle, "shards": ["bundle-1", "bundle-2", "bundle-3", "bundle-4"],
    }]


@pytest.mark.parametrize("mutation", [
    "linux", "wrong_owner", "missing_file", "missing_matrix", "invalid_json",
    "object_matrix", "missing_part", "duplicate_part", "wrong_suite", "wrong_os",
    "extra_part", "missing_owner", "malformed_cell", "non_string_shard",
])
def test_bundle_delegation_rejects_incomplete_native_plan(
    monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    delegate = SHARD_MODULE["_delegate_bundle_files"]
    monkeypatch.setattr(
        delegate.__globals__["platform"], "system",
        lambda: "Linux" if mutation == "linux" else "Windows",
    )
    owner = SHARD_MODULE["BUNDLE_OWNER"]
    files = (SHARD_MODULE["BUNDLE_TEST_FILE"], "tests/test_other.py")
    matrix: Any = _bundle_platform_matrix()
    if mutation == "wrong_owner":
        owner = "core-1"
    elif mutation == "missing_file":
        files = ("tests/test_other.py",)
    elif mutation == "object_matrix":
        matrix = {"include": matrix}
    elif mutation == "missing_part":
        matrix.pop()
    elif mutation == "duplicate_part":
        matrix.append(dict(matrix[-1]))
    elif mutation == "wrong_suite":
        matrix[-1]["suite"] = "python-full"
    elif mutation == "wrong_os":
        matrix[-1]["os"] = "ubuntu-latest"
    elif mutation == "extra_part":
        matrix.append({"suite": "windows-high-risk", "os": "windows-latest", "shard": "bundle-5"})
    elif mutation == "missing_owner":
        matrix.pop(0)
    elif mutation == "malformed_cell":
        matrix[-1].pop("os")
    elif mutation == "non_string_shard":
        matrix[-1]["shard"] = []
    raw = None if mutation == "missing_matrix" else json.dumps(matrix)
    if mutation == "invalid_json":
        raw = "{"
    with pytest.raises(ValueError):
        delegate(files, owner, raw)


def test_platform_matrix_requires_explicit_bundle_delegation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SHARD_MODULE["_run"]
    monkeypatch.setitem(
        run.__globals__, "validated_files_for_shard", lambda *_, **__: ("test.py",),
    )
    with pytest.raises(ValueError, match="requires --delegate-bundle"):
        run(SimpleNamespace(
            root=tmp_path, shard="core-1", delegate_bundle=False,
            planned_platform_matrix=json.dumps(_bundle_platform_matrix()),
        ))


@pytest.mark.parametrize("delegate_bundle", [False, True])
def test_bundle_delegation_changes_only_explicit_runner_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delegate_bundle: bool,
) -> None:
    run = SHARD_MODULE["_run"]
    bundle = SHARD_MODULE["BUNDLE_TEST_FILE"]
    files = (bundle, "tests/test_other.py")
    for relative in files:
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("def test_case(): pass\n", encoding="utf-8")
    monkeypatch.setitem(run.__globals__, "validated_files_for_shard", lambda *_, **__: files)
    monkeypatch.setattr(run.__globals__["platform"], "system", lambda: "Windows")
    selections: list[tuple[str, ...]] = []

    def fake_pytest(root: Path, arguments: list[str], *, phase: str) -> int:
        argfile = Path(next(arg[1:] for arg in arguments if arg.startswith("@")))
        selected = tuple(argfile.read_text(encoding="utf-8").splitlines())
        selections.append(selected)
        junit = Path(next(
            arg.split("=", 1)[1] for arg in arguments if arg.startswith("--junitxml=")
        ))
        suite = ET.Element("testsuite", tests="1" if phase == "parallel" else "0")
        if phase == "parallel":
            ET.SubElement(suite, "testcase", classname="tests.test_other", name="test_case")
        ET.ElementTree(suite).write(junit, encoding="utf-8")
        return 0 if phase == "parallel" else 5

    monkeypatch.setitem(run.__globals__, "_run_pytest_subprocess", fake_pytest)
    metadata = tmp_path / "reports" / "metadata.json"
    assert run(SimpleNamespace(
        root=tmp_path, shard=SHARD_MODULE["BUNDLE_OWNER"], delegate_bundle=delegate_bundle,
        planned_platform_matrix=json.dumps(_bundle_platform_matrix()) if delegate_bundle else None,
        junit=metadata.with_name("junit.xml"), summary=metadata.with_name("summary.txt"),
        metadata=metadata, workers=2, pytest_args=[],
    )) == 0
    expected = ("tests/test_other.py",) if delegate_bundle else files
    assert selections == [expected, expected]
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert payload["test_files"] == list(expected)
    assert bool(payload.get("delegated_case_files")) == delegate_bundle


def test_windows_assignment_snapshot_governs_reviewed_rebalancing() -> None:
    baseline, assignments, guardrails, overrides = assignment_governance()
    report = assignment_governance_summary(Path.cwd())

    expected_moved_paths = {
        "tests/test_gateway/test_rpc_sessions.py",
        "tests/test_recovery/test_session_merge.py",
        "tests/test_engine/test_placeholder_escalation_and_wrapup.py",
        "tests/test_gateway/test_goal_rpc.py",
        "tests/test_gateway/test_rpc_router_decisions.py",
        "tests/test_live_long_task_case_driver.py",
        "tests/test_live_multi_provider_matrix.py",
        "tests/test_observability/test_bundle.py",
        "tests/test_persistence/test_router_decision_writer.py",
        "tests/test_sandbox/test_windows_default_capability.py",
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


def test_active_unweighted_fallback_retains_registered_inventory() -> None:
    discovered = set(discover_test_files(Path.cwd()))
    weighted = set(historical_test_weights())
    unweighted = discovered - weighted

    assert OFFLINE_MARKER_EXCLUSIONS <= unweighted
    assert RECENTLY_ADDED_ACTIVE_TESTS <= weighted
    # Missing timing samples are reported by the planner; coverage and explicit
    # registrations remain required independently of this performance debt.


def test_missing_timing_weights_warn_without_excluding_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "repository"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\nnorecursedirs = []\n", encoding="utf-8"
    )
    paths = {f"tests/test_new_{index}.py" for index in range(6)}
    for path in paths:
        (root / path).write_text("def test_new(): pass\n", encoding="utf-8")
    report_fn = SHARD_MODULE["_report"]
    monkeypatch.setitem(report_fn.__globals__, "historical_test_weights", lambda: {})
    monkeypatch.setitem(report_fn.__globals__, "assignment_governance", lambda: ({}, {}, {}, []))
    summary = tmp_path / "summary.md"

    assert report_fn(SimpleNamespace(root=root, github_summary=summary)) == 0
    output = capsys.readouterr().out
    assert "::warning title=Test timing refresh recommended::" in output
    for path in paths:
        assert path in summary.read_text(encoding="utf-8")
    assigned = [path for shard in SHARD_NAMES for path in files_for_shard(root, shard)]
    assert len(assigned) == len(set(assigned)) == len(paths)
    assert set(assigned) == paths


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
        "tests/test_gateway/test_rpc_sessions.py": "core",
        "tests/test_recovery/test_session_merge.py": "core",
        "tests/contracts/test_gateway_contract_parallel.py": "core",
        "tests/test_ci/test_migrations_packaged.py": "core",
        "tests/test_gateway/test_goal_rpc.py": "desktop-installer-contracts",
        "tests/test_gateway/test_rpc_router_decisions.py": (
            "desktop-installer-contracts"
        ),
        "tests/test_observability/test_bundle.py": "desktop-installer-contracts",
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
    # This contract can itself execute inside the repository's xdist phase.
    # The nested runner under test starts as a fresh controller, so do not let
    # the outer worker identity leak into its environment.
    for key in (
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
        "OPENSQUILLA_PYTEST_XDIST_SCOPE",
        "OPENSQUILLA_TEST_CORE_WHEEL",
        "OPENSQUILLA_TEST_CORE_WHEEL_SHA256",
    ):
        env.pop(key, None)

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
    assert len(metadata_payload["test_files_sha256"]) == 64
    assert metadata_payload["execution"] == {
        "parallel": {
            "dist": "loadfile",
            "item_parallel_files": [],
            "marker": "(not stress) and (not ci_serial)",
            "workers": 4,
        },
        "serial": {"marker": "(not stress) and (ci_serial)", "workers": 1},
    }
    assert len(metadata_payload["assignment_sha256"]) == 64
    text = summary.read_text(encoding="utf-8")
    assert "pytest_exit_code=1" in text
    assert "parallel_pytest_exit_code=1" in text
    assert "serial_pytest_exit_code=5" in text
    assert "junit_status=failed" in text
    assert "synthetic shard failure" in text


@pytest.mark.parametrize("fail_case", [False, True])
def test_audited_bundle_cases_overlap_without_splitting_other_files(
    tmp_path: Path, fail_case: bool,
) -> None:
    """Exercise the real runner, worker scheduling, serial phase and failure gate."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers = ["ci_serial: serial CI contract"]\n',
        encoding="utf-8",
    )
    audited_path = "tests/test_observability/test_bundle.py"
    ordinary_path = next(
        path for path, owner in partition_assignments().items()
        if owner == "desktop-installer-contracts-1" and path != audited_path
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    common = (
        "import json, os, time\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "EVIDENCE = Path(os.environ['EVIDENCE_DIR'])\n"
        "WORKER = os.environ.get('PYTEST_XDIST_WORKER')\n"
    )
    bundle = tmp_path / audited_path
    bundle.parent.mkdir(parents=True)
    bundle.write_text(
        common
        + "@pytest.mark.parametrize('case', range(6))\n"
        "def test_parallel(case):\n"
        "    assert WORKER is not None\n"
        "    (EVIDENCE / f'bundle-{case}.json').write_text(json.dumps(WORKER))\n"
        "    (EVIDENCE / f'worker-{WORKER}.ready').write_text('ready')\n"
        "    deadline = time.monotonic() + 20\n"
        "    while len(list(EVIDENCE.glob('worker-*.ready'))) < 2:\n"
        "        assert time.monotonic() < deadline, 'audited cases did not overlap'\n"
        "        time.sleep(0.01)\n"
        "    assert not (case == 3 and os.environ['FAIL_CASE'] == '1'), 'synthetic failure'\n"
        "@pytest.mark.ci_serial\n"
        "def test_serial():\n"
        "    assert WORKER is None\n"
        "    assert len(list(EVIDENCE.glob('bundle-*.json'))) == 6\n"
        "    (EVIDENCE / 'serial.txt').write_text('completed')\n",
        encoding="utf-8",
    )
    ordinary = tmp_path / ordinary_path
    ordinary.parent.mkdir(parents=True, exist_ok=True)
    ordinary.write_text(
        common
        + "@pytest.mark.parametrize('case', range(3))\n"
        "def test_keeps_file_group(case):\n"
        "    assert WORKER is not None\n"
        "    (EVIDENCE / f'ordinary-{case}.json').write_text(json.dumps(WORKER))\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    for key in (
        "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID",
        "OPENSQUILLA_PYTEST_XDIST_SCOPE", "OPENSQUILLA_TEST_CORE_WHEEL",
        "OPENSQUILLA_TEST_CORE_WHEEL_SHA256",
    ):
        env.pop(key, None)
    env.update(EVIDENCE_DIR=str(evidence), FAIL_CASE=str(int(fail_case)))
    reports = tmp_path / "reports"
    result = subprocess.run(
        [
            sys.executable, str(SHARD_SCRIPT.resolve()), "run",
            "desktop-installer-contracts-1", "--root", str(tmp_path),
            "--junit", str(reports / "junit.xml"),
            "--summary", str(reports / "summary.txt"),
            "--metadata", str(reports / "metadata.json"),
            "--workers", "2", "--", "-q", "-o", "addopts=",
        ],
        env=env, text=True, capture_output=True, check=False, timeout=90,
    )
    assert result.returncode == int(fail_case), result.stdout + result.stderr
    bundle_workers = [json.loads(path.read_text()) for path in evidence.glob("bundle-*.json")]
    assert len(bundle_workers) == 6
    assert len(set(bundle_workers)) == 2
    ordinary_workers = [json.loads(path.read_text()) for path in evidence.glob("ordinary-*.json")]
    assert len(ordinary_workers) == 3
    assert len(set(ordinary_workers)) == 1
    assert (evidence / "serial.txt").read_text() == "completed"
    cases = list(ET.parse(reports / "junit.xml").getroot().iter("testcase"))
    assert len(cases) == 10
    assert sum(case.find("failure") is not None for case in cases) == int(fail_case)
    metadata = json.loads((reports / "metadata.json").read_text())
    assert metadata["execution"]["parallel"]["item_parallel_files"] == [audited_path]
    summary = (reports / "summary.txt").read_text()
    assert f"parallel_pytest_exit_code={int(fail_case)}" in summary
    assert "serial_pytest_exit_code=0" in summary


def test_windows_shard_runner_uses_argfile_for_large_file_selection() -> None:
    files = tuple(
        f"tests/test_gateway/test_long_windows_selection_{index:04d}.py"
        for index in range(600)
    )

    with pytest_file_selection_arg(files) as selection_arg:
        argfile = Path(selection_arg.removeprefix("@"))
        assert len(selection_arg) < 260
        assert argfile.read_text(encoding="utf-8").splitlines() == list(files)

    assert not argfile.exists()


def test_windows_shard_runner_accepts_parallel_no_tests_when_serial_passes(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'norecursedirs = ["tests/fixtures"]\n'
        'markers = ["ci_serial: synthetic serial CI contract"]\n',
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_serial_only.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.ci_serial\n"
        "def test_serial_only():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    report_dir = tmp_path / "reports"
    junit = report_dir / "junit.xml"
    summary = report_dir / "first-failure.txt"
    env = os.environ.copy()
    for key in (
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
        "OPENSQUILLA_PYTEST_XDIST_SCOPE",
        "OPENSQUILLA_TEST_CORE_WHEEL",
        "OPENSQUILLA_TEST_CORE_WHEEL_SHA256",
    ):
        env.pop(key, None)

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
            "--workers",
            "2",
            "--",
            "-q",
            "-m",
            "ci_serial",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary_text = summary.read_text(encoding="utf-8")
    assert "pytest_exit_code=0" in summary_text
    assert "parallel_pytest_exit_code=5" in summary_text
    assert "serial_pytest_exit_code=0" in summary_text
    assert "junit_status=passed" in summary_text
    junit_root = ET.parse(junit).getroot()
    assert junit_root.get("tests") == "1"
    assert len(list(junit_root.iter("testcase"))) == 1


def test_windows_shard_runner_finalizes_core_wheel_timeout_artifacts(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nnorecursedirs = ["tests/fixtures"]\n',
        encoding="utf-8",
    )
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "build_test_core_wheel.py").write_text(
        "import subprocess\n"
        "from pathlib import Path\n\n"
        "def build_isolated_core_wheel(repo_root: Path, temp_root: Path) -> Path:\n"
        "    raise subprocess.TimeoutExpired(cmd=['uv', 'build'], timeout=300)\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_needs_wheel.py").write_text(
        "def test_needs_wheel(isolated_core_wheel):\n"
        "    assert isolated_core_wheel\n",
        encoding="utf-8",
    )
    report_dir = tmp_path / "reports"
    junit = report_dir / "junit.xml"
    summary = report_dir / "first-failure.txt"
    env = os.environ.copy()
    for key in (
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
        "OPENSQUILLA_PYTEST_XDIST_SCOPE",
        "OPENSQUILLA_TEST_CORE_WHEEL",
        "OPENSQUILLA_TEST_CORE_WHEEL_SHA256",
    ):
        env.pop(key, None)

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
            "--workers",
            "2",
            "--",
            "-q",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 2
    assert "timed out after 300 seconds" in result.stderr
    summary_text = summary.read_text(encoding="utf-8")
    assert "pytest_status=started" not in summary_text
    assert "pytest_exit_code=2" in summary_text
    assert "junit_status=failed" in summary_text
    assert "TimeoutExpired" in summary_text
    junit_root = ET.parse(junit).getroot()
    assert junit_root.get("tests") == "1"
    assert junit_root.get("errors") == "1"
    error = junit_root.find(".//error")
    assert error is not None
    assert error.get("type") == "TimeoutExpired"


def test_windows_shard_runner_splits_parallel_and_serial_tests(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'norecursedirs = ["tests/fixtures"]\n'
        'markers = [\n'
        '  "ci_serial: synthetic serial CI contract",\n'
        '  "llm: synthetic excluded marker",\n'
        ']\n',
        encoding="utf-8",
    )
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "build_test_core_wheel.py").write_text(
        "from pathlib import Path\n\n"
        "def build_isolated_core_wheel(repo_root: Path, temp_root: Path) -> Path:\n"
        "    counter = Path(__import__('os').environ['PREBUILD_COUNTER'])\n"
        "    count = int(counter.read_text()) if counter.exists() else 0\n"
        "    counter.write_text(str(count + 1))\n"
        "    temp_root.mkdir(parents=True)\n"
        "    wheel = temp_root / 'opensquilla-0-py3-none-any.whl'\n"
        "    wheel.write_bytes(b'synthetic immutable wheel')\n"
        "    return wheel\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "conftest.py").write_text(
        "import hashlib\n"
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        "@pytest.fixture(scope='session')\n"
        "def isolated_core_wheel():\n"
        "    wheel = Path(os.environ['OPENSQUILLA_TEST_CORE_WHEEL'])\n"
        "    assert wheel.is_file()\n"
        "    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()\n"
        "    assert digest == os.environ['OPENSQUILLA_TEST_CORE_WHEEL_SHA256']\n"
        "    return wheel\n",
        encoding="utf-8",
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    prebuild_counter = evidence_dir / "prebuild-count.txt"
    (tests_dir / "test_bulk.py").write_text(
        "import os\n"
        "from pathlib import Path\n\n"
        "def test_bulk_phase(isolated_core_wheel):\n"
        "    assert isolated_core_wheel.name.endswith('.whl')\n"
        "    worker = os.environ.get('PYTEST_XDIST_WORKER', '')\n"
        "    assert worker.startswith('gw')\n"
        "    (Path(os.environ['EVIDENCE_DIR']) / 'bulk.txt').write_text(worker)\n",
        encoding="utf-8",
    )
    (tests_dir / "test_serial.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        "@pytest.mark.ci_serial\n"
        "def test_serial_phase(isolated_core_wheel):\n"
        "    assert isolated_core_wheel.name.endswith('.whl')\n"
        "    assert 'PYTEST_XDIST_WORKER' not in os.environ\n"
        "    (Path(os.environ['EVIDENCE_DIR']) / 'serial.txt').write_text('serial')\n",
        encoding="utf-8",
    )
    report_dir = tmp_path / "reports"
    junit = report_dir / "junit.xml"
    summary = report_dir / "first-failure.txt"
    metadata = report_dir / "windows-shard-metadata.json"
    env = os.environ.copy()
    env["EVIDENCE_DIR"] = str(evidence_dir)
    env["PREBUILD_COUNTER"] = str(prebuild_counter)
    for key in (
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
        "OPENSQUILLA_PYTEST_XDIST_SCOPE",
        "OPENSQUILLA_TEST_CORE_WHEEL",
        "OPENSQUILLA_TEST_CORE_WHEEL_SHA256",
    ):
        env.pop(key, None)

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
            "--workers",
            "2",
            "--",
            "-q",
            "-m",
            "not llm",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("Prepared one shared isolated core wheel") == 1
    assert "Running parallel bulk phase with 2 workers" in result.stdout
    assert "Running serial phase in the controller process" in result.stdout
    assert prebuild_counter.read_text(encoding="utf-8") == "1"
    assert (evidence_dir / "bulk.txt").read_text(encoding="utf-8").startswith("gw")
    assert (evidence_dir / "serial.txt").read_text(encoding="utf-8") == "serial"
    junit_root = ET.parse(junit).getroot()
    assert junit_root.get("tests") == "2"
    assert len(list(junit_root.iter("testcase"))) == 2
    summary_text = summary.read_text(encoding="utf-8")
    assert "pytest_exit_code=0" in summary_text
    assert "parallel_pytest_exit_code=0" in summary_text
    assert "serial_pytest_exit_code=0" in summary_text
    metadata_payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert metadata_payload["test_files"] == [
        "tests/test_bulk.py",
        "tests/test_serial.py",
    ]
    assert metadata_payload["execution"]["parallel"]["workers"] == 2


@pytest.mark.parametrize(("selected_shard", "marker_args", "expected_cases"), [
    ("core-1", [], {"test_parallel", "test_serial"}),
    ("gateway-sqlite-3", ["-m", ""], {
        "test_parallel", "test_serial", "test_stress_parallel", "test_stress_serial",
    }),
    ("gateway-sqlite-4", ["-m", "stress"], {"test_stress_parallel", "test_stress_serial"}),
])
def test_windows_physical_runner_selects_partition_and_preserves_both_phases(
    tmp_path: Path, selected_shard: str, marker_args: list[str], expected_cases: set[str],
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-m \'not stress\'"\n'
        'markers = ["ci_serial: serial CI contract", "stress: capacity contract"]\n',
        encoding="utf-8",
    )
    family = shard_family(selected_shard)
    prefix = "tests/test_gateway" if family == "gateway-sqlite" else "tests"
    (tmp_path / prefix).mkdir(parents=True)
    selected_path = next(
        f"{prefix}/test_partition_probe_{index}.py"
        for index in range(100)
        if windows_shard_for_test(f"{prefix}/test_partition_probe_{index}.py") == selected_shard
    )
    (tmp_path / selected_path).write_text(
        "import os\nimport pytest\n\n"
        "def test_parallel():\n"
        "    assert os.environ.get('PYTEST_XDIST_WORKER', '').startswith('gw')\n\n"
        "@pytest.mark.ci_serial\n"
        "def test_serial():\n"
        "    assert 'PYTEST_XDIST_WORKER' not in os.environ\n\n"
        "@pytest.mark.stress\n"
        "def test_stress_parallel():\n"
        "    assert os.environ.get('PYTEST_XDIST_WORKER', '').startswith('gw')\n\n"
        "@pytest.mark.stress\n"
        "@pytest.mark.ci_serial\n"
        "def test_stress_serial():\n"
        "    assert 'PYTEST_XDIST_WORKER' not in os.environ\n",
        encoding="utf-8-sig",
    )
    unselected_path = next(
        f"{prefix}/test_partition_other_{index}.py"
        for index in range(100)
        if windows_shard_for_test(f"{prefix}/test_partition_other_{index}.py") != selected_shard
    )
    (tmp_path / unselected_path).write_text(
        "def test_must_not_execute():\n    assert False, 'another physical partition'\n",
        encoding="utf-8",
    )
    reports = tmp_path / "reports"
    env = os.environ.copy()
    for key in (
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
        "OPENSQUILLA_PYTEST_XDIST_SCOPE",
    ):
        env.pop(key, None)
    result = subprocess.run(
        [
            sys.executable,
            str(SHARD_SCRIPT.resolve()),
            "run",
            selected_shard,
            "--root",
            str(tmp_path),
            "--junit",
            str(reports / "junit.xml"),
            "--summary",
            str(reports / "summary.txt"),
            "--metadata",
            str(reports / "metadata.json"),
            "--workers",
            "1",
            "--",
            "-q",
            *marker_args,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    metadata = json.loads((reports / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["test_files"] == [selected_path]
    assert metadata["partition_sha256"] == partition_snapshot_fingerprint()
    assert metadata["family"] == family
    base_marker = marker_args[1] if marker_args else "not stress"
    for phase, selector in (("parallel", "not ci_serial"), ("serial", "ci_serial")):
        expected_marker = f"({base_marker}) and ({selector})" if base_marker else selector
        assert metadata["execution"][phase]["marker"] == expected_marker
    junit = ET.parse(reports / "junit.xml").getroot()
    assert {test.get("name") for test in junit.iter("testcase")} == expected_cases
    assert (reports / "junit.parallel.xml").is_file()
    assert (reports / "junit.serial.xml").is_file()


def test_windows_empty_physical_partition_fails_with_diagnostics(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    junit = tmp_path / "reports" / "junit.xml"
    summary = tmp_path / "reports" / "summary.txt"
    metadata = tmp_path / "reports" / "metadata.json"
    result = SHARD_MODULE["_run"](
        SimpleNamespace(
            root=tmp_path,
            shard="core-1",
            junit=junit,
            summary=summary,
            metadata=metadata,
            workers=4,
            pytest_args=[],
        )
    )
    assert result == 2
    assert ET.parse(junit).getroot().get("errors") == "1"
    assert "has no tests" in summary.read_text(encoding="utf-8")
    assert json.loads(metadata.read_text(encoding="utf-8"))["test_files"] == []
