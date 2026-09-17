"""Provider-neutral capability connection compatibility and safety contract."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.llm_runtime import reset_profile_credential_pools
from opensquilla.skills.capability_runtime import (
    CAPABILITY_AUDIO_GENERATE,
    CAPABILITY_IMAGE_GENERATE,
    CAPABILITY_VIDEO_GENERATE,
    META_CAPABILITY_API_KEY_ENV,
    META_CAPABILITY_BASE_URL_ENV,
    META_CAPABILITY_INTERNAL_CREDENTIAL_LEASE_TOKEN,
    META_CAPABILITY_INTERNAL_CREDENTIAL_SOURCE,
    META_CAPABILITY_INTERNAL_PROVIDER,
    META_CAPABILITY_INTERNAL_SESSION_KEY,
    CapabilityProviderCandidate,
    CapabilityRequirement,
    capability_requirements_for_consumers,
    capability_runtime_env_for_consumers,
    lease_capability_connection,
    resolve_capability_status,
    trusted_capability_consumers_for_meta_plan,
)
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.types import SkillLayer

_BUNDLED = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "opensquilla"
    / "skills"
    / "bundled"
)


def _active(
    provider: str = "tokenrhythm",
    *,
    api_key: str = "synthetic-primary-key",
    api_key_env: str = "",
    base_url: str = "https://tokenrhythm.studio/v1",
    proxy: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        provider=provider,
        model="synthetic-model",
        api_key=api_key,
        api_key_env=api_key_env,
        base_url=base_url,
        proxy=proxy,
        provider_routing={},
    )


def _profile(
    *,
    api_key: str = "",
    api_key_env: str = "",
    api_key_env_pool: list[str] | None = None,
    base_url: str = "https://openrouter.ai/api/v1",
    proxy: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        model="bytedance/seedance-2.0",
        api_key=api_key,
        api_key_env=api_key_env,
        api_key_env_pool=api_key_env_pool or [],
        base_url=base_url,
        proxy=proxy,
    )


def _config(
    *,
    llm: SimpleNamespace | None = None,
    profiles: dict[str, SimpleNamespace] | None = None,
    legacy: SimpleNamespace | None = None,
) -> SimpleNamespace:
    image_generation = None
    if legacy is not None:
        image_generation = SimpleNamespace(
            enabled=False,
            providers=SimpleNamespace(openrouter=legacy),
        )
    return SimpleNamespace(
        llm=llm or _active(),
        llm_profiles=profiles or {},
        image_generation=image_generation,
    )


def _requirement():
    return capability_requirements_for_consumers(["nano-banana-pro"])[0]


def _trusted_short_drama(tmp_path: Path):
    loader = SkillLoader(
        bundled_dir=_BUNDLED,
        snapshot_path=tmp_path / "capability-snapshot.json",
    )
    spec = loader.get_by_name("meta-short-drama")
    assert spec is not None
    plan = parse_meta_plan(spec)
    assert plan is not None
    return spec, plan, loader


def _trusted_awesome_webpage(tmp_path: Path):
    loader = SkillLoader(
        bundled_dir=_BUNDLED,
        snapshot_path=tmp_path / "awesome-capability-snapshot.json",
    )
    spec = loader.get_by_name("AwesomeWebpageMetaSkill")
    assert spec is not None
    plan = parse_meta_plan(spec)
    assert plan is not None
    return spec, plan, loader


@pytest.fixture(autouse=True)
def _isolated_credentials(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "OPENROUTER_API_KEY",
        "OPENROUTER_PROFILE_KEY",
        "OPENROUTER_POOL_A",
        "OPENROUTER_POOL_B",
        "LEGACY_MEDIA_KEY",
        "OPENSQUILLA_LLM_PROXY",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_profile_credential_pools()
    yield
    reset_profile_credential_pools()


def test_active_openrouter_connection_keeps_key_endpoint_and_proxy_atomic() -> None:
    secret = "synthetic-active-openrouter-key"
    config = _config(
        llm=_active(
            "openrouter",
            api_key=secret,
            base_url="https://media.example.test/openrouter/v1",
            proxy="http://proxy.example.test:8080",
        )
    )

    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:active",
    )

    assert lease.ready is True
    assert lease.status.connection_source == "active_llm"
    assert lease.api_key == secret
    assert lease.base_url == "https://media.example.test/openrouter/v1"
    assert lease.proxy == "http://proxy.example.test:8080"
    assert secret not in repr(lease)


def test_secondary_profile_works_without_becoming_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_PROFILE_KEY", "synthetic-secondary-key")
    config = _config(
        profiles={
            "OpenRouter": _profile(api_key_env="OPENROUTER_PROFILE_KEY"),
        }
    )

    status = resolve_capability_status(config, _requirement())
    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:secondary",
    )

    assert status.ready is True
    assert status.connection_source == "llm_profile"
    assert status.credential_env == "OPENROUTER_PROFILE_KEY"
    assert lease.api_key == "synthetic-secondary-key"
    assert config.llm.provider == "tokenrhythm"


def test_real_gateway_config_resolves_secondary_profile_without_mutation() -> None:
    config = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "deepseek-v4-pro",
            "api_key": "synthetic-primary-key",
            "base_url": "https://tokenrhythm.studio/v1",
        },
        llm_profiles={
            "openrouter": {
                "model": "bytedance/seedance-2.0",
                "api_key": "synthetic-secondary-key",
                "base_url": "https://openrouter.ai/api/v1",
            }
        },
    )

    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:real-config",
    )

    assert lease.ready is True
    assert lease.status.connection_source == "llm_profile"
    assert lease.api_key == "synthetic-secondary-key"
    assert config.llm.provider == "tokenrhythm"
    assert config.llm.api_key == "synthetic-primary-key"


def test_real_gateway_active_registry_key_does_not_follow_custom_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "synthetic-canonical-openrouter-key"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "",
            "base_url": "https://private-router.example.test/v1",
        }
    )

    status = resolve_capability_status(config, _requirement())
    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:real-config-mismatch",
    )

    assert status.ready is False
    assert status.reason_code == "credential_endpoint_mismatch"
    assert status.connection_source == "active_llm"
    assert status.credential_source == "registry_env"
    assert status.credential_env == "OPENROUTER_API_KEY"
    assert status.endpoint_source == "inherited"
    assert lease.api_key == ""
    assert lease.base_url == ""
    assert lease.proxy == ""
    assert secret not in repr(status)
    assert secret not in repr(lease)


def test_real_gateway_explicit_key_remains_paired_with_custom_endpoint() -> None:
    secret = "synthetic-explicit-custom-endpoint-key"
    endpoint = "https://private-router.example.test/v1"
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": secret,
            "base_url": endpoint,
        }
    )

    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:real-config-explicit-pair",
    )

    assert lease.ready is True
    assert lease.status.connection_source == "active_llm"
    assert lease.api_key == secret
    assert lease.base_url == endpoint
    assert secret not in repr(lease)


def test_profile_pool_readiness_does_not_prevent_session_pinned_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENROUTER_POOL_A", "synthetic-pool-a")
    monkeypatch.setenv("OPENROUTER_POOL_B", "synthetic-pool-b")
    config = _config(
        profiles={
            "openrouter": _profile(
                api_key_env_pool=["OPENROUTER_POOL_A", "OPENROUTER_POOL_B"]
            )
        }
    )

    assert resolve_capability_status(config, _requirement()).ready is True
    first = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:pool",
    )
    second = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:pool",
    )

    assert first.api_key in {"synthetic-pool-a", "synthetic-pool-b"}
    assert second.api_key == first.api_key
    assert first.credential_pool_lease_token
    assert second.credential_pool_lease_token == first.credential_pool_lease_token
    assert first.credential_pool_lease_token not in repr(first)
    assert "synthetic-pool" not in repr(first.status)

    # The real bundled parent-plan gate carries the opaque lease only in the
    # executor's parent-side trusted mapping. Both consumers share one exact
    # provider pin for this run.
    parent_spec, plan, loader = _trusted_short_drama(tmp_path)
    runtime_env = capability_runtime_env_for_consumers(
        config,
        ["nano-banana-pro", "seedance-2-prompt"],
        parent_spec=parent_spec,
        plan=plan,
        session_key="agent:main:pool",
        skill_resolver=loader,
    )
    image_env = runtime_env["nano-banana-pro"]
    video_env = runtime_env["seedance-2-prompt"]
    assert image_env[META_CAPABILITY_INTERNAL_CREDENTIAL_SOURCE] == "profile_pool"
    assert image_env[META_CAPABILITY_INTERNAL_PROVIDER] == "openrouter"
    assert image_env[META_CAPABILITY_INTERNAL_SESSION_KEY] == "agent:main:pool"
    assert (
        image_env[META_CAPABILITY_INTERNAL_CREDENTIAL_LEASE_TOKEN]
        == first.credential_pool_lease_token
    )
    assert (
        video_env[META_CAPABILITY_INTERNAL_CREDENTIAL_LEASE_TOKEN]
        == first.credential_pool_lease_token
    )


def test_custom_profile_endpoint_never_reuses_canonical_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "official-origin-key")
    config = _config(
        profiles={
            "openrouter": _profile(base_url="https://router.example.test/v1"),
        }
    )

    status = resolve_capability_status(config, _requirement())
    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:mismatch",
    )

    assert status.ready is False
    assert status.reason_code == "missing_credential"
    assert lease.api_key == ""
    assert "official-origin-key" not in repr(status)


def test_legacy_media_connection_is_supported_as_an_atomic_tuple() -> None:
    secret = "synthetic-legacy-media-key"
    config = _config(
        legacy=SimpleNamespace(
            api_key=secret,
            api_key_env="OPENROUTER_API_KEY",
            base_url="https://legacy-router.example.test/v1",
        )
    )

    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:legacy",
    )

    assert lease.ready is True
    assert lease.status.connection_source == "legacy_image_generation"
    assert lease.api_key == secret
    assert lease.base_url == "https://legacy-router.example.test/v1"


def test_legacy_custom_endpoint_does_not_borrow_canonical_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "official-origin-key")
    config = _config(
        profiles={
            "openrouter": _profile(base_url="https://profile.example.test/v1"),
        },
        legacy=SimpleNamespace(
            api_key="",
            api_key_env="OPENROUTER_API_KEY",
            base_url="https://legacy.example.test/v1",
        ),
    )

    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:legacy-mismatch",
    )

    assert lease.ready is False
    assert lease.api_key == ""


def test_explicit_legacy_connection_outweighs_bare_canonical_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "canonical-env-key")
    config = _config(
        legacy=SimpleNamespace(
            api_key="legacy-explicit-key",
            api_key_env="OPENROUTER_API_KEY",
            base_url="https://legacy.example.test/v1",
        )
    )

    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:precedence",
    )

    assert lease.api_key == "legacy-explicit-key"
    assert lease.base_url == "https://legacy.example.test/v1"


@pytest.mark.parametrize(
    "base_url",
    (
        "not-a-url",
        "ftp://openrouter.ai/api/v1",
        "https://user:password@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?token=unsafe",
    ),
)
def test_malformed_or_credential_bearing_endpoint_fails_closed(base_url: str) -> None:
    secret = "synthetic-invalid-endpoint-key"
    config = _config(
        llm=_active("openrouter", api_key=secret, base_url=base_url),
    )

    status = resolve_capability_status(config, _requirement())
    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:invalid-endpoint",
    )

    assert status.ready is False
    assert status.reason_code == "invalid_endpoint"
    assert lease.api_key == ""
    assert secret not in repr(status)


@pytest.mark.parametrize(
    "proxy",
    (
        "not-a-url",
        "ftp://proxy.example.test:8080",
        "http://proxy.example.test:99999",
        "https://proxy.example.test:8080/path?token=proxy-secret",
    ),
)
def test_real_gateway_invalid_active_proxy_fails_closed(proxy: str) -> None:
    secret = "synthetic-invalid-proxy-provider-key"
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": secret,
            "base_url": "https://openrouter.ai/api/v1",
            "proxy": proxy,
        }
    )

    status = resolve_capability_status(config, _requirement())
    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:invalid-active-proxy",
    )

    assert status.ready is False
    assert status.reason_code == "invalid_proxy"
    assert lease.api_key == ""
    assert lease.base_url == ""
    assert lease.proxy == ""
    assert secret not in repr(status)
    assert "proxy-secret" not in repr(status)
    assert secret not in repr(lease)
    assert "proxy-secret" not in repr(lease)


def test_real_gateway_authenticated_http_proxy_remains_supported() -> None:
    proxy = "http://synthetic-user:synthetic-password@proxy.example.test:8080"
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "synthetic-authenticated-proxy-key",
            "base_url": "https://openrouter.ai/api/v1",
            "proxy": proxy,
        }
    )

    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:authenticated-proxy",
    )

    assert lease.ready is True
    assert lease.proxy == proxy
    assert "synthetic-password" not in repr(lease)


def test_invalid_active_proxy_is_not_bypassed_by_legacy_registry_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "synthetic-registry-key-with-invalid-proxy"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "",
            "base_url": "https://openrouter.ai/api/v1",
            "proxy": "ftp://proxy.example.test:2121",
        }
    )

    status = resolve_capability_status(config, _requirement())
    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:invalid-proxy-no-legacy-bypass",
    )

    assert status.ready is False
    assert status.reason_code == "invalid_proxy"
    assert status.credential_source == "registry_env"
    assert lease.api_key == ""
    assert lease.base_url == ""
    assert lease.proxy == ""
    assert secret not in repr(status)


def test_real_gateway_invalid_global_proxy_blocks_secondary_profile() -> None:
    secret = "synthetic-secondary-invalid-proxy-key"
    config = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "deepseek-v4-pro",
            "api_key": "synthetic-primary-key",
            "base_url": "https://tokenrhythm.studio/v1",
            "proxy": "socks5://proxy.example.test:1080",
        },
        llm_profiles={
            "openrouter": {
                "model": "bytedance/seedance-2.0",
                "api_key": secret,
                "base_url": "https://openrouter.ai/api/v1",
            }
        },
    )

    status = resolve_capability_status(config, _requirement())
    lease = lease_capability_connection(
        config,
        _requirement(),
        session_key="agent:main:invalid-global-proxy",
    )

    assert status.ready is False
    assert status.reason_code == "invalid_proxy"
    assert status.proxy_source == "global"
    assert lease.api_key == ""
    assert lease.base_url == ""
    assert lease.proxy == ""
    assert secret not in repr(status)


def test_runtime_env_is_consumer_scoped_and_never_repr_exposes_connection(
    tmp_path: Path,
) -> None:
    secret = "synthetic-runtime-secret"
    parent_spec, plan, loader = _trusted_short_drama(tmp_path)
    config = _config(
        llm=_active(
            "openrouter",
            api_key=secret,
            base_url="https://openrouter.ai/api/v1",
        )
    )

    env = capability_runtime_env_for_consumers(
        config,
        ["nano-banana-pro", "paper-latex-sanitizer"],
        parent_spec=parent_spec,
        plan=plan,
        session_key="agent:main:scoped",
        skill_resolver=loader,
    )

    assert set(env) == {"nano-banana-pro"}
    assert env["nano-banana-pro"][META_CAPABILITY_API_KEY_ENV] == secret
    assert (
        env["nano-banana-pro"][META_CAPABILITY_BASE_URL_ENV]
        == "https://openrouter.ai/api/v1"
    )
    assert (
        META_CAPABILITY_INTERNAL_CREDENTIAL_LEASE_TOKEN
        not in env["nano-banana-pro"]
    )
    status = resolve_capability_status(config, _requirement())
    assert secret not in repr(status)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("id", "unreviewed_reference_image"),
        ("skill", "seedance-2-prompt"),
        ("kind", "agent"),
        ("side_effect", ""),
        ("when", "true"),
    ),
)
def test_paid_step_contract_drift_revokes_all_capability_consumers(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    parent_spec, plan, loader = _trusted_short_drama(tmp_path)
    steps = list(plan.steps)
    paid_index = next(
        index for index, step in enumerate(steps) if step.id == "reference_image"
    )
    steps[paid_index] = replace(steps[paid_index], **{field: value})
    drifted = replace(plan, steps=tuple(steps))

    assert (
        trusted_capability_consumers_for_meta_plan(
            parent_spec,
            drifted,
            skill_resolver=loader,
        )
        == ()
    )


def test_short_drama_duration_gate_is_part_of_the_trusted_capability_contract(
    tmp_path: Path,
) -> None:
    parent_spec, plan, loader = _trusted_short_drama(tmp_path)
    assert set(
        trusted_capability_consumers_for_meta_plan(
            parent_spec,
            plan,
            skill_resolver=loader,
        )
    ) == {"nano-banana-pro", "seedance-2-prompt"}

    steps = list(plan.steps)
    paid_index = next(
        index for index, step in enumerate(steps) if step.id == "shot1_video"
    )
    paid_step = steps[paid_index]
    assert "short_drama_duration_contract_valid" in paid_step.when
    steps[paid_index] = replace(
        paid_step,
        when=paid_step.when.replace(
            " and (outputs.final_script | short_drama_duration_contract_valid)",
            "",
        ),
    )

    assert trusted_capability_consumers_for_meta_plan(
        parent_spec,
        replace(plan, steps=tuple(steps)),
        skill_resolver=loader,
    ) == ()


def test_non_paid_review_step_drift_revokes_all_capability_consumers(
    tmp_path: Path,
) -> None:
    parent_spec, plan, loader = _trusted_short_drama(tmp_path)
    steps = list(plan.steps)
    review_index = next(
        index for index, step in enumerate(steps) if step.id == "review_normalize"
    )
    steps[review_index] = replace(
        steps[review_index],
        skill="short-drama-delivery-audit",
    )
    drifted = replace(plan, steps=tuple(steps))

    # The paid steps and their `when` expressions are untouched.  A subset-
    # only validator would still grant a key even though the producer of the
    # consent signal is no longer the current reviewed step.
    assert (
        trusted_capability_consumers_for_meta_plan(
            parent_spec,
            drifted,
            skill_resolver=loader,
        )
        == ()
    )


def test_workspace_shadow_of_review_normalizer_revokes_provider_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_spec, plan, loader = _trusted_short_drama(tmp_path)
    resolved = {spec.name: spec for spec in loader.load_all()}
    bundled_normalizer = resolved["short-drama-review-normalizer"]
    resolved["short-drama-review-normalizer"] = replace(
        bundled_normalizer,
        layer=SkillLayer.WORKSPACE,
        base_dir=str(tmp_path / "shadowed-review-normalizer"),
    )
    lease_calls = 0

    def forbidden_lease(*_args, **_kwargs):
        nonlocal lease_calls
        lease_calls += 1
        raise AssertionError("a shadowed consent producer must not acquire a lease")

    monkeypatch.setattr(
        "opensquilla.skills.capability_runtime.lease_capability_connection",
        forbidden_lease,
    )

    env = capability_runtime_env_for_consumers(
        _config(llm=_active("openrouter")),
        ["nano-banana-pro", "seedance-2-prompt"],
        parent_spec=parent_spec,
        plan=plan,
        session_key="agent:main:shadowed-review",
        skill_resolver=resolved,
    )

    assert env == {}
    assert lease_calls == 0


@pytest.mark.parametrize(
    "layer",
    (SkillLayer.WORKSPACE, SkillLayer.PROJECT, SkillLayer.PERSONAL),
)
def test_non_bundled_parent_cannot_lease_genuine_bundled_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layer: SkillLayer,
) -> None:
    parent_spec, plan, loader = _trusted_short_drama(tmp_path)
    untrusted_parent = replace(parent_spec, layer=layer)
    lease_calls = 0

    def forbidden_lease(*_args, **_kwargs):
        nonlocal lease_calls
        lease_calls += 1
        raise AssertionError("untrusted parent must not acquire a provider lease")

    monkeypatch.setattr(
        "opensquilla.skills.capability_runtime.lease_capability_connection",
        forbidden_lease,
    )

    env = capability_runtime_env_for_consumers(
        _config(llm=_active("openrouter")),
        ["nano-banana-pro", "seedance-2-prompt"],
        parent_spec=untrusted_parent,
        plan=plan,
        session_key="agent:main:untrusted-parent",
        skill_resolver=loader,
    )

    assert env == {}
    assert lease_calls == 0


def test_awesome_webpage_capabilities_use_ordered_provider_candidates() -> None:
    requirements = capability_requirements_for_consumers(
        [
            "nano-banana-pro-openrouter",
            "audio-cog",
            "openrouter-video-generator",
        ]
    )

    assert {requirement.capability_id for requirement in requirements} == {
        CAPABILITY_AUDIO_GENERATE,
        CAPABILITY_IMAGE_GENERATE,
        CAPABILITY_VIDEO_GENERATE,
    }
    for requirement in requirements:
        assert [candidate.provider_id for candidate in requirement.provider_candidates] == [
            "openrouter"
        ]
        assert requirement.provider_candidates[0].profile_preference == (
            "active_then_provider_profile"
        )


def test_capability_resolution_uses_next_ready_ordered_candidate() -> None:
    requirement = CapabilityRequirement(
        capability_id="synthetic.ordered",
        consumer="synthetic-consumer",
        provider_candidates=(
            CapabilityProviderCandidate(
                provider_id="not-a-provider",
                model="synthetic-model",
            ),
            CapabilityProviderCandidate(
                provider_id="openrouter",
                model="synthetic-openrouter-model",
            ),
        ),
    )
    config = _config(
        profiles={"openrouter": _profile(api_key="synthetic-secondary-key")}
    )

    lease = lease_capability_connection(
        config,
        requirement,
        session_key="agent:main:ordered-candidate",
    )

    assert lease.ready is True
    assert lease.status.provider_id == "openrouter"
    assert lease.api_key == "synthetic-secondary-key"


def test_trusted_awesome_webpage_leases_only_exact_paid_media_children(
    tmp_path: Path,
) -> None:
    secret = "synthetic-awesome-runtime-secret"
    parent_spec, plan, loader = _trusted_awesome_webpage(tmp_path)
    config = _config(
        llm=_active(
            "openrouter",
            api_key=secret,
            base_url="https://openrouter.ai/api/v1",
        )
    )

    consumers = trusted_capability_consumers_for_meta_plan(
        parent_spec,
        plan,
        skill_resolver=loader,
    )
    env = capability_runtime_env_for_consumers(
        config,
        [*consumers, "awesome-webpage-image-download", "filesystem"],
        parent_spec=parent_spec,
        plan=plan,
        session_key="agent:main:awesome-runtime",
        skill_resolver=loader,
    )

    assert consumers == (
        "audio-cog",
        "nano-banana-pro-openrouter",
        "openrouter-video-generator",
    )
    assert set(env) == set(consumers)
    for values in env.values():
        assert values[META_CAPABILITY_API_KEY_ENV] == secret
        assert values[META_CAPABILITY_BASE_URL_ENV] == "https://openrouter.ai/api/v1"
    assert secret not in repr(
        capability_requirements_for_consumers(consumers)
    )


def test_awesome_approval_or_paid_step_drift_revokes_all_provider_leases(
    tmp_path: Path,
) -> None:
    parent_spec, plan, loader = _trusted_awesome_webpage(tmp_path)
    for step_id, changes in (
        ("media_provider_approval", {"depends_on": ("page_outline",)}),
        ("image_aigc", {"when": "true"}),
        ("audio_aigc", {"side_effect": ""}),
        ("video_aigc", {"skill": "audio-cog"}),
    ):
        steps = list(plan.steps)
        index = next(i for i, step in enumerate(steps) if step.id == step_id)
        steps[index] = replace(steps[index], **changes)
        drifted = replace(plan, steps=tuple(steps))

        assert trusted_capability_consumers_for_meta_plan(
            parent_spec,
            drifted,
            skill_resolver=loader,
        ) == ()


def test_awesome_workspace_shadow_of_paid_child_revokes_provider_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_spec, plan, loader = _trusted_awesome_webpage(tmp_path)
    resolved = {spec.name: spec for spec in loader.load_all()}
    resolved["audio-cog"] = replace(
        resolved["audio-cog"],
        layer=SkillLayer.WORKSPACE,
        base_dir=str(tmp_path / "shadowed-audio-cog"),
    )
    lease_calls = 0

    def forbidden_lease(*_args, **_kwargs):
        nonlocal lease_calls
        lease_calls += 1
        raise AssertionError("a shadowed child must not acquire a provider lease")

    monkeypatch.setattr(
        "opensquilla.skills.capability_runtime.lease_capability_connection",
        forbidden_lease,
    )

    env = capability_runtime_env_for_consumers(
        _config(llm=_active("openrouter")),
        ["audio-cog", "nano-banana-pro-openrouter", "openrouter-video-generator"],
        parent_spec=parent_spec,
        plan=plan,
        session_key="agent:main:shadowed-awesome",
        skill_resolver=resolved,
    )

    assert env == {}
    assert lease_calls == 0
