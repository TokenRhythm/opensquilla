from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.sandbox.default_allowlist import default_allowlist_payload
from opensquilla.sandbox.domain_validation import domain_matches
from opensquilla.sandbox.network_guard import NetworkDecision, decide_network_access
from opensquilla.sandbox.package_bundles import expand_package_bundle
from opensquilla.sandbox.run_context import (
    DomainGrant,
    PackageBundleGrant,
    PublicNetworkGrant,
    RunContext,
)
from opensquilla.sandbox.run_mode import RunMode


class _SessionManager:
    def __init__(self) -> None:
        self.node = SimpleNamespace(
            session_key="agent:main:webchat:abc",
            agent_id="main",
            origin=None,
        )
        self.sessions = {self.node.session_key: self.node}

    async def get_session(self, session_key: str):
        return self.sessions.get(session_key)

    async def update(self, session_key: str, **fields):
        node = self.sessions[session_key]
        for key, value in fields.items():
            setattr(node, key, value)
        return node


def _context(
    *,
    run_mode: RunMode = RunMode.SAFE,
    domains: tuple[DomainGrant, ...] = (),
    bundles: tuple[PackageBundleGrant, ...] = (),
    public_network: tuple[PublicNetworkGrant, ...] = (),
) -> RunContext:
    return RunContext(
        run_mode=run_mode,
        domains=domains,
        bundles=bundles,
        public_network=public_network,
    )


def test_decide_network_access_allows_explicit_domain_grant() -> None:
    context = _context(domains=(DomainGrant("pypi.org", source="user"),))

    decision = decide_network_access("HTTPS://PyPI.org/simple", context)

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="pypi.org",
        reason="domain_grant",
        source="domain:pypi.org",
    )


def test_decide_network_access_allows_wildcard_domain_grant() -> None:
    context = _context(domains=(DomainGrant("*.pythonhosted.org"),))

    decision = decide_network_access("files.pythonhosted.org", context)

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="files.pythonhosted.org",
        reason="domain_grant",
        source="domain:*.pythonhosted.org",
    )


def test_decide_network_access_allows_unknown_valid_public_domain() -> None:
    decision = decide_network_access("example.com", _context())

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="example.com",
        reason="public_default",
        source="policy:default_open",
    )


def test_safe_sandbox_unknown_public_host_is_open_by_default() -> None:
    decision = decide_network_access(
        "https://new-public-example.invalid",
        _context(run_mode=RunMode.SAFE),
    )

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="new-public-example.invalid",
        reason="public_default",
        source="policy:default_open",
    )


def test_safe_sandbox_uses_public_default_for_unknown_domain() -> None:
    decision = decide_network_access("docs.example.com", _context(run_mode=RunMode.SAFE))

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="docs.example.com",
        reason="public_default",
        source="policy:default_open",
    )


def test_safe_sandbox_url_uses_public_default() -> None:
    decision = decide_network_access(
        "https://new-public-example.invalid",
        _context(run_mode=RunMode.SAFE),
    )

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="new-public-example.invalid",
        reason="public_default",
        source="policy:default_open",
    )


def test_standard_public_network_grant_allows_unknown_public_domain() -> None:
    context = _context(public_network=(PublicNetworkGrant(scope="chat"),))

    decision = decide_network_access("docs.example.com", context)

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="docs.example.com",
        reason="public_network",
        source="public_network:chat",
    )


def test_workspace_public_network_grant_reports_user_source() -> None:
    context = _context(public_network=(PublicNetworkGrant(scope="workspace"),))

    decision = decide_network_access("docs.example.com", context)

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="docs.example.com",
        reason="public_network",
        source="public_network:user",
    )


def test_standard_public_network_grant_does_not_override_validation_block() -> None:
    context = _context(public_network=(PublicNetworkGrant(scope="chat"),))

    for host, expected in (
        (
            "127.0.0.1",
            NetworkDecision(
                status="block",
                normalized_host="127.0.0.1",
                reason="ip_literal",
                source="validation",
            ),
        ),
        (
            "localhost",
            NetworkDecision(
                status="block",
                normalized_host="localhost",
                reason="not_fqdn",
                source="validation",
            ),
        ),
        (
            "*.com",
            NetworkDecision(
                status="block",
                normalized_host="*.com",
                reason="broad_wildcard",
                source="validation",
            ),
        ),
    ):
        decision = decide_network_access(host, context)

        assert decision == expected


def test_default_open_domain_does_not_build_network_escalation() -> None:
    from opensquilla.sandbox.escalation import build_network_approval_params

    decision = decide_network_access("example.com", _context())

    proposal = build_network_approval_params(
        decision,
        session_key="agent:main:webchat:abc",
        workspace="/tmp/ws",
        fingerprint="fp123",
    )

    assert proposal is None


def test_blocked_network_decision_has_no_escalation_choices() -> None:
    from opensquilla.sandbox.escalation import build_network_approval_params

    decision = decide_network_access("127.0.0.1", _context())

    assert decision.status == "block"
    assert build_network_approval_params(
        decision,
        session_key="agent:main:webchat:abc",
        workspace="/tmp/ws",
        fingerprint="fp123",
    ) is None


def test_standard_sandbox_allows_builtin_github_default_hosts() -> None:
    expected_hosts = {
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "codeload.github.com",
        "objects.githubusercontent.com",
        "github.githubassets.com",
        "avatars.githubusercontent.com",
        "uploads.github.com",
        "release-assets.githubusercontent.com",
        "ghcr.io",
        "pkg-containers.githubusercontent.com",
    }

    for host in expected_hosts:
        decision = decide_network_access(host, _context())

        assert decision.status == "allow", (host, decision)
        assert decision.reason == "default_allowlist"
        assert decision.source == "default:github"


def test_standard_sandbox_allows_builtin_search_provider_hosts_without_custom_domains() -> None:
    for host in (
        "api.search.brave.com",
        "html.duckduckgo.com",
        "duckduckgo.com",
        "www.google.com",
        "www.bing.com",
    ):
        decision = decide_network_access(host, _context())

        assert decision == NetworkDecision(
            status="allow",
            normalized_host=host,
            reason="default_allowlist",
            source="default:search",
        )


def test_standard_sandbox_allows_developer_doc_hosts_without_custom_domains() -> None:
    for host in (
        "developer.mozilla.org",
        "docs.python.org",
        "docs.npmjs.com",
        "doc.rust-lang.org",
        "go.dev",
    ):
        decision = decide_network_access(host, _context())

        assert decision == NetworkDecision(
            status="allow",
            normalized_host=host,
            reason="default_allowlist",
            source="default:developer-docs",
        )


def test_standard_sandbox_allows_common_package_and_doc_hosts_by_default() -> None:
    for host in (
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "crates.io",
        "proxy.golang.org",
        "developer.mozilla.org",
        "docs.python.org",
    ):
        decision = decide_network_access(host, _context())

        assert decision.status == "allow", (host, decision)


def test_default_allowlist_payload_exposes_read_only_builtin_groups() -> None:
    payload = {entry["group"]: entry for entry in default_allowlist_payload()}

    assert {"github", "search", "developer-docs"}.issubset(payload)
    for group in ("github", "search", "developer-docs"):
        assert payload[group]["read_only"] is True

    assert "pkg-containers.githubusercontent.com" in payload["github"]["domains"]
    assert "www.google.com" in payload["search"]["domains"]
    assert "developer.mozilla.org" in payload["developer-docs"]["domains"]


def test_github_exact_domain_grant_does_not_cover_github_workflow_hosts() -> None:
    for host in (
        "api.github.com",
        "raw.githubusercontent.com",
        "codeload.github.com",
        "objects.githubusercontent.com",
    ):
        assert not domain_matches("github.com", host), host


def test_full_host_access_bypasses_sandbox_domain_controls() -> None:
    decision = decide_network_access(
        "unknown-public.example",
        _context(run_mode=RunMode.FULL),
    )

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="unknown-public.example",
        reason="full_host_access",
        source="run_mode:full",
    )


def test_safe_sandbox_keeps_sources_for_recognized_hosts() -> None:
    context = _context(run_mode=RunMode.SAFE)

    for host in ("api.github.com", "registry.npmjs.org"):
        decision = decide_network_access(host, context)

        assert decision.status == "allow"
        assert decision.normalized_host == host
        assert decision.reason in {"default_allowlist", "package_bundle"}


@pytest.mark.asyncio
async def test_trusted_sandbox_auto_trust_persists_chat_scoped_grant() -> None:
    from opensquilla.sandbox.run_context import get_run_context
    from opensquilla.sandbox.run_context_service import auto_add_trusted_domain_grant

    manager = _SessionManager()
    config = SimpleNamespace(
        sandbox=SimpleNamespace(run_mode="trusted"),
        permissions=SimpleNamespace(default_mode="off"),
    )

    context = await auto_add_trusted_domain_grant(
        manager,
        manager.node.session_key,
        domain="api.github.com",
        config=config,
        workspace="/tmp/ws",
    )
    saved = await get_run_context(
        manager,
        manager.node.session_key,
        config=config,
        workspace="/tmp/ws",
    )

    expected_grant = DomainGrant(
        domain="api.github.com",
        scope="chat",
        source="auto_trusted",
    )
    assert expected_grant in context.domains
    assert expected_grant in saved.domains
    assert manager.node.origin is not None


@pytest.mark.asyncio
async def test_trusted_sandbox_auto_trust_idempotent_second_call_does_not_mutate() -> None:
    from opensquilla.sandbox.run_context import get_run_context
    from opensquilla.sandbox.run_context_service import auto_add_trusted_domain_grant

    manager = _SessionManager()
    config = SimpleNamespace(
        sandbox=SimpleNamespace(run_mode="trusted"),
        permissions=SimpleNamespace(default_mode="off"),
    )
    manager.node.origin = {
        "sandbox_run_context": {
            "run_mode": "trusted",
            "workspace": "/tmp/ws",
            "domains": [{"domain": "registry.npmjs.org", "source": "manual"}],
        }
    }

    first = await auto_add_trusted_domain_grant(
        manager,
        manager.node.session_key,
        domain="HTTPS://Api.GitHub.com",
        config=config,
        workspace="/tmp/ws",
    )
    saved_after_first = await get_run_context(
        manager,
        manager.node.session_key,
        config=config,
        workspace="/tmp/ws",
    )
    origin_after_first = manager.node.origin

    second = await auto_add_trusted_domain_grant(
        manager,
        manager.node.session_key,
        domain="api.github.com",
        config=config,
        workspace="/tmp/ws",
    )
    saved_after_second = await get_run_context(
        manager,
        manager.node.session_key,
        config=config,
        workspace="/tmp/ws",
    )

    assert (
        first.domains
        == saved_after_first.domains
        == saved_after_second.domains
        == second.domains
    )
    assert [grant.domain for grant in saved_after_second.domains] == [
        "registry.npmjs.org",
        "api.github.com",
    ]
    assert manager.node.origin is origin_after_first


@pytest.mark.asyncio
async def test_trusted_sandbox_auto_trust_does_not_persist_unsafe_hosts() -> None:
    from opensquilla.sandbox.run_context import get_run_context
    from opensquilla.sandbox.run_context_service import auto_add_trusted_domain_grant

    config = SimpleNamespace(
        sandbox=SimpleNamespace(run_mode="trusted"),
        permissions=SimpleNamespace(default_mode="off"),
    )

    for host in (
        "10.0.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "*.com",
    ):
        manager = _SessionManager()

        try:
            result = await auto_add_trusted_domain_grant(
                manager,
                manager.node.session_key,
                domain=host,
                config=config,
                workspace="/tmp/ws",
            )
        except ValueError:
            result = None

        saved = await get_run_context(
            manager,
            manager.node.session_key,
            config=config,
            workspace="/tmp/ws",
        )

        assert manager.node.origin is None, host
        assert all(grant.source != "auto_trusted" for grant in saved.domains), host
        if result is not None:
            assert getattr(result, "status", "block") in {"ask", "block"}, (host, result)


def test_trusted_sandbox_does_not_auto_trust_unsafe_hosts() -> None:
    context = _context(run_mode=RunMode.SAFE)

    for host in (
        "127.0.0.1",
        "10.0.0.1",
        "192.0.2.1",
        "localhost",
        "169.254.169.254",
        "*.example.com",
        "*.com",
    ):
        decision = decide_network_access(host, context)
        assert decision.status != "allow", (host, decision)
        assert decision.reason != "auto_trusted", (host, decision)


def test_decide_network_access_blocks_unsafe_ip_literal() -> None:
    decision = decide_network_access("169.254.169.254", _context())

    assert decision == NetworkDecision(
        status="block",
        normalized_host="169.254.169.254",
        reason="metadata_blocked",
        source="validation",
    )


def test_decide_network_access_blocks_malformed_host_with_validation_reason() -> None:
    decision = decide_network_access("example.com:99999", _context())

    assert decision == NetworkDecision(
        status="block",
        normalized_host="example.com",
        reason="invalid_port",
        source="validation",
    )


def test_decide_network_access_allows_package_bundle_domain() -> None:
    context = _context(bundles=(PackageBundleGrant("node-package-install"),))

    decision = decide_network_access("registry.npmjs.org", context)

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="registry.npmjs.org",
        reason="package_bundle",
        source="bundle:node-package-install",
    )


def test_decide_network_access_allows_default_bundle_domain_without_explicit_grant() -> None:
    decision = decide_network_access("registry.npmjs.org", _context())

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="registry.npmjs.org",
        reason="package_bundle",
        source="bundle:node-package-install",
    )


def test_disabled_default_bundle_falls_back_to_public_default() -> None:
    context = _context(
        bundles=(
            PackageBundleGrant(
                bundle_id="node-package-install",
                source="disabled",
            ),
        ),
    )

    decision = decide_network_access("registry.npmjs.org", context)

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="registry.npmjs.org",
        reason="public_default",
        source="policy:default_open",
    )


def test_package_install_bundles_cover_common_registry_and_artifact_hosts() -> None:
    expectations = {
        "python-package-install": {
            "pypi.org",
            "files.pythonhosted.org",
            "pypi.python.org",
            "bootstrap.pypa.io",
        },
        "node-package-install": {
            "registry.npmjs.org",
            "registry.yarnpkg.com",
            "yarnpkg.com",
            "nodejs.org",
        },
        "rust-package-install": {
            "crates.io",
            "static.crates.io",
            "index.crates.io",
            "github.com",
            "objects.githubusercontent.com",
        },
        "go-package-install": {
            "proxy.golang.org",
            "sum.golang.org",
            "go.dev",
            "golang.org",
            "storage.googleapis.com",
        },
    }

    for bundle_id, hosts in expectations.items():
        context = _context(bundles=(PackageBundleGrant(bundle_id),))
        for host in hosts:
            decision = decide_network_access(host, context)
            assert decision.status == "allow", (bundle_id, host, decision)
            assert decision.reason == "package_bundle"


def test_default_github_bundle_covers_full_git_and_release_workflow_hosts() -> None:
    expected_hosts = {
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "codeload.github.com",
        "objects.githubusercontent.com",
    }

    assert expected_hosts.issubset(set(expand_package_bundle("github-default")))


def test_default_github_bundle_covers_developer_workflow_hosts() -> None:
    expected_hosts = {
        "actions.githubusercontent.com",
        "pipelines.actions.githubusercontent.com",
        "results-receiver.actions.githubusercontent.com",
        "uploads.github.com",
        "release-assets.githubusercontent.com",
        "ghcr.io",
        "pkg-containers.githubusercontent.com",
    }

    assert expected_hosts.issubset(set(expand_package_bundle("github-default")))


def test_default_github_bundle_allows_workflow_host() -> None:
    decision = decide_network_access("actions.githubusercontent.com", _context())

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="actions.githubusercontent.com",
        reason="package_bundle",
        source="bundle:github-default",
    )


def test_disabled_default_github_bundle_falls_back_to_public_default() -> None:
    context = _context(
        bundles=(PackageBundleGrant(bundle_id="github-default", source="disabled"),),
    )

    decision = decide_network_access("actions.githubusercontent.com", context)

    assert decision == NetworkDecision(
        status="allow",
        normalized_host="actions.githubusercontent.com",
        reason="public_default",
        source="policy:default_open",
    )
