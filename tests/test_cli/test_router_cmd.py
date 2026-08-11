"""``opensquilla router calibrate`` CLI.

Seeds a synthetic ``router_decisions`` table (hand-created — never real state)
and drives the command through Typer's ``CliRunner``. The output file always
lands under a monkeypatched ``OPENSQUILLA_STATE_DIR`` temp dir, never the real
home.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from opensquilla.canary_rollout import (
    CanaryAttemptOutcome,
    CanaryProviderOutcome,
    CanaryQualityOutcome,
    CanaryRolloutLedger,
    CanaryRolloutRole,
    CanaryRolloutScope,
    CanaryUsageOutcome,
    canary_rollout_policy_sha256,
)
from opensquilla.cli import router_cmd
from opensquilla.cli.main import app
from opensquilla.gateway.config import GatewayConfig
from opensquilla.persistence.router_decision_writer import RouterDecisionWriter
from opensquilla.provider.ensemble import _materialize_persistent_canary_policy
from opensquilla.recovery.locking import (
    acquire_gateway_legacy_lease,
    release_gateway_legacy_lease,
)

runner = CliRunner()

_CREATE_TABLE = (
    "CREATE TABLE router_decisions ("
    " decision_id TEXT PRIMARY KEY, session_key TEXT NOT NULL,"
    " turn_index INTEGER, ts_ms INTEGER NOT NULL, classifier TEXT,"
    " proposed_tier TEXT, confidence REAL, probs TEXT, flags TEXT,"
    " final_tier TEXT, provider TEXT, model TEXT, thinking_level TEXT,"
    " source TEXT, trail TEXT, baseline_model TEXT, savings_pct REAL,"
    " executed_kind TEXT, ensemble_profile TEXT,"
    " fallback_hops INTEGER NOT NULL DEFAULT 0)"
)


def _seed_db(path: Path, *, count: int = 40) -> None:
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_TABLE)
    writer = RouterDecisionWriter(conn)
    for index in range(count):
        writer.record_decision(
            {
                "decision_id": f"c{index}",
                "session_key": "agent:calib:main",
                "turn_index": index,
                "proposed_tier": "c2",
                "final_tier": "c1",
                "source": "v4_phase3",
                "trail": [{"stage": "confidence_gate", "applied": True}],
            }
        )
    writer.close()


def _env(monkeypatch: Any, tmp_path: Path, db: Path | None) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    if db is not None:
        monkeypatch.setenv("OPENSQUILLA_ROUTER_DECISIONS_DB", str(db))
    else:
        monkeypatch.delenv("OPENSQUILLA_ROUTER_DECISIONS_DB", raising=False)


def _output_file(tmp_path: Path) -> Path:
    return tmp_path / "home" / "state" / "router_calibration.json"


def _canary_config(state_root: Path) -> GatewayConfig:
    return GatewayConfig(
        state_dir=str(state_root),
        llm_ensemble={
            "canary_rollout": {
                "enabled": True,
                "global_basis_points": 100,
                "proposer": {
                    "basis_points": 100,
                    "max_candidates_per_decision": 1,
                },
                "auto_rollback": {"enabled": True},
            }
        },
    )


def _seed_canary_rollback(
    state_root: Path,
) -> tuple[GatewayConfig, CanaryRolloutLedger, CanaryRolloutScope, Any]:
    config = _canary_config(state_root)
    rollout = config.llm_ensemble.canary_rollout.model_dump(mode="json")
    policy = _materialize_persistent_canary_policy(rollout)
    assert policy is not None
    scope = CanaryRolloutScope.from_identity(
        policy_sha256=canary_rollout_policy_sha256(rollout),
        role=CanaryRolloutRole.PROPOSER,
        provider="fake",
        model="private-canary",
    )
    ledger = CanaryRolloutLedger(CanaryRolloutLedger.default_path(state_root))
    admission = ledger.begin_attempt(scope, policy)
    assert admission.allowed
    result = ledger.settle_attempt(
        scope,
        policy,
        admission.token,
        CanaryAttemptOutcome(
            provider=CanaryProviderOutcome.RATE_LIMITED,
            usage=CanaryUsageOutcome.NOT_APPLICABLE,
            quality=CanaryQualityOutcome.NOT_APPLICABLE,
        ),
    )
    assert result.applied
    return config, ledger, scope, policy


def test_calibrate_dry_run_prints_without_writing(tmp_path: Path, monkeypatch: Any) -> None:
    db = tmp_path / "sessions.db"
    _seed_db(db)
    _env(monkeypatch, tmp_path, db)

    result = runner.invoke(app, ["router", "calibrate", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "samples:" in result.output
    assert "threshold_adjust:" in result.output
    assert "dry-run" in result.output
    assert not _output_file(tmp_path).exists()


def test_calibrate_writes_file(tmp_path: Path, monkeypatch: Any) -> None:
    db = tmp_path / "sessions.db"
    _seed_db(db)
    _env(monkeypatch, tmp_path, db)

    result = runner.invoke(app, ["router", "calibrate"])
    assert result.exit_code == 0, result.output

    out = _output_file(tmp_path)
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["sample_count"] == 40
    # 40 clean c2 downgrades -> bias c2 down (clamped), threshold up.
    assert payload["per_class_bias"] == {"c2": -0.15}
    assert payload["threshold_adjust"] > 0.0


def test_calibrate_json_output(tmp_path: Path, monkeypatch: Any) -> None:
    db = tmp_path / "sessions.db"
    _seed_db(db)
    _env(monkeypatch, tmp_path, db)

    result = runner.invoke(app, ["router", "calibrate", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["wrote"] is True
    assert doc["calibration"]["sample_count"] == 40


def test_calibrate_missing_db_is_neutral_no_crash(tmp_path: Path, monkeypatch: Any) -> None:
    _env(monkeypatch, tmp_path, tmp_path / "does-not-exist.db")

    result = runner.invoke(app, ["router", "calibrate", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "samples:          0" in result.output
    assert not _output_file(tmp_path).exists()


def test_canary_rollout_status_and_offline_reset_exact_scope(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config, ledger, scope, policy = _seed_canary_rollback(tmp_path / "state")
    monkeypatch.setattr(router_cmd, "_load_canary_admin_config", lambda: config)
    arguments = [
        "--provider",
        "fake",
        "--model",
        "private-canary",
        "--role",
        "proposer",
        "--json",
    ]

    status = runner.invoke(app, ["router", "canary-rollout-status", *arguments])

    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload["found"] is True
    assert status_payload["state"] == "rolled_back"
    assert status_payload["latch_reason"] == "rate_limited"
    assert status_payload["policy_sha256_prefix"] == scope.policy_sha256[:12]

    reset = runner.invoke(
        app,
        ["router", "canary-rollout-reset", *arguments, "--yes"],
    )

    assert reset.exit_code == 0, reset.output
    reset_payload = json.loads(reset.output)
    assert reset_payload["state"] == "active"
    assert reset_payload["window_attempts"] == 0
    assert ledger.snapshot(scope, policy).state.value == "active"


def test_canary_rollout_reset_refuses_live_gateway_lock(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config, _ledger, _scope, _policy = _seed_canary_rollback(tmp_path / "state")
    monkeypatch.setattr(router_cmd, "_load_canary_admin_config", lambda: config)
    lease = acquire_gateway_legacy_lease(config.state_dir or "")
    assert lease is not None
    try:
        result = runner.invoke(
            app,
            [
                "router",
                "canary-rollout-reset",
                "--provider",
                "fake",
                "--model",
                "private-canary",
                "--yes",
            ],
        )
    finally:
        release_gateway_legacy_lease(lease)

    assert result.exit_code == 1
    assert "stop it before reset" in result.output


def test_canary_rollout_historical_hash_works_when_current_policy_is_disabled(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    enabled, _ledger, scope, _policy = _seed_canary_rollback(tmp_path / "state")
    disabled = GatewayConfig(state_dir=enabled.state_dir)
    monkeypatch.setattr(router_cmd, "_load_canary_admin_config", lambda: disabled)

    result = runner.invoke(
        app,
        [
            "router",
            "canary-rollout-status",
            "--provider",
            "fake",
            "--model",
            "private-canary",
            "--policy-sha256",
            scope.policy_sha256,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "rolled_back"


def test_canary_rollout_cli_rejects_bad_hash_and_wildcard_identity(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config = GatewayConfig(state_dir=str(tmp_path / "state"))
    monkeypatch.setattr(router_cmd, "_load_canary_admin_config", lambda: config)

    bad_hash = runner.invoke(
        app,
        [
            "router",
            "canary-rollout-status",
            "--provider",
            "fake",
            "--model",
            "private-canary",
            "--policy-sha256",
            "BAD",
        ],
    )
    wildcard = runner.invoke(
        app,
        [
            "router",
            "canary-rollout-status",
            "--provider",
            "",
            "--model",
            "*",
            "--policy-sha256",
            "a" * 64,
        ],
    )

    assert bad_hash.exit_code == 2
    assert "lowercase SHA-256" in bad_hash.output
    assert wildcard.exit_code == 2
    assert "provider and model are required" in wildcard.output
