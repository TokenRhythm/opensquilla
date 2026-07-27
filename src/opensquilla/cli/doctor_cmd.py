"""User-facing readiness doctor command."""

from __future__ import annotations

import asyncio
import copy
import os
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.table import Table

from opensquilla.cli.gateway_rpc import default_gateway_url, gateway_url_from_config
from opensquilla.cli.output import print_json
from opensquilla.cli.url_utils import normalize_gateway_url
from opensquilla.health.model import (
    FixStep,
    HealthFinding,
    build_report,
    summarize_impact_counts,
)
from opensquilla.health.recovery_commands import command_with_config as _command_with_config

_LOCAL_GATEWAY_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}
_API_KEY_PLACEHOLDER = "YOUR_API_KEY"


def _config_option(config_path: str | Path | None) -> str:
    if config_path is None:
        return ""
    return f" --config {shlex.quote(str(config_path))}"


def _onboard_status_command(config_path: str | Path | None) -> str:
    return f"opensquilla onboard status --json{_config_option(config_path)}"


def _onboard_if_needed_command(config_path: str | Path | None) -> str:
    return f"opensquilla onboard --if-needed{_config_option(config_path)}"


def _gateway_commands(
    gateway_url: str,
    *,
    config_path: str | Path | None = None,
    config_owns_target: bool = False,
) -> list[dict[str, str]]:
    parsed = urlparse(gateway_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "wss" else 18791)
    if host not in _LOCAL_GATEWAY_HOSTS:
        remote_target = shlex.quote(gateway_url)
        return [
            {
                "label": "Inspect remote gateway",
                "command": f"opensquilla gateway status --gateway {remote_target} --json",
            },
            {
                "label": "Repair remote deployment",
                "detail": (
                    "Start or repair the remote OpenSquilla gateway deployment, "
                    "then rerun doctor."
                ),
            },
            {"label": "Inspect diagnostics", "command": "opensquilla diagnostics status"},
        ]

    bind_args = f"--bind {host} --port {port}"
    config_args = _config_option(config_path)
    if config_owns_target and config_args:
        return [
            {
                "label": "Start gateway",
                "command": f"opensquilla gateway start{config_args}",
            },
            {
                "label": "Inspect gateway",
                "command": f"opensquilla gateway status --json{config_args}",
            },
            {
                "label": "Inspect diagnostics",
                "command": f"opensquilla diagnostics status{config_args}",
            },
        ]
    return [
        {
            "label": "Start gateway",
            "command": f"opensquilla gateway start {bind_args}{config_args}",
        },
        {
            "label": "Inspect gateway",
            "command": f"opensquilla gateway status {bind_args} --json{config_args}",
        },
        {
            "label": "Inspect diagnostics",
            "command": f"opensquilla diagnostics status{config_args}",
        },
    ]


def _gateway_fix_steps(
    gateway_url: str,
    *,
    config_path: str | Path | None = None,
    config_owns_target: bool = False,
) -> list[FixStep]:
    return [
        FixStep(
            label=step["label"],
            command=step.get("command"),
            detail=step.get("detail"),
        )
        for step in _gateway_commands(
            gateway_url,
            config_path=config_path,
            config_owns_target=config_owns_target,
        )
    ]


def _is_local_gateway(gateway_url: str) -> bool:
    host = urlparse(gateway_url).hostname or "127.0.0.1"
    return host in _LOCAL_GATEWAY_HOSTS


def _gateway_unavailable_finding(
    error: BaseException,
    *,
    gateway_url: str,
    config_path: str | Path | None = None,
    config_owns_target: bool = False,
) -> HealthFinding:
    detail = (
        f"Cannot connect to OpenSquilla gateway at {gateway_url}. "
        "Use the recovery steps below to start or inspect the target gateway."
    )
    return HealthFinding(
        id="gateway.unavailable",
        severity="error",
        readiness_impact="blocks_ready",
        surface="gateway",
        title="Gateway is unavailable",
        detail=detail,
        evidence={
            "errorType": type(error).__name__,
            "error": str(error),
            "gatewayUrl": gateway_url,
        },
        fix_steps=_gateway_fix_steps(
            gateway_url,
            config_path=config_path,
            config_owns_target=config_owns_target,
        ),
    )


def _section_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _problem_sections(status: Any) -> dict[str, str]:
    return {
        name: state
        for name, value in dict(getattr(status, "sections", {}) or {}).items()
        if (state := _section_value(value)) not in {"ok", "optional"}
    }


def _image_generation_provider_id(config: Any) -> str:
    image_cfg = getattr(config, "image_generation", None)
    primary = str(getattr(image_cfg, "primary", "") or "")
    fallbacks = list(getattr(image_cfg, "fallbacks", []) or [])
    for ref in [primary, *fallbacks]:
        provider_id, sep, _model = str(ref or "").partition("/")
        if sep and provider_id.strip():
            return provider_id.strip().lower()
    return "openai"


def _image_generation_api_key_env(config: Any) -> str:
    provider_id = _image_generation_provider_id(config)
    try:
        from opensquilla.onboarding.image_generation_specs import (
            get_image_generation_provider_setup_spec,
        )

        spec = get_image_generation_provider_setup_spec(provider_id)
    except KeyError:
        return ""
    providers = getattr(getattr(config, "image_generation", None), "providers", None)
    provider_cfg = getattr(providers, provider_id, None) if providers is not None else None
    configured_env = str(getattr(provider_cfg, "api_key_env", "") or "")
    return configured_env or str(spec.env_key or "")


def _local_optional_env_recovery(
    config: Any,
    problem_sections: dict[str, str],
) -> tuple[list[str], list[FixStep]]:
    notes: list[str] = []
    steps: list[FixStep] = []
    if problem_sections.get("search") == "degraded":
        env_key = str(getattr(config, "search_api_key_env", "") or "")
        if env_key:
            notes.append(f"search={env_key}")
            steps.append(
                FixStep(
                    label="Set search environment variable",
                    detail=(
                        f"Set {env_key} in the gateway environment, then start "
                        "OpenSquilla."
                    ),
                )
            )
    if problem_sections.get("image_generation") == "degraded":
        env_key = _image_generation_api_key_env(config)
        if env_key:
            notes.append(f"image_generation={env_key}")
            steps.append(
                FixStep(
                    label="Set image environment variable",
                    detail=(
                        f"Set {env_key} in the gateway environment, then start "
                        "OpenSquilla."
                    ),
                )
            )
    return notes, steps


def _local_onboarding_findings(
    config: Any,
    *,
    config_path: str | Path | None = None,
) -> list[HealthFinding]:
    from opensquilla.onboarding.status import get_onboarding_status

    status = get_onboarding_status(config)
    problem_sections = _problem_sections(status)
    if not problem_sections:
        return []

    llm_state = problem_sections.get("llm")
    llm_cfg = getattr(config, "llm", None)
    llm_env_key = str(getattr(llm_cfg, "api_key_env", "") or "")
    provider = str(getattr(llm_cfg, "provider", "") or "openrouter")
    section_text = ", ".join(
        f"{name}={state}" for name, state in sorted(problem_sections.items())
    )
    detail = f"Local configuration still needs onboarding: {section_text}."
    fix_steps: list[FixStep] = []
    env_notes: list[str] = []
    if getattr(status, "llm_source", "") == "missing_env" and llm_env_key:
        env_notes.append(f"llm={llm_env_key}")
        fix_steps.append(
            FixStep(
                label="Set LLM environment variable",
                detail=(
                    f"Set {llm_env_key} in the gateway environment, then start "
                    "OpenSquilla."
                ),
            )
        )
    elif llm_state:
        fix_steps.append(
            FixStep(
                label="Configure LLM provider",
                command=(
                    "opensquilla configure provider --provider "
                    f"{provider} --api-key {_API_KEY_PLACEHOLDER}"
                ),
            )
        )
    optional_env_notes, optional_env_steps = _local_optional_env_recovery(
        config,
        problem_sections,
    )
    env_notes.extend(optional_env_notes)
    fix_steps.extend(optional_env_steps)
    if env_notes:
        detail = (
            f"{detail} Missing environment references: {', '.join(env_notes)}."
        )
    fix_steps.extend(
        [
            FixStep(
                label="Inspect onboarding",
                command=_onboard_status_command(config_path),
            ),
            FixStep(
                label="Run onboarding",
                command=_onboard_if_needed_command(config_path),
            ),
        ]
    )
    return [
        HealthFinding(
            id="config.local.needs_onboarding",
            severity="error" if llm_state else "warn",
            readiness_impact="blocks_ready" if llm_state else "degrades",
            surface="config",
            title="Local configuration needs onboarding",
            detail=detail,
            evidence={
                "configPath": getattr(status, "config_path", None),
                "sections": {
                    name: _section_value(value)
                    for name, value in dict(getattr(status, "sections", {}) or {}).items()
                },
                "llmSource": getattr(status, "llm_source", None),
            },
            fix_steps=fix_steps,
        )
    ]


def _local_config_findings(config_path: str | Path | None = None) -> list[HealthFinding]:
    from opensquilla.onboarding.config_store import default_config_path, load_config

    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001 - doctor turns config load failures into guidance.
        try:
            resolved_config_path = (
                str(Path(config_path)) if config_path else str(default_config_path())
            )
        except Exception:  # noqa: BLE001 - keep doctor total even path resolution failed.
            resolved_config_path = None
        return [
            HealthFinding(
                id="config.local.unreadable",
                severity="error",
                readiness_impact="blocks_ready",
                surface="config",
                title="Local configuration cannot be loaded",
                detail=f"{type(exc).__name__}: {exc}",
                evidence={
                    "errorType": type(exc).__name__,
                    "configPath": resolved_config_path,
                },
                fix_steps=[
                    FixStep(
                        label="Inspect onboarding",
                        command=_onboard_status_command(config_path),
                    ),
                    FixStep(
                        label="Run onboarding",
                        command=_onboard_if_needed_command(config_path),
                    ),
                ],
            )
        ]
    return _local_onboarding_findings(config, config_path=config_path)


def _local_tui_findings() -> list[HealthFinding]:
    """Explain which chat surface `--ui auto` selects on THIS terminal.

    The gateway report cannot cover this: renderer availability (companion,
    bun, terminal capabilities) is a property of the CLI process's machine, so
    the finding is appended locally to both the online and offline reports. A
    healthy full-screen selection reports nothing.
    """
    from opensquilla.cli.tui.backend.render_summary import sanitize_terminal_text
    from opensquilla.cli.tui.renderers.selection import select_chat_ui_backend

    try:
        selection = select_chat_ui_backend("auto")
    except Exception as exc:  # noqa: BLE001 - a UI probe must never crash doctor.
        # Exception text can wrap host/subprocess output: same trust level and
        # scrubbing as the fallback detail below.
        crash_detail = (
            sanitize_terminal_text(f"{type(exc).__name__}: {exc}")
            .replace("\r", " ")
            .replace("\n", " ")
        )
        return [
            HealthFinding(
                id="tui.selection_failed",
                severity="warn",
                readiness_impact="optional",
                surface="cli",
                title="Terminal UI selection failed",
                detail=crash_detail,
            )
        ]
    fallback = selection.fallback
    if fallback is None:
        return []
    fix_steps: list[FixStep] = []
    from opensquilla.cli.tui.source_checkout import resolve_tui_source_checkout_hint

    hint = resolve_tui_source_checkout_hint()
    if hint is not None:
        fix_steps = [
            FixStep(label="Install the TUI host dependencies", command=hint.install_command),
            FixStep(label="Launch the full-screen TUI", command=hint.launch_command),
        ]
    detail = sanitize_terminal_text(fallback.detail).replace("\r", " ").replace("\n", " ")
    return [
        HealthFinding(
            id="tui.opentui_unavailable",
            severity="info",
            readiness_impact="optional",
            surface="cli",
            title="Full-screen terminal UI not active — chat uses plain mode",
            detail=detail,
            evidence={
                "reasonCode": fallback.code.value,
                "selectedBackend": selection.backend.backend_id,
            },
            fix_steps=fix_steps,
        )
    ]


def _append_local_tui_findings(report: dict[str, Any]) -> None:
    """Merge the CLI-local terminal-UI findings into a finished report.

    The whole report is re-derived through ``_refresh_report_readiness`` so
    counts, impact counts, ordering, and the summary stay mutually consistent.
    An offline report keeps its "unavailable" sentinel status/ready/summary —
    those describe the failed gateway probe, which an optional UI capability
    note must not rewrite.
    """
    findings = _local_tui_findings()
    if not findings:
        return
    report_findings = report.setdefault("findings", [])
    if not isinstance(report_findings, list):
        return
    report_findings.extend(finding.to_dict() for finding in findings)
    offline_sentinel = (
        (report.get("status"), report.get("ready"), report.get("summary"))
        if report.get("status") == "unavailable"
        else None
    )
    _refresh_report_readiness(report)
    if offline_sentinel is not None:
        report["status"], report["ready"], report["summary"] = offline_sentinel


def _offline_report(
    error: BaseException,
    *,
    gateway_url: str,
    config_path: str | Path | None = None,
    config_owns_target: bool = False,
) -> dict[str, Any]:
    findings: list[HealthFinding] = []
    if _is_local_gateway(gateway_url):
        if config_path is None:
            findings.extend(_local_config_findings())
        else:
            findings.extend(_local_config_findings(config_path))
    findings.append(
        _gateway_unavailable_finding(
            error,
            gateway_url=gateway_url,
            config_path=config_path,
            config_owns_target=config_owns_target,
        )
    )
    report = build_report(findings)
    report["status"] = "unavailable"
    report["ready"] = False
    report["summary"] = (
        "Gateway unavailable"
        if len(findings) == 1
        else "Gateway unavailable; local config needs attention"
    )
    return report


async def _fetch_report(
    *,
    gateway_url: str,
    agent_id: str,
    deep: bool,
) -> dict[str, Any]:
    from opensquilla.cli import gateway_client as gateway_client_module

    client = gateway_client_module.GatewayClient()
    try:
        await client.connect(gateway_url)
        payload = await client.call("doctor.status", {"agentId": agent_id, "deep": deep})
        return dict(payload)
    finally:
        await client.close()


def _fix_step_text(step: dict[str, Any]) -> str:
    return str(step.get("command") or step.get("detail") or step.get("label") or "")


def _impact_value(finding: dict[str, Any]) -> str:
    impact = str(finding.get("readinessImpact") or "")
    if impact in {"blocks_ready", "degrades", "optional", "none"}:
        return impact
    severity = str(finding.get("severity") or "")
    if severity == "error":
        return "blocks_ready"
    if severity == "warn":
        return "degrades"
    if severity == "info":
        return "optional"
    return "none"


def _impact_text(finding: dict[str, Any]) -> str:
    labels = {
        "blocks_ready": "blocks readiness",
        "degrades": "degrades",
        "optional": "optional",
        "none": "reference",
    }
    return labels[_impact_value(finding)]


def _same_config_path(left: str, right: str) -> bool:
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return left == right


def _refresh_report_readiness(report: dict[str, Any]) -> None:
    counts = {"error": 0, "warn": 0, "info": 0, "ok": 0}
    impact_counts = {"blocks_ready": 0, "degrades": 0, "optional": 0, "none": 0}
    attention_count = 0
    findings = list(report.get("findings") or [])
    for finding in findings:
        severity = str(finding.get("severity") or "info")
        if severity not in counts:
            severity = "info"
        counts[severity] += 1
        impact_counts[_impact_value(finding)] += 1
        if severity == "warn" and _impact_value(finding) == "optional":
            attention_count += 1
    severity_rank = {"error": 0, "warn": 1, "info": 2, "ok": 3}
    impact_rank = {"blocks_ready": 0, "degrades": 1, "optional": 2, "none": 3}
    ordered_findings = sorted(
        enumerate(findings),
        key=lambda item: (
            impact_rank[_impact_value(item[1])],
            severity_rank.get(str(item[1].get("severity") or "info"), 2),
            item[0],
        ),
    )
    report["counts"] = counts
    report["impactCounts"] = impact_counts
    report["findings"] = [finding for _, finding in ordered_findings]
    report["status"] = "action_required" if impact_counts["blocks_ready"] else (
        "degraded" if impact_counts["degrades"] else "ready"
    )
    report["ready"] = impact_counts["blocks_ready"] == 0
    report["summary"] = summarize_impact_counts(impact_counts, attention_count=attention_count)


def _apply_requested_config_context(
    report: dict[str, Any],
    requested_config_path: str,
) -> str:
    report["requestedConfigPath"] = requested_config_path
    running_config_path = str(report.get("configPath") or "")
    if not running_config_path or _same_config_path(requested_config_path, running_config_path):
        return requested_config_path
    finding = HealthFinding(
        id="gateway.config.mismatch",
        severity="error",
        readiness_impact="blocks_ready",
        surface="gateway",
        title="Gateway is running with a different config",
        detail=(
            "The requested config path does not match the config reported by the "
            "running gateway. Restart or inspect the gateway for the requested config "
            "before treating this report as ready."
        ),
        evidence={
            "requestedConfigPath": requested_config_path,
            "runningConfigPath": running_config_path,
        },
        fix_steps=[
            FixStep(
                label="Restart requested gateway",
                command=f"opensquilla gateway restart{_config_option(requested_config_path)}",
            ),
            FixStep(
                label="Inspect requested gateway",
                command=(
                    "opensquilla gateway status --json"
                    f"{_config_option(requested_config_path)}"
                ),
            ),
        ],
        restart_required=True,
    )
    findings = report.setdefault("findings", [])
    if isinstance(findings, list):
        findings.insert(0, finding.to_dict())
    else:
        report["findings"] = [finding.to_dict()]
    _refresh_report_readiness(report)
    return running_config_path


def _apply_recovery_config_context(
    report: dict[str, Any],
    config_path: str | Path | None,
) -> None:
    if not config_path:
        return
    for finding in report.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        for step in finding.get("fixSteps") or []:
            if not isinstance(step, dict):
                continue
            command = step.get("command")
            if isinstance(command, str):
                step["command"] = _command_with_config(command, config_path)


def _visible_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    attention_findings = [
        finding
        for finding in findings
        if _impact_value(finding) in {"blocks_ready", "degrades"}
    ]
    if attention_findings:
        return attention_findings
    return [finding for finding in findings if _impact_value(finding) == "optional"]


def _step_column(findings: list[dict[str, Any]]) -> str:
    impacts = {_impact_value(finding) for finding in findings}
    if impacts == {"optional"}:
        return "Optional setup"
    if impacts == {"none"}:
        return "Reference"
    return "Recovery"


def _report_context(report: dict[str, Any]) -> str:
    items = []
    if gateway_url := report.get("gatewayUrl"):
        items.append(f"Gateway: {gateway_url}")
    if config_path := report.get("configPath"):
        items.append(f"Config: {config_path}")
    requested_config_path = report.get("requestedConfigPath")
    if requested_config_path and requested_config_path != config_path:
        items.append(f"Requested config: {requested_config_path}")
    if agent_id := report.get("agentId"):
        items.append(f"Agent: {agent_id}")
    return " | ".join(items)


def _render_report(report: dict[str, Any]) -> None:
    console = Console(width=180, force_terminal=False)
    console.print(f"[bold]OpenSquilla Doctor[/bold] - {report.get('summary', '')}")
    if context := _report_context(report):
        console.print(context)
    report_findings = list(report.get("findings", []))
    visible_findings = _visible_findings(report_findings)
    if not visible_findings:
        console.print("No action needed. Use --json for the full health report.")
        return
    step_column = _step_column(visible_findings)
    table = Table(show_header=True)
    table.add_column("Severity", no_wrap=True)
    table.add_column("Impact", no_wrap=True)
    table.add_column("Surface", no_wrap=True)
    table.add_column("Finding")
    table.add_column(step_column)
    for finding in visible_findings:
        recovery = "\n".join(_fix_step_text(step) for step in finding.get("fixSteps") or [])
        restart = " Recovery requires restart." if finding.get("restartRequired") else ""
        table.add_row(
            str(finding.get("severity") or ""),
            _impact_text(finding),
            str(finding.get("surface") or ""),
            f"{finding.get('title')}\n{finding.get('detail')}{restart}",
            recovery,
        )
    console.print(table)



def _implicit_existing_config_path() -> Path | None:
    if os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"):
        return None
    from opensquilla.onboarding.config_store import resolve_config_path

    path, source = resolve_config_path(None)
    if source in {"cwd", "home"} and path.is_file():
        return path
    return None


def _target_url_for_doctor(
    *,
    gateway_url: str | None,
    config_path: Path | None,
) -> str:
    if gateway_url is not None:
        return normalize_gateway_url(gateway_url)
    if config_path is not None:
        return gateway_url_from_config(config_path)
    if implicit_config_path := _implicit_existing_config_path():
        return gateway_url_from_config(implicit_config_path)
    return default_gateway_url()

def _requested_config_path(
    config_path: Path | None,
    *,
    gateway_url: str | None = None,
) -> str | None:
    if config_path is not None:
        if gateway_url is not None and not _is_local_gateway(normalize_gateway_url(gateway_url)):
            return None
        return str(config_path)
    if gateway_url is not None:
        return None
    if env_config_path := os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"):
        return env_config_path
    if implicit_config_path := _implicit_existing_config_path():
        return str(implicit_config_path)
    return None


def doctor_command(
    agent_id: str = typer.Option("main", "--agent", help="Agent id for memory diagnostics."),
    deep: bool = typer.Option(
        True,
        "--deep/--quick",
        help="Include deeper memory diagnostics; use --quick for shallow checks.",
    ),
    gateway_url: str | None = typer.Option(None, "--gateway", envvar="OPENSQUILLA_GATEWAY_URL"),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        help="Config path to inspect when the local gateway is unavailable.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Diagnose OpenSquilla readiness and print recovery steps."""

    requested_config_path = _requested_config_path(
        config_path,
        gateway_url=gateway_url,
    )
    config_owns_gateway_target = gateway_url is None and bool(requested_config_path)
    target_url = (
        normalize_gateway_url(gateway_url)
        if gateway_url is not None
        else normalize_gateway_url("ws://localhost:18791/ws")
    )
    try:
        target_url = _target_url_for_doctor(
            gateway_url=gateway_url,
            config_path=config_path,
        )
        report = asyncio.run(_fetch_report(gateway_url=target_url, agent_id=agent_id, deep=deep))
    except SystemExit as exc:
        report = _offline_report(
            exc,
            gateway_url=target_url,
            config_path=requested_config_path if config_owns_gateway_target else config_path,
            config_owns_target=config_owns_gateway_target,
        )
    except Exception as exc:  # noqa: BLE001 - doctor should explain gateway failures.
        report = _offline_report(
            exc,
            gateway_url=target_url,
            config_path=requested_config_path if config_owns_gateway_target else config_path,
            config_owns_target=config_owns_gateway_target,
        )
    report = copy.deepcopy(report)
    _append_local_tui_findings(report)
    report.setdefault("gatewayUrl", target_url)
    report.setdefault("agentId", agent_id)
    if requested_config_path:
        recovery_config_path = _apply_requested_config_context(report, requested_config_path)
        _apply_recovery_config_context(report, recovery_config_path)
        report.setdefault("configPath", requested_config_path)
    if json_output:
        print_json(report)
    else:
        _render_report(report)
    if report.get("ready") is not True:
        raise typer.Exit(1)
