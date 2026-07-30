"""Regression tests for channel-plugin contract invariants.

The channel contract previously enforced ``CAPABILITY_TIER`` /
``DM_SAFETY_TIERS`` / error-class taxonomy membership with bare
``assert`` statements. Those are stripped under ``python -O``. These
tests pin the helpers to explicit ``raise`` semantics so contract
validation still rejects invalid adapter declarations when the
interpreter runs with optimization enabled.

The tests use a synthetic module so they don't depend on a particular
shipped adapter diverging from the canonical taxonomy — they exercise
the helper directly.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from opensquilla.channels import contract
from opensquilla.channels.contract import (
    assert_capability_tier,
    assert_dm_safety_tiers,
    assert_error_class_taxonomy,
    run_channel_contract,
)


def _make_module(**attrs: object) -> ModuleType:
    module = ModuleType("synthetic_adapter")
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def test_assert_capability_tier_raises_value_error_for_unknown_tier() -> None:
    module = _make_module(CAPABILITY_TIER="PURPLE-experimental")
    with pytest.raises(ValueError, match="CAPABILITY_TIER"):
        assert_capability_tier(module)


def test_assert_capability_tier_raises_for_missing_attribute() -> None:
    module = _make_module()
    with pytest.raises(ValueError, match="CAPABILITY_TIER"):
        assert_capability_tier(module)


def test_assert_dm_safety_tiers_rejects_non_tuple() -> None:
    module = _make_module(DM_SAFETY_TIERS=["safe"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be a tuple"):
        assert_dm_safety_tiers(module)


def test_assert_dm_safety_tiers_rejects_empty() -> None:
    module = _make_module(DM_SAFETY_TIERS=())
    with pytest.raises(ValueError, match="must be non-empty"):
        assert_dm_safety_tiers(module)


def test_assert_dm_safety_tiers_rejects_admin_only() -> None:
    module = _make_module(DM_SAFETY_TIERS=("safe", "admin-only"))
    with pytest.raises(ValueError, match="admin-only"):
        assert_dm_safety_tiers(module)


def test_assert_dm_safety_tiers_rejects_unknown_tier_value() -> None:
    module = _make_module(DM_SAFETY_TIERS=("safe", "purple"))
    with pytest.raises(ValueError, match="unknown safety tier"):
        assert_dm_safety_tiers(module)


def test_assert_error_class_taxonomy_rejects_diverged_retryable() -> None:
    module = _make_module(
        RETRYABLE_ERROR_CLASSES=("transport_transient",),
        FATAL_ERROR_CLASSES=contract.REQUIRED_FATAL_ERROR_CLASSES,
    )
    with pytest.raises(ValueError, match="RETRYABLE_ERROR_CLASSES"):
        assert_error_class_taxonomy(module)


def test_assert_error_class_taxonomy_rejects_diverged_fatal() -> None:
    module = _make_module(
        RETRYABLE_ERROR_CLASSES=contract.REQUIRED_RETRYABLE_ERROR_CLASSES,
        FATAL_ERROR_CLASSES=("auth_failure",),
    )
    with pytest.raises(ValueError, match="FATAL_ERROR_CLASSES"):
        assert_error_class_taxonomy(module)


def test_run_channel_contract_raises_value_error_for_bad_tier() -> None:
    module = _make_module(
        CAPABILITY_TIER="PURPLE-experimental",
        DM_SAFETY_TIERS=("safe",),
        RETRYABLE_ERROR_CLASSES=contract.REQUIRED_RETRYABLE_ERROR_CLASSES,
        FATAL_ERROR_CLASSES=contract.REQUIRED_FATAL_ERROR_CLASSES,
    )
    with pytest.raises(ValueError, match="CAPABILITY_TIER"):
        run_channel_contract(module)


def test_contract_invariants_are_explicit_not_assert() -> None:
    """Sanity: the contract helpers must use ``raise``, not ``assert``.

    Asserts are stripped under ``python -O``; this test guards against a
    regression that re-introduces ``assert``-based enforcement in any
    of the three contract helpers.
    """

    import inspect

    cap_source = inspect.getsource(assert_capability_tier)
    assert "assert tier in" not in cap_source, (
        "assert_capability_tier must not use assert — invariants must "
        "survive ``python -O``"
    )
    err_source = inspect.getsource(assert_error_class_taxonomy)
    assert "assert retryable ==" not in err_source, (
        "assert_error_class_taxonomy must not use assert — a misconfigured "
        "adapter must not be able to relabel errors when assertions are "
        "stripped"
    )
    assert "assert fatal ==" not in err_source, (
        "assert_error_class_taxonomy must not use assert for the fatal "
        "tuple either"
    )


@pytest.mark.skipif(sys.platform != "linux", reason="symlink semantics differ on Windows")
def test_run_channel_contract_isolated_under_optimization() -> None:
    """When run with ``-O``, the contract must still reject bad tiers.

    This is the headline regression: a crafted plugin's ``CAPABILITY_TIER``
    must never be accepted when the interpreter is hardened. We verify by
    importing the helpers in a subprocess with ``-O``.
    """

    import subprocess

    snippet = """
import sys
from types import ModuleType

from opensquilla.channels.contract import assert_capability_tier

m = ModuleType('bad')
m.CAPABILITY_TIER = 'PURPLE-experimental'
try:
    assert_capability_tier(m)
except ValueError:
    print('OK')
    sys.exit(0)
except AssertionError:
    print('ASSERT-LEAKED')
    sys.exit(2)
except Exception as exc:
    print(f'OTHER:{type(exc).__name__}')
    sys.exit(3)
print('NO-RAISE')
sys.exit(4)
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", snippet],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"contract invariant stripped under -O (rc={result.returncode}):\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"
