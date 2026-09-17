"""Shared dependency preflight for meta-skill launch surfaces.

Meta-skills are composed workflows, so checking only the top-level manifest is
not sufficient.  This module walks declared ``skill`` references and rolls up
hard eligibility requirements before an expensive run is stamped or started.
Advisory static dependency hints intentionally do not participate in the gate.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, cast

from opensquilla.skills.capability_runtime import (
    META_OPENROUTER_API_KEY_ENV as META_OPENROUTER_API_KEY_ENV,
)
from opensquilla.skills.capability_runtime import (
    capability_ambient_credential_env_names,
    capability_manifest_env_aliases_for_consumers,
    capability_provider_display_name,
    capability_registered_consumers,
    capability_requirements_for_consumers,
    capability_runtime_env_for_consumers,
    capability_supported_readiness_env_aliases,
    resolve_capability_status,
    trusted_capability_consumers_for_meta_plan,
)
from opensquilla.skills.eligibility import (
    EligibilityContext,
    diagnose_eligibility,
    is_skill_available_live,
)
from opensquilla.skills.types import SkillLayer, SkillSpec

META_READINESS_ENV_ALIASES_METADATA_KEY = "meta_readiness_env_aliases"
META_SKILL_RUNTIME_ENV_PROVIDER_METADATA_KEY = "meta_skill_runtime_env_provider"
_CONFIGURED_ENV_SENTINEL = "configured"
_TRUSTED_PROVIDER_WORKFLOW_OVERRIDDEN_REASON = (
    "A trusted provider workflow component is overridden by a higher-priority "
    "skill source. Remove or rename that override before running this MetaSkill."
)


def configured_meta_readiness_env_aliases(
    config: Any | None,
    *,
    parent_spec: Any,
    plan: Any,
    skill_resolver: Any | None = None,
) -> tuple[str, ...]:
    """Return non-secret manifest env names satisfied by a provider connection.

    OpenRouter-backed media skills declare ``OPENROUTER_API_KEY`` because
    that is their portable direct-CLI contract.  Gateway execution may satisfy
    it through the active provider, a secondary provider profile, the legacy
    image-provider section, or the canonical provider environment.  Project
    only the canonical env *name* used by manifests; key material never leaves
    this helper.
    """

    consumers = trusted_capability_consumers_for_meta_plan(
        parent_spec,
        plan,
        skill_resolver=skill_resolver,
    )
    if not consumers:
        return ()
    requirements = capability_requirements_for_consumers(consumers)
    if requirements and all(
        resolve_capability_status(config, requirement).ready
        for requirement in requirements
    ):
        return capability_manifest_env_aliases_for_consumers(
            consumers
        )
    return ()


def configured_meta_skill_runtime_env(
    config: Any | None,
    *,
    parent_spec: Any,
    plan: Any,
    session_key: str = "",
    skill_resolver: Any | None = None,
) -> dict[str, dict[str, str]]:
    """Resolve trusted, least-privilege child env for paid media skills.

    Meta-skill subprocesses run with the user's workspace as their current
    directory. They must therefore never rediscover ``opensquilla.toml`` from
    that directory: a checked-out project could otherwise choose an unrelated
    ``api_key_env`` and make the bundled client send that secret upstream.

    The parent resolves one atomic provider deployment (provider id, key,
    endpoint, and proxy) from the active config or a secondary profile, then
    exposes that volatile tuple only to exact code-owned consumers. The skill
    executor separately filters ambient secret-shaped variables, so a project
    or workspace cannot redirect a discovered credential. The returned mapping
    stays behind an in-memory callable; it must not enter turn metadata,
    transcripts, or persisted run inputs.
    """

    consumers = trusted_capability_consumers_for_meta_plan(
        parent_spec,
        plan,
        skill_resolver=skill_resolver,
    )
    return capability_runtime_env_for_consumers(
        config,
        consumers,
        parent_spec=parent_spec,
        plan=plan,
        session_key=session_key,
        skill_resolver=skill_resolver,
    )


def meta_readiness_context(
    *,
    config: Any | None = None,
    env_aliases: object = (),
    parent_spec: Any | None = None,
    plan: Any | None = None,
    skill_resolver: Any | None = None,
) -> EligibilityContext:
    """Build an eligibility context with trusted, non-secret env aliases.

    ``env_aliases`` is used at the Agent boundary, where TurnRunner already
    resolved the live Gateway config and forwards names only.  Unknown names
    are ignored so arbitrary Agent metadata cannot satisfy unrelated manifest
    requirements.
    """

    trusted_consumers = trusted_capability_consumers_for_meta_plan(
        parent_spec,
        plan,
        skill_resolver=skill_resolver,
    )
    aliases = set(
        configured_meta_readiness_env_aliases(
            config,
            parent_spec=parent_spec,
            plan=plan,
            skill_resolver=skill_resolver,
        )
        if config is not None and trusted_consumers
        else ()
    )
    if isinstance(env_aliases, Iterable) and not isinstance(
        env_aliases, (str, bytes, Mapping)
    ):
        supported_aliases = frozenset(capability_supported_readiness_env_aliases())
        aliases.update(
            name
            for name in env_aliases
                if (
                    trusted_consumers
                    and isinstance(name, str)
                    and name in supported_aliases
                )
            )
    ctx = EligibilityContext.auto()
    # ``EligibilityContext`` carries strings for ordinary environment values.
    # This fixed marker preserves that contract without copying any credential.
    # Provider-capability children never inherit ambient credentials during a
    # MetaSkill run.  Force their portable direct-CLI names absent, then mark
    # only aliases proven by the trusted parent+plan provider connection.
    ctx.env_cache.update(
        {name: None for name in capability_ambient_credential_env_names()}
    )
    ctx.env_cache.update({name: _CONFIGURED_ENV_SENTINEL for name in aliases})
    return ctx


@dataclass(frozen=True)
class MetaSetupAction:
    """One trusted dependency action that can satisfy part of a setup gate.

    The owning skill and manifest install id are returned instead of a URL or
    shell command.  The server re-resolves both values before installation, so
    clients cannot turn this additive projection into arbitrary execution.
    """

    id: str
    skill: str
    install_id: str
    kind: str
    label: str
    bins: tuple[str, ...]
    available: bool = True
    reason: str = ""
    version: str = ""
    download_size_bytes: int | None = None
    download_size_is_minimum: bool = False
    source: str = ""
    license: str = ""
    requires_admin: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "skill": self.skill,
            "install_id": self.install_id,
            "kind": self.kind,
            "label": self.label,
            "bins": list(self.bins),
            "available": self.available,
            "reason": self.reason,
            "version": self.version,
            "download_size_bytes": self.download_size_bytes,
            "download_size_is_minimum": self.download_size_is_minimum,
            "source": self.source,
            "license": self.license,
            "requires_admin": self.requires_admin,
        }


@dataclass(frozen=True)
class MetaManualSetupAction:
    """One user-driven setup action; never executable by ``meta.setup.install``."""

    id: str
    kind: str
    provider_id: str
    capability_ids: tuple[str, ...]
    reason_code: str
    label: str
    recommended: bool = True
    available: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "provider_id": self.provider_id,
            "capability_ids": list(self.capability_ids),
            "reason_code": self.reason_code,
            "label": self.label,
            "recommended": self.recommended,
            "available": self.available,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MetaSkillReadiness:
    """Launch readiness projected onto RPC, CLI, and agent surfaces."""

    ready: bool
    missing_bins: tuple[str, ...] = ()
    missing_env: tuple[str, ...] = ()
    missing_env_any: tuple[tuple[str, ...], ...] = ()
    missing_skills: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()
    missing_provider_capabilities: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    setup_actions: tuple[MetaSetupAction, ...] = ()
    manual_setup_actions: tuple[MetaManualSetupAction, ...] = ()

    @property
    def status(self) -> str:
        return "ready" if self.ready else "needs_setup"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "status": self.status,
            "missing_bins": list(self.missing_bins),
            "missing_env": list(self.missing_env),
            "missing_env_any": [list(group) for group in self.missing_env_any],
            "missing_skills": list(self.missing_skills),
            "missing_capabilities": list(self.missing_capabilities),
            "missing_provider_capabilities": list(
                self.missing_provider_capabilities
            ),
            "reasons": list(self.reasons),
            "setup_actions": [action.to_dict() for action in self.setup_actions],
            "manual_setup_actions": [
                action.to_dict() for action in self.manual_setup_actions
            ],
        }


@dataclass
class _ReadinessCollector:
    missing_bins: set[str] = field(default_factory=set)
    missing_env: set[str] = field(default_factory=set)
    missing_env_any: set[tuple[str, ...]] = field(default_factory=set)
    missing_skills: set[str] = field(default_factory=set)
    missing_capabilities: set[str] = field(default_factory=set)
    missing_provider_capabilities: set[str] = field(default_factory=set)
    reasons: set[str] = field(default_factory=set)
    setup_actions: dict[str, MetaSetupAction] = field(default_factory=dict)
    manual_setup_actions: dict[str, MetaManualSetupAction] = field(default_factory=dict)


def assess_meta_skill_readiness(
    spec: SkillSpec,
    *,
    loader: Any | None = None,
    skill_index: Mapping[str, SkillSpec] | None = None,
    ctx: EligibilityContext | None = None,
    verify_capabilities: bool = True,
    config: Any | None = None,
    validated_plan: Any | None = None,
) -> MetaSkillReadiness:
    """Return declared dependency readiness for ``spec`` and its sub-skills.

    ``skill_index`` lets callers pin the assessment to the same catalog
    generation used for listing.  ``loader`` remains available for direct
    agent invocation, where ``get_by_name`` is the established lookup boundary.
    """

    parent_plan = validated_plan
    if parent_plan is None:
        try:
            from opensquilla.skills.meta.parser import MetaPlanError, parse_meta_plan

            parent_plan = parse_meta_plan(spec)
        except (MetaPlanError, TypeError, ValueError):
            parent_plan = None
    capability_skill_resolver: Any = skill_index if skill_index is not None else loader
    trusted_consumers = set(
        trusted_capability_consumers_for_meta_plan(
            spec,
            parent_plan,
            skill_resolver=capability_skill_resolver,
        )
    )

    base_ctx = ctx or EligibilityContext.auto()
    trusted_aliases: set[str] = set()
    supported_aliases = capability_supported_readiness_env_aliases()
    provider_capability_skills = frozenset(capability_registered_consumers())
    provider_capability_env_names = capability_ambient_credential_env_names()
    if trusted_consumers:
        if config is not None:
            trusted_aliases.update(
                configured_meta_readiness_env_aliases(
                    config,
                    parent_spec=spec,
                    plan=parent_plan,
                    skill_resolver=capability_skill_resolver,
                )
            )
        trusted_aliases.update(
            name
            for name in supported_aliases
            if base_ctx.env_cache.get(name) == _CONFIGURED_ENV_SENTINEL
        )
    eligibility_ctx = replace(
        base_ctx,
        env_cache={
            **base_ctx.env_cache,
            **{name: None for name in provider_capability_env_names},
            **{
                name: _CONFIGURED_ENV_SENTINEL
                for name in trusted_aliases
            },
        },
    )
    if not verify_capabilities:
        eligibility_ctx.passive_managed_bins = True
    collector = _ReadinessCollector()
    provider_workflow_trust_failed = (
        getattr(spec, "name", None) in {"meta-short-drama", "AwesomeWebpageMetaSkill"}
        and getattr(spec, "layer", None) == SkillLayer.BUNDLED
        and parent_plan is not None
        and not trusted_consumers
    )
    if provider_workflow_trust_failed:
        collector.reasons.add(_TRUSTED_PROVIDER_WORKFLOW_OVERRIDDEN_REASON)
    seen: set[str] = set()

    def lookup(name: str) -> SkillSpec | None:
        if skill_index is not None:
            return skill_index.get(name)
        getter = getattr(loader, "get_by_name", None)
        if callable(getter):
            return cast(SkillSpec | None, getter(name))
        return None

    def visit(current: SkillSpec) -> None:
        if current.name in seen:
            return
        seen.add(current.name)
        is_bundled = getattr(current, "layer", None) == SkillLayer.BUNDLED

        current_ctx = eligibility_ctx
        portable_aliases = (
            capability_manifest_env_aliases_for_consumers((current.name,))
            if is_bundled
            and (
                (config is not None and current.name in trusted_consumers)
                or (
                    provider_workflow_trust_failed
                    and current.name in provider_capability_skills
                )
            )
            else ()
        )
        if portable_aliases:
            # These exact bundled clients keep portable direct-CLI env hints
            # in their manifests. Config-aware MetaSkill readiness replaces
            # that low-level gate with the provider-capability result below,
            # so RPC/UI payloads do not tell profile users to edit process env.
            current_ctx = replace(
                eligibility_ctx,
                env_cache={
                    **eligibility_ctx.env_cache,
                    **{name: _CONFIGURED_ENV_SENTINEL for name in portable_aliases},
                },
            )

        # Several narrow runtime tests use SkillSpec-like namespaces with no
        # platform metadata. They represent a dependency-free skill.
        if hasattr(current, "metadata"):
            report = diagnose_eligibility(current, current_ctx)
            if not report.eligible:
                collector.missing_bins.update(report.missing_bins)
                collector.missing_env.update(report.missing_env)
                collector.missing_env_any.update(
                    tuple(group) for group in report.missing_env_any
                )
                collector.reasons.update(report.reasons)
            _collect_setup_actions(
                current,
                missing_bins=set(report.missing_bins),
                ctx=current_ctx,
                collector=collector,
                verify_capabilities=verify_capabilities,
            )

        for child_name in _referenced_skill_names(current):
            child = lookup(child_name)
            if child is None or not is_skill_available_live(child_name):
                collector.missing_skills.add(child_name)
                collector.reasons.add(f"Unavailable sub-skill: {child_name}")
                continue
            visit(child)

    visit(spec)
    _collect_provider_readiness(
        consumers=trusted_consumers,
        config=config,
        collector=collector,
    )
    ready = not (
        collector.missing_bins
        or collector.missing_env
        or collector.missing_env_any
        or collector.missing_skills
        or collector.missing_capabilities
        or collector.missing_provider_capabilities
        or collector.reasons
    )
    return MetaSkillReadiness(
        ready=ready,
        missing_bins=tuple(sorted(collector.missing_bins)),
        missing_env=tuple(sorted(collector.missing_env)),
        missing_env_any=tuple(sorted(collector.missing_env_any)),
        missing_skills=tuple(sorted(collector.missing_skills)),
        missing_capabilities=tuple(sorted(collector.missing_capabilities)),
        missing_provider_capabilities=tuple(
            sorted(collector.missing_provider_capabilities)
        ),
        reasons=tuple(sorted(collector.reasons)),
        setup_actions=tuple(
            collector.setup_actions[key] for key in sorted(collector.setup_actions)
        ),
        manual_setup_actions=tuple(
            collector.manual_setup_actions[key]
            for key in sorted(collector.manual_setup_actions)
        ),
    )


def _collect_provider_readiness(
    *,
    consumers: set[str],
    config: Any | None,
    collector: _ReadinessCollector,
) -> None:
    """Project provider requirements separately from local install actions."""

    if config is None:
        return
    missing_by_provider: dict[str, list[tuple[str, str]]] = {}
    for requirement in capability_requirements_for_consumers(consumers):
        status = resolve_capability_status(config, requirement)
        if status.ready:
            continue
        collector.missing_provider_capabilities.add(requirement.capability_id)
        missing_by_provider.setdefault(status.provider_id, []).append(
            (requirement.capability_id, status.reason_code or "missing_connection")
        )

    reason_labels = {
        "missing_credential": "A provider credential is required.",
        "credential_pool_exhausted": "Configured provider credentials are temporarily unavailable.",
        "credential_endpoint_mismatch": "The credential does not match the configured endpoint.",
        "missing_base_url": "A provider endpoint is required.",
        "invalid_endpoint": "The configured provider endpoint is invalid.",
        "invalid_proxy": "The configured provider proxy is invalid.",
        "runtime_unsupported": "This provider is not available in the current runtime.",
        "unknown_provider": "The configured provider is not recognized.",
        "unsupported_profile_preference": (
            "This capability's provider profile preference is not supported."
        ),
    }
    for provider_id, missing in sorted(missing_by_provider.items()):
        capability_ids = tuple(sorted({capability for capability, _ in missing}))
        reason_code = missing[0][1]
        action_id = f"provider:{provider_id}"
        collector.manual_setup_actions[action_id] = MetaManualSetupAction(
            id=action_id,
            kind="provider_connection",
            provider_id=provider_id,
            capability_ids=capability_ids,
            reason_code=reason_code,
            label=capability_provider_display_name(provider_id) or provider_id,
            reason=reason_labels.get(reason_code, "A provider connection is required."),
        )
        collector.reasons.add(
            f"Provider {provider_id!r} is not ready for: {', '.join(capability_ids)}"
        )


def _collect_setup_actions(
    spec: SkillSpec,
    *,
    missing_bins: set[str],
    ctx: EligibilityContext,
    collector: _ReadinessCollector,
    verify_capabilities: bool,
) -> None:
    """Project applicable manifest installers for the binaries still missing."""

    metadata = getattr(spec, "metadata", None)
    if metadata is None:
        return
    for install in metadata.install:
        covered = tuple(sorted(set(install.bins) & missing_bins))
        action_id = f"{spec.name}:{install.id}"
        if action_id in collector.setup_actions:
            continue

        available = not install.os or not ctx.os_name or ctx.os_name in install.os
        reason = ""
        version = ""
        size: int | None = None
        size_is_minimum = False
        source = ""
        license_name = ""
        capability_missing = False

        if not available:
            reason = (
                f"Installer is unavailable on {ctx.os_name or 'this platform'} "
                f"(supports: {', '.join(install.os)})"
            )
        elif install.kind == "toolchain":
            try:
                from opensquilla.skills.toolchains import (
                    describe_component,
                    probe_component,
                    trusted_brew_executable,
                )

                descriptor = describe_component(install.id)
                available = descriptor.supported
                reason = descriptor.unsupported_reason or ""
                version = descriptor.version
                size = descriptor.total_download_size
                size_is_minimum = bool(descriptor.closure_source)
                if size is None and descriptor.auxiliary_assets:
                    # External package managers do not expose a stable bottle
                    # size. Still disclose OpenSquilla's pinned downloads as a
                    # clearly marked minimum.
                    size = sum(asset.size for asset in descriptor.auxiliary_assets)
                    size_is_minimum = True
                source = descriptor.source
                license_name = descriptor.license
                if (
                    available
                    and getattr(descriptor, "install_backend", "archive") == "brew"
                    and trusted_brew_executable() is None
                ):
                    available = False
                    reason = (
                        "Homebrew is required for this managed installer but was not "
                        "found in a trusted installation location"
                    )
                if verify_capabilities:
                    capability = probe_component(install.id)
                    if not capability.ready:
                        capability_missing = True
                        collector.missing_capabilities.add(install.id)
                        collector.reasons.add(
                            f"Runtime capability {install.id!r} is not ready: "
                            f"{capability.reason}"
                        )
                        if available:
                            reason = capability.reason
            except (AttributeError, ImportError, KeyError, RuntimeError, ValueError) as exc:
                available = False
                reason = str(exc) or "Managed toolchain is unavailable"
                capability_missing = True
                collector.missing_capabilities.add(install.id)
                collector.reasons.add(
                    f"Runtime capability {install.id!r} could not be checked: {reason}"
                )
        elif install.kind == "brew":
            source = "Homebrew"
            available = shutil.which("brew") is not None
            if not available:
                reason = "Homebrew is not installed"

        if not covered and not capability_missing:
            continue

        next_action = MetaSetupAction(
            id=action_id,
            skill=spec.name,
            install_id=install.id,
            kind=install.kind,
            label=install.label or f"Install {', '.join(covered)}",
            bins=covered or tuple(sorted(set(install.bins))),
            available=available,
            reason=reason,
            version=version,
            download_size_bytes=size,
            download_size_is_minimum=size_is_minimum,
            source=source,
            license=license_name,
        )
        if install.kind == "toolchain":
            # A parent MetaSkill and one or more internal children may declare
            # the same globally registered managed component. Presenting each
            # manifest declaration as a separate action would ask the user to
            # install the same artifact twice and the setup job would execute
            # it twice. Keep the first (parent-first traversal) trusted action
            # identity and merge only the binary coverage for that component.
            duplicate_id = next(
                (
                    existing_id
                    for existing_id, existing in collector.setup_actions.items()
                    if existing.kind == "toolchain"
                    and existing.install_id == install.id
                ),
                "",
            )
            if duplicate_id:
                existing = collector.setup_actions[duplicate_id]
                collector.setup_actions[duplicate_id] = replace(
                    existing,
                    bins=tuple(sorted(set(existing.bins) | set(next_action.bins))),
                )
                continue

        collector.setup_actions[action_id] = next_action


def format_meta_setup_error(name: str, readiness: MetaSkillReadiness) -> str:
    """Build a compact fallback error for clients that only render ``error``."""

    missing: list[str] = []
    missing.extend(readiness.missing_bins)
    missing.extend(readiness.missing_env)
    missing.extend(" or ".join(group) for group in readiness.missing_env_any)
    missing.extend(readiness.missing_skills)
    missing.extend(readiness.missing_capabilities)
    missing.extend(readiness.missing_provider_capabilities)
    detail = f": {', '.join(missing)}" if missing else ""
    return f"Meta-skill {name!r} requires setup before it can run{detail}"


def _referenced_skill_names(spec: SkillSpec) -> list[str]:
    composition = getattr(spec, "composition_raw", None)
    if not isinstance(composition, dict):
        return []
    steps = composition.get("steps")
    if not isinstance(steps, list):
        return []

    names: list[str] = []
    seen: set[str] = set()

    def append(raw: object) -> None:
        if isinstance(raw, str) and raw and raw not in seen:
            seen.add(raw)
            names.append(raw)

    for step in steps:
        if not isinstance(step, dict):
            continue

        # ``skill`` and conditional ``route[].to`` are execution targets only
        # for the two step kinds that dispatch a skill.  Other kinds may carry
        # an informational ``skill`` value, and treating it as a dependency
        # would incorrectly block an otherwise runnable meta-skill.
        kind = step.get("kind", "agent")
        if kind in {"agent", "skill_exec"}:
            append(step.get("skill"))
            route_cases = step.get("route")
            if isinstance(route_cases, list):
                for route_case in route_cases:
                    if isinstance(route_case, dict):
                        append(route_case.get("to"))

        # Retain the pre-MVP route projection used by older manifests and
        # dependency summaries.  Its explicit ``skill`` field unambiguously
        # denotes a sub-skill even when the enclosing step is a classifier.
        routes = step.get("routes")
        if isinstance(routes, list):
            for route in routes:
                if isinstance(route, dict):
                    append(route.get("skill"))
    return names
