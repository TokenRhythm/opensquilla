from __future__ import annotations

import shlex
from collections.abc import Iterable

from typer.testing import CliRunner

from opensquilla.cli.main import app
from opensquilla.health.evaluator import (
    evaluate_channels,
    evaluate_image_generation,
    evaluate_llm_ensemble,
    evaluate_logs,
    evaluate_memory,
    evaluate_memory_embedding,
    evaluate_provider,
    evaluate_router,
    evaluate_sandbox,
    evaluate_search,
)
from opensquilla.health.model import HealthFinding

runner = CliRunner()

_PLACEHOLDER_VALUES = {
    "YOUR_API_KEY": "dummy-key",
    "PATH_TO_ONNX_MODELS": "/tmp/onnx-models",
}

_COMMAND_HELP_PATHS = (
    ("channels", "enable"),
    ("channels", "pairings", "list"),
    ("channels", "restart"),
    ("channels", "status"),
    ("config", "set"),
    ("configure",),
    ("diagnostics", "status"),
    ("gateway", "restart"),
    ("gateway", "start"),
    ("gateway", "status"),
    ("memory", "repair", "list"),
    ("migrate", "opensquilla"),
    ("memory", "repair", "run"),
    ("memory", "status"),
    ("onboard",),
    ("onboard", "status"),
    ("providers", "configure"),
    ("providers", "list"),
    ("providers", "status"),
    ("sandbox", "full"),
    ("sandbox", "on"),
    ("sandbox", "status"),
    ("search", "list"),
    ("search", "status"),
)


def _help(args: list[str]) -> str:
    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0, result.stdout
    return result.stdout


def _normalize_command(command: str) -> str:
    for placeholder, value in _PLACEHOLDER_VALUES.items():
        command = command.replace(placeholder, value)
    return command


def _commands_from(findings: Iterable[HealthFinding]) -> list[str]:
    return [
        step.command
        for finding in findings
        for step in finding.fix_steps
        if step.command
    ]


def _command_help_path(command: str) -> tuple[str, ...]:
    parts = shlex.split(_normalize_command(command))
    assert parts
    assert parts[0] == "opensquilla", command

    command_parts = tuple(parts[1:])
    matches = [
        path for path in _COMMAND_HELP_PATHS if command_parts[: len(path)] == path
    ]
    assert matches, f"No CLI help path registered for health fix command: {command}"
    return max(matches, key=len)


def _command_options(command: str, path: tuple[str, ...]) -> list[str]:
    parts = shlex.split(_normalize_command(command))
    return [
        part
        for part in parts[1 + len(path) :]
        if part.startswith("--") and part != "--help"
    ]


def _sample_findings() -> list[HealthFinding]:
    samples = [
        evaluate_provider(
            {
                "activeProvider": "openrouter",
                "providers": [
                    {
                        "providerId": "openrouter",
                        "active": True,
                        "configured": False,
                        "apiKeyConfigured": False,
                        "baseUrlConfigured": True,
                    }
                ],
            }
        ),
        evaluate_provider(
            {
                "activeProvider": "openrouter",
                "providers": [
                    {
                        "providerId": "openrouter",
                        "active": True,
                        "configured": True,
                        "buildable": False,
                        "model": "x",
                        "error": "bad",
                    }
                ],
            }
        ),
        evaluate_provider(
            {
                "activeProvider": "zai",
                "providers": [
                    {"providerId": "openrouter", "active": False},
                    {"providerId": "zhipu", "active": False},
                ],
            }
        ),
        evaluate_provider(
            {
                "activeProvider": "",
                "providers": [
                    {
                        "active": True,
                        "configured": True,
                        "buildable": True,
                        "model": "x",
                    }
                ],
            }
        ),
        evaluate_memory({"status": "error", "backend": "sqlite", "error": "bad"}),
        evaluate_memory({"status": "ok", "pendingRepairs": 1}),
        evaluate_logs({"enabled": True, "path": None}),
        evaluate_logs(
            {
                "gateway_file_log": {
                    "enabled": False,
                    "path": "/tmp/opensquilla-debug.log",
                    "exists": False,
                }
            }
        ),
        evaluate_search(
            {
                "provider": "brave",
                "activeProvider": "brave",
                "configured": False,
                "buildable": False,
                "runtimeSupported": True,
                "requiresApiKey": True,
                "apiKeyConfigured": False,
            }
        ),
        evaluate_search(
            {
                "provider": "",
                "activeProvider": "",
                "configured": False,
                "buildable": False,
                "runtimeSupported": False,
                "requiresApiKey": False,
                "apiKeyConfigured": False,
            }
        ),
        evaluate_search(
            {
                "provider": "unknown",
                "activeProvider": "unknown",
                "configured": True,
                "buildable": False,
                "runtimeSupported": False,
            }
        ),
        evaluate_image_generation(
            {
                "enabled": False,
                "configured": False,
                "status": "optional",
                "primary": "openai/gpt-image-1",
            }
        ),
        evaluate_image_generation(
            {
                "enabled": True,
                "configured": False,
                "status": "missing",
                "primary": "openai/gpt-image-1",
            }
        ),
        evaluate_router(
            {
                "enabled": False,
                "rolloutPhase": "disabled",
                "strategy": "disabled",
                "runtimeValid": True,
            }
        ),
        evaluate_router(
            {
                "enabled": True,
                "rolloutPhase": "full",
                "strategy": "v4",
                "runtimeValid": False,
                "error": "missing",
            }
        ),
        evaluate_router(
            {
                "enabled": True,
                "rolloutPhase": "observe",
                "strategy": "v4",
                "runtimeValid": True,
            }
        ),
        evaluate_router(
            {
                "enabled": True,
                "rolloutPhase": "mystery",
                "strategy": "v4",
                "runtimeValid": True,
            }
        ),
        evaluate_memory_embedding(
            {
                "status": "config_error",
                "requestedProvider": "openai",
                "effectiveProvider": None,
                "error": "missing",
            }
        ),
        evaluate_memory_embedding(
            {
                "status": "fts_only",
                "requestedProvider": "auto",
                "effectiveProvider": None,
                "retrievalMode": "fts",
            }
        ),
        evaluate_memory_embedding(
            {
                "status": "warming_up",
                "requestedProvider": "auto",
                "effectiveProvider": "local",
                "retrievalMode": "hybrid",
            }
        ),
        evaluate_channels(
            {
                "channels": [],
            }
        ),
        evaluate_channels(
            {
                "channels": [
                    {
                        "name": "slack-main",
                        "type": "slack",
                        "enabled": False,
                        "status": "disabled",
                    }
                ]
            }
        ),
        evaluate_channels(
            {
                "channels": [
                    {
                        "name": "slack-main",
                        "type": "slack",
                        "enabled": True,
                        "configured": True,
                        "status": "dead",
                        "connected": False,
                    }
                ]
            }
        ),
        evaluate_channels(
            {
                "channels": [
                    {
                        "name": "tele gram",
                        "type": "telegram",
                        "enabled": True,
                        "configured": True,
                        "status": "connected",
                        "connected": True,
                        "pendingPairings": 2,
                    }
                ]
            }
        ),
        evaluate_channels(
            {
                "channels": [
                    {
                        "name": "slack-main",
                        "type": "slack",
                        "enabled": True,
                        "configured": True,
                        "status": "restarting",
                        "connected": False,
                    }
                ]
            }
        ),
        evaluate_channels(
            {
                "channels": [
                    {
                        "name": "slack-main",
                        "type": "slack",
                        "enabled": True,
                        "configured": True,
                        "status": "warming_up",
                        "connected": False,
                    }
                ]
            }
        ),
        evaluate_sandbox(
            {
                "posture": "bypass",
                "sandbox": {"sandbox": False},
                "permissions": {"default_mode": "bypass"},
            }
        ),
        evaluate_sandbox(
            {
                "posture": "custom",
                "sandbox": {"sandbox": True, "security_grading": False},
                "permissions": {"default_mode": "full"},
            }
        ),
        evaluate_llm_ensemble(
            {
                "enabled": True,
                "selectionMode": "static_openrouter_b5",
                "activeProvider": "groq",
                "apiKeyEnv": "OPENROUTER_API_KEY",
                "credentialAvailable": False,
            }
        ),
    ]
    collected: list[HealthFinding] = []
    for sample_findings in samples:
        collected.extend(sample_findings)
    return collected


def _sample_recovery_commands() -> list[str]:
    commands: list[str] = []
    for finding in _sample_findings():
        commands.extend(_commands_from([finding]))
    return sorted(set(commands))


def test_recovery_steps_that_restart_gateway_mark_restart_required() -> None:
    offenders = [
        finding.id
        for finding in _sample_findings()
        if any(
            step.command == "opensquilla gateway restart"
            for step in finding.fix_steps
            if step.command
        )
        and not finding.restart_required
    ]

    assert offenders == []


def test_health_recovery_command_paths_still_exist() -> None:
    command_paths = [
        ["gateway", "start"],
        ["gateway", "status"],
        ["gateway", "restart"],
        ["diagnostics", "status"],
        ["providers", "configure"],
        ["providers", "list"],
        ["providers", "status"],
        ["memory", "status"],
        ["memory", "repair", "list"],
        ["memory", "repair", "run"],
        ["onboard"],
        ["search", "list"],
        ["search", "status"],
        ["onboard", "status"],
        ["channels", "enable"],
        ["channels", "restart"],
        ["channels", "status"],
        ["config", "set"],
        ["sandbox", "on"],
        ["sandbox", "full"],
        ["sandbox", "status"],
        ["configure"],
    ]

    for path in command_paths:
        _help(path)


def test_health_recovery_command_options_still_match_cli_help() -> None:
    configure_help = _help(["configure"])
    for option in [
        "--section",
        "--provider",
        "--api-key",
        "--search-provider",
        "--image-provider",
        "--memory-provider",
        "--router",
        "--channel-type",
        "--name",
        "--token",
        "--onnx-dir",
        "--config",
    ]:
        assert option in configure_help

    assert "--config" in _help(["config", "set"])
    assert "--api-key" in _help(["providers", "configure"])
    assert "--json" in _help(["providers", "list"])
    provider_status_help = _help(["providers", "status"])
    assert "--json" in provider_status_help
    assert "--config" in provider_status_help
    memory_status_help = _help(["memory", "status"])
    assert "--deep" in memory_status_help
    assert "--config" in memory_status_help
    memory_repair_list_help = _help(["memory", "repair", "list"])
    assert "--json" in memory_repair_list_help
    assert "--config" in memory_repair_list_help
    memory_repair_run_help = _help(["memory", "repair", "run"])
    assert "--json" in memory_repair_run_help
    assert "--config" in memory_repair_run_help
    assert "--yes" not in memory_repair_run_help
    channels_status_help = _help(["channels", "status"])
    assert "--json" in channels_status_help
    assert "--config" in channels_status_help
    channels_restart_help = _help(["channels", "restart"])
    assert "--yes" in channels_restart_help
    assert "--config" in channels_restart_help
    search_status_help = _help(["search", "status"])
    assert "--json" in search_status_help
    assert "--config" in search_status_help
    diagnostics_status_help = _help(["diagnostics", "status"])
    assert "--json" in diagnostics_status_help
    assert "--config" in diagnostics_status_help
    assert "--config" in _help(["gateway", "start"])
    assert "--config" in _help(["gateway", "status"])
    assert "--config" in _help(["gateway", "restart"])
    onboard_help = _help(["onboard"])
    assert "--if-needed" in onboard_help
    assert "--config" in onboard_help
    onboard_status_help = _help(["onboard", "status"])
    assert "--json" in onboard_status_help
    assert "--config" in onboard_status_help


def test_health_recovery_commands_avoid_shell_redirection_placeholders() -> None:
    commands = _sample_recovery_commands()

    offenders = [
        command
        for command in commands
        if "<key>" in command or "<path>" in command
    ]

    assert offenders == []


def test_health_recovery_commands_from_evaluator_samples_resolve_to_cli_help() -> None:
    commands = _sample_recovery_commands()
    assert commands

    for command in commands:
        path = _command_help_path(command)
        help_text = _help(list(path))
        for option in _command_options(command, path):
            assert option in help_text, f"{command} uses {option}, missing from {' '.join(path)}"


def test_doctor_collection_failure_recovery_commands_resolve_to_cli_help() -> None:
    from opensquilla.gateway.rpc_doctor import _COLLECTION_INSPECT_COMMANDS

    commands = sorted(
        {
            *_COLLECTION_INSPECT_COMMANDS.values(),
            "opensquilla diagnostics status",
            "opensquilla gateway restart",
        }
    )
    assert commands

    for command in commands:
        path = _command_help_path(command)
        help_text = _help(list(path))
        for option in _command_options(command, path):
            assert option in help_text, f"{command} uses {option}, missing from {' '.join(path)}"


def test_doctor_offline_recovery_commands_resolve_to_cli_help() -> None:
    from opensquilla.cli.doctor_cmd import _offline_report

    commands = [
        step["command"]
        for report in [
            _offline_report(
                ConnectionError("connection refused"),
                gateway_url="ws://127.0.0.1:19999/ws",
            ),
            _offline_report(
                ConnectionError("connection refused"),
                gateway_url="wss://squilla.example.com/ws",
            ),
        ]
        for finding in report["findings"]
        for step in finding["fixSteps"]
        if "command" in step
    ]
    assert commands

    for command in commands:
        path = _command_help_path(command)
        help_text = _help(list(path))
        for option in _command_options(command, path):
            assert option in help_text, f"{command} uses {option}, missing from {' '.join(path)}"


def test_llm_ensemble_custom_b5_health_findings_cover_ready_and_blocked() -> None:
    ready = evaluate_llm_ensemble(
        {
            "enabled": True,
            "selectionMode": "custom_b5",
            "activeProvider": "volcengine",
            "lineupReady": True,
            "lineupBlockedReason": "",
        }
    )
    assert [finding.id for finding in ready] == ["llm_ensemble.custom_b5.ready"]
    assert ready[0].severity == "ok"

    blocked = evaluate_llm_ensemble(
        {
            "enabled": True,
            "selectionMode": "custom_b5",
            "activeProvider": "volcengine",
            "lineupReady": False,
            "lineupBlockedReason": "missing_credential:openrouter",
        }
    )
    assert [finding.id for finding in blocked] == ["llm_ensemble.custom_b5.not_ready"]
    assert blocked[0].severity == "warn"
    assert "openrouter" in blocked[0].detail
    # The lineup is fixable live from settings; no restart demanded.
    assert blocked[0].restart_required is False

    empty = evaluate_llm_ensemble(
        {
            "enabled": True,
            "selectionMode": "custom_b5",
            "activeProvider": "volcengine",
            "lineupReady": False,
            "lineupBlockedReason": "no_proposers",
        }
    )
    assert "no enabled proposer" in empty[0].detail
