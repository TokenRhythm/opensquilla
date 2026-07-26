"""Provider credential checks for code-task, before and during a run.

A code-task run clones the repo, installs dependencies, and drives an agent
for many minutes. When the configured provider has no usable credential the
subagent only discovers it once it starts calling the model — after the
expensive setup — and the failure surfaces as a misleading "no valid
verification manifest" after burning retries. Two cheap guards fix that:

* ``provider_preflight`` runs one throwaway request against the SAME provider
  config the subagent will use, before any clone, and blocks the run with an
  actionable message when the credential is missing or rejected.
* ``provider_block_reason`` inspects the agent's JSON envelope so a credential
  failure that happens mid-run (e.g. a balance hitting zero) stops honestly
  instead of being retried into an opaque verification error.

Only unambiguous credential/config failures block. Transient transport errors,
model-not-found, rate limits, and unknown failures fail open — the run
proceeds and its own error handling takes over — so a probe blip never turns a
workable run into a hard stop.
"""

from __future__ import annotations

import asyncio

import structlog

from opensquilla.contrib.codetask.agent_config import AgentConfigBundle
from opensquilla.onboarding.probe import ProviderProbeResult, probe_llm_provider
from opensquilla.provider.failures import ProviderFailureKind

log = structlog.get_logger(__name__)

# Probe failure kinds that mean "this credential cannot work" — block the run.
# Everything else (transport_transient, model_not_found, rate_limited, unknown,
# …) fails open.
_CREDENTIAL_BLOCK_KINDS = frozenset(
    {ProviderFailureKind.AUTH_INVALID.value, ProviderFailureKind.INSUFFICIENT_CREDITS.value}
)

# Envelope error codes from the child agent that mean the same thing. The
# engine emits "no_provider" (provider_and_tools_stage) and the HTTP status as
# a string ("401"/"402"/"403", from the provider adapters); these are the codes
# that actually reach result.errors.
_CREDENTIAL_BLOCK_CODES = frozenset({"no_provider", "401", "402", "403"})


def _effective_provider_config(
    bundle: AgentConfigBundle, model_override: str
) -> tuple[str, str, str, str, str] | None:
    """Resolve (provider, model, api_key, base_url, proxy) the subagent will use.

    Returns ``None`` if the bundle carries no usable ``[llm]`` (nothing to
    probe). Mirrors the child's own resolution: build the config from the
    per-run payload, then apply the same env/credential precedence.
    """
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config

    try:
        cfg = GatewayConfig(**bundle.payload)
    except Exception:  # already validated in load_agent_config_bundle; defensive
        return None
    cfg._mark_env_absorbed_secrets(bundle.payload)
    # Cross-provider router tiers resolve credentials per tier from
    # [llm_profiles]/pools that a primary-provider probe cannot see, and may
    # bypass the primary entirely — probing only the primary would risk a
    # false block. Skip the preflight for those (preview) configs.
    if bool(getattr(getattr(cfg, "squilla_router", None), "cross_provider_tiers", False)):
        return None
    runtime = resolve_llm_runtime_config(cfg)
    provider = (runtime.provider or "").strip()
    if not provider:
        return None
    model = model_override.strip() or (runtime.model or "").strip()
    # The child receives an explicit key through OPENSQUILLA_LLM_API_KEY (kept
    # out of the on-disk payload); otherwise the key resolves from the env var
    # named by api_key_env / the provider spec, which resolve_llm_runtime_config
    # already applied.
    api_key = bundle.child_env.get("OPENSQUILLA_LLM_API_KEY", "") or (runtime.api_key or "")
    return provider, model, api_key, runtime.base_url or "", runtime.proxy or ""


def provider_preflight(bundle: AgentConfigBundle, model_override: str = "") -> tuple[bool, str]:
    """Return (ok, reason). ``ok`` False blocks the run with ``reason``.

    Runs one small, provider-bounded live request. Keyless providers (ollama, lm_studio, …)
    and un-probeable configs pass without a network call.
    """
    from opensquilla.provider.registry import get_provider_spec

    resolved = _effective_provider_config(bundle, model_override)
    if resolved is None:
        return True, ""
    provider, model, api_key, base_url, proxy = resolved

    try:
        spec = get_provider_spec(provider)
    except ValueError:
        # Unknown provider id: not our call to make here — let the run surface it.
        return True, ""
    if not spec.requires_api_key():
        return True, ""  # keyless / local provider — nothing to probe
    if not model:
        # Can't probe without a model; the run itself will report a bad config.
        return True, ""

    try:
        probe: ProviderProbeResult = asyncio.run(
            probe_llm_provider(
                provider_id=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                proxy=proxy,
            )
        )
    except ValueError:
        return True, ""  # validation-level probe issue — fail open
    except Exception as exc:  # never let a probe bug block a run
        log.warning("codetask.preflight.probe_error", error=str(exc))
        return True, ""

    if probe.ok:
        return True, ""
    if probe.failure_kind in _CREDENTIAL_BLOCK_KINDS:
        return False, (
            f"code-task's provider '{provider}' cannot authenticate for model "
            f"'{model}': {probe.message} Configure a working provider (run "
            "`opensquilla onboard`) or set that provider's API key, then retry."
        )
    # Non-credential failure (transport blip, model-not-found, …): fail open.
    log.info(
        "codetask.preflight.non_blocking_failure",
        provider=provider,
        failure_kind=probe.failure_kind,
    )
    return True, ""


def provider_block_reason(errors: list[dict]) -> str | None:
    """Actionable message if the agent's envelope errors are credential-class.

    ``errors`` is the ``errors`` array from the agent JSON envelope (each item
    ``{message, code}``). Returns ``None`` for non-credential errors so the run
    keeps its existing verification path.
    """
    for err in errors:
        if not isinstance(err, dict):
            continue
        code = str(err.get("code") or "").strip().lower()
        if code in _CREDENTIAL_BLOCK_CODES:
            message = str(err.get("message") or "").strip()
            detail = f": {message}" if message else ""
            return (
                "code-task's provider rejected the request (the configured "
                f"provider/credential is missing or invalid){detail} Configure a "
                "working provider (run `opensquilla onboard`) or set that "
                "provider's API key, then retry."
            )
    return None
