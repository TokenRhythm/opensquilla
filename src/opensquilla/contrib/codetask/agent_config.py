"""Per-run agent-config assembly for the code-task subagent.

The subagent loads its config through ``OPENSQUILLA_GATEWAY_CONFIG_PATH``,
which REPLACES the normal config chain — so the bundled template used to pin
the subagent to the template's own provider no matter what the operator
configured (issue #541). Assembly keeps the template authoritative for run
policy (tool deny list, sandbox and access posture, meta-skill gating, memory
flush) while the operator's provider stack is carried into the per-run config,
so the subagent talks to the same provider as every other surface.
"""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opensquilla.contrib.codetask.config import agent_config_override, agent_config_path
from opensquilla.paths import default_opensquilla_home

# Sections that follow the OPERATOR's config into the subagent. [llm] and
# [squilla_router] must travel as a pair: a tier_profile that does not match
# llm.provider fails GatewayConfig validation, and preset tier model ids from
# one provider 404 on another. [llm_ensemble] is deliberately NOT carried:
# its static profiles opt back in on env-key presence alone, which would
# re-pin the subagent to a provider the operator moved away from.
OPERATOR_SECTIONS = (
    "llm",
    "squilla_router",
    "llm_profiles",
    "models",
    "model_catalog",
    "privacy",
)

# Field-level pydantic precedence is init (TOML) > env, and env fills fields
# ABSENT from the TOML section — so a key stripped from the written file is
# still picked up by the child through this variable.
_PRIMARY_KEY_ENV = "OPENSQUILLA_LLM_API_KEY"


class AgentConfigError(RuntimeError):
    """The subagent config cannot work; message is operator-actionable."""


@dataclass(frozen=True)
class AgentConfigBundle:
    """Validated per-run config payload plus env additions for the child."""

    payload: dict[str, Any]
    child_env: dict[str, str] = field(default_factory=dict)
    source_path: str | None = None  # operator config file merged in, if any


def user_config_payload() -> tuple[dict[str, Any], str | None]:
    """Raw TOML payload of the operator's effective config file, if any.

    Mirrors ``GatewayConfig.load`` candidate order (explicit env path is the
    sole candidate; otherwise ``./opensquilla.toml`` then the home config).
    Raw TOML — not the loaded model — so only values the operator actually
    wrote travel, and pydantic defaults are not materialized into the
    per-run file.
    """
    explicit = os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return (_read_payload(path), str(path)) if path.is_file() else ({}, None)
    for path in (Path.cwd() / "opensquilla.toml", default_opensquilla_home() / "config.toml"):
        if path.is_file():
            return _read_payload(path), str(path)
    return {}, None


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AgentConfigError(f"cannot read operator config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AgentConfigError(f"operator config {path} is not valid TOML: {exc}") from exc


def load_agent_config_bundle() -> AgentConfigBundle:
    """Assemble and validate the subagent config for one code-task run."""
    override = agent_config_override()
    if override is not None:
        # An explicit operator override remains authoritative for provider and
        # run policy (the documented #541 escape hatch).  The gateway's
        # explicitly configured privacy policy still applies to every child
        # process, however, so it cannot be relaxed by a provider override.
        payload = _read_payload(override)
        try:
            user_payload, _source = user_config_payload()
        except AgentConfigError:
            # The explicit override is the recovery path for a broken ordinary
            # config. Keep it usable, but never silently enable installation
            # metadata when the operator's privacy policy cannot be read.
            payload["privacy"] = {"disable_network_observability": True}
        else:
            if "privacy" in user_payload:
                privacy_payload = user_payload["privacy"]
                try:
                    from opensquilla.gateway.config import PrivacyConfig

                    PrivacyConfig(**privacy_payload)
                except Exception:
                    payload["privacy"] = {
                        "disable_network_observability": True
                    }
                else:
                    payload["privacy"] = copy.deepcopy(privacy_payload)
        payload = _validated(payload, source=str(override))
        return AgentConfigBundle(payload=payload, source_path=str(override))
    template_payload = _read_payload(agent_config_path())
    user_payload, source = user_config_payload()
    return build_per_run_agent_config(template_payload, user_payload, user_config_path=source)


def build_per_run_agent_config(
    template_payload: dict[str, Any],
    user_payload: dict[str, Any],
    *,
    user_config_path: str | None = None,
) -> AgentConfigBundle:
    """Overlay the operator's provider sections onto the policy template.

    Operator sections replace the template's wholesale (and are dropped when
    the operator has none, falling back to built-in defaults plus env), so
    the subagent resolves provider, model, credentials, router tiers, and
    network-observability privacy exactly like the operator's own gateway.
    """
    merged = copy.deepcopy(template_payload)
    child_env: dict[str, str] = {}
    for section in OPERATOR_SECTIONS:
        if section in user_payload:
            merged[section] = copy.deepcopy(user_payload[section])
        else:
            merged.pop(section, None)

    llm = merged.get("llm")
    if isinstance(llm, dict):
        api_key = llm.get("api_key")
        if isinstance(api_key, str) and api_key:
            # Keep the literal out of the on-disk per-run config (which is
            # snapshotted per attempt); the child re-reads it from env.
            del llm["api_key"]
            child_env[_PRIMARY_KEY_ENV] = api_key
        # An explicitly EMPTY api_key stays in place: the operator relies on
        # api_key_env resolution, and dropping the key would let a stale
        # OPENSQUILLA_LLM_API_KEY in the inherited env fill it in the child
        # (init "" beats env, but an absent field does not).

    payload = _validated(merged, source=user_config_path or "built-in defaults")
    return AgentConfigBundle(payload=payload, child_env=child_env, source_path=user_config_path)


def _validated(payload: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Migrate + construct GatewayConfig so a bad config fails in the runner
    process (milliseconds, actionable) instead of at subagent boot (minutes,
    opaque). Returns the migration-stamped payload to write."""
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.config_migration import migrate_config_payload

    try:
        migration = migrate_config_payload(copy.deepcopy(payload))
        GatewayConfig(**migration.payload)
    except Exception as exc:
        raise AgentConfigError(
            f"code-task subagent config (from {source}) is invalid: {exc}"
        ) from exc
    return migration.payload
