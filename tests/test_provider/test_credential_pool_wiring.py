"""Wiring of ``CredentialPool`` into profile credential resolution.

``[llm_profiles.<id>].api_key_env_pool`` holds env-var NAMES only (never key
values); the runtime rotates over the resolved keys with per-session pinning,
429 cooldown, long parking on insufficient credits, and process-permanent
parking on invalid auth. A profile without a pool must take exactly the
pre-pool single-key path.

All env vars and key values here are synthetic test dummies.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from opensquilla.engine.runtime import _report_credential_pool_failure
from opensquilla.engine.selector_override import resolve_tier_provider_config
from opensquilla.gateway.config import (
    GatewayConfig,
    LlmProviderConfig,
    LlmProviderProfile,
    is_sensitive_config_key,
)
from opensquilla.gateway.llm_runtime import (
    INSUFFICIENT_CREDITS_COOLDOWN_SECONDS,
    RATE_LIMITED_COOLDOWN_SECONDS,
    ProfileCredentialPools,
    masked_key_id,
    profile_credential_pools,
    reset_profile_credential_pools,
)
from opensquilla.provider.credentials import Credential, NoCredentialsAvailable
from opensquilla.provider.failures import ProviderFailureKind
from opensquilla.provider.types import ErrorEvent

_SECRET_A = "sk-test-000-aaaaaaaaaaaaaaaa"
_SECRET_B = "sk-test-000-bbbbbbbbbbbbbbbb"
_ENV_A = "OPENSQUILLA_TEST_POOL_KEY_A"
_ENV_B = "OPENSQUILLA_TEST_POOL_KEY_B"
_ENV_UNSET = "OPENSQUILLA_TEST_POOL_KEY_UNSET"


@pytest.fixture(autouse=True)
def _fresh_pools():
    reset_profile_credential_pools()
    yield
    reset_profile_credential_pools()


@pytest.fixture()
def pool_env(monkeypatch):
    monkeypatch.setenv(_ENV_A, _SECRET_A)
    monkeypatch.setenv(_ENV_B, _SECRET_B)
    monkeypatch.delenv(_ENV_UNSET, raising=False)


def _config(profile: LlmProviderProfile) -> GatewayConfig:
    cfg = GatewayConfig()
    cfg.squilla_router.cross_provider_tiers = True
    cfg.llm_profiles = {"openai": profile}
    return cfg


def _resolve(cfg: GatewayConfig, session_key: str, metadata: dict | None = None):
    return resolve_tier_provider_config(
        cfg,
        "openai",
        "gpt-5.4-nano",
        session_key=session_key,
        turn_metadata=metadata,
    )


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _LogRecorder:
    """Deterministic structlog stand-in.

    Global structlog config is shared test state (other suites install
    level-filtering wrapper classes that drop info/debug before
    ``capture_logs`` processors run), so telemetry assertions patch the
    module loggers with this recorder instead.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def _record(self, level: str, event: str, **kw) -> None:
        self.events.append({"event": event, "log_level": level, **kw})

    def debug(self, event: str, **kw) -> None:
        self._record("debug", event, **kw)

    def info(self, event: str, **kw) -> None:
        self._record("info", event, **kw)

    def warning(self, event: str, **kw) -> None:
        self._record("warning", event, **kw)


@pytest.fixture()
def log_recorder(monkeypatch):
    recorder = _LogRecorder()
    monkeypatch.setattr("opensquilla.gateway.llm_runtime.log", recorder)
    monkeypatch.setattr("opensquilla.engine.selector_override.log", recorder)
    return recorder


# ---------------------------------------------------------------------------
# Credential repr must never leak the secret
# ---------------------------------------------------------------------------


def test_credential_repr_omits_secret() -> None:
    cred = Credential(cred_id=_ENV_A, secret=_SECRET_A)
    rendered = repr(cred)
    assert _SECRET_A not in rendered
    assert _SECRET_A not in str(cred)
    assert _ENV_A in rendered  # the non-secret id stays identifiable
    assert f"cred={cred}" == f"cred={rendered}"  # f-string interpolation is safe


def test_pooled_credential_repr_omits_secret(pool_env) -> None:
    pooled = profile_credential_pools().acquire_for_session("openai", [_ENV_A], "s")
    assert pooled is not None
    assert _SECRET_A not in repr(pooled)
    assert pooled.lease_token
    assert pooled.lease_token not in repr(pooled)
    assert pooled.env_name == _ENV_A
    assert pooled.key_id == masked_key_id(_SECRET_A)
    assert _SECRET_A not in pooled.key_id


# ---------------------------------------------------------------------------
# Empty pool == today's single-key path
# ---------------------------------------------------------------------------


def test_empty_pool_matches_single_key_resolution(monkeypatch) -> None:
    monkeypatch.setenv(_ENV_A, _SECRET_A)

    def _explode(*args, **kwargs):  # pragma: no cover - failure marker
        raise AssertionError("pool manager must not be consulted without a pool")

    monkeypatch.setattr(
        "opensquilla.gateway.llm_runtime.profile_credential_pools",
        _explode,
    )
    cfg = _config(LlmProviderProfile(api_key_env=_ENV_A))
    resolved = _resolve(cfg, "s1")
    legacy = resolve_tier_provider_config(cfg, "openai", "gpt-5.4-nano")
    assert resolved is not None and legacy is not None
    assert resolved == legacy  # same dataclass, field-for-field
    assert resolved.api_key == _SECRET_A
    assert resolved.provider == "openai"
    assert resolved.base_url == "https://api.openai.com/v1"
    assert resolved.replay_provider_state is False


def test_explicit_api_key_beats_pool(pool_env) -> None:
    cfg = _config(
        LlmProviderProfile(api_key="sk-test-000-explicit", api_key_env_pool=[_ENV_A, _ENV_B])
    )
    metadata: dict = {}
    resolved = _resolve(cfg, "s1", metadata)
    assert resolved is not None
    assert resolved.api_key == "sk-test-000-explicit"
    assert "credential_pool" not in metadata


# ---------------------------------------------------------------------------
# Rotation + session pinning
# ---------------------------------------------------------------------------


def test_pool_rotates_across_sessions(pool_env) -> None:
    cfg = _config(LlmProviderProfile(api_key_env_pool=[_ENV_A, _ENV_B]))
    first = _resolve(cfg, "session-1")
    second = _resolve(cfg, "session-2")
    assert first is not None and second is not None
    assert {first.api_key, second.api_key} == {_SECRET_A, _SECRET_B}


def test_session_pinning_reuses_key_across_turns(pool_env) -> None:
    cfg = _config(LlmProviderProfile(api_key_env_pool=[_ENV_A, _ENV_B]))
    resolutions = [_resolve(cfg, "sticky-session") for _ in range(5)]
    assert all(r is not None for r in resolutions)
    keys = {r.api_key for r in resolutions if r is not None}
    assert len(keys) == 1  # same session -> same key -> warm prompt cache


def test_pool_stamps_non_secret_metadata(pool_env) -> None:
    cfg = _config(LlmProviderProfile(api_key_env_pool=[_ENV_A, _ENV_B]))
    metadata: dict = {}
    resolved = _resolve(cfg, "s1", metadata)
    assert resolved is not None
    stamp = metadata["credential_pool"]
    assert stamp["provider"] == "openai"
    assert stamp["session_key"] == "s1"
    assert stamp["env_name"] in {_ENV_A, _ENV_B}
    assert "lease_token" not in stamp
    assert _SECRET_A not in str(stamp) and _SECRET_B not in str(stamp)


# ---------------------------------------------------------------------------
# Failure handling: 429 cooldown, credits, auth
# ---------------------------------------------------------------------------


def test_429_cooldown_rotates_then_reinstates(monkeypatch) -> None:
    clock = _FakeClock()
    pools = ProfileCredentialPools(clock=clock)
    monkeypatch.setenv(_ENV_A, _SECRET_A)
    monkeypatch.setenv(_ENV_B, _SECRET_B)
    first = pools.acquire_for_session("openai", [_ENV_A, _ENV_B], "s1")
    assert first is not None
    pools.report_failure("openai", "s1", ProviderFailureKind.RATE_LIMITED)
    rotated = pools.acquire_for_session("openai", [_ENV_A, _ENV_B], "s1")
    assert rotated is not None
    assert rotated.env_name != first.env_name
    # Cooldown expires -> the parked key is eligible again.
    clock.now += RATE_LIMITED_COOLDOWN_SECONDS + 1
    pools.report_failure("openai", "s1", ProviderFailureKind.RATE_LIMITED)
    third = pools.acquire_for_session("openai", [_ENV_A, _ENV_B], "s1")
    assert third is not None
    assert third.env_name == first.env_name


def test_retry_after_hint_overrides_default_cooldown(monkeypatch) -> None:
    clock = _FakeClock()
    pools = ProfileCredentialPools(clock=clock)
    monkeypatch.setenv(_ENV_A, _SECRET_A)
    acquired = pools.acquire_for_session("openai", [_ENV_A], "s1")
    assert acquired is not None
    pools.report_failure(
        "openai",
        "s1",
        ProviderFailureKind.RATE_LIMITED,
        retry_after_seconds=5.0,
    )
    with pytest.raises(NoCredentialsAvailable):
        pools.acquire_for_session("openai", [_ENV_A], "s1")
    clock.now += 6.0
    assert pools.acquire_for_session("openai", [_ENV_A], "s1") is not None


def test_insufficient_credits_rotates_immediately_and_parks_long(monkeypatch) -> None:
    clock = _FakeClock()
    pools = ProfileCredentialPools(clock=clock)
    monkeypatch.setenv(_ENV_A, _SECRET_A)
    monkeypatch.setenv(_ENV_B, _SECRET_B)
    first = pools.acquire_for_session("openai", [_ENV_A, _ENV_B], "s1")
    assert first is not None
    pools.report_failure("openai", "s1", ProviderFailureKind.INSUFFICIENT_CREDITS)
    rotated = pools.acquire_for_session("openai", [_ENV_A, _ENV_B], "s1")
    assert rotated is not None and rotated.env_name != first.env_name
    # Far longer than a 429 window, still parked; eligible after the credits
    # cooldown (accounts may be topped up mid-process, so not permanent).
    clock.now += RATE_LIMITED_COOLDOWN_SECONDS + 1
    pools.report_failure("openai", "s1", ProviderFailureKind.RATE_LIMITED)
    with pytest.raises(NoCredentialsAvailable):
        pools.acquire_for_session("openai", [_ENV_A, _ENV_B], "s1")
    clock.now += INSUFFICIENT_CREDITS_COOLDOWN_SECONDS
    assert pools.acquire_for_session("openai", [_ENV_A, _ENV_B], "s1") is not None


def test_auth_invalid_marks_key_bad_for_process(monkeypatch) -> None:
    clock = _FakeClock()
    pools = ProfileCredentialPools(clock=clock)
    monkeypatch.setenv(_ENV_A, _SECRET_A)
    monkeypatch.setenv(_ENV_B, _SECRET_B)
    first = pools.acquire_for_session("openai", [_ENV_A, _ENV_B], "s1")
    assert first is not None
    pools.report_failure("openai", "s1", ProviderFailureKind.AUTH_INVALID)
    clock.now += 1e9  # no cooldown ever reinstates an invalid key
    for session in ("s1", "s2", "s3"):
        acquired = pools.acquire_for_session("openai", [_ENV_A, _ENV_B], session)
        assert acquired is not None
        assert acquired.env_name != first.env_name


def test_exact_lease_report_rejects_stale_result_after_rotation(pool_env) -> None:
    pools = profile_credential_pools()
    names = [_ENV_A, _ENV_B]
    session = "media-session"

    first = pools.acquire_for_session("openai", names, session)
    assert first is not None and first.lease_token
    assert pools.report_failure_for_lease(
        "openai",
        session,
        first.lease_token,
        ProviderFailureKind.AUTH_INVALID,
    )

    second = pools.acquire_for_session("openai", names, session)
    assert second is not None and second.lease_token
    assert second.env_name != first.env_name
    assert second.lease_token != first.lease_token

    # A late completion from the first paid request cannot park the key now
    # pinned to the same session for the next explicit run.
    assert not pools.report_failure_for_lease(
        "openai",
        session,
        first.lease_token,
        ProviderFailureKind.AUTH_INVALID,
    )
    reused = pools.acquire_for_session("openai", names, session)
    assert reused is not None
    assert reused.env_name == second.env_name
    assert reused.lease_token == second.lease_token

    assert pools.report_failure_for_lease(
        "openai",
        session,
        second.lease_token,
        ProviderFailureKind.AUTH_INVALID,
    )
    with pytest.raises(NoCredentialsAvailable):
        pools.acquire_for_session("openai", names, session)


def test_exact_lease_report_rejects_empty_random_and_pre_rebuild_tokens(
    pool_env,
) -> None:
    pools = profile_credential_pools()
    session = "media-session"
    first = pools.acquire_for_session("openai", [_ENV_A], session)
    assert first is not None

    for invalid_token in ("", "synthetic-unrelated-opaque-token"):
        assert not pools.report_failure_for_lease(
            "openai",
            session,
            invalid_token,
            ProviderFailureKind.AUTH_INVALID,
        )

    # A profile-pool config rebuild creates a fresh pin. The old token must
    # not remain authoritative even when the provider and session are equal.
    rebuilt = pools.acquire_for_session("openai", [_ENV_B], session)
    assert rebuilt is not None
    assert rebuilt.lease_token != first.lease_token
    assert not pools.report_failure_for_lease(
        "openai",
        session,
        first.lease_token,
        ProviderFailureKind.AUTH_INVALID,
    )
    reused = pools.acquire_for_session("openai", [_ENV_B], session)
    assert reused is not None
    assert reused.lease_token == rebuilt.lease_token


def test_all_parked_surfaces_as_unresolved_credentials(pool_env) -> None:
    cfg = _config(LlmProviderProfile(api_key_env_pool=[_ENV_A, _ENV_B]))
    assert _resolve(cfg, "s1") is not None
    assert _resolve(cfg, "s2") is not None
    pools = profile_credential_pools()
    pools.report_failure("openai", "s1", ProviderFailureKind.AUTH_INVALID)
    pools.report_failure("openai", "s2", ProviderFailureKind.RATE_LIMITED)
    # NoCredentialsAvailable is absorbed into the existing unresolved-
    # credentials contract: None back, caller keeps the active provider.
    assert _resolve(cfg, "s3") is None


def test_unattributable_failure_is_noop(pool_env) -> None:
    pools = profile_credential_pools()
    # Never-pinned session / never-served provider: nothing to park.
    pools.report_failure("openai", "ghost-session", ProviderFailureKind.RATE_LIMITED)
    pools.report_failure("acme-llm", "s1", ProviderFailureKind.RATE_LIMITED)


# ---------------------------------------------------------------------------
# Unset env names
# ---------------------------------------------------------------------------


def test_unset_env_names_skipped_with_warning(pool_env, log_recorder) -> None:
    cfg = _config(LlmProviderProfile(api_key_env_pool=[_ENV_UNSET, _ENV_A]))
    resolved = _resolve(cfg, "s1")
    assert resolved is not None
    assert resolved.api_key == _SECRET_A
    skipped = [e for e in log_recorder.events if e["event"] == "credential_pool.env_unset"]
    assert skipped and skipped[0]["env_name"] == _ENV_UNSET
    assert skipped[0]["log_level"] == "warning"


def test_pool_with_no_resolvable_names_degrades_to_single_key(
    monkeypatch, log_recorder
) -> None:
    monkeypatch.delenv(_ENV_UNSET, raising=False)
    monkeypatch.setenv(_ENV_A, _SECRET_A)
    cfg = _config(
        LlmProviderProfile(api_key_env=_ENV_A, api_key_env_pool=[_ENV_UNSET])
    )
    resolved = _resolve(cfg, "s1")
    assert resolved is not None
    assert resolved.api_key == _SECRET_A  # singular api_key_env picked it up
    assert any(
        e["event"] == "credential_pool.no_resolvable_keys" for e in log_recorder.events
    )


# ---------------------------------------------------------------------------
# Telemetry: env-var name + masked id only, never the secret
# ---------------------------------------------------------------------------


def test_no_secret_values_in_rotation_or_cooldown_logs(pool_env, log_recorder) -> None:
    cfg = _config(LlmProviderProfile(api_key_env_pool=[_ENV_A, _ENV_B]))
    assert _resolve(cfg, "s1") is not None
    assert _resolve(cfg, "s1") is not None  # pin reuse (debug event)
    assert _resolve(cfg, "s2") is not None
    pools = profile_credential_pools()
    pinned = pools.acquire_for_session("openai", [_ENV_A, _ENV_B], "s1")
    assert pinned is not None
    pools.report_failure("openai", "s1", ProviderFailureKind.RATE_LIMITED)
    pools.report_failure("openai", "s2", ProviderFailureKind.AUTH_INVALID)
    captured = log_recorder.events
    everything = repr(captured)
    assert _SECRET_A not in everything
    assert _SECRET_B not in everything
    assert pinned.lease_token not in everything
    rotations = [e for e in captured if e["event"] == "credential_pool.rotation"]
    assert rotations
    for event in rotations:
        assert set(event) >= {"provider", "session_key", "env_name", "key_id", "pool_size"}
        assert event["key_id"] in {masked_key_id(_SECRET_A), masked_key_id(_SECRET_B)}
    cooldowns = [e for e in captured if e["event"] == "credential_pool.cooldown"]
    assert len(cooldowns) == 2
    for event in cooldowns:
        assert set(event) >= {
            "provider",
            "session_key",
            "env_name",
            "key_id",
            "failure_kind",
            "cooldown_seconds",
            "permanent",
        }
    assert {e["failure_kind"] for e in cooldowns} == {"rate_limited", "auth_invalid"}


# ---------------------------------------------------------------------------
# Provider-failure hook (engine/runtime.py)
# ---------------------------------------------------------------------------


def test_stream_error_hook_parks_pinned_key(pool_env) -> None:
    cfg = _config(LlmProviderProfile(api_key_env_pool=[_ENV_A, _ENV_B]))
    metadata: dict = {}
    first = _resolve(cfg, "s1", metadata)
    assert first is not None
    metadata["routed_provider_applied"] = "openai"  # tier config was applied
    _report_credential_pool_failure(
        "openai",
        metadata,
        ErrorEvent(message="rate limit exceeded", code="429"),
    )
    rotated = _resolve(cfg, "s1", metadata)
    assert rotated is not None
    assert rotated.api_key != first.api_key


def test_stream_error_hook_ignores_unapplied_tier(pool_env) -> None:
    cfg = _config(LlmProviderProfile(api_key_env_pool=[_ENV_A, _ENV_B]))
    metadata: dict = {}
    first = _resolve(cfg, "s1", metadata)
    assert first is not None
    # Tier config resolved but never applied (e.g. explicit override won):
    # the failing provider is not the pool's, so nothing may be parked.
    _report_credential_pool_failure(
        "openai",
        metadata,
        ErrorEvent(message="rate limit exceeded", code="429"),
    )
    unchanged = _resolve(cfg, "s1", metadata)
    assert unchanged is not None
    assert unchanged.api_key == first.api_key


def test_stream_error_hook_ignores_non_reportable_kinds(pool_env) -> None:
    cfg = _config(LlmProviderProfile(api_key_env_pool=[_ENV_A, _ENV_B]))
    metadata: dict = {}
    first = _resolve(cfg, "s1", metadata)
    assert first is not None
    metadata["routed_provider_applied"] = "openai"
    _report_credential_pool_failure(
        "openai",
        metadata,
        ErrorEvent(message="internal server error", code="500"),
    )
    unchanged = _resolve(cfg, "s1", metadata)
    assert unchanged is not None
    assert unchanged.api_key == first.api_key


# ---------------------------------------------------------------------------
# Config contract: profiles-only, additive, downgrade-tolerated
# ---------------------------------------------------------------------------


def test_profile_accepts_api_key_env_pool() -> None:
    profile = LlmProviderProfile(api_key_env_pool=[_ENV_A, _ENV_B])
    assert profile.api_key_env_pool == [_ENV_A, _ENV_B]
    cfg = GatewayConfig.model_validate(
        {"llm_profiles": {"openai": {"api_key_env_pool": [_ENV_A]}}}
    )
    assert cfg.llm_profiles["openai"].api_key_env_pool == [_ENV_A]
    dumped = cfg.to_toml_dict()
    assert dumped["llm_profiles"]["openai"]["api_key_env_pool"] == [_ENV_A]


def test_top_level_llm_does_not_gain_pool_field() -> None:
    assert "api_key_env_pool" not in LlmProviderConfig.model_fields
    # [llm] stays extra="forbid": an rc1 config never sees the field there,
    # and a stamped default would brick downgrade (decision D5).
    with pytest.raises(ValidationError):
        LlmProviderConfig(api_key_env_pool=[_ENV_A])


def test_pool_field_is_not_treated_as_secret() -> None:
    # Env-var NAMES must stay readable in the public config surface, exactly
    # like api_key_env and the other *_env reference fields.
    assert is_sensitive_config_key("api_key_env_pool") is False
    assert is_sensitive_config_key("api_key") is True


class _Rc1LlmProviderProfile(BaseSettings):
    """Shape of the 0.5.0rc1 profile model: no pool field, extra ignored."""

    model_config = SettingsConfigDict(extra="ignore")

    api_key: str = ""
    api_key_env: str = ""
    base_url: str = ""
    proxy: str = ""


def test_rc1_rollback_tolerates_pool_field_on_load() -> None:
    """Downgrade contract: rc1 loads a pool-bearing profile, then strips it.

    The current cycle may persist ``api_key_env_pool`` inside
    ``[llm_profiles.<id>]``. rc1's profile model is ``extra="ignore"``, so
    the field is tolerated on load and silently dropped by rc1's first
    persist — accepted, release-noted lossiness (the pool must be re-added
    on upgrade).
    """
    profile = _Rc1LlmProviderProfile.model_validate(
        {"api_key_env": _ENV_A, "api_key_env_pool": [_ENV_A, _ENV_B]}
    )
    assert profile.api_key_env == _ENV_A
    assert "api_key_env_pool" not in profile.model_dump()
