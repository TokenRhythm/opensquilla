from __future__ import annotations

import importlib.util
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from opensquilla.observability.ensemble_execution_metrics_contract import (
    validate_ensemble_execution_metrics,
)

_ROOT = Path(__file__).resolve().parents[2]
_STACK = _ROOT / "deploy" / "observability" / "ensemble-metrics"
_COMPOSE = _STACK / "compose.yaml"
_ALLOY = _STACK / "alloy" / "config.alloy"
_RULES = _STACK / "loki" / "rules" / "fake" / "ensemble.rules.yaml"
_DASHBOARD = _STACK / "grafana" / "dashboards" / "ensemble-execution.json"
_VERIFY = _STACK / "verify_reference_stack.py"

_EXPECTED_IMAGES = {
    "loki": "grafana/loki:3.7.3",
    "alertmanager": "prom/alertmanager:v0.32.1",
    "alloy": "grafana/alloy:v1.18.0",
    "grafana": "grafana/grafana:12.4.0",
}
_FORBIDDEN_IDENTITY_TERMS = (
    "deployment_sha256",
    "policy_sha256",
    "session_id",
    "task_id",
    "user_id",
    "provider_name",
    "model_name",
)


def _load_yaml(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ensemble_reference_verifier", _VERIFY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reference_compose_is_pinned_loopback_and_opt_in() -> None:
    compose = _load_yaml(_COMPOSE)
    assert type(compose) is dict
    services = compose["services"]
    assert set(services) == set(_EXPECTED_IMAGES)

    for service, expected_image in _EXPECTED_IMAGES.items():
        definition = services[service]
        assert definition["image"] == expected_image
        assert not expected_image.endswith(":latest")
        assert all(str(port).startswith("127.0.0.1:") for port in definition["ports"])
        assert definition["restart"] == "unless-stopped"

    alloy_volumes = services["alloy"]["volumes"]
    metrics_mount = next(
        volume for volume in alloy_volumes if "OPENSQUILLA_ENSEMBLE_METRICS_JSONL_DIR" in volume
    )
    assert ":?set an absolute owner-only metrics directory}" in metrics_mount
    assert metrics_mount.endswith(":ro")
    password = services["grafana"]["environment"]["GF_SECURITY_ADMIN_PASSWORD"]
    assert password.startswith("${OPENSQUILLA_GRAFANA_ADMIN_PASSWORD:?")
    assert services["grafana"]["environment"]["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert services["grafana"]["environment"]["GF_PLUGINS_PREINSTALL_DISABLED"] == "true"
    assert services["grafana"]["environment"]["GF_PLUGINS_PREINSTALL_AUTO_UPDATE"] == "false"
    assert "loki-data:/loki" in services["loki"]["volumes"]
    assert "user" not in services["loki"]


def test_alloy_accepts_only_reviewed_rows_and_fixed_labels() -> None:
    alloy = _ALLOY.read_text(encoding="utf-8")
    selectors = alloy.replace(chr(92) + '"', '"')
    assert '"/var/lib/opensquilla-metrics/ensemble-execution-metrics-v1.jsonl"' in alloy
    assert "ensemble-execution-metrics-v1.jsonl*" not in alloy
    assert 'transport_schema!="opensquilla.ensemble-execution-metrics-jsonl/v1"' in selectors
    assert 'event!="llm_ensemble.execution.metrics"' in selectors
    assert 'schema!="opensquilla.ensemble-execution-metrics/v1"' in selectors
    assert 'terminal_outcome!~"completed|failed"' in selectors
    assert 'execution_status!~"success|degraded|failed"' in selectors
    assert 'selection_family!~"router_dynamic|router_tree_baseline|fixed|unknown"' in selectors
    assert 'source            = "emitted_at"' in alloy
    assert 'values = ["event", "filename", "schema", "transport_schema"]' in alloy
    assert 'job      = "opensquilla-ensemble"' in alloy
    for forbidden in _FORBIDDEN_IDENTITY_TERMS:
        assert forbidden not in alloy


def test_loki_rules_are_loaded_from_the_single_tenant_and_evidence_gated() -> None:
    loki = _load_yaml(_STACK / "loki" / "config.yaml")
    assert loki["ruler"]["storage"]["local"]["directory"] == "/etc/loki/rules"
    assert loki["limits_config"]["retention_period"] == "336h"
    assert loki["compactor"]["retention_enabled"] is True

    rules_document = _load_yaml(_RULES)
    groups = rules_document["groups"]
    assert [group["name"] for group in groups] == ["opensquilla-ensemble-safety"]
    rules = groups[0]["rules"]
    alert_names = [rule["alert"] for rule in rules]
    assert len(alert_names) == len(set(alert_names)) == 6
    assert {
        "OpenSquillaEnsembleFailureRateHigh",
        "OpenSquillaAggregatorUsageCoverageLow",
        "OpenSquillaAggregatorUnknownUsageHigh",
        "OpenSquillaCleanupEvidenceUnsafe",
        "OpenSquillaPersistentCanaryLedgerUnavailable",
        "OpenSquillaTraceEvidenceCapRateHigh",
    } == set(alert_names)
    for rule in rules:
        expression = rule["expr"]
        assert '{job="opensquilla-ensemble"}' in expression or (
            'job="opensquilla-ensemble",' in expression
        )
        assert rule["labels"] == {"severity": "critical"}
        assert set(rule["annotations"]) == {"summary", "description"}
        if "unwrap " in expression:
            assert '| __error__=""' in expression
        for forbidden in _FORBIDDEN_IDENTITY_TERMS:
            assert forbidden not in expression

    coverage = next(
        rule for rule in rules if rule["alert"] == "OpenSquillaAggregatorUsageCoverageLow"
    )["expr"]
    assert 'aggregator_usage_projection_complete = "true"' in coverage
    assert 'aggregator_usage_observed = "true"' in coverage


def test_grafana_provisioning_and_dashboard_are_queryable_low_cardinality() -> None:
    datasource = _load_yaml(_STACK / "grafana" / "provisioning" / "datasources" / "loki.yaml")
    assert datasource["datasources"][0]["uid"] == "opensquilla-loki"
    assert datasource["datasources"][0]["url"] == "http://loki:3100"
    provider = _load_yaml(_STACK / "grafana" / "provisioning" / "dashboards" / "dashboards.yaml")
    assert provider["providers"][0]["options"]["path"] == "/var/lib/grafana/dashboards"

    dashboard = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    assert dashboard["uid"] == "opensquilla-ensemble-execution-v1"
    assert dashboard["title"] == "OpenSquilla Ensemble Execution"
    assert len(dashboard["panels"]) == 22
    assert {
        "Analyzer reliability",
        "Exact admission pressure",
        "Runtime health pressure",
        "Observed logical terminal HTTP",
        "Quorum and proposer recovery",
        "Aggregator attempt outcomes",
        "Role usage and cache evidence",
        "Canary rollout and budget",
        "Core latency p95",
        "Trace size evidence p95",
        "Evidence boundary",
    }.issubset({panel["title"] for panel in dashboard["panels"]})
    targets = [target for panel in dashboard["panels"] for target in panel.get("targets", [])]
    assert targets
    for target in targets:
        assert target["datasource"]["uid"] == "opensquilla-loki"
        assert '{job="opensquilla-ensemble"' in target["expr"]
        for forbidden in _FORBIDDEN_IDENTITY_TERMS:
            assert forbidden not in target["expr"]

    expressions = "\n".join(target["expr"] for target in targets)
    assert 'aggregator_usage_projection_complete = "true"' in expressions
    assert 'aggregator_usage_observed = "true"' in expressions
    assert 'ranking_snapshot_build_ms_observed = "true"' in expressions
    assert 'ranking_hard_filter_ms_observed = "true"' in expressions
    assert 'ranking_score_ms_observed = "true"' in expressions
    assert 'aggregator_cost_projection_complete = "true"' in expressions
    assert "canary_persistent_rollout_admission_unavailable_count" in expressions
    assert "cleanup_stream_close_unproven_count" in expressions
    assert not re.search(r"\b(provider|model|deployment|session|task|user)=", expressions)


def test_live_verifier_rows_satisfy_the_runtime_transport_contract() -> None:
    verifier = _load_verifier()
    rows = verifier._synthetic_rows(datetime(2026, 8, 12, tzinfo=UTC))
    assert len(rows) == 3
    for row in rows:
        assert row["transport_schema"] == ("opensquilla.ensemble-execution-metrics-jsonl/v1")
        assert row["event"] == "llm_ensemble.execution.metrics"
        metrics = dict(row)
        del metrics["transport_schema"]
        del metrics["event"]
        del metrics["emitted_at"]
        validate_ensemble_execution_metrics(metrics)
        for forbidden in _FORBIDDEN_IDENTITY_TERMS:
            assert forbidden not in row


def test_live_verifier_cleans_partial_project_when_compose_up_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _load_verifier()
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(verifier, "verify_compose_configuration", lambda _environment: None)

    def _failed_up(command: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        calls.append(tuple(command))
        if "up" in command:
            raise verifier.VerificationError("deterministic partial-up failure")
        return SimpleNamespace(stdout="", stderr="", returncode=1)

    monkeypatch.setattr(verifier, "_run", _failed_up)

    with pytest.raises(verifier.VerificationError, match="partial-up"):
        verifier.verify_live_stack(timeout=1)

    assert any("up" in command for command in calls)
    assert any("down" in command and "--volumes" in command for command in calls)


def test_reference_stack_documentation_keeps_production_boundary_explicit() -> None:
    readme = (_STACK / "README.md").read_text(encoding="utf-8")
    slo = (_ROOT / "docs" / "features" / "ensemble-execution-metrics-dashboard-slo.md").read_text(
        encoding="utf-8"
    )
    environment = (_ROOT / ".env.example").read_text(encoding="utf-8")
    for text in (readme, slo):
        assert "single-host" in text
        assert "delivery acknowledgement" in text
        assert "production" in text
    assert "trusted handoff" in readme
    assert "不是第二个任意 JSON allowlist" in slo
    assert "verify_reference_stack.py --live" in readme
    assert "OPENSQUILLA_GRAFANA_ADMIN_PASSWORD" in environment
    assert "OPENSQUILLA_ENSEMBLE_METRICS_JSONL_DIR" in environment
