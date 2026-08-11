#!/usr/bin/env python3
"""Validate the opt-in ensemble metrics reference stack.

The default mode performs a dependency-free Docker Compose configuration
check. ``--live`` starts an isolated project on random loopback ports, writes
strict synthetic transport rows, verifies ingestion/provisioning/rules, and
removes the project and volumes in ``finally``.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

STACK_DIRECTORY = Path(__file__).resolve().parent
COMPOSE_PATH = STACK_DIRECTORY / "compose.yaml"
DASHBOARD_UID = "opensquilla-ensemble-execution-v1"
DATASOURCE_UID = "opensquilla-loki"
METRICS_FILE_NAME = "ensemble-execution-metrics-v1.jsonl"


class VerificationError(RuntimeError):
    """The reference stack did not satisfy a checked deployment contract."""


def _run(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout: float,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=STACK_DIRECTORY,
            env=dict(environment),
            check=check,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise VerificationError(f"required command is unavailable: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VerificationError(f"command timed out: {' '.join(command)}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise VerificationError(
            f"command failed ({exc.returncode}): {' '.join(command)}\n{detail}"
        ) from exc


def _compose_command(project_name: str, *arguments: str) -> tuple[str, ...]:
    return (
        "docker",
        "compose",
        "--project-name",
        project_name,
        "--file",
        str(COMPOSE_PATH),
        *arguments,
    )


def _reserve_loopback_ports(count: int) -> list[int]:
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            sockets.append(listener)
        return [int(listener.getsockname()[1]) for listener in sockets]
    finally:
        for listener in sockets:
            listener.close()


def _request_bytes(
    url: str,
    *,
    timeout: float = 3.0,
    username: str | None = None,
    password: str | None = None,
) -> bytes:
    request = urllib.request.Request(url)
    if username is not None and password is not None:
        credential = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        request.add_header("Authorization", f"Basic {credential}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise VerificationError(f"unexpected HTTP {response.status} from {url}")
            return response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise VerificationError(f"request failed for {url}: {type(exc).__name__}") from exc


def _request_json(
    url: str,
    *,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    try:
        value = json.loads(
            _request_bytes(url, username=username, password=password).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"endpoint did not return JSON: {url}") from exc
    if type(value) is not dict:
        raise VerificationError(f"endpoint returned a non-object JSON value: {url}")
    return value


def _wait_for(
    description: str,
    callback: Callable[[], Any],
    *,
    deadline: float,
) -> Any:
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return callback()
        except Exception as exc:  # noqa: BLE001 - polling preserves the final cause
            last_error = exc
            time.sleep(0.5)
    raise VerificationError(f"timed out waiting for {description}: {last_error}")


def _synthetic_rows(timestamp: datetime) -> list[dict[str, Any]]:
    """Return strict, identity-free transport rows for live ingestion checks."""

    rows: list[dict[str, Any]] = []
    for offset, (terminal_outcome, execution_status) in enumerate(
        (("completed", "success"), ("completed", "degraded"), ("failed", "failed"))
    ):
        emitted_at = timestamp + timedelta(milliseconds=offset)
        rows.append(
            {
                "transport_schema": "opensquilla.ensemble-execution-metrics-jsonl/v1",
                "event": "llm_ensemble.execution.metrics",
                "emitted_at": emitted_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "schema": "opensquilla.ensemble-execution-metrics/v1",
                "terminal_outcome": terminal_outcome,
                "execution_status": execution_status,
                "selection_family": "router_dynamic",
                "fallback_used_observed": True,
                "fallback_used": execution_status == "degraded",
                "ranking_stage_observed": True,
                "ranking_stage_projection_complete": True,
                "ranking_snapshot_build_ms_observed": True,
                "ranking_snapshot_build_ms": 2 + offset,
                "ranking_hard_filter_ms_observed": True,
                "ranking_hard_filter_ms": 1,
                "ranking_score_ms_observed": True,
                "ranking_score_ms": 3 + offset,
                "ranking_packaged_template_cache_hit_observed": True,
                "ranking_packaged_template_cache_hit": offset > 0,
                "aggregator_usage_observed": True,
                "aggregator_usage_projection_complete": True,
                "aggregator_usage_accounting_observed": True,
                "aggregator_usage_physical_request_count": 1,
                "aggregator_usage_missing_count": 0,
                "aggregator_cost_projection_complete": True,
                "aggregator_billed_cost_usd": 0.001,
                "aggregator_selected_kind_observed": True,
                "aggregator_selected_kind": "primary",
                "cleanup_observed": True,
                "cleanup_lingering_task_count": 0,
                "cleanup_stream_close_unproven_count": 0,
                "canary_persistent_rollout_admission_unavailable_count": 0,
                "canary_persistent_rollout_mutation_unavailable_count": 0,
                "trace_size_observed": True,
                "trace_compact_json_bytes_capped": False,
                "trace_compact_json_bytes": 512,
            }
        )
    return rows


def _write_synthetic_transport(directory: Path) -> Path:
    path = directory / METRICS_FILE_NAME
    rows = _synthetic_rows(datetime.now(UTC))
    payload = b"".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        for row in rows
    )
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise VerificationError("short write while creating synthetic transport")
            view = view[written:]
    finally:
        os.close(descriptor)
    return path


def _loki_query_url(port: int, expression: str, *, query_range: bool = False) -> str:
    endpoint = "query_range" if query_range else "query"
    parameters: dict[str, str] = {"query": expression}
    if query_range:
        now = datetime.now(UTC)
        parameters.update(
            {
                "start": str(int((now - timedelta(minutes=5)).timestamp() * 1_000_000_000)),
                "end": str(int((now + timedelta(minutes=1)).timestamp() * 1_000_000_000)),
                "limit": "100",
            }
        )
    return f"http://127.0.0.1:{port}/loki/api/v1/{endpoint}?{urllib.parse.urlencode(parameters)}"


def _require_successful_loki_query(port: int, expression: str) -> dict[str, Any]:
    payload = _request_json(_loki_query_url(port, expression))
    if payload.get("status") != "success":
        raise VerificationError(f"Loki query did not succeed: {expression}")
    return payload


def verify_compose_configuration(environment: Mapping[str, str]) -> None:
    """Resolve the Compose model without pulling or starting images."""

    _run(
        _compose_command("opensquilla-ensemble-config-check", "config", "--quiet"),
        environment=environment,
        timeout=30,
    )


def verify_live_stack(*, timeout: float) -> None:
    project_name = f"opensquilla-ensemble-smoke-{os.getpid()}-{secrets.token_hex(4)}"
    grafana_password = secrets.token_urlsafe(24)
    grafana_port, loki_port, alertmanager_port, alloy_port = _reserve_loopback_ports(4)
    with tempfile.TemporaryDirectory(prefix="opensquilla-ensemble-observability-") as raw_tmp:
        metrics_directory = Path(raw_tmp) / "metrics"
        metrics_directory.mkdir(mode=0o700)
        os.chmod(metrics_directory, 0o700)
        environment = dict(os.environ)
        environment.update(
            {
                "OPENSQUILLA_ENSEMBLE_METRICS_JSONL_DIR": str(metrics_directory),
                "OPENSQUILLA_GRAFANA_ADMIN_PASSWORD": grafana_password,
                "OPENSQUILLA_GRAFANA_PORT": str(grafana_port),
                "OPENSQUILLA_LOKI_PORT": str(loki_port),
                "OPENSQUILLA_ALERTMANAGER_PORT": str(alertmanager_port),
                "OPENSQUILLA_ALLOY_PORT": str(alloy_port),
            }
        )
        verify_compose_configuration(environment)
        started = False
        try:
            # ``up`` can create part of the project before returning non-zero.
            # From this point onward, always issue the exact-project cleanup.
            started = True
            _run(
                _compose_command(project_name, "up", "--detach"),
                environment=environment,
                timeout=max(timeout, 300),
            )
            deadline = time.monotonic() + timeout
            _wait_for(
                "Loki readiness",
                lambda: _request_bytes(f"http://127.0.0.1:{loki_port}/ready"),
                deadline=deadline,
            )
            _wait_for(
                "Alertmanager readiness",
                lambda: _request_bytes(f"http://127.0.0.1:{alertmanager_port}/-/ready"),
                deadline=deadline,
            )
            _wait_for(
                "Alloy readiness",
                lambda: _request_bytes(f"http://127.0.0.1:{alloy_port}/-/ready"),
                deadline=deadline,
            )
            _wait_for(
                "Grafana readiness",
                lambda: _request_json(f"http://127.0.0.1:{grafana_port}/api/health"),
                deadline=deadline,
            )

            _write_synthetic_transport(metrics_directory)

            def _ingested_rows() -> dict[str, Any]:
                payload = _request_json(
                    _loki_query_url(
                        loki_port,
                        '{job="opensquilla-ensemble"}',
                        query_range=True,
                    )
                )
                result = payload.get("data", {}).get("result", [])
                if payload.get("status") != "success" or not result:
                    raise VerificationError("synthetic rows are not queryable in Loki")
                return payload

            _wait_for("Alloy-to-Loki ingestion", _ingested_rows, deadline=deadline)

            for expression in (
                'sum(count_over_time({job="opensquilla-ensemble"}[5m]))',
                '100 * sum(count_over_time({job="opensquilla-ensemble"} | json '
                '| aggregator_usage_projection_complete = "true" | __error__="" [5m])) '
                '/ sum(count_over_time({job="opensquilla-ensemble"} | json '
                '| aggregator_usage_observed = "true" | __error__="" [5m]))',
                'max(quantile_over_time(0.95, {job="opensquilla-ensemble"} | json '
                '| ranking_snapshot_build_ms_observed = "true" '
                '| unwrap ranking_snapshot_build_ms | __error__="" [5m]))',
            ):
                _require_successful_loki_query(loki_port, expression)

            dashboard_model = json.loads(
                (STACK_DIRECTORY / "grafana" / "dashboards" / "ensemble-execution.json").read_text(
                    encoding="utf-8"
                )
            )
            dashboard_expressions = [
                target["expr"].replace("$__range", "5m").replace("$__interval", "5m")
                for panel in dashboard_model["panels"]
                if panel["type"] != "logs"
                for target in panel.get("targets", [])
            ]
            if len(dashboard_expressions) < 20:
                raise VerificationError("dashboard has too few executable Loki queries")
            for expression in dashboard_expressions:
                _require_successful_loki_query(loki_port, expression)

            rules = _request_json(f"http://127.0.0.1:{loki_port}/prometheus/api/v1/rules")
            groups = rules.get("data", {}).get("groups", [])
            if not any(group.get("name") == "opensquilla-ensemble-safety" for group in groups):
                raise VerificationError("Loki did not load the provisioned alert rules")

            datasource = _request_json(
                f"http://127.0.0.1:{grafana_port}/api/datasources/uid/{DATASOURCE_UID}",
                username="admin",
                password=grafana_password,
            )
            if datasource.get("uid") != DATASOURCE_UID:
                raise VerificationError("Grafana did not provision the Loki datasource")

            dashboard = _request_json(
                f"http://127.0.0.1:{grafana_port}/api/dashboards/uid/{DASHBOARD_UID}",
                username="admin",
                password=grafana_password,
            )
            if dashboard.get("dashboard", {}).get("uid") != DASHBOARD_UID:
                raise VerificationError("Grafana did not provision the ensemble dashboard")
        except Exception:
            if started:
                logs = _run(
                    _compose_command(project_name, "logs", "--no-color", "--tail", "200"),
                    environment=environment,
                    timeout=30,
                    check=False,
                )
                if logs.stdout:
                    print(logs.stdout, file=sys.stderr)
                if logs.stderr:
                    print(logs.stderr, file=sys.stderr)
            raise
        finally:
            if started:
                primary_exception_active = sys.exc_info()[0] is not None
                try:
                    cleanup = _run(
                        _compose_command(
                            project_name,
                            "down",
                            "--volumes",
                            "--remove-orphans",
                        ),
                        environment=environment,
                        timeout=120,
                        check=False,
                    )
                    if cleanup.returncode != 0:
                        raise VerificationError(
                            f"isolated Compose cleanup failed with {cleanup.returncode}"
                        )
                except Exception as cleanup_error:
                    if not primary_exception_active:
                        raise
                    print(
                        f"warning: cleanup also failed: {cleanup_error}",
                        file=sys.stderr,
                    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="start an isolated stack and verify real ingestion/provisioning",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="seconds to wait for live services and ingestion (default: 120)",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _parse_args()
    if arguments.timeout <= 0:
        raise VerificationError("--timeout must be positive")
    environment = dict(os.environ)
    environment.setdefault(
        "OPENSQUILLA_ENSEMBLE_METRICS_JSONL_DIR",
        str(STACK_DIRECTORY),
    )
    environment.setdefault("OPENSQUILLA_GRAFANA_ADMIN_PASSWORD", "config-check-only")
    verify_compose_configuration(environment)
    if arguments.live:
        verify_live_stack(timeout=arguments.timeout)
        print("reference stack live verification passed")
    else:
        print("reference stack Compose configuration is valid; use --live for smoke test")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
