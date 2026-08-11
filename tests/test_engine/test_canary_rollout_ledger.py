"""Persistent canary rollout ledger and its cycle-free public boundary."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from opensquilla.canary_rollout import (
    CanaryAdmissionReason,
    CanaryAttemptOutcome,
    CanaryLatchReason,
    CanaryMutationReason,
    CanaryProviderOutcome,
    CanaryQualityOutcome,
    CanaryRolloutLedger,
    CanaryRolloutPolicy,
    CanaryRolloutRole,
    CanaryRolloutScope,
    CanaryRolloutState,
    CanaryUsageOutcome,
    canary_deployment_sha256,
    canary_rollout_policy_sha256,
)

POLICY_SHA256 = hashlib.sha256(b"canary-policy").hexdigest()
OTHER_POLICY_SHA256 = hashlib.sha256(b"other-canary-policy").hexdigest()
SUCCESS = CanaryAttemptOutcome(
    provider=CanaryProviderOutcome.SUCCESS,
    usage=CanaryUsageOutcome.OBSERVED,
    quality=CanaryQualityOutcome.UNOBSERVED,
)
QUALITY_PASSED = CanaryAttemptOutcome(
    provider=CanaryProviderOutcome.SUCCESS,
    usage=CanaryUsageOutcome.OBSERVED,
    quality=CanaryQualityOutcome.PASSED,
)
RATE_LIMITED = CanaryAttemptOutcome(
    provider=CanaryProviderOutcome.RATE_LIMITED,
    usage=CanaryUsageOutcome.NOT_APPLICABLE,
    quality=CanaryQualityOutcome.NOT_APPLICABLE,
)
UPSTREAM_5XX = CanaryAttemptOutcome(
    provider=CanaryProviderOutcome.UPSTREAM_5XX,
    usage=CanaryUsageOutcome.NOT_APPLICABLE,
    quality=CanaryQualityOutcome.NOT_APPLICABLE,
)


def test_canary_rollout_import_orders_and_legacy_module_remain_compatible() -> None:
    repository = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(repository / "src"),
            environment.get("PYTHONPATH", ""),
        ]
    ).rstrip(os.pathsep)
    import_orders = (
        (
            "import opensquilla.gateway.config; "
            "import opensquilla.provider; "
            "import opensquilla.engine.routing.canary_rollout"
        ),
        (
            "import opensquilla.provider; "
            "import opensquilla.gateway.config; "
            "import opensquilla.engine.runtime"
        ),
    )
    for source in import_orders:
        completed = subprocess.run(
            [sys.executable, "-c", source],
            cwd=repository,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert completed.returncode == 0, completed.stderr

    from opensquilla.engine.routing import canary_rollout as legacy
    from opensquilla.provider.deployment import (
        canonicalize_provider_routing_upstream as deployment_canonicalize,
    )
    from opensquilla.routing_identity import canonicalize_provider_routing_upstream

    assert legacy.CanaryRolloutLedger is CanaryRolloutLedger
    assert deployment_canonicalize is canonicalize_provider_routing_upstream


def _scope(
    suffix: str = "a",
    *,
    role: CanaryRolloutRole = CanaryRolloutRole.PROPOSER,
    policy_sha256: str = POLICY_SHA256,
) -> CanaryRolloutScope:
    return CanaryRolloutScope.from_identity(
        policy_sha256=policy_sha256,
        role=role,
        provider="Fake",
        model=f"private-canary-{suffix}",
        upstream=f"https://UPSTREAM.example/{suffix}/",
    )


def _policy(**overrides: Any) -> CanaryRolloutPolicy:
    return replace(CanaryRolloutPolicy(), **overrides)


def _admit_and_settle(
    ledger: CanaryRolloutLedger,
    scope: CanaryRolloutScope,
    policy: CanaryRolloutPolicy,
    outcome: CanaryAttemptOutcome,
    *,
    now_ms: int,
) -> None:
    admission = ledger.begin_attempt(scope, policy, now_ms=now_ms)
    assert admission.allowed
    settled = ledger.settle_attempt(
        scope,
        policy,
        admission.token,
        outcome,
        now_ms=now_ms + 1,
    )
    assert settled.available
    assert settled.applied


def test_rollout_policy_sha256_uses_canonical_json_and_rejects_nonfinite() -> None:
    first = {
        "enabled": True,
        "policy_version": "router-canary-v1",
        "nested": {"x": 1},
    }
    second = {
        "nested": {"x": 1},
        "policy_version": "router-canary-v1",
        "enabled": True,
    }

    assert canary_rollout_policy_sha256(first) == canary_rollout_policy_sha256(second)
    assert canary_rollout_policy_sha256({"bad": float("nan")}) == hashlib.sha256(
        b'{"config_valid":false,"policy_version":"router-canary-v1"}'
    ).hexdigest()


def test_admin_snapshot_and_reset_use_authenticated_stored_contract(
    tmp_path: Path,
) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope("admin")
    policy = _policy()
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=100)

    snapshot = ledger.admin_snapshot(scope, now_ms=102)
    assert snapshot.available is True
    assert snapshot.state is CanaryRolloutState.ROLLED_BACK
    assert snapshot.latch_reason is CanaryLatchReason.RATE_LIMITED

    reset = ledger.admin_manual_reset(scope, now_ms=103)
    assert reset.available is True
    assert reset.applied is True
    assert reset.reason is CanaryMutationReason.APPLIED
    assert reset.snapshot.state is CanaryRolloutState.ACTIVE
    assert reset.snapshot.window_attempts == 0
    assert ledger.snapshot(scope, policy, now_ms=104).state is CanaryRolloutState.ACTIVE


def test_admin_reset_unknown_scope_does_not_create_state(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope("unknown-admin")

    reset = ledger.admin_manual_reset(scope, now_ms=100)

    assert reset.available is True
    assert reset.applied is False
    assert reset.reason is CanaryMutationReason.UNKNOWN_SCOPE
    snapshot = ledger.admin_snapshot(scope, now_ms=101)
    assert snapshot.available is True
    assert snapshot.found is False


def _persisted_ledger_rows(database: Path) -> tuple[tuple[tuple[Any, ...], ...], ...]:
    """Return every mutable ledger row for before/after safety assertions."""

    with sqlite3.connect(database) as connection:
        scopes = tuple(
            connection.execute(
                "SELECT * FROM canary_rollout_scopes "
                "ORDER BY policy_sha256, role, deployment_sha256"
            ).fetchall()
        )
        attempts = tuple(
            connection.execute(
                "SELECT * FROM canary_rollout_attempts "
                "ORDER BY policy_sha256, role, deployment_sha256, token_sha256"
            ).fetchall()
        )
        outcomes = tuple(
            connection.execute(
                "SELECT * FROM canary_rollout_outcomes ORDER BY outcome_id"
            ).fetchall()
        )
        sequences = tuple(
            connection.execute("SELECT * FROM sqlite_sequence ORDER BY name").fetchall()
        )
    return scopes, attempts, outcomes, sequences


def _install_v1_schema(
    database: Path,
    transform: Callable[[str], str],
) -> None:
    with sqlite3.connect(database) as connection:
        for statement in CanaryRolloutLedger._schema_statements():
            connection.execute(transform(statement))
        connection.execute("PRAGMA user_version=1")


def _half_open_worker(
    database: str,
    scope: CanaryRolloutScope,
    policy: CanaryRolloutPolicy,
    start: multiprocessing.synchronize.Event,
    output: multiprocessing.queues.Queue,
) -> None:
    ledger = CanaryRolloutLedger(database, busy_timeout_ms=2_000)
    start.wait(timeout=10)
    admission = ledger.begin_attempt(scope, policy, now_ms=2_000)
    output.put((admission.allowed, admission.reason.value, admission.probe))


def _abandon_attempt_worker(
    database: str,
    scope: CanaryRolloutScope,
    policy: CanaryRolloutPolicy,
    output: multiprocessing.queues.Queue,
) -> None:
    ledger = CanaryRolloutLedger(database, busy_timeout_ms=2_000)
    admission = ledger.begin_attempt(scope, policy, now_ms=10)
    output.put(admission.allowed)
    # Process exit deliberately abandons the durable lease.


def test_identity_is_canonical_hash_and_database_is_owner_only(tmp_path: Path) -> None:
    database = tmp_path / "state" / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy()

    admission = ledger.begin_attempt(scope, policy, now_ms=1)

    assert admission.allowed
    assert database.stat().st_mode & 0o777 == 0o600
    assert canary_deployment_sha256(
        " Fake ",
        "private-canary-a",
        "https://upstream.example/a",
    ) == canary_deployment_sha256(
        "fake",
        "private-canary-a",
        "https://UPSTREAM.example/a/",
    )
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT policy_sha256, role, deployment_sha256 FROM canary_rollout_scopes"
        ).fetchone()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert row == (POLICY_SHA256, "proposer", scope.deployment_sha256)
    assert "private-canary" not in repr(row)
    assert "upstream.example" not in repr(row)
    assert str(journal_mode).casefold() == "wal"
    assert synchronous == 2  # FULL
    assert version == 1


def test_identity_and_scope_reject_empty_or_malformed_keys() -> None:
    with pytest.raises(ValueError):
        canary_deployment_sha256("", "model")
    with pytest.raises(ValueError):
        canary_deployment_sha256("fake", "")
    with pytest.raises(ValueError):
        CanaryRolloutScope(
            policy_sha256=None,  # type: ignore[arg-type]
            role=CanaryRolloutRole.PROPOSER,
            deployment_sha256="a" * 64,
        )


@pytest.mark.parametrize(
    "invalid",
    [
        {"min_attempts": 51},
        {"max_provider_failure_basis_points": 10_001},
        {"max_consecutive_provider_failures": 0},
        {"half_open_successes_required": 0},
        {
            "window_max_attempts": 1,
            "min_attempts": 1,
            "min_quality_attempts": 1,
            "max_consecutive_provider_failures": 1,
            "half_open_successes_required": 3,
        },
        {"max_scopes": 0},
        {"quality_gate_enabled": 1},
        {"rollback_cooldown_s": 1},
    ],
)
def test_policy_rejects_unbounded_or_coerced_values(invalid: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _policy(**invalid)


def test_active_begin_rejects_scope_clock_regression_before_writing(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy()
    assert ledger.begin_attempt(scope, policy, now_ms=100).allowed
    before = _persisted_ledger_rows(database)

    regressed = ledger.begin_attempt(scope, policy, now_ms=50)

    assert not regressed.available
    assert not regressed.allowed
    assert regressed.reason is CanaryAdmissionReason.LEDGER_UNAVAILABLE
    assert _persisted_ledger_rows(database) == before
    assert ledger.begin_attempt(scope, policy, now_ms=101).allowed


def test_equal_scope_timestamp_is_allowed(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy()

    first = ledger.begin_attempt(scope, policy, now_ms=100)
    second = ledger.begin_attempt(scope, policy, now_ms=100)

    assert first.allowed
    assert second.allowed
    assert ledger.cancel_attempt(scope, policy, first.token, now_ms=100).applied


def test_rolled_back_begin_rejects_scope_clock_regression_before_writing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy()
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=100)
    before = _persisted_ledger_rows(database)

    regressed = ledger.begin_attempt(scope, policy, now_ms=100)

    assert not regressed.available
    assert not regressed.allowed
    assert regressed.reason is CanaryAdmissionReason.LEDGER_UNAVAILABLE
    assert _persisted_ledger_rows(database) == before


def test_half_open_begin_rejects_scope_clock_regression_before_writing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)
    probe = ledger.begin_attempt(scope, policy, now_ms=1_001)
    assert probe.allowed and probe.probe
    before = _persisted_ledger_rows(database)

    regressed = ledger.begin_attempt(scope, policy, now_ms=1_000)

    assert not regressed.available
    assert not regressed.allowed
    assert regressed.reason is CanaryAdmissionReason.LEDGER_UNAVAILABLE
    assert _persisted_ledger_rows(database) == before


def test_settle_and_cancel_reject_scope_clock_regression_before_writing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    policy = _policy()

    settle_scope = _scope("settle-clock")
    settlement = ledger.begin_attempt(settle_scope, policy, now_ms=100)
    before_settle = _persisted_ledger_rows(database)
    regressed_settlement = ledger.settle_attempt(
        settle_scope,
        policy,
        settlement.token,
        SUCCESS,
        now_ms=99,
    )
    assert not regressed_settlement.available
    assert not regressed_settlement.applied
    assert _persisted_ledger_rows(database) == before_settle
    assert ledger.settle_attempt(
        settle_scope,
        policy,
        settlement.token,
        SUCCESS,
        now_ms=101,
    ).applied

    cancel_scope = _scope("cancel-clock")
    cancellation = ledger.begin_attempt(cancel_scope, policy, now_ms=200)
    before_cancel = _persisted_ledger_rows(database)
    regressed_cancellation = ledger.cancel_attempt(
        cancel_scope,
        policy,
        cancellation.token,
        now_ms=199,
    )
    assert not regressed_cancellation.available
    assert not regressed_cancellation.applied
    assert _persisted_ledger_rows(database) == before_cancel
    assert ledger.cancel_attempt(
        cancel_scope,
        policy,
        cancellation.token,
        now_ms=201,
    ).applied


def test_snapshot_and_reset_reject_scope_clock_regression_before_writing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy()
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=100)
    before = _persisted_ledger_rows(database)

    assert not ledger.snapshot(scope, policy, now_ms=100).available
    assert _persisted_ledger_rows(database) == before
    reset = ledger.manual_reset(scope, policy, now_ms=100)
    assert not reset.available
    assert not reset.applied
    assert _persisted_ledger_rows(database) == before
    assert ledger.manual_reset(scope, policy, now_ms=102).applied


def test_cancel_and_window_pruning_advance_the_scope_clock_watermark(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    policy = _policy(window_max_age_s=1)

    cancelled_scope = _scope("cancel-watermark")
    admission = ledger.begin_attempt(cancelled_scope, policy, now_ms=100)
    assert ledger.cancel_attempt(
        cancelled_scope,
        policy,
        admission.token,
        now_ms=200,
    ).applied
    before_cancel_replay = _persisted_ledger_rows(database)
    assert not ledger.begin_attempt(cancelled_scope, policy, now_ms=199).available
    assert _persisted_ledger_rows(database) == before_cancel_replay

    pruned_scope = _scope("prune-watermark")
    _admit_and_settle(ledger, pruned_scope, policy, SUCCESS, now_ms=1_000)
    assert ledger.snapshot(pruned_scope, policy, now_ms=5_000).available
    before_prune_replay = _persisted_ledger_rows(database)
    assert not ledger.begin_attempt(pruned_scope, policy, now_ms=4_999).available
    assert _persisted_ledger_rows(database) == before_prune_replay


def test_scope_clocks_are_independent_and_gc_never_requires_global_order(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    policy = _policy(max_scopes=2, scope_retention_s=300)
    later_scope = _scope("later-clock")
    earlier_scope = _scope("earlier-clock")

    later = ledger.begin_attempt(later_scope, policy, now_ms=100)
    earlier = ledger.begin_attempt(earlier_scope, policy, now_ms=50)

    assert later.allowed
    assert earlier.allowed
    assert ledger.cancel_attempt(earlier_scope, policy, earlier.token, now_ms=51).applied
    assert ledger.cancel_attempt(later_scope, policy, later.token, now_ms=101).applied


def test_gc_only_evicts_a_scope_stale_relative_to_the_new_scope_time(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    policy = _policy(max_scopes=1, scope_retention_s=300)
    existing_scope = _scope("gc-existing")
    new_scope = _scope("gc-new")
    _admit_and_settle(ledger, existing_scope, policy, SUCCESS, now_ms=100_000)
    before = _persisted_ledger_rows(database)

    earlier = ledger.begin_attempt(new_scope, policy, now_ms=50)

    assert earlier.available
    assert not earlier.allowed
    assert earlier.reason is CanaryAdmissionReason.SCOPE_CAPACITY
    assert _persisted_ledger_rows(database) == before

    later = ledger.begin_attempt(new_scope, policy, now_ms=400_002)
    assert later.allowed
    assert not ledger.snapshot(existing_scope, policy, now_ms=400_003).found


def test_cross_policy_gc_uses_each_rows_authenticated_retention(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    long_policy = _policy(window_max_age_s=1_000, scope_retention_s=1_000)
    short_policy = _policy(window_max_age_s=1, scope_retention_s=1)
    long_scope = _scope("long-retention")
    short_scope = _scope(
        "short-retention",
        policy_sha256=OTHER_POLICY_SHA256,
    )
    _admit_and_settle(ledger, long_scope, long_policy, SUCCESS, now_ms=0)

    short_admission = ledger.begin_attempt(short_scope, short_policy, now_ms=2_000)

    assert short_admission.allowed
    with sqlite3.connect(database) as connection:
        persisted = connection.execute(
            "SELECT state, scope_retention_s FROM canary_rollout_scopes WHERE deployment_sha256=?",
            (long_scope.deployment_sha256,),
        ).fetchone()
        tombstones = connection.execute(
            "SELECT COUNT(*) FROM canary_rollout_outcomes WHERE deployment_sha256=?",
            (long_scope.deployment_sha256,),
        ).fetchone()[0]
    assert persisted == ("active", 1_000)
    assert tombstones == 1


def test_same_policy_identity_cannot_introduce_a_shorter_gc_contract(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    long_policy = _policy(window_max_age_s=1_000, scope_retention_s=1_000)
    short_policy = _policy(window_max_age_s=1, scope_retention_s=1)
    long_scope = _scope("same-policy-long")
    conflicting_scope = _scope("same-policy-short")
    _admit_and_settle(ledger, long_scope, long_policy, SUCCESS, now_ms=0)
    before = _persisted_ledger_rows(database)

    conflicting = ledger.begin_attempt(conflicting_scope, short_policy, now_ms=2_000)

    assert not conflicting.available
    assert not conflicting.allowed
    assert _persisted_ledger_rows(database) == before
    snapshot = CanaryRolloutLedger(database).snapshot(long_scope, long_policy, now_ms=2_001)
    assert snapshot.available
    assert snapshot.state is CanaryRolloutState.ACTIVE


@pytest.mark.parametrize(
    "tamper_sql",
    [
        "UPDATE canary_rollout_scopes SET scope_retention_s=1",
        "UPDATE canary_rollout_scopes SET policy_contract_json='{}'",
        "UPDATE canary_rollout_scopes SET policy_contract_sha256=" + "'" + "0" * 64 + "'",
    ],
)
def test_gc_rejects_malformed_row_owned_policy_contract(
    tmp_path: Path,
    tamper_sql: str,
) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    long_policy = _policy(window_max_age_s=1_000, scope_retention_s=1_000)
    short_policy = _policy(window_max_age_s=1, scope_retention_s=1)
    long_scope = _scope("corrupt-gc-contract")
    trigger_scope = _scope(
        "corrupt-gc-trigger",
        policy_sha256=OTHER_POLICY_SHA256,
    )
    _admit_and_settle(ledger, long_scope, long_policy, SUCCESS, now_ms=0)
    with sqlite3.connect(database) as connection:
        connection.execute(tamper_sql)
    before = _persisted_ledger_rows(database)

    trigger = ledger.begin_attempt(trigger_scope, short_policy, now_ms=2_000)

    assert not trigger.available
    assert not trigger.allowed
    assert _persisted_ledger_rows(database) == before
    assert (
        not CanaryRolloutLedger(database)
        .begin_attempt(
            trigger_scope,
            short_policy,
            now_ms=2_001,
        )
        .available
    )


def test_gc_never_evicts_pending_rolled_back_or_half_open_safety_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    pending_policy = _policy(
        window_max_age_s=1,
        scope_retention_s=1,
        rollback_cooldown_s=3_600,
        active_attempt_lease_s=3_600,
    )
    recovery_policy = _policy(
        window_max_age_s=1,
        scope_retention_s=1,
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
    )
    pending_scope = _scope(
        "gc-pending",
        policy_sha256=hashlib.sha256(b"gc-pending-policy").hexdigest(),
    )
    rolled_scope = _scope(
        "gc-rolled",
        policy_sha256=hashlib.sha256(b"gc-rolled-policy").hexdigest(),
    )
    half_open_scope = _scope(
        "gc-half-open",
        policy_sha256=hashlib.sha256(b"gc-half-open-policy").hexdigest(),
    )
    trigger_scope = _scope(
        "gc-state-trigger",
        policy_sha256=OTHER_POLICY_SHA256,
    )
    assert ledger.begin_attempt(pending_scope, pending_policy, now_ms=0).allowed
    _admit_and_settle(ledger, rolled_scope, recovery_policy, RATE_LIMITED, now_ms=0)
    _admit_and_settle(ledger, half_open_scope, recovery_policy, RATE_LIMITED, now_ms=0)
    probe = ledger.begin_attempt(half_open_scope, recovery_policy, now_ms=1_001)
    assert (
        ledger.settle_attempt(
            half_open_scope,
            recovery_policy,
            probe.token,
            SUCCESS,
            now_ms=1_002,
        ).snapshot.state
        is CanaryRolloutState.HALF_OPEN
    )

    trigger = ledger.begin_attempt(
        trigger_scope,
        _policy(window_max_age_s=1, scope_retention_s=1),
        now_ms=10_000,
    )

    assert trigger.allowed
    with sqlite3.connect(database) as connection:
        states = dict(
            connection.execute(
                "SELECT deployment_sha256, state FROM canary_rollout_scopes"
            ).fetchall()
        )
    assert states[pending_scope.deployment_sha256] == "active"
    assert states[rolled_scope.deployment_sha256] == "rolled_back"
    assert states[half_open_scope.deployment_sha256] == "half_open"


def test_active_gc_preserves_exact_expiry_then_cascades_tombstones(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    policy = _policy(window_max_age_s=1, scope_retention_s=1)
    active_scope = _scope("gc-boundary")
    boundary_scope = _scope(
        "gc-boundary-trigger",
        policy_sha256=OTHER_POLICY_SHA256,
    )
    after_scope = _scope(
        "gc-after-trigger",
        policy_sha256=OTHER_POLICY_SHA256,
    )
    _admit_and_settle(ledger, active_scope, policy, SUCCESS, now_ms=0)

    boundary = ledger.begin_attempt(boundary_scope, policy, now_ms=1_001)
    assert boundary.allowed
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM canary_rollout_scopes WHERE deployment_sha256=?",
                (active_scope.deployment_sha256,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM canary_rollout_outcomes WHERE deployment_sha256=?",
                (active_scope.deployment_sha256,),
            ).fetchone()[0]
            == 1
        )
    assert ledger.cancel_attempt(
        boundary_scope,
        policy,
        boundary.token,
        now_ms=1_001,
    ).applied

    assert ledger.begin_attempt(after_scope, policy, now_ms=1_002).allowed
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM canary_rollout_scopes WHERE deployment_sha256=?",
                (active_scope.deployment_sha256,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM canary_rollout_outcomes WHERE deployment_sha256=?",
                (active_scope.deployment_sha256,),
            ).fetchone()[0]
            == 0
        )


def test_rate_limit_latches_and_late_outcome_cannot_rewrite_root_cause(
    tmp_path: Path,
) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy()
    first = ledger.begin_attempt(scope, policy, now_ms=10)
    late = ledger.begin_attempt(scope, policy, now_ms=11)
    assert first.allowed and late.allowed

    latched = ledger.settle_attempt(
        scope,
        policy,
        first.token,
        RATE_LIMITED,
        now_ms=20,
    )
    assert latched.snapshot.state is CanaryRolloutState.ROLLED_BACK
    assert latched.snapshot.latch_reason is CanaryLatchReason.RATE_LIMITED
    assert latched.snapshot.latched_at_ms == 20

    configuration_failure = CanaryAttemptOutcome(
        provider=CanaryProviderOutcome.CONFIGURATION_FAILURE,
        usage=CanaryUsageOutcome.NOT_APPLICABLE,
        quality=CanaryQualityOutcome.NOT_APPLICABLE,
    )
    later = ledger.settle_attempt(
        scope,
        policy,
        late.token,
        configuration_failure,
        now_ms=30,
    )
    assert later.snapshot.latch_reason is CanaryLatchReason.RATE_LIMITED
    assert later.snapshot.latched_at_ms == 20
    blocked = ledger.begin_attempt(scope, policy, now_ms=31)
    assert not blocked.allowed
    assert blocked.reason is CanaryAdmissionReason.LATCHED


def test_provider_threshold_uses_exact_integer_basis_points(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy(
        min_attempts=4,
        max_provider_failure_basis_points=2_500,
        max_consecutive_provider_failures=4,
        max_rate_limited_count=50,
        max_usage_missing_count=50,
    )
    for timestamp, outcome in enumerate(
        (UPSTREAM_5XX, SUCCESS, SUCCESS, SUCCESS),
        start=1,
    ):
        _admit_and_settle(
            ledger,
            scope,
            policy,
            outcome,
            now_ms=timestamp * 10,
        )
    assert ledger.snapshot(scope, policy, now_ms=50).state is CanaryRolloutState.ACTIVE

    _admit_and_settle(ledger, scope, policy, UPSTREAM_5XX, now_ms=60)
    snapshot = ledger.snapshot(scope, policy, now_ms=70)
    assert snapshot.state is CanaryRolloutState.ROLLED_BACK
    assert snapshot.latch_reason is CanaryLatchReason.PROVIDER_FAILURE_RATE


def test_consecutive_failures_and_usage_missing_have_distinct_reasons(
    tmp_path: Path,
) -> None:
    policy = _policy(
        min_attempts=20,
        max_provider_failure_basis_points=10_000,
        max_consecutive_provider_failures=3,
        max_rate_limited_count=50,
        max_usage_missing_count=0,
    )
    first = CanaryRolloutLedger(tmp_path / "first.sqlite3")
    first_scope = _scope("first")
    for timestamp in (10, 20, 30):
        _admit_and_settle(first, first_scope, policy, UPSTREAM_5XX, now_ms=timestamp)
    assert (
        first.snapshot(first_scope, policy, now_ms=40).latch_reason
        is CanaryLatchReason.CONSECUTIVE_PROVIDER_FAILURES
    )

    second = CanaryRolloutLedger(tmp_path / "second.sqlite3")
    second_scope = _scope("second")
    missing = CanaryAttemptOutcome(
        provider=CanaryProviderOutcome.SUCCESS,
        usage=CanaryUsageOutcome.MISSING,
        quality=CanaryQualityOutcome.UNOBSERVED,
    )
    _admit_and_settle(second, second_scope, policy, missing, now_ms=10)
    assert (
        second.snapshot(second_scope, policy, now_ms=20).latch_reason
        is CanaryLatchReason.USAGE_MISSING
    )


def test_window_is_bounded_by_count_and_age(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy(
        window_max_attempts=5,
        window_max_age_s=1,
        min_attempts=5,
        max_provider_failure_basis_points=10_000,
        max_consecutive_provider_failures=5,
        max_rate_limited_count=5,
        max_usage_missing_count=5,
        min_quality_attempts=5,
    )
    for timestamp in range(0, 60, 10):
        _admit_and_settle(ledger, scope, policy, SUCCESS, now_ms=timestamp)
    bounded = ledger.snapshot(scope, policy, now_ms=100)
    assert bounded.window_attempts == 5

    expired = ledger.snapshot(scope, policy, now_ms=2_000)
    assert expired.window_attempts == 0


def test_quality_gate_requires_coverage_then_uses_observed_denominator(
    tmp_path: Path,
) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy(
        min_attempts=2,
        max_provider_failure_basis_points=10_000,
        max_consecutive_provider_failures=50,
        max_rate_limited_count=50,
        max_usage_missing_count=50,
        quality_gate_enabled=True,
        min_quality_attempts=2,
        min_quality_coverage_basis_points=5_000,
        max_quality_failure_basis_points=5_000,
    )
    unobserved = SUCCESS
    passed = replace(SUCCESS, quality=CanaryQualityOutcome.PASSED)
    failed = replace(SUCCESS, quality=CanaryQualityOutcome.FAILED)
    _admit_and_settle(ledger, scope, policy, unobserved, now_ms=10)
    _admit_and_settle(ledger, scope, policy, passed, now_ms=20)
    assert ledger.snapshot(scope, policy, now_ms=30).state is CanaryRolloutState.ACTIVE
    _admit_and_settle(ledger, scope, policy, failed, now_ms=40)
    assert ledger.snapshot(scope, policy, now_ms=50).state is CanaryRolloutState.ACTIVE
    _admit_and_settle(ledger, scope, policy, failed, now_ms=60)
    assert (
        ledger.snapshot(scope, policy, now_ms=70).latch_reason
        is CanaryLatchReason.QUALITY_FAILURE_RATE
    )


def test_idempotent_settlement_rejects_conflicting_replay(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy()
    admission = ledger.begin_attempt(scope, policy, now_ms=1)

    first = ledger.settle_attempt(scope, policy, admission.token, SUCCESS, now_ms=2)
    duplicate = ledger.settle_attempt(scope, policy, admission.token, SUCCESS, now_ms=3)
    conflict = ledger.settle_attempt(
        scope,
        policy,
        admission.token,
        UPSTREAM_5XX,
        now_ms=4,
    )

    assert first.applied
    assert duplicate.reason is CanaryMutationReason.DUPLICATE
    assert not duplicate.applied
    assert conflict.reason is CanaryMutationReason.TOKEN_CONFLICT
    assert conflict.snapshot.window_attempts == 1


def test_idempotency_tombstone_expires_with_bounded_window(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy(window_max_age_s=1)
    admission = ledger.begin_attempt(scope, policy, now_ms=0)
    ledger.settle_attempt(scope, policy, admission.token, SUCCESS, now_ms=1)

    expired = ledger.settle_attempt(
        scope,
        policy,
        admission.token,
        SUCCESS,
        now_ms=1_002,
    )

    assert expired.reason is CanaryMutationReason.UNKNOWN_TOKEN
    assert expired.snapshot.window_attempts == 0


def test_half_open_recovery_requires_serial_complete_successes(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_successes_required=2,
        half_open_probe_spacing_s=0,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)

    first_probe = ledger.begin_attempt(scope, policy, now_ms=1_001)
    assert first_probe.allowed and first_probe.probe
    first_result = ledger.settle_attempt(
        scope,
        policy,
        first_probe.token,
        SUCCESS,
        now_ms=1_002,
    )
    assert first_result.snapshot.state is CanaryRolloutState.HALF_OPEN
    assert first_result.snapshot.recovery_successes == 1

    second_probe = ledger.begin_attempt(scope, policy, now_ms=1_003)
    assert second_probe.allowed and second_probe.probe
    recovered = ledger.settle_attempt(
        scope,
        policy,
        second_probe.token,
        SUCCESS,
        now_ms=1_004,
    )
    assert recovered.snapshot.state is CanaryRolloutState.ACTIVE
    assert recovered.snapshot.latch_reason is None
    assert recovered.snapshot.window_attempts == 0


def test_failed_probe_starts_new_latch_epoch(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)
    probe = ledger.begin_attempt(scope, policy, now_ms=1_001)
    failed = ledger.settle_attempt(
        scope,
        policy,
        probe.token,
        UPSTREAM_5XX,
        now_ms=1_010,
    )

    assert failed.snapshot.state is CanaryRolloutState.ROLLED_BACK
    assert failed.snapshot.latch_reason is CanaryLatchReason.PROBE_FAILED
    assert failed.snapshot.latched_at_ms == 1_010


def test_configuration_failure_requires_manual_reset(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy()
    configuration_failure = CanaryAttemptOutcome(
        provider=CanaryProviderOutcome.CONFIGURATION_FAILURE,
        usage=CanaryUsageOutcome.NOT_APPLICABLE,
        quality=CanaryQualityOutcome.NOT_APPLICABLE,
    )
    _admit_and_settle(
        ledger,
        scope,
        policy,
        configuration_failure,
        now_ms=0,
    )

    blocked = ledger.begin_attempt(scope, policy, now_ms=10_000_000)
    assert not blocked.allowed
    assert blocked.reason is CanaryAdmissionReason.LATCHED
    assert blocked.snapshot.latch_reason is CanaryLatchReason.CONFIGURATION_FAILURE
    assert ledger.manual_reset(scope, policy, now_ms=10_000_001).applied
    assert ledger.begin_attempt(scope, policy, now_ms=10_000_002).allowed


def test_probe_cancel_preserves_latch_epoch_and_enforces_spacing(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=10,
        active_attempt_lease_s=10,
        half_open_probe_spacing_s=5,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)
    probe = ledger.begin_attempt(scope, policy, now_ms=10_001)
    cancelled = ledger.cancel_attempt(
        scope,
        policy,
        probe.token,
        now_ms=10_002,
    )
    assert cancelled.snapshot.state is CanaryRolloutState.ROLLED_BACK
    assert cancelled.snapshot.latch_reason is CanaryLatchReason.RATE_LIMITED
    assert cancelled.snapshot.latched_at_ms == 1

    waiting = ledger.begin_attempt(scope, policy, now_ms=10_003)
    assert not waiting.allowed
    assert waiting.reason is CanaryAdmissionReason.RECOVERY_WAIT
    assert waiting.snapshot.state is CanaryRolloutState.ROLLED_BACK
    retry = ledger.begin_attempt(scope, policy, now_ms=15_001)
    assert retry.allowed and retry.probe


def test_active_process_crash_expires_to_durable_rollback(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    database = tmp_path / "canary.sqlite3"
    scope = _scope()
    policy = _policy(active_attempt_lease_s=1)
    output = context.Queue()
    process = context.Process(
        target=_abandon_attempt_worker,
        args=(str(database), scope, policy, output),
    )
    process.start()
    assert output.get(timeout=10) is True
    process.join(timeout=10)
    assert process.exitcode == 0

    ledger = CanaryRolloutLedger(database)
    snapshot = ledger.snapshot(scope, policy, now_ms=1_011)
    assert snapshot.state is CanaryRolloutState.ROLLED_BACK
    assert snapshot.latch_reason is CanaryLatchReason.ATTEMPT_ABANDONED


def test_active_cancel_is_allowed_before_lease_but_not_at_or_after_expiry(
    tmp_path: Path,
) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    policy = _policy(active_attempt_lease_s=1)

    live_scope = _scope("live-cancel")
    live = ledger.begin_attempt(live_scope, policy, now_ms=0)
    cancelled = ledger.cancel_attempt(
        live_scope,
        policy,
        live.token,
        now_ms=999,
    )
    assert cancelled.applied
    assert cancelled.reason is CanaryMutationReason.CANCELLED
    assert cancelled.snapshot.state is CanaryRolloutState.ACTIVE
    assert cancelled.snapshot.window_attempts == 0

    expired_scope = _scope("expired-cancel")
    expired = ledger.begin_attempt(expired_scope, policy, now_ms=0)
    at_boundary = ledger.cancel_attempt(
        expired_scope,
        policy,
        expired.token,
        now_ms=1_000,
    )
    assert not at_boundary.applied
    assert at_boundary.reason is CanaryMutationReason.UNKNOWN_TOKEN
    assert at_boundary.snapshot.state is CanaryRolloutState.ROLLED_BACK
    assert at_boundary.snapshot.latch_reason is CanaryLatchReason.ATTEMPT_ABANDONED
    assert at_boundary.snapshot.latched_at_ms == 1_000

    late_scope = _scope("late-cancel")
    late = ledger.begin_attempt(late_scope, policy, now_ms=0)
    long_after_expiry = ledger.cancel_attempt(
        late_scope,
        policy,
        late.token,
        now_ms=2_000,
    )
    assert not long_after_expiry.applied
    assert long_after_expiry.reason is CanaryMutationReason.UNKNOWN_TOKEN
    assert long_after_expiry.snapshot.latch_reason is CanaryLatchReason.ATTEMPT_ABANDONED
    assert long_after_expiry.snapshot.latched_at_ms == 2_000

    repeated = ledger.cancel_attempt(
        expired_scope,
        policy,
        expired.token,
        now_ms=1_001,
    )
    assert repeated.reason is CanaryMutationReason.UNKNOWN_TOKEN
    assert repeated.snapshot.latch_reason is CanaryLatchReason.ATTEMPT_ABANDONED
    conflicting_settlement = ledger.settle_attempt(
        expired_scope,
        policy,
        expired.token,
        SUCCESS,
        now_ms=1_002,
    )
    assert conflicting_settlement.reason is CanaryMutationReason.TOKEN_CONFLICT
    assert conflicting_settlement.snapshot.latch_reason is CanaryLatchReason.ATTEMPT_ABANDONED


def test_abandoned_probe_relatches_and_does_not_auto_admit(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
        half_open_probe_lease_s=1,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)
    probe = ledger.begin_attempt(scope, policy, now_ms=1_001)
    assert probe.allowed and probe.probe

    blocked = ledger.begin_attempt(scope, policy, now_ms=2_002)
    assert not blocked.allowed
    assert blocked.reason is CanaryAdmissionReason.LATCHED
    assert blocked.snapshot.latch_reason is CanaryLatchReason.PROBE_ABANDONED
    assert blocked.snapshot.latched_at_ms == 2_002


def test_probe_cancel_cannot_erase_expired_recovery_lease(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
        half_open_probe_lease_s=1,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)

    live_probe = ledger.begin_attempt(scope, policy, now_ms=1_001)
    before_boundary = ledger.cancel_attempt(
        scope,
        policy,
        live_probe.token,
        now_ms=2_000,
    )
    assert before_boundary.applied
    assert before_boundary.reason is CanaryMutationReason.CANCELLED
    assert before_boundary.snapshot.latch_reason is CanaryLatchReason.RATE_LIMITED

    expired_probe = ledger.begin_attempt(scope, policy, now_ms=2_000)
    assert expired_probe.allowed and expired_probe.probe
    at_boundary = ledger.cancel_attempt(
        scope,
        policy,
        expired_probe.token,
        now_ms=3_000,
    )
    assert not at_boundary.applied
    assert at_boundary.reason is CanaryMutationReason.UNKNOWN_TOKEN
    assert at_boundary.snapshot.latch_reason is CanaryLatchReason.PROBE_ABANDONED
    assert at_boundary.snapshot.latched_at_ms == 3_000

    repeated = ledger.cancel_attempt(
        scope,
        policy,
        expired_probe.token,
        now_ms=3_001,
    )
    assert repeated.reason is CanaryMutationReason.UNKNOWN_TOKEN
    conflicting_settlement = ledger.settle_attempt(
        scope,
        policy,
        expired_probe.token,
        SUCCESS,
        now_ms=3_002,
    )
    assert conflicting_settlement.reason is CanaryMutationReason.TOKEN_CONFLICT
    assert conflicting_settlement.snapshot.latch_reason is CanaryLatchReason.PROBE_ABANDONED


def test_manual_reset_clears_window_latch_and_pending_tokens(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    scope = _scope()
    policy = _policy()
    pending = ledger.begin_attempt(scope, policy, now_ms=0)
    trigger = ledger.begin_attempt(scope, policy, now_ms=1)
    ledger.settle_attempt(scope, policy, trigger.token, RATE_LIMITED, now_ms=2)

    reset = ledger.manual_reset(scope, policy, now_ms=3)

    assert reset.applied
    assert reset.snapshot.state is CanaryRolloutState.ACTIVE
    assert reset.snapshot.window_attempts == 0
    assert reset.snapshot.latch_reason is None
    stale = ledger.settle_attempt(scope, policy, pending.token, SUCCESS, now_ms=4)
    assert stale.reason is CanaryMutationReason.UNKNOWN_TOKEN


def test_pending_and_scope_caps_fail_closed_without_evicting_latch(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    pending_policy = _policy(max_pending_per_scope=1)
    scope = _scope("pending")
    assert ledger.begin_attempt(scope, pending_policy, now_ms=0).allowed
    capped = ledger.begin_attempt(scope, pending_policy, now_ms=1)
    assert not capped.allowed
    assert capped.reason is CanaryAdmissionReason.PENDING_CAPACITY

    capacity_ledger = CanaryRolloutLedger(tmp_path / "capacity.sqlite3")
    capacity_policy = _policy(max_scopes=1, scope_retention_s=300)
    latched_scope = _scope("latched")
    _admit_and_settle(
        capacity_ledger,
        latched_scope,
        capacity_policy,
        RATE_LIMITED,
        now_ms=0,
    )
    rejected = capacity_ledger.begin_attempt(
        _scope("other"),
        capacity_policy,
        now_ms=1_000_000,
    )
    assert not rejected.allowed
    assert rejected.reason is CanaryAdmissionReason.SCOPE_CAPACITY
    assert (
        capacity_ledger.snapshot(latched_scope, capacity_policy, now_ms=1_000_001).state
        is CanaryRolloutState.ROLLED_BACK
    )


def test_stale_active_scope_is_garbage_collected(tmp_path: Path) -> None:
    ledger = CanaryRolloutLedger(tmp_path / "canary.sqlite3")
    policy = _policy(max_scopes=1, scope_retention_s=300)
    old_scope = _scope("old")
    admission = ledger.begin_attempt(old_scope, policy, now_ms=0)
    ledger.cancel_attempt(old_scope, policy, admission.token, now_ms=1)

    new_scope = _scope("new")
    admitted = ledger.begin_attempt(new_scope, policy, now_ms=301_001)
    assert admitted.allowed
    assert not ledger.snapshot(old_scope, policy, now_ms=301_002).found


def test_sqlite_busy_rejects_only_that_admission_and_can_retry(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database, busy_timeout_ms=0)
    scope = _scope()
    policy = _policy()
    blocker = sqlite3.connect(database, isolation_level=None, timeout=0)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        blocked = ledger.begin_attempt(scope, policy, now_ms=1)
    finally:
        blocker.rollback()
        blocker.close()

    assert not blocked.available
    assert not blocked.allowed
    assert blocked.reason is CanaryAdmissionReason.LEDGER_UNAVAILABLE
    assert ledger.begin_attempt(scope, policy, now_ms=2).allowed


@pytest.mark.parametrize("kind", ["corrupt", "unknown_version"])
def test_corruption_and_unknown_schema_are_sticky_fail_closed(
    tmp_path: Path,
    kind: str,
) -> None:
    database = tmp_path / "canary.sqlite3"
    if kind == "corrupt":
        database.write_bytes(b"not a sqlite database")
        original = database.read_bytes()
    else:
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA user_version=99")
        original = b""
    ledger = CanaryRolloutLedger(database, busy_timeout_ms=0)

    first = ledger.begin_attempt(_scope(), _policy(), now_ms=1)
    second = ledger.begin_attempt(_scope(), _policy(), now_ms=2)

    assert not first.available and not first.allowed
    assert not second.available and not second.allowed
    if kind == "corrupt":
        assert database.read_bytes() == original
    else:
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_same_policy_hash_with_different_contract_is_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy()
    admission = ledger.begin_attempt(scope, policy, now_ms=1)
    assert admission.allowed
    ledger.cancel_attempt(scope, policy, admission.token, now_ms=2)

    mismatch = ledger.begin_attempt(
        scope,
        replace(policy, rollback_cooldown_s=901),
        now_ms=3,
    )
    assert not mismatch.available
    assert not ledger.begin_attempt(scope, policy, now_ms=4).available

    other_worker = CanaryRolloutLedger(database)
    snapshot = other_worker.snapshot(scope, policy, now_ms=5)
    assert snapshot.available
    assert snapshot.state is CanaryRolloutState.ROLLED_BACK
    assert snapshot.latch_reason is CanaryLatchReason.POLICY_CONTRACT_MISMATCH
    assert not other_worker.begin_attempt(scope, policy, now_ms=10_000_000).allowed
    assert other_worker.manual_reset(scope, policy, now_ms=10_000_001).applied
    assert other_worker.begin_attempt(scope, policy, now_ms=10_000_002).allowed


def test_tampered_recovery_counter_cannot_skip_required_probes(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
        half_open_successes_required=3,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)
    probe = ledger.begin_attempt(scope, policy, now_ms=1_001)
    assert probe.allowed and probe.probe
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE canary_rollout_scopes SET recovery_successes=999")

    settlement = ledger.settle_attempt(
        scope,
        policy,
        probe.token,
        SUCCESS,
        now_ms=1_002,
    )

    assert not settlement.available
    assert not settlement.applied
    with sqlite3.connect(database) as connection:
        persisted = connection.execute(
            "SELECT state, recovery_successes FROM canary_rollout_scopes"
        ).fetchone()
    assert persisted == ("half_open", 999)
    assert (
        not CanaryRolloutLedger(database)
        .snapshot(
            scope,
            policy,
            now_ms=1_003,
        )
        .available
    )


@pytest.mark.parametrize("tampered_counter", [0, 2])
def test_recovery_counter_requires_exact_current_epoch_probe_proof(
    tmp_path: Path,
    tampered_counter: int,
) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
        half_open_successes_required=3,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)
    first = ledger.begin_attempt(scope, policy, now_ms=1_001)
    assert (
        ledger.settle_attempt(
            scope,
            policy,
            first.token,
            SUCCESS,
            now_ms=1_002,
        ).snapshot.recovery_successes
        == 1
    )
    second = ledger.begin_attempt(scope, policy, now_ms=1_003)
    assert second.allowed and second.probe
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE canary_rollout_scopes SET recovery_successes=?",
            (tampered_counter,),
        )
    before = _persisted_ledger_rows(database)

    settlement = ledger.settle_attempt(
        scope,
        policy,
        second.token,
        SUCCESS,
        now_ms=1_004,
    )

    assert not settlement.available
    assert not settlement.applied
    assert _persisted_ledger_rows(database) == before


def test_fabricated_current_epoch_probe_outcome_is_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
        half_open_successes_required=3,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)
    probe = ledger.begin_attempt(scope, policy, now_ms=1_001)
    assert ledger.settle_attempt(
        scope,
        policy,
        probe.token,
        SUCCESS,
        now_ms=1_002,
    ).applied
    fake_token = hashlib.sha256(b"fabricated-probe").hexdigest()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO canary_rollout_outcomes ("
            "policy_sha256, role, deployment_sha256, token_sha256, "
            "observed_at_ms, provider_outcome, usage_outcome, quality_outcome, "
            "is_probe, recovery_epoch_sha256, probe_ordinal"
            ") SELECT policy_sha256, role, deployment_sha256, ?, updated_at_ms, "
            "'success', 'observed', 'unobserved', 1, "
            "recovery_epoch_sha256, 2 FROM canary_rollout_scopes",
            (fake_token,),
        )

    assert not ledger.snapshot(scope, policy, now_ms=1_003).available


def test_probe_outcome_epoch_and_ordinal_are_validated(tmp_path: Path) -> None:
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
        half_open_successes_required=4,
    )

    epoch_database = tmp_path / "epoch.sqlite3"
    epoch_ledger = CanaryRolloutLedger(epoch_database)
    epoch_scope = _scope("epoch-proof")
    _admit_and_settle(epoch_ledger, epoch_scope, policy, RATE_LIMITED, now_ms=0)
    epoch_probe = epoch_ledger.begin_attempt(epoch_scope, policy, now_ms=1_001)
    assert epoch_ledger.settle_attempt(
        epoch_scope,
        policy,
        epoch_probe.token,
        SUCCESS,
        now_ms=1_002,
    ).applied
    with sqlite3.connect(epoch_database) as connection:
        connection.execute(
            "UPDATE canary_rollout_outcomes SET recovery_epoch_sha256=? WHERE is_probe=1",
            (hashlib.sha256(b"wrong-epoch").hexdigest(),),
        )
    assert not epoch_ledger.snapshot(epoch_scope, policy, now_ms=1_003).available

    order_database = tmp_path / "ordinal.sqlite3"
    order_ledger = CanaryRolloutLedger(order_database)
    order_scope = _scope("ordinal-proof")
    _admit_and_settle(order_ledger, order_scope, policy, RATE_LIMITED, now_ms=0)
    first = order_ledger.begin_attempt(order_scope, policy, now_ms=1_001)
    assert order_ledger.settle_attempt(
        order_scope,
        policy,
        first.token,
        SUCCESS,
        now_ms=1_002,
    ).applied
    second = order_ledger.begin_attempt(order_scope, policy, now_ms=1_003)
    assert order_ledger.settle_attempt(
        order_scope,
        policy,
        second.token,
        SUCCESS,
        now_ms=1_004,
    ).applied
    with sqlite3.connect(order_database) as connection:
        connection.execute(
            "UPDATE canary_rollout_outcomes SET probe_ordinal=3 "
            "WHERE is_probe=1 AND probe_ordinal=2"
        )
    assert not order_ledger.snapshot(order_scope, policy, now_ms=1_005).available


def test_quality_gated_probe_proof_requires_passed_quality(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
        half_open_successes_required=3,
        quality_gate_enabled=True,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)
    probe = ledger.begin_attempt(scope, policy, now_ms=1_001)
    assert (
        ledger.settle_attempt(
            scope,
            policy,
            probe.token,
            QUALITY_PASSED,
            now_ms=1_002,
        ).snapshot.recovery_successes
        == 1
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE canary_rollout_outcomes SET quality_outcome='unobserved' WHERE is_probe=1"
        )

    assert not ledger.snapshot(scope, policy, now_ms=1_003).available


def test_recovery_epoch_rollover_keeps_bounded_idempotency_tombstones(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
        half_open_successes_required=3,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)
    success = ledger.begin_attempt(scope, policy, now_ms=1_001)
    assert ledger.settle_attempt(
        scope,
        policy,
        success.token,
        SUCCESS,
        now_ms=1_002,
    ).applied
    failed = ledger.begin_attempt(scope, policy, now_ms=1_003)
    failure = ledger.settle_attempt(
        scope,
        policy,
        failed.token,
        RATE_LIMITED,
        now_ms=1_004,
    )
    assert failure.snapshot.state is CanaryRolloutState.ROLLED_BACK
    assert failure.snapshot.recovery_successes == 0
    with sqlite3.connect(database) as connection:
        current_epoch = connection.execute(
            "SELECT recovery_epoch_sha256 FROM canary_rollout_scopes"
        ).fetchone()[0]
        current_proofs = connection.execute(
            "SELECT COUNT(*) FROM canary_rollout_outcomes "
            "WHERE is_probe=1 AND recovery_epoch_sha256=?",
            (current_epoch,),
        ).fetchone()[0]
    assert current_proofs == 0
    duplicate = ledger.settle_attempt(
        scope,
        policy,
        failed.token,
        RATE_LIMITED,
        now_ms=1_005,
    )
    assert duplicate.reason is CanaryMutationReason.DUPLICATE


def test_current_recovery_proof_survives_generic_window_age_pruning(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
        half_open_successes_required=3,
        window_max_age_s=1,
        scope_retention_s=1,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)
    probe = ledger.begin_attempt(scope, policy, now_ms=1_001)
    assert ledger.settle_attempt(
        scope,
        policy,
        probe.token,
        SUCCESS,
        now_ms=1_002,
    ).applied

    snapshot = ledger.snapshot(scope, policy, now_ms=10_000)

    assert snapshot.available
    assert snapshot.recovery_successes == 1
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM canary_rollout_outcomes WHERE is_probe=1"
            ).fetchone()[0]
            == 1
        )


def test_successful_activation_archives_no_probe_outcomes(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
        half_open_successes_required=2,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)
    first = ledger.begin_attempt(scope, policy, now_ms=1_001)
    assert ledger.settle_attempt(
        scope,
        policy,
        first.token,
        SUCCESS,
        now_ms=1_002,
    ).applied
    second = ledger.begin_attempt(scope, policy, now_ms=1_003)
    activated = ledger.settle_attempt(
        scope,
        policy,
        second.token,
        SUCCESS,
        now_ms=1_004,
    )
    assert activated.snapshot.state is CanaryRolloutState.ACTIVE
    with sqlite3.connect(database) as connection:
        persisted = connection.execute(
            "SELECT state, recovery_epoch_sha256, recovery_successes FROM canary_rollout_scopes"
        ).fetchone()
        outcome_count = connection.execute(
            "SELECT COUNT(*) FROM canary_rollout_outcomes"
        ).fetchone()[0]
    assert persisted == ("active", None, 0)
    assert outcome_count == 0
    fake_epoch = hashlib.sha256(b"active-fake-epoch").hexdigest()
    fake_token = hashlib.sha256(b"active-fake-probe").hexdigest()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO canary_rollout_outcomes ("
            "policy_sha256, role, deployment_sha256, token_sha256, "
            "observed_at_ms, provider_outcome, usage_outcome, quality_outcome, "
            "is_probe, recovery_epoch_sha256, probe_ordinal"
            ") SELECT policy_sha256, role, deployment_sha256, ?, updated_at_ms, "
            "'success', 'observed', 'unobserved', 1, ?, 1 "
            "FROM canary_rollout_scopes",
            (fake_token, fake_epoch),
        )
    assert not ledger.snapshot(scope, policy, now_ms=1_005).available


def test_active_state_rejects_impossible_recovery_progress(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy()
    admission = ledger.begin_attempt(scope, policy, now_ms=1)
    ledger.cancel_attempt(scope, policy, admission.token, now_ms=2)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE canary_rollout_scopes SET recovery_successes=1")

    assert not ledger.snapshot(scope, policy, now_ms=3).available
    assert not ledger.manual_reset(scope, policy, now_ms=4).available


def test_half_open_state_requires_probe_or_recovery_progress(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
        half_open_successes_required=3,
    )
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)
    probe = ledger.begin_attempt(scope, policy, now_ms=1_001)
    assert probe.allowed and probe.probe
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM canary_rollout_attempts")

    assert not ledger.snapshot(scope, policy, now_ms=1_002).available
    assert not ledger.begin_attempt(scope, policy, now_ms=1_003).available


def test_tampered_attempt_epoch_is_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy(active_attempt_lease_s=10)
    admission = ledger.begin_attempt(scope, policy, now_ms=100)
    assert admission.allowed
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE canary_rollout_attempts SET admitted_at_ms=101, lease_expires_at_ms=10101"
        )

    assert not ledger.snapshot(scope, policy, now_ms=102).available
    assert not ledger.cancel_attempt(scope, policy, admission.token, now_ms=103).available


def test_tampered_outcome_epoch_is_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy()
    _admit_and_settle(ledger, scope, policy, SUCCESS, now_ms=100)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE canary_rollout_outcomes SET observed_at_ms=999")

    assert not ledger.snapshot(scope, policy, now_ms=101).available
    assert not ledger.begin_attempt(scope, policy, now_ms=102).available


def test_tampered_enum_becomes_sticky_storage_failure(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy()
    admission = ledger.begin_attempt(scope, policy, now_ms=1)
    ledger.cancel_attempt(scope, policy, admission.token, now_ms=2)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE canary_rollout_scopes SET state='tampered'")

    assert not ledger.snapshot(scope, policy, now_ms=3).available
    assert not ledger.begin_attempt(scope, policy, now_ms=4).available


def test_tampered_persistent_timestamp_is_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "canary.sqlite3"
    ledger = CanaryRolloutLedger(database)
    scope = _scope()
    policy = _policy()
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE canary_rollout_scopes SET latched_at_ms='tampered'")

    assert not ledger.snapshot(scope, policy, now_ms=3).available
    assert not ledger.begin_attempt(scope, policy, now_ms=4).available


@pytest.mark.parametrize(
    ("replacement", "case_name"),
    [
        ("'RATE_LIMITED'", "literal-case"),
        ("'rate''_limited'", "literal-escape"),
        ("'rate_ limited'", "literal-whitespace"),
    ],
)
def test_schema_contract_preserves_check_literal_bytes(
    tmp_path: Path,
    replacement: str,
    case_name: str,
) -> None:
    database = tmp_path / f"{case_name}.sqlite3"
    _install_v1_schema(
        database,
        lambda statement: statement.replace("'rate_limited'", replacement),
    )
    ledger = CanaryRolloutLedger(database)

    first = ledger.begin_attempt(_scope(), _policy(), now_ms=1)
    second = ledger.begin_attempt(_scope(), _policy(), now_ms=2)

    assert not first.available and not first.allowed
    assert not second.available and not second.allowed
    assert (
        not CanaryRolloutLedger(database)
        .begin_attempt(
            _scope(),
            _policy(),
            now_ms=3,
        )
        .available
    )
    with sqlite3.connect(database) as connection:
        schema_sql = "\n".join(
            str(row[0])
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
            ).fetchall()
        )
        assert replacement in schema_sql
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_schema_contract_normalizes_unquoted_tokens_and_external_whitespace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "keyword-whitespace.sqlite3"

    def rewrite_syntax(statement: str) -> str:
        rewritten = (
            statement.replace("CREATE TABLE", "cReAtE\n\tTaBlE", 1)
            .replace("CREATE INDEX", "cReAtE\n\tInDeX", 1)
            .replace(" NOT NULL", "\n  nOt\tNuLl")
            .replace("canary_rollout_scopes", "CANARY_ROLLOUT_SCOPES")
            .replace("canary_rollout_attempts", "CANARY_ROLLOUT_ATTEMPTS")
            .replace("canary_rollout_outcomes", "CANARY_ROLLOUT_OUTCOMES")
        )
        return f"\n\t{rewritten}\n"

    _install_v1_schema(database, rewrite_syntax)
    ledger = CanaryRolloutLedger(database)

    admission = ledger.begin_attempt(_scope(), _policy(), now_ms=1)

    assert admission.available
    assert admission.allowed


def test_same_columns_without_v1_constraints_or_indexes_are_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canary.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE canary_rollout_scopes (
                policy_sha256 TEXT,
                role TEXT,
                deployment_sha256 TEXT,
                policy_contract_sha256 TEXT,
                policy_contract_json TEXT,
                scope_retention_s INTEGER,
                state TEXT,
                latch_reason TEXT,
                latched_at_ms INTEGER,
                recovery_epoch_sha256 TEXT,
                recovery_successes INTEGER,
                last_probe_at_ms INTEGER,
                created_at_ms INTEGER,
                updated_at_ms INTEGER
            );
            CREATE TABLE canary_rollout_attempts (
                policy_sha256 TEXT,
                role TEXT,
                deployment_sha256 TEXT,
                token_sha256 TEXT,
                admitted_at_ms INTEGER,
                lease_expires_at_ms INTEGER,
                is_probe INTEGER,
                recovery_epoch_sha256 TEXT,
                probe_ordinal INTEGER
            );
            CREATE TABLE canary_rollout_outcomes (
                outcome_id INTEGER,
                policy_sha256 TEXT,
                role TEXT,
                deployment_sha256 TEXT,
                token_sha256 TEXT,
                observed_at_ms INTEGER,
                provider_outcome TEXT,
                usage_outcome TEXT,
                quality_outcome TEXT,
                is_probe INTEGER,
                recovery_epoch_sha256 TEXT,
                probe_ordinal INTEGER
            );
            PRAGMA user_version=1;
            """
        )

    ledger = CanaryRolloutLedger(database)
    admission = ledger.begin_attempt(_scope(), _policy(), now_ms=1)

    assert not admission.available
    assert not admission.allowed
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'canary_rollout_%'"
            ).fetchone()[0]
            == 0
        )


def test_two_processes_can_acquire_only_one_half_open_probe(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    database = tmp_path / "canary.sqlite3"
    scope = _scope()
    policy = _policy(
        rollback_cooldown_s=1,
        active_attempt_lease_s=1,
        half_open_probe_spacing_s=0,
    )
    ledger = CanaryRolloutLedger(database)
    _admit_and_settle(ledger, scope, policy, RATE_LIMITED, now_ms=0)

    start = context.Event()
    output = context.Queue()
    processes = [
        context.Process(
            target=_half_open_worker,
            args=(str(database), scope, policy, start, output),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [output.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert sum(1 for allowed, _, _ in results if allowed) == 1
    assert sorted(reason for _, reason, _ in results) == [
        CanaryAdmissionReason.HALF_OPEN_BUSY,
        CanaryAdmissionReason.HALF_OPEN_PROBE,
    ]
    assert sum(1 for _, _, probe in results if probe) == 1
