"""Shared SKILL.md parser/compiler and Community candidate validation.

Trusted local and bundled skills deliberately retain the historical tolerant
compiler. Community artifacts use an instruction-only projection: portable
instructions and dependency declarations survive, while host-specific
execution extensions remain inert and are reported as compatibility advice.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import yaml

from opensquilla.skills.types import (
    SkillInstallSpec,
    SkillLayer,
    SkillPlatformMeta,
    SkillProvenance,
    SkillRequires,
    SkillSpec,
)

MAX_SKILL_FILE_BYTES = 256_000
MAX_STANDARD_SKILL_NAME_LENGTH = 64
MAX_STANDARD_SKILL_DESCRIPTION_LENGTH = 1_024
_MAX_STRICT_YAML_NESTING = 64

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_INLINE_DYNAMIC_CONTEXT_RE = re.compile(r"(?<!\\)!`[^`\r\n]+`")
_FENCED_DYNAMIC_CONTEXT_RE = re.compile(r"(?m)^[ \t]*```![ \t]*\r?$")
_UNSUPPORTED_DIALECT_FIELDS = frozenset(
    {
        "always",
        "triggers",
        "hooks",
        "agent",
        "plugin",
        "plugins",
        "mcp",
        "mcp-servers",
        "mcpservers",
        "skills",
        "model",
        "effort",
        "shell",
        "command",
        "command-dispatch",
        "commanddispatch",
        "command-tool",
        "commandtool",
        "command-arg-mode",
        "commandargmode",
        "entrypoint",
        "composition",
        "requires-tools",
        "fallback-for-toolsets",
        "meta-priority",
        "final-text-mode",
        "request-template",
        "output-contract",
        "eval-prompts",
        "preference-keys",
        "policy-tags",
    }
)
_DEGRADED_DIALECT_FIELDS = frozenset({"allowed-tools", "allowedtools"})


class SkillCompileProfile(StrEnum):
    """Select the trust boundary used while compiling one manifest."""

    TRUSTED = "trusted"
    COMMUNITY_INSTRUCTION = "community-instruction-v1"


@dataclass(frozen=True)
class SkillManifestValidation:
    """Result of validating one not-yet-installed Community skill directory.

    Diagnostics intentionally use a narrow mapping rather than the broader
    service/Doctor diagnostic contract.  The install service can translate
    these manifest-local findings at its boundary without coupling the loader
    catalog to a lifecycle wire schema.
    """

    spec: SkillSpec | None
    diagnostics: tuple[dict[str, str], ...] = ()
    compatibility_diagnostics: tuple[dict[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return self.spec is not None and not self.diagnostics


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader for bounded, unambiguous Community frontmatter."""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._compose_depth = 0

    def compose_node(
        self,
        parent: yaml.nodes.Node | None,
        index: object,
    ) -> yaml.nodes.Node:
        event = self.peek_event()
        if isinstance(event, yaml.events.AliasEvent):
            raise yaml.composer.ComposerError(
                None,
                None,
                "YAML aliases are not allowed in Community Skill frontmatter",
                event.start_mark,
            )
        if self._compose_depth >= _MAX_STRICT_YAML_NESTING:
            raise yaml.composer.ComposerError(
                None,
                None,
                (
                    "Community Skill frontmatter exceeds the maximum YAML "
                    f"nesting depth of {_MAX_STRICT_YAML_NESTING}"
                ),
                event.start_mark,
            )
        self._compose_depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._compose_depth -= 1


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _explicit_bool(value: object, *, default: bool) -> bool:
    """Parse the boolean spellings accepted by common Skill frontmatter.

    ``bool("false")`` is true in Python and previously enabled optional Skill
    capabilities accidentally. Community projection must never use truthiness
    for values that control catalog visibility or invocation.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    return default


def _is_explicit_bool(value: object) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return value in {0, 1}
    return isinstance(value, str) and value.strip().casefold() in {
        "true",
        "yes",
        "on",
        "1",
        "false",
        "no",
        "off",
        "0",
    }


def _community_string_list(value: object) -> list[str]:
    """Return only non-empty strings from an untrusted list declaration."""

    if isinstance(value, str):
        item = value.strip()
        return [item] if item else []
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _community_platform_mapping(frontmatter: dict) -> dict[object, object]:
    """Resolve the same metadata namespaces as trusted manifests, defensively."""

    raw_meta = frontmatter.get("metadata", {})
    if not isinstance(raw_meta, dict):
        return {}
    base_meta = raw_meta.get(
        "platform",
        raw_meta.get("openclaw", raw_meta.get("clawdbot", raw_meta)),
    )
    merged = dict(base_meta) if isinstance(base_meta, dict) else {}
    opensquilla_meta = raw_meta.get("opensquilla")
    if isinstance(opensquilla_meta, dict):
        # Community projection imports only descriptive/dependency metadata.
        # Execution and policy fields (always, risk, capabilities, etc.) do not
        # cross this trust boundary.
        for key in (
            "emoji",
            "skillKey",
            "primaryEnv",
            "homepage",
            "os",
            "requires",
            "install",
        ):
            if key in opensquilla_meta:
                merged[key] = opensquilla_meta[key]
    return merged


def _community_install_specs(raw: object) -> list[SkillInstallSpec]:
    """Project dependency hints without admitting command-shaped extensions."""

    if not isinstance(raw, list):
        return []
    projected: list[SkillInstallSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        def text(field: str) -> str:
            value = item.get(field, "")
            return value.strip() if isinstance(value, str) else ""

        kind = text("kind").casefold()
        if kind not in {"brew", "node", "npm", "go", "uv", "download", "toolchain"}:
            continue
        # Normalize the common npm spelling to the executor's node kind. The
        # eventual dependency mutation still applies its own strict allowlist.
        if kind == "npm":
            kind = "node"
        projected.append(
            SkillInstallSpec(
                kind=kind,
                id=text("id"),
                label=text("label"),
                bins=_community_string_list(item.get("bins", [])),
                os=_community_string_list(item.get("os", [])),
                formula=text("formula"),
                package=text("package"),
                module=text("module"),
                url=text("url"),
            )
        )
    return projected


def resolve_community_skill_metadata(frontmatter: dict) -> SkillPlatformMeta | None:
    """Extract inert metadata needed for readiness and explicit dependency setup."""

    raw_meta = _community_platform_mapping(frontmatter)
    if not raw_meta:
        return None
    raw_requires = raw_meta.get("requires")
    requires = None
    if isinstance(raw_requires, dict):
        bins_value = raw_requires.get("bins")
        if bins_value is None:
            bins_value = raw_requires.get("commands", [])
        requires = SkillRequires(
            bins=_community_string_list(bins_value),
            any_bins=_community_string_list(raw_requires.get("anyBins", [])),
            env=_community_string_list(raw_requires.get("env", [])),
            env_any=_community_string_list(raw_requires.get("envAny", [])),
            config=_community_string_list(raw_requires.get("config", [])),
        )

    def text(field: str) -> str:
        value = raw_meta.get(field, "")
        return value.strip() if isinstance(value, str) else ""

    return SkillPlatformMeta(
        emoji=text("emoji"),
        skill_key=text("skillKey"),
        primary_env=text("primaryEnv"),
        homepage=text("homepage"),
        always=None,
        os=_community_string_list(raw_meta.get("os", [])),
        requires=requires,
        install=_community_install_specs(raw_meta.get("install", [])),
        # Community-declared risk/capabilities are advisory upstream values,
        # not authority to activate local execution paths.
        risk_level="",
        capabilities=[],
    )


def _validated_skill_name(value: object) -> str:
    """Return a usable catalog key or reject malformed source/cache data."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("skill name must be a non-empty string")
    return value


def parse_skill_frontmatter(text: str) -> tuple[dict, str]:
    """Parse frontmatter with the loader's historical tolerant behaviour.

    Invalid or missing frontmatter is represented as an empty mapping and the
    original text.  The compiler subsequently rejects it because ``name`` is
    absent.  Keeping this behaviour is important for last-known-good loading
    of existing local skills.
    """

    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    fm_text, body = match.groups()
    try:
        frontmatter = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}, text

    if not isinstance(frontmatter, dict):
        return {}, text

    return frontmatter, body.strip()


def _parse_skill_frontmatter_strict(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("SKILL.md must start with YAML frontmatter delimited by ---")

    fm_text, body = match.groups()
    try:
        frontmatter = yaml.load(fm_text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise ValueError("frontmatter must be a mapping")
    return frontmatter, body.strip()


def resolve_skill_metadata(frontmatter: dict) -> SkillPlatformMeta | None:
    """Extract OpenSquilla/OpenClaw-compatible platform metadata."""

    raw_meta = frontmatter.get("metadata", {})
    if isinstance(raw_meta, dict):
        # Namespace fallback: platform > openclaw > clawdbot > top-level.
        # `opensquilla` overlays advisory fields without erasing upstream
        # dependency metadata.
        base_meta = raw_meta.get(
            "platform",
            raw_meta.get("openclaw", raw_meta.get("clawdbot", raw_meta)),
        )
        if not isinstance(base_meta, dict):
            base_meta = {}
        merged_meta = dict(base_meta)
        opensquilla_meta = raw_meta.get("opensquilla", {})
        if isinstance(opensquilla_meta, dict):
            for key in (
                "emoji",
                "skillKey",
                "primaryEnv",
                "homepage",
                "always",
                "os",
                "requires",
                "install",
                "risk",
                "risk_level",
                "riskLevel",
                "capabilities",
            ):
                if key in opensquilla_meta:
                    merged_meta[key] = opensquilla_meta[key]
        raw_meta = merged_meta
    if not isinstance(raw_meta, dict):
        return None

    requires = None
    raw_req = raw_meta.get("requires", {})
    if isinstance(raw_req, dict):
        # ClawHub frontmatter sometimes uses commands instead of bins.
        bins_value = raw_req.get("bins")
        if bins_value is None:
            bins_value = raw_req.get("commands", [])
        requires = SkillRequires(
            bins=bins_value if isinstance(bins_value, list) else [],
            any_bins=raw_req.get("anyBins", []),
            env=raw_req.get("env", []),
            env_any=raw_req.get("envAny", []),
            config=raw_req.get("config", []),
        )

    install_specs: list[SkillInstallSpec] = []
    for item in raw_meta.get("install", []):
        if isinstance(item, dict):
            install_specs.append(
                SkillInstallSpec(
                    kind=item.get("kind", ""),
                    id=item.get("id", ""),
                    label=item.get("label", ""),
                    bins=item.get("bins", []),
                    os=item.get("os", []),
                    formula=item.get("formula", ""),
                    package=item.get("package", ""),
                    module=item.get("module", ""),
                    url=item.get("url", ""),
                )
            )

    always_val = raw_meta.get("always")
    return SkillPlatformMeta(
        emoji=raw_meta.get("emoji", ""),
        skill_key=raw_meta.get("skillKey", ""),
        primary_env=raw_meta.get("primaryEnv", ""),
        homepage=raw_meta.get("homepage", ""),
        always=bool(always_val) if always_val is not None else None,
        os=_string_list(raw_meta.get("os", [])),
        requires=requires,
        install=install_specs,
        risk_level=str(
            raw_meta.get("risk") or raw_meta.get("risk_level") or raw_meta.get("riskLevel") or ""
        )
        .strip()
        .lower(),
        capabilities=_string_list(raw_meta.get("capabilities", [])),
    )


def resolve_skill_provenance(frontmatter: dict) -> SkillProvenance:
    """Extract provenance metadata from top-level frontmatter."""

    raw = frontmatter.get("provenance", {})
    if not isinstance(raw, dict):
        raw = {}
    return SkillProvenance(
        origin=str(raw.get("origin") or "unknown"),
        license=str(raw.get("license") or "unknown"),
        upstream_url=str(raw.get("upstream_url") or ""),
        maintained_by=str(raw.get("maintained_by") or "OpenSquilla"),
    )


def skill_instance_id(*, layer: SkillLayer, file_path: str) -> str:
    """Return an opaque, stable identity for one physical catalog instance."""

    normalized = os.path.abspath(file_path)
    digest = hashlib.sha256(f"{layer.value}\0{normalized}".encode()).hexdigest()
    return f"{layer.value}:{digest}"


def _compile_community_instruction_manifest(
    skill_dir: Path,
    layer: SkillLayer,
    frontmatter: dict,
    body: str,
) -> SkillSpec:
    """Project an untrusted Community manifest onto inert instruction semantics."""

    raw_name = _validated_skill_name(frontmatter.get("name"))
    name = raw_name.strip()
    raw_description = frontmatter.get("description", "")
    description = raw_description if isinstance(raw_description, str) else ""
    raw_description_zh = frontmatter.get("description_zh", "")
    description_zh = raw_description_zh if isinstance(raw_description_zh, str) else ""
    metadata = resolve_community_skill_metadata(frontmatter)
    provenance = resolve_skill_provenance(frontmatter)
    homepage_raw = frontmatter.get("homepage", "")
    homepage = homepage_raw.strip() if isinstance(homepage_raw, str) else ""
    if not homepage and metadata is not None:
        homepage = metadata.homepage

    file_path = os.path.abspath(skill_dir / "SKILL.md")
    return SkillSpec(
        name=name,
        description=description,
        description_zh=description_zh,
        layer=layer,
        # Third-party Skills cannot become ambient/always-on prompt content.
        always=False,
        triggers=[],
        content=body,
        path=skill_dir,
        metadata=metadata,
        provenance=provenance,
        user_invocable=_explicit_bool(
            frontmatter.get("user-invocable", True),
            default=True,
        ),
        disable_model_invocation=_explicit_bool(
            frontmatter.get("disable-model-invocation", False),
            default=False,
        ),
        homepage=homepage,
        file_path=file_path,
        base_dir=str(skill_dir.resolve()),
        # All OpenSquilla-native execution and meta-orchestration fields are
        # deliberately empty at this trust boundary.
        requires_tools=[],
        fallback_for_toolsets=[],
        kind="skill",
        meta_priority=0,
        composition_raw=None,
        final_text_mode="auto",
        request_template={},
        output_contract={},
        eval_prompts=[],
        preference_keys=[],
        policy_tags=[],
        entrypoint=None,
        instance_id=skill_instance_id(layer=layer, file_path=file_path),
    )


def compile_skill_manifest(
    skill_dir: Path,
    layer: SkillLayer,
    *,
    skill_bytes: bytes | None = None,
    profile: SkillCompileProfile = SkillCompileProfile.TRUSTED,
) -> SkillSpec:
    """Compile one SKILL.md using the requested trust-boundary profile.

    The default intentionally preserves historical tolerant semantics for
    trusted local layers. ``COMMUNITY_INSTRUCTION`` uses strict, unambiguous
    YAML parsing and strips every host-executable extension.
    """

    skill_file = skill_dir / "SKILL.md"
    if skill_bytes is None:
        with skill_file.open("rb") as handle:
            skill_bytes = handle.read(MAX_SKILL_FILE_BYTES + 1)
    if len(skill_bytes) > MAX_SKILL_FILE_BYTES:
        raise ValueError(f"SKILL.md exceeds {MAX_SKILL_FILE_BYTES} bytes")

    text = skill_bytes.decode("utf-8")
    if profile is SkillCompileProfile.COMMUNITY_INSTRUCTION:
        frontmatter, body = _parse_skill_frontmatter_strict(text)
    else:
        frontmatter, body = parse_skill_frontmatter(text)
    if not frontmatter or "name" not in frontmatter:
        raise ValueError("SKILL.md has no usable frontmatter name")

    if profile is SkillCompileProfile.COMMUNITY_INSTRUCTION:
        return _compile_community_instruction_manifest(
            skill_dir,
            layer,
            frontmatter,
            body,
        )

    name = _validated_skill_name(frontmatter["name"])
    description = frontmatter.get("description", "")
    description_zh = frontmatter.get("description_zh", "") or ""

    always_raw = frontmatter.get("always", False)
    always = bool(always_raw) if always_raw is not None else False

    triggers = frontmatter.get("triggers", [])
    if not isinstance(triggers, list):
        triggers = [str(triggers)]

    metadata = resolve_skill_metadata(frontmatter)
    provenance = resolve_skill_provenance(frontmatter)
    if metadata and metadata.always is not None:
        always = metadata.always

    user_invocable = frontmatter.get("user-invocable", True)
    disable_model_invocation = frontmatter.get("disable-model-invocation", False)
    homepage = frontmatter.get("homepage", "")

    activation_meta: dict[str, Any] = {}
    raw_meta_dict = frontmatter.get("metadata", {})
    if isinstance(raw_meta_dict, dict):
        raw_activation_meta = raw_meta_dict.get("opensquilla", {})
        if isinstance(raw_activation_meta, dict):
            activation_meta = cast(dict[str, Any], raw_activation_meta)
    requires_tools = activation_meta.get("requires_tools", [])
    fallback_for_toolsets = activation_meta.get("fallback_for_toolsets", [])

    kind_raw = frontmatter.get("kind", "skill")
    kind = str(kind_raw) if isinstance(kind_raw, str) else "skill"
    meta_priority_raw = frontmatter.get("meta_priority", 0)
    try:
        meta_priority = int(meta_priority_raw) if meta_priority_raw is not None else 0
    except (TypeError, ValueError):
        meta_priority = 0
    composition_raw = frontmatter.get("composition")
    if not isinstance(composition_raw, dict):
        composition_raw = None

    entrypoint_raw = frontmatter.get("entrypoint")
    entrypoint = entrypoint_raw if isinstance(entrypoint_raw, dict) else None

    final_text_mode_raw = frontmatter.get("final_text_mode", "auto")
    final_text_mode = (
        str(final_text_mode_raw).strip() if final_text_mode_raw else "auto"
    ) or "auto"
    request_template_raw = frontmatter.get("request_template")
    request_template = dict(request_template_raw) if isinstance(request_template_raw, dict) else {}
    output_contract_raw = frontmatter.get("output_contract")
    output_contract = dict(output_contract_raw) if isinstance(output_contract_raw, dict) else {}
    eval_prompts_raw = frontmatter.get("eval_prompts")
    eval_prompts = (
        [dict(item) for item in eval_prompts_raw if isinstance(item, dict)]
        if isinstance(eval_prompts_raw, list)
        else []
    )
    preference_keys = _string_list(frontmatter.get("preference_keys", []))
    policy_tags = _string_list(frontmatter.get("policy_tags", []))

    file_path = os.path.abspath(skill_file)
    return SkillSpec(
        name=name,
        description=description,
        description_zh=str(description_zh),
        layer=layer,
        always=always,
        triggers=triggers,
        content=body,
        path=skill_dir,
        metadata=metadata,
        provenance=provenance,
        user_invocable=user_invocable,
        disable_model_invocation=disable_model_invocation,
        homepage=homepage,
        file_path=file_path,
        base_dir=str(skill_dir.resolve()),
        requires_tools=requires_tools if isinstance(requires_tools, list) else [],
        fallback_for_toolsets=(
            fallback_for_toolsets if isinstance(fallback_for_toolsets, list) else []
        ),
        kind=kind,
        meta_priority=meta_priority,
        composition_raw=composition_raw,
        final_text_mode=final_text_mode,
        request_template=request_template,
        output_contract=output_contract,
        eval_prompts=eval_prompts,
        preference_keys=preference_keys,
        policy_tags=policy_tags,
        entrypoint=entrypoint,
        instance_id=skill_instance_id(layer=layer, file_path=file_path),
    )


def _diagnostic(
    code: str,
    message: str,
    *,
    path: Path,
    field: str = "",
) -> dict[str, str]:
    return {
        "code": code,
        "message": message,
        "path": str(path),
        "field": field,
    }


def _validate_string_list(
    diagnostics: list[dict[str, str]],
    value: object,
    *,
    field: str,
    path: Path,
) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        diagnostics.append(
            _diagnostic(
                "FIELD_TYPE_INVALID",
                f"{field} must be a list of strings",
                path=path,
                field=field,
            )
        )


def _validate_platform_metadata_mapping(
    diagnostics: list[dict[str, str]],
    metadata: dict[object, object],
    *,
    prefix: str,
    path: Path,
) -> None:
    """Validate the metadata fields the tolerant compiler actually consumes."""

    for key in (
        "emoji",
        "skillKey",
        "primaryEnv",
        "homepage",
        "risk",
        "risk_level",
        "riskLevel",
    ):
        if key in metadata and not isinstance(metadata[key], str):
            diagnostics.append(
                _diagnostic(
                    "FIELD_TYPE_INVALID",
                    f"{prefix}.{key} must be a string",
                    path=path,
                    field=f"{prefix}.{key}",
                )
            )
    if "always" in metadata and not isinstance(metadata["always"], bool):
        diagnostics.append(
            _diagnostic(
                "FIELD_TYPE_INVALID",
                f"{prefix}.always must be a boolean",
                path=path,
                field=f"{prefix}.always",
            )
        )
    for key in ("os", "capabilities", "requires_tools", "fallback_for_toolsets"):
        if key in metadata:
            _validate_string_list(
                diagnostics,
                metadata[key],
                field=f"{prefix}.{key}",
                path=path,
            )

    requires = metadata.get("requires")
    if requires is not None and not isinstance(requires, dict):
        diagnostics.append(
            _diagnostic(
                "FIELD_TYPE_INVALID",
                f"{prefix}.requires must be a mapping",
                path=path,
                field=f"{prefix}.requires",
            )
        )
    elif isinstance(requires, dict):
        for key in ("bins", "commands", "anyBins", "env", "envAny", "config"):
            if key in requires:
                _validate_string_list(
                    diagnostics,
                    requires[key],
                    field=f"{prefix}.requires.{key}",
                    path=path,
                )

    installs = metadata.get("install")
    if installs is not None and (
        not isinstance(installs, list) or any(not isinstance(item, dict) for item in installs)
    ):
        diagnostics.append(
            _diagnostic(
                "FIELD_TYPE_INVALID",
                f"{prefix}.install must be a list of mappings",
                path=path,
                field=f"{prefix}.install",
            )
        )
    elif isinstance(installs, list):
        for index, item in enumerate(installs):
            assert isinstance(item, dict)
            item_prefix = f"{prefix}.install[{index}]"
            for key in ("kind", "id", "label", "formula", "package", "module", "url"):
                if key in item and not isinstance(item[key], str):
                    diagnostics.append(
                        _diagnostic(
                            "FIELD_TYPE_INVALID",
                            f"{item_prefix}.{key} must be a string",
                            path=path,
                            field=f"{item_prefix}.{key}",
                        )
                    )
            for key in ("bins", "os"):
                if key in item:
                    _validate_string_list(
                        diagnostics,
                        item[key],
                        field=f"{item_prefix}.{key}",
                        path=path,
                    )


def _validate_known_manifest_types(
    frontmatter: dict,
    *,
    path: Path,
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []

    for field in ("user-invocable", "disable-model-invocation", "always"):
        if field in frontmatter and not _is_explicit_bool(frontmatter[field]):
            diagnostics.append(
                _diagnostic(
                    "FIELD_TYPE_INVALID",
                    f"{field} has no recognized boolean value and will use its default",
                    path=path,
                    field=field,
                )
            )

    for field in ("description_zh", "homepage", "kind", "final_text_mode"):
        if field in frontmatter and not isinstance(frontmatter[field], str):
            diagnostics.append(
                _diagnostic(
                    "FIELD_TYPE_INVALID",
                    f"{field} must be a string",
                    path=path,
                    field=field,
                )
            )

    if "triggers" in frontmatter:
        _validate_string_list(
            diagnostics,
            frontmatter["triggers"],
            field="triggers",
            path=path,
        )

    for field in ("preference_keys", "policy_tags"):
        if field in frontmatter:
            _validate_string_list(
                diagnostics,
                frontmatter[field],
                field=field,
                path=path,
            )

    for field in (
        "composition",
        "entrypoint",
        "request_template",
        "output_contract",
        "provenance",
    ):
        if field in frontmatter and not isinstance(frontmatter[field], dict):
            diagnostics.append(
                _diagnostic(
                    "FIELD_TYPE_INVALID",
                    f"{field} must be a mapping",
                    path=path,
                    field=field,
                )
            )

    if "eval_prompts" in frontmatter and (
        not isinstance(frontmatter["eval_prompts"], list)
        or any(not isinstance(item, dict) for item in frontmatter["eval_prompts"])
    ):
        diagnostics.append(
            _diagnostic(
                "FIELD_TYPE_INVALID",
                "eval_prompts must be a list of mappings",
                path=path,
                field="eval_prompts",
            )
        )

    if "meta_priority" in frontmatter and (
        not isinstance(frontmatter["meta_priority"], int)
        or isinstance(frontmatter["meta_priority"], bool)
    ):
        diagnostics.append(
            _diagnostic(
                "FIELD_TYPE_INVALID",
                "meta_priority must be an integer",
                path=path,
                field="meta_priority",
            )
        )

    provenance = frontmatter.get("provenance")
    if isinstance(provenance, dict):
        for key in ("origin", "license", "upstream_url", "maintained_by"):
            if key in provenance and not isinstance(provenance[key], str):
                diagnostics.append(
                    _diagnostic(
                        "FIELD_TYPE_INVALID",
                        f"provenance.{key} must be a string",
                        path=path,
                        field=f"provenance.{key}",
                    )
                )

    metadata = frontmatter.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        diagnostics.append(
            _diagnostic(
                "FIELD_TYPE_INVALID",
                "metadata must be a mapping",
                path=path,
                field="metadata",
            )
        )
        return diagnostics

    if not isinstance(metadata, dict):
        return diagnostics
    # Direct metadata is the historical fallback. Namespaced mappings use the
    # same known field schema and are all checked even when precedence means a
    # different namespace will win; type pollution must never be hidden behind
    # an overlay.
    _validate_platform_metadata_mapping(
        diagnostics,
        metadata,
        prefix="metadata",
        path=path,
    )
    for namespace in ("platform", "openclaw", "clawdbot", "opensquilla"):
        value = metadata.get(namespace)
        if value is not None and not isinstance(value, dict):
            diagnostics.append(
                _diagnostic(
                    "FIELD_TYPE_INVALID",
                    f"metadata.{namespace} must be a mapping",
                    path=path,
                    field=f"metadata.{namespace}",
                )
            )
        elif isinstance(value, dict):
            _validate_platform_metadata_mapping(
                diagnostics,
                value,
                prefix=f"metadata.{namespace}",
                path=path,
            )

    return diagnostics


def _validate_unsupported_dialect_fields(
    frontmatter: dict,
    *,
    path: Path,
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []

    def inspect(
        mapping: dict[object, object],
        *,
        prefix: str = "",
        install_item: bool = False,
    ) -> None:
        for raw_field, value in mapping.items():
            if not isinstance(raw_field, str):
                continue
            normalized = raw_field.strip().lower().replace("_", "-")
            unsupported = normalized in _UNSUPPORTED_DIALECT_FIELDS
            if normalized in {"context", "execution-context", "executioncontext"}:
                unsupported = str(value).strip().lower() == "fork"
            if normalized == "kind" and not install_item:
                unsupported = str(value).strip().lower() not in {"", "skill"}
            if not unsupported:
                continue
            field = f"{prefix}.{raw_field}" if prefix else raw_field
            diagnostics.append(
                _diagnostic(
                    "DIALECT_FIELD_UNSUPPORTED",
                    (
                        f"{field} changes dialect-specific execution semantics "
                        "and will be ignored by the instruction-only Community loader"
                    ),
                    path=path,
                    field=field,
                )
            )

    def inspect_platform(mapping: dict[object, object], *, prefix: str) -> None:
        inspect(mapping, prefix=prefix)
        installs = mapping.get("install")
        if not isinstance(installs, list):
            return
        for index, item in enumerate(installs):
            if isinstance(item, dict):
                inspect(
                    item,
                    prefix=f"{prefix}.install[{index}]",
                    install_item=True,
                )

    inspect(frontmatter)
    metadata = frontmatter.get("metadata")
    if isinstance(metadata, dict):
        # These are the only nested mappings interpreted as platform runtime
        # declarations. Do not recurse through arbitrary descriptive extension
        # objects, nor through ``requires.commands`` (a supported legacy alias).
        inspect_platform(metadata, prefix="metadata")
        for namespace in ("platform", "openclaw", "clawdbot", "opensquilla"):
            value = metadata.get(namespace)
            if isinstance(value, dict):
                inspect_platform(value, prefix=f"metadata.{namespace}")
    return diagnostics


def _validate_degraded_dialect_fields(
    frontmatter: dict,
    *,
    path: Path,
) -> list[dict[str, str]]:
    """Report portable instructions whose host-specific conveniences are ignored.

    ``allowed-tools`` is a permission preapproval in supporting hosts, not a
    restriction on the tools a Skill may call. OpenSquilla keeps its ordinary
    approval policy, so accepting the instructions is safe but not native.
    """

    diagnostics: list[dict[str, str]] = []

    def inspect(mapping: dict[object, object], *, prefix: str = "") -> None:
        for raw_field in mapping:
            if not isinstance(raw_field, str):
                continue
            normalized = raw_field.strip().lower().replace("_", "-")
            if normalized not in _DEGRADED_DIALECT_FIELDS:
                continue
            field = f"{prefix}.{raw_field}" if prefix else raw_field
            diagnostics.append(
                _diagnostic(
                    "TOOL_PREAPPROVAL_IGNORED",
                    (
                        f"{field} requests dialect-specific tool preapproval; "
                        "OpenSquilla will keep its normal tool approval policy"
                    ),
                    path=path,
                    field=field,
                )
            )

    def inspect_platform(mapping: dict[object, object], *, prefix: str) -> None:
        inspect(mapping, prefix=prefix)
        installs = mapping.get("install")
        if not isinstance(installs, list):
            return
        for index, item in enumerate(installs):
            if isinstance(item, dict):
                inspect(item, prefix=f"{prefix}.install[{index}]")

    inspect(frontmatter)
    metadata = frontmatter.get("metadata")
    if isinstance(metadata, dict):
        inspect_platform(metadata, prefix="metadata")
        for namespace in ("platform", "openclaw", "clawdbot", "opensquilla"):
            value = metadata.get(namespace)
            if isinstance(value, dict):
                inspect_platform(value, prefix=f"metadata.{namespace}")
    return diagnostics


def _validate_degraded_body_features(
    body: str,
    *,
    path: Path,
) -> list[dict[str, str]]:
    if not (_INLINE_DYNAMIC_CONTEXT_RE.search(body) or _FENCED_DYNAMIC_CONTEXT_RE.search(body)):
        return []
    return [
        _diagnostic(
            "DYNAMIC_CONTEXT_UNSUPPORTED",
            (
                "Skill body requests dynamic shell context; OpenSquilla will keep "
                "the command as instruction text instead of executing it during loading"
            ),
            path=path,
            field="body.dynamic-context",
        )
    ]


def validate_hub_candidate(
    skill_dir: Path,
    *,
    expected_name: str | None = None,
    allowed_legacy_name: str | None = None,
    layer: SkillLayer = SkillLayer.MANAGED,
) -> SkillManifestValidation:
    """Validate and project a freshly downloaded Community candidate.

    The artifact boundary remains strict about bytes, paths, and unambiguous
    YAML. Authoring-profile differences are compatibility observations: source,
    storage, and runtime names may differ, and unsupported host extensions are
    inert rather than installation blockers. ``allowed_legacy_name`` remains an
    accepted no-op parameter for callers from the previous compatibility shim.
    """

    del allowed_legacy_name, expected_name

    skill_file = skill_dir / "SKILL.md"
    diagnostics: list[dict[str, str]] = []
    if not skill_dir.is_dir():
        return SkillManifestValidation(
            spec=None,
            diagnostics=(
                _diagnostic(
                    "MISSING_SKILL_DIRECTORY",
                    "candidate is not a directory",
                    path=skill_dir,
                ),
            ),
        )
    if not skill_file.is_file():
        return SkillManifestValidation(
            spec=None,
            diagnostics=(
                _diagnostic(
                    "MISSING_SKILL_MANIFEST",
                    "candidate must contain SKILL.md at its root",
                    path=skill_file,
                ),
            ),
        )

    try:
        resolved_dir = skill_dir.resolve(strict=True)
        resolved_file = skill_file.resolve(strict=True)
        resolved_file.relative_to(resolved_dir)
    except (OSError, ValueError):
        return SkillManifestValidation(
            spec=None,
            diagnostics=(
                _diagnostic(
                    "MANIFEST_PATH_ESCAPE",
                    "SKILL.md must remain inside the candidate directory",
                    path=skill_file,
                ),
            ),
        )

    try:
        with skill_file.open("rb") as handle:
            skill_bytes = handle.read(MAX_SKILL_FILE_BYTES + 1)
    except OSError as exc:
        return SkillManifestValidation(
            spec=None,
            diagnostics=(_diagnostic("MANIFEST_READ_FAILED", str(exc), path=skill_file),),
        )
    if len(skill_bytes) > MAX_SKILL_FILE_BYTES:
        return SkillManifestValidation(
            spec=None,
            diagnostics=(
                _diagnostic(
                    "MANIFEST_TOO_LARGE",
                    f"SKILL.md exceeds {MAX_SKILL_FILE_BYTES} bytes",
                    path=skill_file,
                ),
            ),
        )
    try:
        text = skill_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return SkillManifestValidation(
            spec=None,
            diagnostics=(_diagnostic("MANIFEST_NOT_UTF8", str(exc), path=skill_file),),
        )

    try:
        frontmatter, body = _parse_skill_frontmatter_strict(text)
    except ValueError as exc:
        return SkillManifestValidation(
            spec=None,
            diagnostics=(_diagnostic("FRONTMATTER_INVALID", str(exc), path=skill_file),),
        )

    raw_name = frontmatter.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        diagnostics.append(
            _diagnostic(
                "NAME_INVALID",
                "name is required and must be a non-empty string",
                path=skill_file,
                field="name",
            )
        )

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        diagnostics.append(
            _diagnostic(
                "DESCRIPTION_INVALID",
                "description is required and must be a non-empty string",
                path=skill_file,
                field="description",
            )
        )
    compatibility_diagnostics: list[dict[str, str]] = []
    compatibility_diagnostics.extend(
        _validate_unsupported_dialect_fields(frontmatter, path=skill_file)
    )
    compatibility_diagnostics.extend(
        _validate_degraded_dialect_fields(
            frontmatter,
            path=skill_file,
        )
    )
    compatibility_diagnostics.extend(_validate_degraded_body_features(body, path=skill_file))
    compatibility_diagnostics.extend(_validate_known_manifest_types(frontmatter, path=skill_file))
    if diagnostics:
        return SkillManifestValidation(
            spec=None,
            diagnostics=tuple(diagnostics),
            compatibility_diagnostics=tuple(compatibility_diagnostics),
        )

    try:
        spec = compile_skill_manifest(
            skill_dir,
            layer,
            skill_bytes=skill_bytes,
            profile=SkillCompileProfile.COMMUNITY_INSTRUCTION,
        )
    except (OSError, UnicodeDecodeError, TypeError, ValueError) as exc:
        return SkillManifestValidation(
            spec=None,
            diagnostics=(_diagnostic("MANIFEST_COMPILE_FAILED", str(exc), path=skill_file),),
        )
    return SkillManifestValidation(
        spec=spec,
        compatibility_diagnostics=tuple(compatibility_diagnostics),
    )


__all__ = [
    "MAX_SKILL_FILE_BYTES",
    "SkillCompileProfile",
    "SkillManifestValidation",
    "compile_skill_manifest",
    "parse_skill_frontmatter",
    "resolve_skill_metadata",
    "resolve_community_skill_metadata",
    "resolve_skill_provenance",
    "skill_instance_id",
    "validate_hub_candidate",
]
