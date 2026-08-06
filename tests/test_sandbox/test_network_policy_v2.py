from __future__ import annotations

from opensquilla.sandbox.network_guard import decide_network_access
from opensquilla.sandbox.policy_models import SandboxPolicy
from opensquilla.sandbox.run_context import RunContext
from opensquilla.sandbox.run_mode import RunMode


def _safe_context() -> RunContext:
    return RunContext(run_mode=RunMode.SAFE)


def test_default_open_allows_public_domain() -> None:
    decision = decide_network_access(
        "api.example.com",
        _safe_context(),
        SandboxPolicy(),
    )

    assert decision.allowed is True


def test_equal_specificity_deny_wins() -> None:
    policy = SandboxPolicy()
    policy.network.allow_domains = ["api.example.com"]
    policy.network.deny_domains = ["api.example.com"]

    decision = decide_network_access("api.example.com", _safe_context(), policy)

    assert decision.allowed is False
    assert decision.code == "policy_domain_denied"


def test_more_specific_allow_beats_broad_deny() -> None:
    policy = SandboxPolicy()
    policy.network.allow_domains = ["api.example.com"]
    policy.network.deny_domains = ["*.example.com"]

    assert (
        decide_network_access("api.example.com", _safe_context(), policy).allowed
        is True
    )


def test_wildcard_does_not_match_apex() -> None:
    policy = SandboxPolicy()
    policy.network.deny_domains = ["*.example.com"]

    assert (
        decide_network_access("example.com", _safe_context(), policy).allowed is True
    )
    assert (
        decide_network_access("www.example.com", _safe_context(), policy).allowed
        is False
    )


def test_block_all_uses_allow_domains_as_exceptions() -> None:
    policy = SandboxPolicy()
    policy.network.block_all_network = True
    policy.network.allow_domains = ["downloads.example.com"]

    assert (
        decide_network_access("downloads.example.com", _safe_context(), policy).allowed
        is True
    )
    assert (
        decide_network_access("other.example.com", _safe_context(), policy).code
        == "policy_block_all"
    )


def test_default_open_never_allows_metadata() -> None:
    decision = decide_network_access(
        "169.254.169.254",
        _safe_context(),
        SandboxPolicy(),
    )

    assert decision.allowed is False
    assert decision.code == "metadata_blocked"


def test_full_bypasses_safe_network_policy() -> None:
    policy = SandboxPolicy()
    policy.network.block_all_network = True

    decision = decide_network_access(
        "169.254.169.254",
        RunContext(run_mode=RunMode.FULL),
        policy,
    )

    assert decision.allowed is True
    assert decision.code == "full_host_access"
