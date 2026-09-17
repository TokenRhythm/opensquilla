from __future__ import annotations

import pytest

from opensquilla.sandbox.policy_models import SandboxPolicy
from opensquilla.sandbox.policy_store import (
    PolicyVersionConflict,
    SandboxPolicyStore,
)


def test_default_policy_has_three_gib_backup_quota() -> None:
    policy = SandboxPolicy()

    assert policy.schema_version == 2
    assert policy.policy_version == 0
    assert policy.files.recursive_delete_backup_enabled is True
    assert policy.files.backup_quota_bytes == 3 * 1024**3
    assert policy.network.block_all_network is False
    assert policy.runtimes.python is True
    assert policy.runtimes.node is True
    assert policy.runtimes.git_bash is True


def test_store_creates_and_reads_default_policy(tmp_path) -> None:
    store = SandboxPolicyStore(tmp_path / "sessions.db")

    first = store.read()
    second = store.read()

    assert first == second == SandboxPolicy()


def test_compare_and_swap_increments_version_atomically(tmp_path) -> None:
    store = SandboxPolicyStore(tmp_path / "sessions.db")
    baseline = store.read()
    draft = baseline.model_copy(deep=True)
    draft.network.deny_domains.append("telemetry.example")

    saved = store.compare_and_swap(baseline.policy_version, draft)

    assert saved.policy_version == 1
    assert saved.network.deny_domains == ["telemetry.example"]
    assert store.read() == saved


def test_compare_and_swap_rejects_stale_version(tmp_path) -> None:
    store = SandboxPolicyStore(tmp_path / "sessions.db")
    first = store.read()
    store.compare_and_swap(first.policy_version, first)

    with pytest.raises(PolicyVersionConflict) as exc_info:
        store.compare_and_swap(first.policy_version, first)

    assert exc_info.value.expected_version == 0
    assert exc_info.value.current_policy.policy_version == 1


def test_policy_rejects_invalid_prefix_and_quota() -> None:
    with pytest.raises(ValueError):
        SandboxPolicy.model_validate(
            {
                "commands": {"requireApprovalPrefixes": [[]]},
                "files": {"backupQuotaBytes": 0},
            }
        )
