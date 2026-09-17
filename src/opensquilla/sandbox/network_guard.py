"""Network allowlist decisions for sandboxed tool traffic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from opensquilla.sandbox.default_allowlist import default_allowlist_source
from opensquilla.sandbox.domain_validation import (
    domain_matches,
    policy_domain_matches,
    validate_domain_pattern,
    validate_policy_domain_pattern,
)
from opensquilla.sandbox.package_bundles import (
    DEFAULT_PACKAGE_BUNDLE_IDS,
    expand_package_bundle,
)
from opensquilla.sandbox.policy_models import SandboxPolicy
from opensquilla.sandbox.run_context import PublicNetworkGrant, RunContext
from opensquilla.sandbox.run_mode import RunMode

NetworkDecisionStatus = Literal["allow", "ask", "block"]


@dataclass(frozen=True)
class NetworkDecision:
    status: NetworkDecisionStatus
    normalized_host: str
    reason: str
    source: str | None

    @property
    def allowed(self) -> bool:
        return self.status == "allow"

    @property
    def code(self) -> str:
        return self.reason


def _metadata_or_validation_reason(normalized: str, reason: str) -> str:
    metadata_hosts = {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.azure.internal",
    }
    return "metadata_blocked" if normalized in metadata_hosts else reason


def _rule_specificity(pattern: str) -> tuple[int, int, int]:
    decision = validate_policy_domain_pattern(pattern)
    if decision.status != "allowed":
        return (-1, -1, -1)
    normalized = decision.normalized
    exact = int(not normalized.startswith("*."))
    suffix = normalized[2:] if not exact else normalized
    return (exact, suffix.count(".") + 1, len(suffix))


def _best_policy_rule(host: str, rules: list[str]) -> tuple[str, tuple[int, int, int]] | None:
    matches = [
        (rule, _rule_specificity(rule))
        for rule in rules
        if policy_domain_matches(rule, host)
    ]
    return max(matches, key=lambda item: item[1], default=None)


def _policy_network_decision(
    host: str,
    policy: SandboxPolicy,
) -> NetworkDecision | None:
    allow = _best_policy_rule(host, policy.network.allow_domains)
    deny = _best_policy_rule(host, policy.network.deny_domains)
    if allow is not None or deny is not None:
        if deny is not None and (allow is None or deny[1] >= allow[1]):
            return NetworkDecision(
                status="block",
                normalized_host=host,
                reason="policy_domain_denied",
                source=f"deny:{deny[0]}",
            )
        if allow is not None:
            return NetworkDecision(
                status="allow",
                normalized_host=host,
                reason="policy_domain_allowed",
                source=f"allow:{allow[0]}",
            )
    if policy.network.block_all_network:
        return NetworkDecision(
            status="block",
            normalized_host=host,
            reason="policy_block_all",
            source="policy:block_all",
        )
    return None


def decide_network_access(
    host: str,
    context: RunContext,
    policy: SandboxPolicy | None = None,
) -> NetworkDecision:
    if context.run_mode == RunMode.FULL:
        return NetworkDecision(
            status="allow",
            normalized_host=str(host),
            reason="full_host_access",
            source="run_mode:full",
        )
    validation = validate_domain_pattern(host)
    if validation.status == "blocked":
        return NetworkDecision(
            status="block",
            normalized_host=validation.normalized,
            reason=_metadata_or_validation_reason(
                validation.normalized,
                validation.reason,
            ),
            source="validation",
        )

    normalized_host = validation.normalized
    if "*" in normalized_host:
        return NetworkDecision(
            status="block",
            normalized_host=normalized_host,
            reason="invalid_domain",
            source="validation",
        )

    stored_policy = policy or SandboxPolicy()
    policy_decision = _policy_network_decision(normalized_host, stored_policy)
    if policy_decision is not None:
        return policy_decision

    disabled_bundle_ids = {
        grant.bundle_id for grant in context.bundles if grant.source == "disabled"
    }
    for domain_grant in context.domains:
        if domain_matches(domain_grant.domain, normalized_host):
            grant_validation = validate_domain_pattern(domain_grant.domain)
            reason = (
                "system_domain_grant"
                if domain_grant.source == "system"
                else "domain_grant"
            )
            source_prefix = "system" if domain_grant.source == "system" else "domain"
            return NetworkDecision(
                status="allow",
                normalized_host=normalized_host,
                reason=reason,
                source=f"{source_prefix}:{grant_validation.normalized}",
            )

    for bundle_grant in context.bundles:
        if bundle_grant.source == "disabled":
            continue
        for bundled_domain in expand_package_bundle(bundle_grant.bundle_id):
            if domain_matches(bundled_domain, normalized_host):
                return NetworkDecision(
                    status="allow",
                    normalized_host=normalized_host,
                    reason="package_bundle",
                    source=f"bundle:{bundle_grant.bundle_id}",
                )

    default_source = default_allowlist_source(normalized_host)
    if default_source is not None:
        return NetworkDecision(
            status="allow",
            normalized_host=normalized_host,
            reason="default_allowlist",
            source=default_source,
        )

    for bundle_id in DEFAULT_PACKAGE_BUNDLE_IDS:
        if bundle_id in disabled_bundle_ids:
            continue
        for bundled_domain in expand_package_bundle(bundle_id):
            if domain_matches(bundled_domain, normalized_host):
                return NetworkDecision(
                    status="allow",
                    normalized_host=normalized_host,
                    reason="package_bundle",
                    source=f"bundle:{bundle_id}",
                )

    public_network_grant = _public_network_grant(context)
    if public_network_grant is not None:
        scope = "user" if public_network_grant.scope == "workspace" else "chat"
        return NetworkDecision(
            status="allow",
            normalized_host=normalized_host,
            reason="public_network",
            source=f"public_network:{scope}",
        )

    if context.run_mode == RunMode.SAFE:
        return NetworkDecision(
            status="allow",
            normalized_host=normalized_host,
            reason="public_default",
            source="policy:default_open",
        )

    return NetworkDecision(
        status="ask",
        normalized_host=normalized_host,
        reason="unknown_domain",
        source=None,
    )


def _is_recognized_default_host(
    normalized_host: str,
    *,
    disabled_bundle_ids: set[str],
) -> bool:
    if default_allowlist_source(normalized_host) is not None:
        return True
    return any(
        domain_matches(bundled_domain, normalized_host)
        for bundle_id in DEFAULT_PACKAGE_BUNDLE_IDS
        if bundle_id not in disabled_bundle_ids
        for bundled_domain in expand_package_bundle(bundle_id)
    )


def _public_network_grant(context: RunContext) -> PublicNetworkGrant | None:
    for scope in ("chat", "workspace"):
        for grant in context.public_network:
            if grant.scope == scope:
                return grant
    return None


__all__ = ["NetworkDecision", "NetworkDecisionStatus", "decide_network_access"]
