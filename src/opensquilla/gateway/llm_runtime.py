"""Runtime LLM provider credential resolution."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from opensquilla.provider.credentials import Credential, CredentialPool, NoCredentialsAvailable
from opensquilla.provider.environment import environment_value
from opensquilla.provider.failures import ProviderFailureKind

log = structlog.get_logger(__name__)

OPENROUTER_DEFAULT_PROVIDER_ROUTING = {
    "anthropic/claude-opus-4.8": "anthropic",
    "anthropic/claude-sonnet-4.6": "anthropic",
    "deepseek/deepseek-v4-flash": "deepseek",
    "google/gemini-3.5-flash": "google",
    "moonshotai/kimi-k2.6": "moonshotai",
    "openai/gpt-5.4-mini": "openai",
    "openai/gpt-5.5": "openai",
    "qwen/qwen3-coder-plus": "qwen",
    "x-ai/grok-4.3": "x-ai",
    "z-ai/glm-4.6": "z-ai",
    "z-ai/glm-5.1": "z-ai",
    "z-ai/glm-5.2": "z-ai",
}


@dataclass(frozen=True)
class LlmRuntimeConfig:
    provider: str
    model: str
    api_key: str
    base_url: str
    proxy: str
    provider_routing: dict[str, str]
    api_key_from_env: bool = False
    api_key_env_name: str = ""
    base_url_from_env: bool = False


@dataclass(frozen=True)
class ResolvedLlmCredential:
    """One primary credential plus secret-free source provenance."""

    api_key: str = field(default="", repr=False)
    source: str = "none"
    env_name: str = ""


def resolve_llm_credential(
    config: Any,
    *,
    registry_env_key: str = "",
    include_runtime_cache: bool = True,
) -> ResolvedLlmCredential:
    """Resolve the primary key exactly as a fresh settings load would.

    ``LlmProviderConfig`` reads the two ``OPENSQUILLA_LLM_*`` settings
    variables when it is constructed.  Hot config mutations do not reconstruct
    that model, so consult those external inputs explicitly as well.  Stored
    config still wins. An explicitly configured env-name is authoritative: if
    it is missing, resolution fails closed instead of substituting another
    source. Otherwise settings/registry env names precede the generic settings
    key. Runtime callers may retain an already-materialized environment secret;
    observability and reveal surfaces disable that cache so they describe only
    currently inspectable sources.
    """

    llm = getattr(config, "llm", None)
    if llm is None:
        return ResolvedLlmCredential()

    runtime_secret_paths: set[str] = getattr(config, "_runtime_secret_paths", set())
    stored_api_key = str(getattr(llm, "api_key", "") or "")
    explicit_api_key = (
        "" if "llm.api_key" in runtime_secret_paths else stored_api_key
    )
    configured_env_name = str(getattr(llm, "api_key_env", "") or "").strip()
    settings_env_name = environment_value("OPENSQUILLA_LLM_API_KEY_ENV").strip()
    env_name = configured_env_name or settings_env_name or str(registry_env_key or "").strip()

    if explicit_api_key:
        return ResolvedLlmCredential(
            api_key=explicit_api_key,
            source="explicit",
            env_name=env_name,
        )

    named_env_key = environment_value(env_name) if env_name else ""
    if named_env_key:
        return ResolvedLlmCredential(
            api_key=named_env_key,
            source="env",
            env_name=env_name,
        )

    if configured_env_name:
        return ResolvedLlmCredential(env_name=configured_env_name)

    settings_api_key = environment_value("OPENSQUILLA_LLM_API_KEY")
    if settings_api_key:
        return ResolvedLlmCredential(
            api_key=settings_api_key,
            source="env",
            env_name="OPENSQUILLA_LLM_API_KEY",
        )

    # With no authoritative configured reference, a value materialized from
    # the environment earlier in this process may remain usable after its
    # source variable is removed. Keep its runtime provenance instead of
    # misclassifying that cached secret as explicit.
    if (
        include_runtime_cache
        and stored_api_key
        and "llm.api_key" in runtime_secret_paths
    ):
        return ResolvedLlmCredential(
            api_key=stored_api_key,
            source="env",
            env_name=env_name,
        )

    return ResolvedLlmCredential(env_name=env_name)


def provider_base_url_env_name(provider: str) -> str:
    from opensquilla.provider.registry import get_provider_spec

    spec = get_provider_spec(provider)
    if spec.env_key.endswith("_API_KEY"):
        return f"{spec.env_key.removesuffix('_API_KEY')}_BASE_URL"
    normalized = spec.provider_id.upper().replace("-", "_")
    return f"{normalized}_BASE_URL"


def _resolve_provider_routing(provider: str, configured: Any) -> dict[str, str]:
    routing = dict(configured or {})
    if provider != "openrouter":
        return routing
    return {**OPENROUTER_DEFAULT_PROVIDER_ROUTING, **routing}


def _field_default_base_url() -> str:
    from opensquilla.gateway.config import LlmProviderConfig

    return str(LlmProviderConfig.model_fields["base_url"].default or "")


def _is_explicit_base_url(stored: str, spec: Any, env_base_url: str) -> bool:
    """Whether ``stored`` names an operator-chosen endpoint.

    Value-vs-baseline, the same attribution rule ``provider.resolution``
    documents for these fields (in-place env materialization poisons
    ``model_fields_set``, so presence tracking cannot answer this): a value
    equal to the pydantic field default, the provider spec default, or the
    current derived env value is derived, not chosen. The env baseline is
    what keeps repeated in-process resolves idempotent — the first resolve
    writes the env URL into the model, and the second must not promote it to
    an explicit choice — and it also stops env URLs persisted by pre-#484
    releases from pinning the endpoint after the operator unsets the var.
    """
    if not stored:
        return False
    baselines = {_field_default_base_url()}
    if spec is not None and spec.default_base_url:
        baselines.add(spec.default_base_url)
    if env_base_url:
        baselines.add(env_base_url)
    return stored not in baselines


def resolve_llm_runtime_config(config: Any) -> LlmRuntimeConfig:
    """Resolve provider credentials: explicit config, then env, then defaults.

    Both api_key and base_url resolve explicit-config-first; the derived env
    var (``<PROVIDER>_API_KEY`` / ``<PROVIDER>_BASE_URL``) fills in only when
    the config never chose a value, and the spec default is the last resort.

    An unset or unregistered provider id must not crash the boot: the gateway
    starts degraded (no spec-derived env key or default base URL) and the
    onboarding readiness surface reports the misconfiguration, leaving the
    control UI available to fix it.
    """
    from opensquilla.provider.registry import UnknownProviderError, get_provider_spec

    llm = config.llm
    provider = str(llm.provider or "").strip().lower()
    try:
        spec = get_provider_spec(provider)
    except UnknownProviderError as exc:
        log.warning("llm_runtime.unknown_provider", provider=provider, error=str(exc))
        spec = None
    spec_env_key = spec.env_key if spec is not None else ""
    credential = resolve_llm_credential(config, registry_env_key=spec_env_key)
    base_url_env_name = provider_base_url_env_name(provider) if spec is not None else ""
    env_base_url = environment_value(base_url_env_name) if base_url_env_name else ""
    api_key = credential.api_key
    # Explicit config > derived env > spec default, mirroring the api_key
    # rule (#484): a base_url the operator chose must not be overridden by
    # OPENAI_BASE_URL-style vars on the next boot/reload. Derived stored
    # values (field default from a minimal TOML, the spec default the Web UI
    # seeds into its endpoint field, a previously materialized env value)
    # keep env-first behavior so a fleet-wide env override still applies to
    # configs that never chose an endpoint. Evaluated BEFORE the in-place
    # materialization below — afterwards llm.base_url holds the resolution
    # result and the distinction is gone.
    stored_base_url = str(llm.base_url or "")
    explicit_base_url = _is_explicit_base_url(stored_base_url, spec, env_base_url)
    if explicit_base_url:
        base_url = stored_base_url
    else:
        base_url = env_base_url or (spec.default_base_url if spec else stored_base_url)
    base_url_from_env = bool(env_base_url) and base_url == env_base_url
    proxy = environment_value("OPENSQUILLA_LLM_PROXY") or getattr(llm, "proxy", "")

    # Record runtime provenance BEFORE mutating the live model: values
    # resolved from the environment (or spec defaults) here must never be
    # baked into config.toml by a later unrelated persist. Only api_key has
    # the runtime-secret mark; base_url/proxy get the override record that
    # onboarding.config_store restores at persist time.
    if hasattr(config, "record_runtime_override"):
        if base_url != stored_base_url:
            config.record_runtime_override("llm.base_url", stored_base_url, base_url)
        stored_proxy = getattr(llm, "proxy", "")
        if proxy != stored_proxy:
            config.record_runtime_override("llm.proxy", stored_proxy, proxy)

    llm.provider = provider
    llm.api_key = api_key
    llm.base_url = base_url
    llm.proxy = proxy
    if credential.source == "env" and hasattr(config, "mark_runtime_secret"):
        config.mark_runtime_secret("llm.api_key")

    return LlmRuntimeConfig(
        provider=provider,
        model=llm.model,
        api_key=api_key,
        base_url=base_url,
        proxy=proxy,
        provider_routing=_resolve_provider_routing(
            provider,
            getattr(llm, "provider_routing", {}),
        ),
        api_key_from_env=credential.source == "env",
        api_key_env_name=credential.env_name,
        base_url_from_env=base_url_from_env,
    )


# ---------------------------------------------------------------------------
# Profile credential pools ([llm_profiles.<id>].api_key_env_pool)
# ---------------------------------------------------------------------------

#: Cooldown applied on RATE_LIMITED when the caller has no Retry-After hint
#: (the provider error event carries no header data today).
RATE_LIMITED_COOLDOWN_SECONDS = 60.0
#: INSUFFICIENT_CREDITS parks much longer than a 429: credits do not refill
#: on a rate-limit window, but the account may be topped up mid-process, so
#: the key is parked rather than permanently retired.
INSUFFICIENT_CREDITS_COOLDOWN_SECONDS = 3600.0

_REPORTABLE_FAILURE_KINDS = frozenset(
    {
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.INSUFFICIENT_CREDITS,
        ProviderFailureKind.AUTH_INVALID,
    }
)


def masked_key_id(secret: str) -> str:
    """Return a short, stable, non-reversible id for a key value.

    Safe to log and correlate across events/processes; never derived from
    key substrings (no prefixes/last-4), only a truncated SHA-256 digest.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class PooledCredential:
    """A pool-resolved profile credential.

    ``env_name`` (the credential id) and ``key_id`` are safe to log;
    ``api_key`` is the live secret and is excluded from ``repr``.
    """

    provider_id: str
    env_name: str
    key_id: str
    api_key: str = field(default="", repr=False)


class ProfileCredentialPools:
    """Process-wide credential pools for ``[llm_profiles.<id>]`` rotation.

    Wraps :class:`CredentialPool` per provider profile and adds the runtime
    concerns the pool itself stays agnostic of:

    - **Construction from env-var names** — ``api_key_env_pool`` holds names
      only; values are resolved from the environment when the pool is built
      and live only in memory. Unset names are skipped with a warning; if no
      name resolves the caller falls back to single-key resolution.
    - **Per-session pinning** — the first acquire for a session key pins the
      credential, so subsequent turns reuse the same key (prompt-cache
      warmth) until it is parked or the pool is rebuilt. Pin reuse does not
      advance the round-robin cursor or acquisition counts.
    - **Failure-kind parking** — RATE_LIMITED parks for the Retry-After hint
      (or a default), INSUFFICIENT_CREDITS parks long, AUTH_INVALID parks
      permanently for the process. Every report drops the session pin so the
      next turn re-acquires (rotating to another key).
    - **Config-change rebuilds** — the pool is rebuilt (and its pins and
      parked state reset) when the profile's configured name list changes,
      e.g. after a config hot-apply.

    Telemetry contract: every event logs only the env-var NAME and the
    masked ``key_id`` — never the resolved secret value.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._pools: dict[str, CredentialPool] = {}
        self._pool_fingerprints: dict[str, tuple[str, ...]] = {}
        self._creds_by_id: dict[str, dict[str, Credential]] = {}
        self._key_ids: dict[str, dict[str, str]] = {}
        self._pins: dict[str, dict[str, str]] = {}

    def acquire_for_session(
        self,
        provider_id: str,
        env_pool: list[str],
        session_key: str,
    ) -> PooledCredential | None:
        """Resolve a pool credential for ``session_key``, pinning it.

        Returns None when no configured env name resolves to a value (the
        caller degrades to the single-key path). Raises
        :class:`NoCredentialsAvailable` when the pool exists but every
        credential is parked.
        """
        provider_id = (provider_id or "").strip().lower()
        names = tuple(dict.fromkeys(n.strip() for n in env_pool if n and n.strip()))
        if not provider_id or not names:
            return None
        with self._lock:
            pool = self._ensure_pool_locked(provider_id, names)
            if pool is None:
                return None

            pins = self._pins.setdefault(provider_id, {})
            pinned_id = pins.get(session_key)
            if pinned_id is not None and pool.available(pinned_id):
                cred = self._creds_by_id[provider_id][pinned_id]
                log.debug(
                    "credential_pool.pin_reused",
                    provider=provider_id,
                    session_key=session_key,
                    env_name=cred.cred_id,
                    key_id=self._key_ids[provider_id][cred.cred_id],
                )
                return self._pooled_locked(provider_id, cred)

            cred = pool.acquire()  # may raise NoCredentialsAvailable
            pins[session_key] = cred.cred_id
            log.info(
                "credential_pool.rotation",
                provider=provider_id,
                session_key=session_key,
                env_name=cred.cred_id,
                key_id=self._key_ids[provider_id][cred.cred_id],
                pool_size=len(pool),
            )
            return self._pooled_locked(provider_id, cred)

    def report_failure(
        self,
        provider_id: str,
        session_key: str,
        kind: ProviderFailureKind,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Park the credential pinned to ``session_key`` per failure kind.

        No-op when the (provider, session) pair holds no pin — failures of
        providers or sessions this manager never served cannot be attributed
        to a pool credential. The pin is always dropped so the next acquire
        rotates to another eligible key.
        """
        if kind not in _REPORTABLE_FAILURE_KINDS:
            return
        provider_id = (provider_id or "").strip().lower()
        with self._lock:
            pool = self._pools.get(provider_id)
            pins = self._pins.get(provider_id, {})
            cred_id = pins.pop(session_key, None)
            if pool is None or cred_id is None:
                return
            if kind is ProviderFailureKind.AUTH_INVALID:
                cooldown = float("inf")
            elif kind is ProviderFailureKind.INSUFFICIENT_CREDITS:
                cooldown = INSUFFICIENT_CREDITS_COOLDOWN_SECONDS
            else:
                cooldown = (
                    retry_after_seconds
                    if retry_after_seconds is not None and retry_after_seconds >= 0
                    else RATE_LIMITED_COOLDOWN_SECONDS
                )
            pool.report_429(cred_id, cooldown_seconds=cooldown)
            # Drop every other session pinned to the now-parked key so they
            # rotate on their next turn instead of failing the same way.
            for other_session in [s for s, c in pins.items() if c == cred_id]:
                pins.pop(other_session, None)
            log.warning(
                "credential_pool.cooldown",
                provider=provider_id,
                session_key=session_key,
                env_name=cred_id,
                key_id=self._key_ids.get(provider_id, {}).get(cred_id, ""),
                failure_kind=str(kind),
                cooldown_seconds=cooldown,
                permanent=cooldown == float("inf"),
            )

    def peek_available(
        self,
        provider_id: str,
        env_pool: list[str],
    ) -> PooledCredential | None:
        """Return one currently runnable credential without mutating pool state.

        Status/readiness surfaces use this instead of ``acquire_for_session``:
        it observes the same process-wide parked state but never advances the
        cursor, increments acquisition counts, creates a session pin, or
        rebuilds a pool.  When the configured name list has not been seen by
        the runtime yet (or has changed), a direct environment inspection
        predicts the next rebuild without performing it.
        """

        provider_id = (provider_id or "").strip().lower()
        names = tuple(dict.fromkeys(n.strip() for n in env_pool if n and n.strip()))
        if not provider_id or not names:
            return None
        with self._lock:
            if self._pool_fingerprints.get(provider_id) != names:
                for env_name in names:
                    secret = environment_value(env_name).strip()
                    if secret:
                        return PooledCredential(
                            provider_id=provider_id,
                            env_name=env_name,
                            key_id=masked_key_id(secret),
                            api_key=secret,
                        )
                return None

            pool = self._pools.get(provider_id)
            if pool is None:
                return None
            for env_name in names:
                if not pool.available(env_name):
                    continue
                cred = self._creds_by_id[provider_id][env_name]
                return self._pooled_locked(provider_id, cred)
            raise NoCredentialsAvailable("all credentials in cooldown")

    def discard_provider(self, provider_id: str) -> None:
        """Forget one profile's resolved secrets, cooldowns and session pins."""
        provider = (provider_id or "").strip().lower()
        if not provider:
            return
        with self._lock:
            self._pools.pop(provider, None)
            self._pool_fingerprints.pop(provider, None)
            self._creds_by_id.pop(provider, None)
            self._key_ids.pop(provider, None)
            self._pins.pop(provider, None)

    def _pooled_locked(self, provider_id: str, cred: Credential) -> PooledCredential:
        return PooledCredential(
            provider_id=provider_id,
            env_name=cred.cred_id,
            key_id=self._key_ids[provider_id][cred.cred_id],
            api_key=cred.secret,
        )

    def _ensure_pool_locked(
        self,
        provider_id: str,
        names: tuple[str, ...],
    ) -> CredentialPool | None:
        if self._pool_fingerprints.get(provider_id) == names:
            return self._pools.get(provider_id)

        # Configured name list changed (or first use): rebuild. Parked state
        # and pins reset by design — a config edit is an operator action.
        credentials: list[Credential] = []
        key_ids: dict[str, str] = {}
        for env_name in names:
            secret = environment_value(env_name).strip()
            if not secret:
                log.warning(
                    "credential_pool.env_unset",
                    provider=provider_id,
                    env_name=env_name,
                )
                continue
            credentials.append(Credential(cred_id=env_name, secret=secret))
            key_ids[env_name] = masked_key_id(secret)

        self._pool_fingerprints[provider_id] = names
        self._pins.pop(provider_id, None)
        if not credentials:
            self._pools.pop(provider_id, None)
            self._creds_by_id.pop(provider_id, None)
            self._key_ids.pop(provider_id, None)
            log.warning(
                "credential_pool.no_resolvable_keys",
                provider=provider_id,
                env_names=list(names),
            )
            return None
        pool = CredentialPool(credentials, clock=self._clock)
        self._pools[provider_id] = pool
        self._creds_by_id[provider_id] = {cred.cred_id: cred for cred in credentials}
        self._key_ids[provider_id] = key_ids
        log.info(
            "credential_pool.built",
            provider=provider_id,
            env_names=[cred.cred_id for cred in credentials],
            pool_size=len(pool),
        )
        return pool


_profile_pools = ProfileCredentialPools()
_profile_pools_lock = threading.Lock()


def profile_credential_pools() -> ProfileCredentialPools:
    """Return the process-wide profile credential pool manager."""
    return _profile_pools


def reset_profile_credential_pools(
    *,
    clock: Callable[[], float] = time.monotonic,
) -> ProfileCredentialPools:
    """Replace the process-wide manager (test hook; also clears all pins)."""
    global _profile_pools
    with _profile_pools_lock:
        _profile_pools = ProfileCredentialPools(clock=clock)
        return _profile_pools


def discard_profile_credential_pool(provider_id: str) -> None:
    """Purge one profile's process-local resolved credential state."""
    profile_credential_pools().discard_provider(provider_id)


__all__ = [
    "INSUFFICIENT_CREDITS_COOLDOWN_SECONDS",
    "OPENROUTER_DEFAULT_PROVIDER_ROUTING",
    "RATE_LIMITED_COOLDOWN_SECONDS",
    "LlmRuntimeConfig",
    "NoCredentialsAvailable",
    "PooledCredential",
    "ProfileCredentialPools",
    "ResolvedLlmCredential",
    "discard_profile_credential_pool",
    "masked_key_id",
    "profile_credential_pools",
    "provider_base_url_env_name",
    "reset_profile_credential_pools",
    "resolve_llm_credential",
    "resolve_llm_runtime_config",
]
