#!/usr/bin/env python3
"""Discover and generate every language-neutral Gateway v4 Contract.

``sessions.list`` predates this aggregate runner.  It deliberately keeps its
original entry point so that adopting the runner does not rewrite its already
reviewed generated artifacts.  New Contracts use the generic JSON Schema
2020-12 path below.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "contracts/gateway/v4"
PYTHON_OUTPUT_ROOT = ROOT / "src/opensquilla/contracts/generated/v4"
TYPESCRIPT_OUTPUT_ROOT = ROOT / "opensquilla-webui/src/contracts/generated/v4"
AJV_GENERATOR = ROOT / "scripts/contracts/generate_gateway_contract_ajv.mjs"
JSON_SCHEMA_2020_12 = "https://json-schema.org/draft/2020-12/schema"
GATEWAY_PROTOCOL = "opensquilla-websocket-json"
REGISTRATION_OUTPUT = PYTHON_OUTPUT_ROOT / "gateway_contract_registry.py"
COMPATIBILITY_MANIFEST_OUTPUT = CONTRACT_ROOT / "compatibility-manifest.generated.json"
PRODUCTION_TARGET_MANIFEST = CONTRACT_ROOT / "production-targets.json"

PINNED_CODEGEN = {
    "python": {
        "tool": "datamodel-code-generator",
        "version": "0.75.1",
        "target": "pydantic_v2.BaseModel",
    },
    "typescript": {
        "tool": "json-schema-to-typescript",
        "version": "15.0.4",
    },
    "runtimeValidation": {
        "tool": "ajv",
        "version": "8.17.1",
        "mode": "standalone-adapter-only",
    },
}

# Exact-output compatibility seam.  Remove an entry only in the PR that
# intentionally regenerates and reviews that Contract's complete wire surface.
LEGACY_GENERATORS = {
    CONTRACT_ROOT / "sessions/sessions-list.schema.json": ROOT
    / "scripts/contracts/generate_sessions_list_contract.py",
}

WIRE_NAME_PATTERN = re.compile(r"^[a-z][a-zA-Z0-9_]*(?:\.[a-z][a-zA-Z0-9_]*)+$")
LEGACY_ROOT_METHOD_NAMES = frozenset({"status"})
LEGACY_HYPHENATED_WIRE_NAMES = frozenset({"sandbox.path.create-directory"})
SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
LEGACY_DOTTED_ERROR_CODES = frozenset(
    {
        "migration.candidate_unavailable",
        "migration.invalid_params",
        "migration.preview_unavailable",
        "migration.unavailable",
        "onboarding.channel.invalid",
        "onboarding.channel.not_found",
    }
)
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FILE_STEM_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
METHOD_KINDS = frozenset({"query", "command"})
IDEMPOTENCY_KINDS = frozenset({"read-only", "idempotent", "non-idempotent"})
TIMEOUT_POLICIES = frozenset({"caller", "server", "transport"})
CAPABILITY_KINDS = frozenset({"method-availability"})
METHOD_LIFECYCLES = frozenset({"stable", "legacy"})

Mode = Literal["write", "check", "verify-determinism"]
Profile = Literal["production", "verification"]
ValidatorTargets = dict[tuple[str, str], tuple[str, ...]]


class ContractConfigurationError(RuntimeError):
    """A discovered schema cannot be generated unambiguously."""


@dataclass(frozen=True)
class ContractSpec:
    schema: Path
    relative_schema: Path
    document: dict[str, Any]
    contract_type: Literal["method", "event"]
    wire_name: str
    semantic_kind: str
    protocol: str
    wire_version: int
    metadata: dict[str, Any]
    targets: tuple[tuple[str, str], ...]

    @property
    def file_stem(self) -> str:
        return self.schema.name.removesuffix(".schema.json")

    @property
    def python_stem(self) -> str:
        return _snake_case(self.file_stem)

    @property
    def typescript_stem(self) -> str:
        return _lower_camel(self.file_stem)

    @property
    def constant_prefix(self) -> str:
        return _snake_case(self.wire_name).upper()

    @property
    def outputs(self) -> tuple[Path, ...]:
        # Generic validators are imported by the browser-side Vite adapter.
        # They must be native ESM: Vite intentionally does not transform
        # source-tree .cjs files during dev, so a named import would leave the
        # browser with a CommonJS module that has no exports.  The legacy
        # sessions.list generator remains byte-for-byte CJS compatible.
        validator_suffix = "Validators.cjs" if self.uses_legacy_generator else "Validators.mjs"
        declaration_suffix = ".d.cts" if self.uses_legacy_generator else ".d.mts"
        return (
            PYTHON_OUTPUT_ROOT / f"{self.python_stem}.py",
            PYTHON_OUTPUT_ROOT / f"{self.python_stem}_metadata.py",
            TYPESCRIPT_OUTPUT_ROOT / f"{self.typescript_stem}.ts",
            TYPESCRIPT_OUTPUT_ROOT / f"{self.typescript_stem}{validator_suffix}",
            TYPESCRIPT_OUTPUT_ROOT / f"{self.typescript_stem}Validators{declaration_suffix}",
        )

    @property
    def uses_legacy_generator(self) -> bool:
        return self.schema.resolve() in {
            legacy_schema.resolve() for legacy_schema in LEGACY_GENERATORS
        }

    def target(self, role: str) -> str:
        try:
            return dict(self.targets)[role]
        except KeyError as exc:
            raise ContractConfigurationError(
                f"{self.schema}: Contract has no generated {role!r} target"
            ) from exc


def load_production_targets(
    specs: tuple[ContractSpec, ...],
    path: Path = PRODUCTION_TARGET_MANIFEST,
) -> ValidatorTargets:
    """Validate explicit production policy against the complete Schema inventory."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractConfigurationError(f"cannot read production target manifest: {path}") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"format", "targets"}
        or type(document["format"]) is not int
        or document["format"] != 1
        or not isinstance(document["targets"], list)
    ):
        raise ContractConfigurationError("invalid production target manifest format")
    contracts = {(spec.contract_type, spec.wire_name): spec for spec in specs}
    selected: ValidatorTargets = {}
    for entry in document["targets"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"kind", "wireName", "roles"}
            or not isinstance(entry["kind"], str)
            or not isinstance(entry["wireName"], str)
        ):
            raise ContractConfigurationError("invalid production target entry")
        key = (entry["kind"], entry["wireName"])
        if key not in contracts or key in selected:
            raise ContractConfigurationError(f"unknown or duplicate production Contract: {key}")
        roles = entry["roles"]
        available = tuple(role for role, _ in contracts[key].targets)
        if (
            not isinstance(roles, list)
            or not roles
            or not all(isinstance(role, str) and role in available for role in roles)
            or len(set(roles)) != len(roles)
        ):
            raise ContractConfigurationError(f"invalid production validator roles: {key}")
        selected[key] = tuple(role for role in available if role in roles)
    return selected


def _snake_case(value: str) -> str:
    with_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^A-Za-z0-9]+", "_", with_boundaries).strip("_").lower()


def _lower_camel(value: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    if not parts:
        raise ContractConfigurationError(f"cannot derive output name from {value!r}")
    return parts[0].lower() + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _definition_name(reference: object, *, schema: Path) -> str:
    prefix = "#/$defs/"
    if not isinstance(reference, str) or not reference.startswith(prefix):
        raise ContractConfigurationError(
            f"{schema}: generated target must be a local $defs reference"
        )
    name = reference.removeprefix(prefix)
    if not name or "/" in name:
        raise ContractConfigurationError(
            f"{schema}: generated target must name one direct $defs member"
        )
    if not IDENTIFIER_PATTERN.fullmatch(name):
        raise ContractConfigurationError(
            f"{schema}: generated target {name!r} is not a legal identifier"
        )
    return name


def _require_mapping(value: object, *, label: str, schema: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractConfigurationError(f"{schema}: {label} must be an object")
    return value


def _require_string(metadata: dict[str, Any], key: str, *, schema: Path) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise ContractConfigurationError(
            f"{schema}: Contract metadata {key!r} must be a non-empty string"
        )
    return value


def _resolved_command(command: list[str]) -> list[str]:
    """Resolve executable shims before passing them to ``shell=False``.

    In particular, npm is installed as ``npm.cmd`` on Windows.  Passing the
    fully-qualified shim returned by ``shutil.which`` keeps command execution
    shell-free and portable.
    """

    if not command:
        raise ValueError("generator command must not be empty")
    executable = shutil.which(command[0]) or command[0]
    return [executable, *command[1:]]


def _write_text_lf(path: Path, content: str) -> None:
    """Write generated text without platform newline translation."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as artifact:
        artifact.write(content)


def load_contract(schema: Path, *, contract_root: Path = CONTRACT_ROOT) -> ContractSpec:
    """Parse and validate the generation metadata for one Contract schema."""

    try:
        relative_schema = schema.relative_to(contract_root)
    except ValueError as exc:
        raise ContractConfigurationError(
            f"{schema}: schema is outside Contract root {contract_root}"
        ) from exc
    legacy_contract = schema.resolve() in {
        legacy_schema.resolve() for legacy_schema in LEGACY_GENERATORS
    }
    try:
        document = json.loads(schema.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractConfigurationError(f"cannot read Contract schema {schema}: {exc}") from exc
    if not isinstance(document, dict):
        raise ContractConfigurationError(f"{schema}: Contract schema must be an object")
    if document.get("$schema") != JSON_SCHEMA_2020_12:
        raise ContractConfigurationError(f"{schema}: Contract must use JSON Schema 2020-12")
    if not isinstance(document.get("$id"), str):
        raise ContractConfigurationError(f"{schema}: Contract must declare a string $id")
    if document.get("x-opensquilla-codegen") != PINNED_CODEGEN:
        raise ContractConfigurationError(
            f"{schema}: x-opensquilla-codegen must match the repository-pinned toolchain"
        )
    wire = _require_mapping(
        document.get("x-opensquilla-wire"),
        label="x-opensquilla-wire",
        schema=schema,
    )
    protocol = wire.get("protocol")
    if protocol != GATEWAY_PROTOCOL:
        raise ContractConfigurationError(f"{schema}: Gateway protocol must be {GATEWAY_PROTOCOL!r}")
    wire_version = wire.get("version")
    if wire_version != 4:
        raise ContractConfigurationError(f"{schema}: only Gateway wire version 4 is supported")
    if wire.get("compatibility") != "exact-json-tree":
        raise ContractConfigurationError(
            f"{schema}: Gateway Contract compatibility must be 'exact-json-tree'"
        )
    file_stem = schema.name.removesuffix(".schema.json")
    if not FILE_STEM_PATTERN.fullmatch(file_stem):
        raise ContractConfigurationError(f"{schema}: Contract filename must use lower-kebab-case")

    method = document.get("x-opensquilla-method")
    event = document.get("x-opensquilla-event")
    if (method is None) == (event is None):
        raise ContractConfigurationError(
            f"{schema}: declare exactly one of x-opensquilla-method or x-opensquilla-event"
        )

    definitions = _require_mapping(document.get("$defs"), label="$defs", schema=schema)
    targets: tuple[tuple[str, str], ...]
    if method is not None:
        metadata = _require_mapping(method, label="x-opensquilla-method", schema=schema)
        wire_name = _require_string(metadata, "name", schema=schema)
        if (
            not WIRE_NAME_PATTERN.fullmatch(wire_name)
            and wire_name not in LEGACY_ROOT_METHOD_NAMES
            and wire_name not in LEGACY_HYPHENATED_WIRE_NAMES
        ):
            raise ContractConfigurationError(
                f"{schema}: method name {wire_name!r} is not a legal dotted identifier"
            )
        scope = _require_string(metadata, "scope", schema=schema)
        if not SCOPE_PATTERN.fullmatch(scope):
            raise ContractConfigurationError(
                f"{schema}: scope {scope!r} is not a legal dotted identifier"
            )
        idempotency = _require_string(metadata, "idempotency", schema=schema)
        guest_allowed = metadata.get("guestAllowed")
        if not isinstance(guest_allowed, bool):
            raise ContractConfigurationError(
                f"{schema}: method Contract guestAllowed must be a boolean"
            )
        if idempotency not in IDEMPOTENCY_KINDS:
            raise ContractConfigurationError(f"{schema}: unsupported idempotency {idempotency!r}")
        errors = metadata.get("errors")
        if not isinstance(errors, list):
            raise ContractConfigurationError(f"{schema}: method Contract errors must be an array")
        for error in errors:
            error_metadata = _require_mapping(
                error,
                label="method Contract error",
                schema=schema,
            )
            error_code = _require_string(error_metadata, "code", schema=schema)
            if (
                not ERROR_CODE_PATTERN.fullmatch(error_code)
                and error_code not in LEGACY_DOTTED_ERROR_CODES
            ):
                raise ContractConfigurationError(
                    f"{schema}: error code {error_code!r} is not a legal identifier"
                )
        request_name = _definition_name(metadata.get("request"), schema=schema)
        params_name = _definition_name(metadata.get("params"), schema=schema)
        response_name = _definition_name(metadata.get("response"), schema=schema)
        result_name = _definition_name(metadata.get("result"), schema=schema)
        targets = (
            ("request", request_name),
            ("params", params_name),
            ("response", response_name),
            ("result", result_name),
        )
        semantic_kind = metadata.get("kind")
        if semantic_kind is None:
            if not legacy_contract:
                raise ContractConfigurationError(
                    f"{schema}: new method Contracts must declare kind"
                )
            # sessions.list shipped before the explicit key. Preserve it
            # byte-for-byte while giving the aggregate runner a stable meaning.
            semantic_kind = "query" if idempotency == "read-only" else "command"
        if not isinstance(semantic_kind, str) or not semantic_kind:
            raise ContractConfigurationError(f"{schema}: method kind must be a string")
        if semantic_kind not in METHOD_KINDS:
            raise ContractConfigurationError(f"{schema}: unsupported method kind {semantic_kind!r}")
        if not legacy_contract:
            timeout = metadata.get("timeout")
            if not isinstance(timeout, dict):
                raise ContractConfigurationError(
                    f"{schema}: new method Contracts must declare timeout metadata"
                )
            timeout_policy = _require_string(timeout, "policy", schema=schema)
            if timeout_policy not in TIMEOUT_POLICIES:
                raise ContractConfigurationError(
                    f"{schema}: unsupported timeout policy {timeout_policy!r}"
                )
            capability = metadata.get("capability")
            if not isinstance(capability, dict):
                raise ContractConfigurationError(
                    f"{schema}: new method Contracts must declare capability metadata"
                )
            capability_kind = _require_string(capability, "kind", schema=schema)
            capability_name = _require_string(capability, "name", schema=schema)
            if capability_kind not in CAPABILITY_KINDS:
                raise ContractConfigurationError(
                    f"{schema}: unsupported capability kind {capability_kind!r}"
                )
            if (
                not WIRE_NAME_PATTERN.fullmatch(capability_name)
                and capability_name not in LEGACY_ROOT_METHOD_NAMES
                and capability_name not in LEGACY_HYPHENATED_WIRE_NAMES
            ):
                raise ContractConfigurationError(
                    f"{schema}: capability name {capability_name!r} is not legal"
                )
        lifecycle = metadata.get("lifecycle", "stable")
        if lifecycle not in METHOD_LIFECYCLES:
            raise ContractConfigurationError(
                f"{schema}: method lifecycle {lifecycle!r} is not supported"
            )
        canonical_alias = metadata.get("canonicalAlias")
        if lifecycle == "legacy":
            if not isinstance(canonical_alias, str) or not canonical_alias:
                raise ContractConfigurationError(
                    f"{schema}: legacy method must declare canonicalAlias"
                )
            if (
                not WIRE_NAME_PATTERN.fullmatch(canonical_alias)
                and canonical_alias not in LEGACY_ROOT_METHOD_NAMES
                and canonical_alias not in LEGACY_HYPHENATED_WIRE_NAMES
            ):
                raise ContractConfigurationError(
                    f"{schema}: canonicalAlias {canonical_alias!r} is not legal"
                )
            if canonical_alias == wire_name:
                raise ContractConfigurationError(
                    f"{schema}: legacy method canonicalAlias must differ from its name"
                )
        elif canonical_alias is not None:
            raise ContractConfigurationError(
                f"{schema}: stable method must not declare canonicalAlias"
            )
        compatibility_aliases = metadata.get("compatibilityAliases", [])
        if not isinstance(compatibility_aliases, list) or any(
            not isinstance(alias, str)
            or not alias
            or (
                not WIRE_NAME_PATTERN.fullmatch(alias)
                and alias not in LEGACY_ROOT_METHOD_NAMES
                and alias not in LEGACY_HYPHENATED_WIRE_NAMES
            )
            for alias in compatibility_aliases
        ):
            raise ContractConfigurationError(
                f"{schema}: compatibilityAliases must contain legal wire names"
            )
        if len(set(compatibility_aliases)) != len(compatibility_aliases):
            raise ContractConfigurationError(
                f"{schema}: compatibilityAliases must not contain duplicates"
            )
        contract_type: Literal["method", "event"] = "method"
    else:
        metadata = _require_mapping(event, label="x-opensquilla-event", schema=schema)
        wire_name = _require_string(metadata, "name", schema=schema)
        if not WIRE_NAME_PATTERN.fullmatch(wire_name):
            raise ContractConfigurationError(
                f"{schema}: event name {wire_name!r} is not a legal dotted identifier"
            )
        _require_string(metadata, "delivery", schema=schema)
        wire_names = metadata.get("wireNames")
        if wire_names is not None:
            if (
                not isinstance(wire_names, list)
                or not wire_names
                or any(
                    not isinstance(name, str) or not WIRE_NAME_PATTERN.fullmatch(name)
                    for name in wire_names
                )
                or len(set(wire_names)) != len(wire_names)
            ):
                raise ContractConfigurationError(
                    f"{schema}: event wireNames must be unique legal wire names"
                )
        schema_version = metadata.get("schemaVersion")
        if type(schema_version) is not int or schema_version < 1:
            raise ContractConfigurationError(
                f"{schema}: event Contract schemaVersion must be a positive integer"
            )
        declared_event_targets = [role for role in ("frame", "payload") if role in metadata]
        if len(declared_event_targets) != 1:
            raise ContractConfigurationError(
                f"{schema}: event Contract must declare exactly one of frame or payload"
            )
        target_role = declared_event_targets[0]
        reference = metadata[target_role]
        target_name = _definition_name(reference, schema=schema)
        targets = ((target_role, target_name),)
        semantic_kind = "event"
        contract_type = "event"

    referenced_names = {definition for _, definition in targets}
    missing = sorted(referenced_names - definitions.keys())
    if missing:
        raise ContractConfigurationError(
            f"{schema}: generated targets missing from $defs: {', '.join(missing)}"
        )

    return ContractSpec(
        schema=schema,
        relative_schema=relative_schema,
        document=document,
        contract_type=contract_type,
        wire_name=wire_name,
        semantic_kind=semantic_kind,
        protocol=protocol,
        wire_version=wire_version,
        metadata=metadata,
        targets=targets,
    )


def discover_contracts(contract_root: Path = CONTRACT_ROOT) -> tuple[ContractSpec, ...]:
    """Return every Gateway v4 Contract in a deterministic order."""

    schemas = sorted(contract_root.rglob("*.schema.json"), key=lambda path: path.as_posix())
    if not schemas:
        raise ContractConfigurationError(f"no Contract schemas found under {contract_root}")
    specs = tuple(load_contract(path, contract_root=contract_root) for path in schemas)

    seen_ids: dict[str, Path] = {}
    seen_wire_names: dict[tuple[str, str], Path] = {}
    seen_outputs: dict[Path, Path] = {}
    for spec in specs:
        schema_id = str(spec.document["$id"])
        previous = seen_ids.setdefault(schema_id, spec.schema)
        if previous != spec.schema:
            raise ContractConfigurationError(
                f"duplicate Contract $id {schema_id!r}: {previous} and {spec.schema}"
            )
        identity = (spec.contract_type, spec.wire_name)
        previous = seen_wire_names.setdefault(identity, spec.schema)
        if previous != spec.schema:
            raise ContractConfigurationError(
                f"duplicate {spec.contract_type} Contract {spec.wire_name!r}: "
                f"{previous} and {spec.schema}"
            )
        for output in spec.outputs:
            if output == REGISTRATION_OUTPUT:
                raise ContractConfigurationError(
                    f"generated output {output} is reserved for the aggregate registry: "
                    f"{spec.schema}"
                )
            previous = seen_outputs.setdefault(output, spec.schema)
            if previous != spec.schema:
                raise ContractConfigurationError(
                    f"generated output collision at {output}: {previous} and {spec.schema}"
                )
    return specs


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    cache_root = Path(tempfile.gettempdir()) / "opensquilla-contract-jsonschema-tools"
    env.setdefault("UV_CACHE_DIR", str(cache_root / "uv"))
    env.setdefault("npm_config_cache", str(cache_root / "npm"))
    env.setdefault("npm_config_update_notifier", "false")
    env.setdefault("npm_config_fund", "false")
    env.setdefault("npm_config_audit", "false")
    return env


def _run(command: list[str], *, env: dict[str, str], purpose: str) -> None:
    resolved = _resolved_command(command)
    try:
        subprocess.run(resolved, cwd=ROOT, env=env, check=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"required Contract tool is unavailable: {resolved[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"{purpose} failed") from exc


def _capture(command: list[str], *, env: dict[str, str], purpose: str) -> str:
    resolved = _resolved_command(command)
    try:
        completed = subprocess.run(
            resolved,
            cwd=ROOT,
            env=env,
            check=True,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"required Contract tool is unavailable: {resolved[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else str(exc)
        raise RuntimeError(f"{purpose} failed: {detail}") from exc
    return completed.stdout


def _generator_digest() -> str:
    source = (
        Path(__file__).read_bytes()
        + b"\0"
        + AJV_GENERATOR.read_bytes()
        + b"\0"
        + PRODUCTION_TARGET_MANIFEST.read_bytes()
    )
    return hashlib.sha256(source).hexdigest()


def _header(spec: ContractSpec, prefix: str) -> str:
    source_digest = hashlib.sha256(spec.schema.read_bytes()).hexdigest()
    return (
        f"{prefix} @generated by scripts/contracts/generate_gateway_contracts.py; "
        "do not edit.\n"
        f"{prefix} source-sha256: {source_digest}\n"
        f"{prefix} generator-sha256: {_generator_digest()}\n"
    )


def _normalise(spec: ContractSpec, text: str, *, prefix: str) -> str:
    body = text.replace("\r\n", "\n").rstrip() + "\n"
    lint_directive = "# ruff: noqa\n" if prefix == "#" else ""
    return _header(spec, prefix) + lint_directive + "\n" + body


def _schema_allows_null(
    schema: object,
    definitions: dict[str, Any],
    *,
    seen_refs: frozenset[str] = frozenset(),
) -> bool:
    """Return whether JSON Schema validation accepts an explicit ``null``.

    ``datamodel-code-generator`` represents every non-required property as a
    ``T | None = None`` field.  That is not equivalent to JSON Schema when the
    property is optional-but-non-nullable: omission is valid, while an
    explicit ``null`` is not.  This small evaluator only answers the one
    question needed by the deterministic Python post-processor below; it is
    deliberately not a second JSON Schema validator.
    """

    if not isinstance(schema, dict):
        # A missing assertion (for example ``{}``) places no restriction on
        # null in JSON Schema.
        return True

    allows = True

    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        name = reference.removeprefix("#/$defs/")
        if name in seen_refs:
            # Recursive definitions cannot establish a stricter nullability
            # guarantee at this point.  Keep the generated optional union.
            return True
        target = definitions.get(name)
        if target is None:
            return True
        allows = allows and _schema_allows_null(
            target,
            definitions,
            seen_refs=seen_refs | {name},
        )

    if "const" in schema:
        allows = allows and schema["const"] is None

    enum = schema.get("enum")
    if isinstance(enum, list):
        allows = allows and any(value is None for value in enum)

    declared_type = schema.get("type")
    if isinstance(declared_type, str):
        allows = allows and declared_type == "null"
    elif isinstance(declared_type, list):
        allows = allows and "null" in declared_type

    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            branch_results = [
                _schema_allows_null(branch, definitions, seen_refs=seen_refs) for branch in branches
            ]
            if keyword == "oneOf":
                allows = allows and sum(branch_results) == 1
            else:
                allows = allows and any(branch_results)

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        allows = allows and all(
            _schema_allows_null(branch, definitions, seen_refs=seen_refs) for branch in all_of
        )

    negated = schema.get("not")
    if isinstance(negated, dict):
        allows = allows and not _schema_allows_null(
            negated,
            definitions,
            seen_refs=seen_refs,
        )

    return allows


def _optional_non_nullable_fields_for_definition(
    spec: ContractSpec,
    definition_name: str,
) -> frozenset[str]:
    """Find optional, non-nullable properties in one generated definition."""

    definitions = spec.document.get("$defs", {})
    if not isinstance(definitions, dict):
        return frozenset()
    result_schema = definitions.get(definition_name)
    if not isinstance(result_schema, dict):
        return frozenset()
    properties = result_schema.get("properties")
    if not isinstance(properties, dict):
        return frozenset()
    required = result_schema.get("required")
    required_names = set(required) if isinstance(required, list) else set()
    definitions = spec.document.get("$defs", {})
    if not isinstance(definitions, dict):
        return frozenset()
    return frozenset(
        name
        for name, property_schema in properties.items()
        if isinstance(name, str)
        and name not in required_names
        and not _schema_allows_null(property_schema, definitions)
    )


def _optional_non_nullable_fields(spec: ContractSpec) -> frozenset[str]:
    """Find method result properties where omission is valid but ``null`` is not."""

    if spec.contract_type != "method":
        return frozenset()
    return _optional_non_nullable_fields_for_definition(spec, spec.target("result"))


def _reachable_definition_names(spec: ContractSpec, root_name: str) -> tuple[str, ...]:
    """Return generated definitions reachable from an event frame or payload."""

    definitions = spec.document.get("$defs", {})
    if not isinstance(definitions, dict):
        return ()
    seen: set[str] = set()

    def visit(schema: object) -> None:
        if not isinstance(schema, dict):
            return
        reference = schema.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            if name in seen:
                return
            seen.add(name)
            visit(definitions.get(name))
        for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
            branches = schema.get(keyword)
            if isinstance(branches, list):
                for branch in branches:
                    visit(branch)
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for property_schema in properties.values():
                visit(property_schema)
        visit(schema.get("items"))
        visit(schema.get("additionalProperties"))

    visit({"$ref": f"#/$defs/{root_name}"})
    return tuple(sorted(seen))


def _is_none_annotation(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _union_parts(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return [*_union_parts(node.left), *_union_parts(node.right)]
    if isinstance(node, ast.Subscript):
        value = node.value
        is_union = (isinstance(value, ast.Name) and value.id == "Union") or (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "typing"
            and value.attr == "Union"
        )
        if is_union:
            arguments = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            return [part for argument in arguments for part in _union_parts(argument)]
    return [node]


def _annotation_without_none(node: ast.expr) -> str | None:
    """Render a generated annotation after removing a top-level ``None``."""

    parts = _union_parts(node)
    if len(parts) == 1 or not any(_is_none_annotation(part) for part in parts):
        return None
    remaining = [part for part in parts if not _is_none_annotation(part)]
    if not remaining:
        return "Any"
    return " | ".join(ast.unparse(part) for part in remaining)


def _source_offset(lines: list[str], lineno: int, column: int) -> int:
    return sum(len(line) for line in lines[: lineno - 1]) + column


def _field_wire_name(node: ast.AnnAssign) -> str | None:
    if not isinstance(node.target, ast.Name):
        return None
    value = node.value
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "Field"
    ):
        for keyword in value.keywords:
            if keyword.arg == "alias" and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, str):
                    return keyword.value.value
    return node.target.id


def _normalise_optional_non_nullable_defaults(spec: ContractSpec, text: str) -> str:
    """Make generated Pydantic omission/null semantics match the source schema.

    The transformation is source-driven and deterministic.  Method Contracts
    are restricted to their result definition; event Contracts walk the frame
    or payload union and touch only reachable definitions.  ``T = None`` is
    intentional: Pydantic 2.x treats the field as omittable while validating
    an explicitly supplied value against the non-nullable ``T`` annotation,
    without raising the repository's minimum Pydantic version.
    """

    if spec.contract_type == "method":
        target_fields = {
            spec.target("result"): _optional_non_nullable_fields(spec),
        }
    else:
        target_role = spec.targets[0][0]
        target_fields = {
            definition: _optional_non_nullable_fields_for_definition(spec, definition)
            for definition in _reachable_definition_names(spec, spec.target(target_role))
        }
    target_fields = {definition: fields for definition, fields in target_fields.items() if fields}
    if not target_fields:
        return text
    tree = ast.parse(text)
    target_classes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name in target_fields
    ]
    if not target_classes:
        # Test doubles and future generators may emit a target under a
        # different shape; do not make an otherwise valid renderer fail.
        return text

    lines = text.splitlines(keepends=True)
    replacements: list[tuple[int, int, str]] = []
    for target_class in target_classes:
        fields = target_fields[target_class.name]
        for field in target_class.body:
            if not isinstance(field, ast.AnnAssign):
                continue
            wire_name = _field_wire_name(field)
            if wire_name not in fields or field.value is None:
                continue
            annotation = _annotation_without_none(field.annotation)
            if annotation is None:
                annotation = ast.get_source_segment(text, field.annotation)
            if not annotation:
                continue
            annotation_end_lineno = field.annotation.end_lineno
            annotation_end_col_offset = field.annotation.end_col_offset
            value_end_lineno = field.value.end_lineno
            value_end_col_offset = field.value.end_col_offset
            value_lineno = field.value.lineno
            if (
                annotation_end_lineno is None
                or annotation_end_col_offset is None
                or value_end_lineno is None
                or value_end_col_offset is None
                or value_lineno is None
            ):
                continue
            start = _source_offset(
                lines,
                field.annotation.lineno,
                field.annotation.col_offset,
            )
            end = _source_offset(
                lines,
                annotation_end_lineno,
                annotation_end_col_offset,
            )
            # Replace only the annotation.  Keeping the generated default
            # expression intact preserves Field(alias=...), constraints and
            # other metadata that a future Contract may declare.  The
            # generated ``None`` default is valid Pydantic runtime metadata
            # but is intentionally incompatible with the non-nullable static
            # annotation, so keep the suppression local to this generated
            # field rather than weakening the repository-wide mypy gate.
            replacements.append((start, end, annotation))

            value_end = _source_offset(
                lines,
                value_end_lineno,
                value_end_col_offset,
            )
            line_end = text.find("\n", value_end)
            if line_end < 0:
                line_end = len(text)
            line = text[_source_offset(lines, value_lineno, 0) : line_end]
            if "# type: ignore[assignment]" not in line:
                replacements.append((line_end, line_end, "  # type: ignore[assignment]"))

    for start, end, replacement in reversed(replacements):
        text = text[:start] + replacement + text[end:]
    return text


def _schema_contains_json_number(
    schema: object,
    definitions: dict[str, Any],
    *,
    seen_refs: frozenset[str] = frozenset(),
) -> bool:
    """Return whether a schema branch contains a JSON-Schema number."""

    if not isinstance(schema, dict):
        return False
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        name = reference.removeprefix("#/$defs/")
        if name in seen_refs:
            return False
        target = definitions.get(name)
        return _schema_contains_json_number(
            target,
            definitions,
            seen_refs=seen_refs | {name},
        )
    declared_type = schema.get("type")
    if (isinstance(declared_type, str) and declared_type in {"integer", "number"}) or (
        isinstance(declared_type, list) and {"integer", "number"}.intersection(declared_type)
    ):
        return True
    for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
        branches = schema.get(keyword)
        if isinstance(branches, list) and any(
            _schema_contains_json_number(branch, definitions, seen_refs=seen_refs)
            for branch in branches
        ):
            return True
    properties = schema.get("properties")
    if isinstance(properties, dict) and any(
        _schema_contains_json_number(property_schema, definitions, seen_refs=seen_refs)
        for property_schema in properties.values()
    ):
        return True
    additional = schema.get("additionalProperties")
    if _schema_contains_json_number(additional, definitions, seen_refs=seen_refs):
        return True
    items = schema.get("items")
    return _schema_contains_json_number(items, definitions, seen_refs=seen_refs)


def _normalise_json_number_types(spec: ContractSpec, text: str) -> str:
    """Align generated Python number validation with JSON Schema/AJV.

    JSON Schema's ``number`` accepts finite JSON numbers and ``integer`` also
    accepts an integral value represented as ``1.0`` (AJV's behavior).
    Pydantic's strict scalar types otherwise coerce or reject those values.
    Generated Contracts use validated unions so both runtimes accept the same
    values while preserving the original numeric tree on dump.  This is only
    applied to generic generated Contracts; the reviewed legacy ``sessions.list``
    generator remains byte-for-byte frozen.
    """

    definitions = spec.document.get("$defs", {})
    if not isinstance(definitions, dict):
        return text
    has_number = _schema_contains_json_number(spec.document, definitions) or any(
        _schema_contains_json_number(definition, definitions) for definition in definitions.values()
    )
    if not has_number:
        return text
    has_strict_int = bool(re.search(r"\bStrictInt\b", text))
    has_strict_float = bool(re.search(r"\bStrictFloat\b", text))
    if not has_strict_int and not has_strict_float:
        return text
    if "_JsonInteger = Annotated" in text or "_JsonNumber = Annotated" in text:
        return text

    original = text
    marker = "\n\nclass "
    if marker not in text:
        return original

    typing_import = re.search(r"^from typing import ([^\n]+)$", text, flags=re.MULTILINE)
    if typing_import is None:
        # Tiny generated schemas may not need a typing import until this
        # post-processor introduces ``Annotated`` and ``Any`` helpers.
        future_import = re.search(
            r"^from __future__ import annotations\n",
            text,
            flags=re.MULTILINE,
        )
        if future_import is None:
            raise ContractConfigurationError(
                f"{spec.schema}: generated Python imports changed; cannot install "
                "the JSON number compatibility validator"
            )
        insertion = future_import.end()
        text = text[:insertion] + "\nfrom typing import Annotated, Any\n" + text[insertion:]
    else:
        typing_names = typing_import.group(1)
        names = [name.strip() for name in typing_names.split(",")]
        for required_name in ("Annotated", "Any"):
            if required_name not in names:
                names.insert(0, required_name)
        replacement = "from typing import " + ", ".join(names)
        text = text[: typing_import.start()] + replacement + text[typing_import.end() :]

    # datamodel-code-generator emits a one-line import for small schemas and
    # a parenthesised block for larger ones.  Canonicalise both forms before
    # replacing StrictInt/StrictFloat so the post-processor remains stable as
    # a Contract grows or shrinks.
    pydantic_block = re.search(
        r"^from pydantic import \(\n(?P<body>.*?)\n\)$",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if pydantic_block is not None:
        names = [line.strip().rstrip(",") for line in pydantic_block.group("body").splitlines()]
        start, end = pydantic_block.start(), pydantic_block.end()
    else:
        pydantic_line = re.search(
            r"^from pydantic import (?P<body>[^\n]+)$",
            text,
            flags=re.MULTILINE,
        )
        if pydantic_line is None:
            raise ContractConfigurationError(
                f"{spec.schema}: generated Pydantic imports changed; cannot install "
                "the JSON number compatibility validator"
            )
        names = [name.strip() for name in pydantic_line.group("body").split(",")]
        start, end = pydantic_line.start(), pydantic_line.end()

    names = [name for name in names if name not in {"StrictInt", "StrictFloat"}]
    for required_name in ("WithJsonSchema", "BeforeValidator"):
        if required_name not in names:
            names.insert(0, required_name)
    replacement = "from pydantic import (\n" + "".join(f"    {name},\n" for name in names) + ")"
    text = text[:start] + replacement + text[end:]
    text = re.sub(r"\bStrictInt\b", "_JsonInteger", text)
    text = re.sub(r"\bStrictFloat\b", "_JsonNumber", text)
    if "_JsonInteger" not in text and "_JsonNumber" not in text:
        return original
    helper_parts = [
        "\n\ndef _validate_json_number(value: Any) -> int | float:\n",
        "    if type(value) is int:\n",
        "        return value\n",
        "    if type(value) is float and value == value and abs(value) != float('inf'):\n",
        "        return value\n",
        "    raise ValueError('expected a finite JSON number')\n",
    ]
    if has_strict_int:
        helper_parts.extend(
            [
                "\n\ndef _validate_json_integer(value: Any) -> int | float:\n",
                "    if type(value) is int:\n",
                "        return value\n",
                "    if type(value) is float and value.is_integer():\n",
                "        return value\n",
                "    raise ValueError('expected an integral JSON number')\n",
            ]
        )
    if has_strict_float:
        helper_parts.append(
            "\n\n_JsonNumber = Annotated[\n"
            "    int | float,\n"
            "    BeforeValidator(_validate_json_number),\n"
            "    WithJsonSchema({'type': 'number'}),\n"
            "]"
        )
    if has_strict_int:
        helper_parts.append(
            "\n\n_JsonInteger = Annotated[\n"
            "    int | float,\n"
            "    BeforeValidator(_validate_json_integer),\n"
            "    WithJsonSchema({'type': 'integer'}),\n"
            "]"
        )
    helper = "".join(helper_parts)
    return text.replace(marker, helper + marker, 1)


def _registration_header(specs: tuple[ContractSpec, ...]) -> str:
    sources = b"\0".join(
        spec.relative_schema.as_posix().encode("utf-8") + b"\0" + spec.schema.read_bytes()
        for spec in specs
    )
    return (
        "# @generated by scripts/contracts/generate_gateway_contracts.py; do not edit.\n"
        f"# sources-sha256: {hashlib.sha256(sources).hexdigest()}\n"
        f"# generator-sha256: {_generator_digest()}\n"
        "# ruff: noqa\n\n"
    )


def _canonical_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return tuple(_canonical_value(item) for item in value)
    return value


def render_registration_descriptor(specs: tuple[ContractSpec, ...]) -> str:
    """Render the uniform Python registration descriptor consumed by Gateway adapters."""

    imports: list[str] = []
    method_entries: list[str] = []
    event_entries: list[str] = []
    for spec in specs:
        module = f"opensquilla.contracts.generated.v4.{spec.python_stem}"
        alias_prefix = f"_{spec.python_stem}"
        if spec.contract_type == "method":
            model_aliases = {role: f"{alias_prefix}_{role}_model" for role, _ in spec.targets}
            for role, definition in spec.targets:
                imports.append(f"from {module} import {definition} as {model_aliases[role]}")
            timeout = _canonical_value(spec.metadata.get("timeout"))
            capability = _canonical_value(spec.metadata.get("capability"))
            errors = _canonical_value(spec.metadata["errors"])
            method_entries.append(
                f"    {spec.wire_name!r}: GatewayMethodContract(\n"
                f"        name={spec.wire_name!r},\n"
                f"        kind={spec.semantic_kind!r},\n"
                f"        scope={spec.metadata['scope']!r},\n"
                f"        guest_allowed={spec.metadata['guestAllowed']!r},\n"
                f"        idempotency={spec.metadata['idempotency']!r},\n"
                f"        timeout={timeout!r},\n"
                f"        capability={capability!r},\n"
                f"        errors={errors!r},\n"
                f"        protocol={spec.protocol!r},\n"
                f"        wire_version={spec.wire_version!r},\n"
                f"        request_model={model_aliases['request']},\n"
                f"        params_model={model_aliases['params']},\n"
                f"        response_model={model_aliases['response']},\n"
                f"        result_model={model_aliases['result']},\n"
                "    ),"
            )
        else:
            role, definition = spec.targets[0]
            model_alias = f"{alias_prefix}_{role}_model"
            imports.append(f"from {module} import {definition} as {model_alias}")
            event_entries.append(
                f"    {spec.wire_name!r}: GatewayEventContract(\n"
                f"        name={spec.wire_name!r},\n"
                f"        delivery={spec.metadata['delivery']!r},\n"
                f"        schema_version={spec.metadata['schemaVersion']!r},\n"
                f"        protocol={spec.protocol!r},\n"
                f"        wire_version={spec.wire_version!r},\n"
                f"        {role}_model={model_alias},\n"
                "    ),"
            )

    imports_block = "\n".join(sorted(imports))
    method_block = "\n".join(method_entries)
    event_block = "\n".join(event_entries)
    body = f"""from dataclasses import dataclass
from typing import Any, Final

{imports_block}


@dataclass(frozen=True, slots=True)
class GatewayMethodContract:
    name: str
    kind: str
    scope: str
    guest_allowed: bool
    idempotency: str
    timeout: dict[str, Any] | None
    capability: dict[str, Any] | None
    errors: tuple[dict[str, Any], ...]
    protocol: str
    wire_version: int
    request_model: type[Any]
    params_model: type[Any]
    response_model: type[Any]
    result_model: type[Any]


@dataclass(frozen=True, slots=True)
class GatewayEventContract:
    name: str
    delivery: str
    schema_version: int
    protocol: str
    wire_version: int
    frame_model: type[Any] | None = None
    payload_model: type[Any] | None = None


GATEWAY_METHOD_CONTRACTS: Final[dict[str, GatewayMethodContract]] = {{
{method_block}
}}

GATEWAY_EVENT_CONTRACTS: Final[dict[str, GatewayEventContract]] = {{
{event_block}
}}
"""
    return _registration_header(specs) + body


def _json_literal(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _python_metadata(spec: ContractSpec) -> str:
    prefix = spec.constant_prefix
    lines = [
        "from typing import Final",
        "",
        f"{prefix}_CONTRACT_KIND: Final = {spec.semantic_kind!r}",
    ]
    if spec.contract_type == "method":
        lines.extend(
            [
                f"{prefix}_METHOD: Final = {spec.wire_name!r}",
                f"{prefix}_SCOPE: Final = {spec.metadata['scope']!r}",
                f"{prefix}_IDEMPOTENCY: Final = {spec.metadata['idempotency']!r}",
                f"{prefix}_TIMEOUT: Final = {spec.metadata.get('timeout')!r}",
                f"{prefix}_CAPABILITY: Final = {spec.metadata.get('capability')!r}",
                f"{prefix}_ERRORS: Final = {spec.metadata['errors']!r}",
            ]
        )
    else:
        lines.append(f"{prefix}_EVENT: Final = {spec.wire_name!r}")
        lines.append(f"{prefix}_SCHEMA_VERSION: Final = {spec.metadata['schemaVersion']!r}")
        lines.append(f"{prefix}_EVENT_METADATA: Final = {spec.metadata!r}")
    return _normalise(spec, "\n".join(lines) + "\n", prefix="#")


def _typescript_metadata(spec: ContractSpec, text: str) -> str:
    prefix = spec.constant_prefix
    lines = [
        f"export const {prefix}_CONTRACT_KIND = {_json_literal(spec.semantic_kind)} as const",
    ]
    if spec.contract_type == "method":
        idempotency = _json_literal(spec.metadata["idempotency"])
        timeout = _json_literal(spec.metadata.get("timeout"))
        capability = _json_literal(spec.metadata.get("capability"))
        lines.extend(
            [
                f"export const {prefix}_METHOD = {_json_literal(spec.wire_name)} as const",
                f"export const {prefix}_SCOPE = {_json_literal(spec.metadata['scope'])} as const",
                f"export const {prefix}_IDEMPOTENCY = {idempotency} as const",
                f"export const {prefix}_TIMEOUT = {timeout} as const",
                f"export const {prefix}_CAPABILITY = {capability} as const",
                f"export const {prefix}_ERRORS = {_json_literal(spec.metadata['errors'])} as const",
            ]
        )
    else:
        lines.append(f"export const {prefix}_EVENT = {_json_literal(spec.wire_name)} as const")
        lines.append(
            f"export const {prefix}_SCHEMA_VERSION = "
            f"{_json_literal(spec.metadata['schemaVersion'])} as const"
        )
        lines.append(
            f"export const {prefix}_EVENT_METADATA = {_json_literal(spec.metadata)} as const"
        )
    return text.rstrip() + "\n\n" + "\n".join(lines) + "\n"


def _validator_declarations(
    spec: ContractSpec,
    roles: tuple[str, ...] | None = None,
) -> str:
    exports = "\n".join(
        f"export const validate{definition}: ContractValidator"
        for role, definition in spec.targets
        if roles is None or role in roles
    )
    return _normalise(
        spec,
        "export interface ContractValidator {\n"
        "  (value: unknown): boolean\n"
        "  errors?: readonly unknown[] | null\n"
        "}\n\n"
        f"{exports}\n",
        prefix="//",
    )


def _render_validators(
    spec: ContractSpec,
    roles: tuple[str, ...] | None,
) -> dict[Path, str]:
    if roles == ():
        return {}
    available = {role for role, _ in spec.targets}
    if roles is not None and (not set(roles) <= available or len(set(roles)) != len(roles)):
        raise ContractConfigurationError(f"invalid validator roles for {spec.wire_name}")
    command = ["node", str(AJV_GENERATOR), str(spec.schema)]
    if not spec.uses_legacy_generator:
        command.append("--esm")
    if roles is not None:
        command.extend(["--roles", ",".join(roles)])
    validator = _capture(
        command,
        env=_environment(),
        purpose=f"validator generation for {spec.wire_name}",
    )
    return {
        spec.outputs[3]: _normalise(spec, validator, prefix="//"),
        spec.outputs[4]: _validator_declarations(spec, roles),
    }


def _typescript_params_schema(spec: ContractSpec) -> dict[str, Any]:
    """Expose required-only object alternatives to the pinned TS compiler.

    json-schema-to-typescript otherwise loses the surrounding properties when
    an anyOf branch only names required fields. Distribute those constraints
    in a private compiler input; the wire schema and runtime validators retain
    their original source.
    """

    document = copy.deepcopy(spec.document)
    if spec.contract_type != "method":
        return document
    for name in _reachable_definition_names(spec, spec.target("params")):
        schema = document["$defs"][name]
        branches = schema.get("anyOf")
        properties = schema.get("properties")
        if (
            schema.get("type") != "object"
            or not isinstance(properties, dict)
            or not isinstance(branches, list)
            or not branches
            or any(key in schema for key in ("$id", "$anchor", "$dynamicAnchor", "$defs"))
            or not all(
                isinstance(branch, dict)
                and set(branch) == {"required"}
                and isinstance(branch["required"], list)
                and all(isinstance(key, str) and key in properties for key in branch["required"])
                for branch in branches
            )
        ):
            continue
        common = {key: value for key, value in schema.items() if key != "anyOf"}
        document["$defs"][name] = {
            "anyOf": [
                {
                    **common,
                    "required": list(
                        dict.fromkeys([*common.get("required", []), *branch["required"]])
                    ),
                }
                for branch in branches
            ]
        }
    return document


def render_generic(
    spec: ContractSpec,
    *,
    validator_roles: tuple[str, ...] | None = None,
) -> dict[Path, str]:
    """Render one non-legacy Contract with the pinned generators."""

    if spec.uses_legacy_generator:
        raise ContractConfigurationError(
            f"{spec.schema}: legacy Contract must use its compatibility generator"
        )
    env = _environment()
    with tempfile.TemporaryDirectory(prefix="opensquilla-jsonschema-codegen-") as raw_tmp:
        tmp = Path(raw_tmp)
        python_tmp = tmp / f"{spec.python_stem}.py"
        typescript_tmp = tmp / f"{spec.typescript_stem}.ts"
        typescript_schema = _typescript_params_schema(spec)
        typescript_input = spec.schema
        if typescript_schema != spec.document:
            typescript_input = tmp / spec.schema.name
            _write_text_lf(typescript_input, json.dumps(typescript_schema, ensure_ascii=False))
        _run(
            [
                sys.executable,
                "-m",
                "datamodel_code_generator",
                "--input",
                str(spec.schema),
                "--input-file-type",
                "jsonschema",
                "--output",
                str(python_tmp),
                "--output-model-type",
                "pydantic_v2.BaseModel",
                "--target-python-version",
                "3.12",
                "--use-standard-collections",
                "--use-union-operator",
                "--use-schema-description",
                "--field-constraints",
                "--strict-types",
                "str",
                "int",
                "float",
                "bool",
                "--formatters",
                "builtin",
                "--disable-timestamp",
            ],
            env=env,
            purpose=f"Python generation for {spec.wire_name}",
        )
        _run(
            [
                "npm",
                "--prefix",
                "opensquilla-webui",
                "exec",
                "--",
                "json2ts",
                "--input",
                str(typescript_input),
                "--cwd",
                str(spec.schema.parent),
                "--output",
                str(typescript_tmp),
                "--unreachableDefinitions",
                "--bannerComment",
                "",
            ],
            env=env,
            purpose=f"TypeScript generation for {spec.wire_name}",
        )
        validators = _render_validators(spec, validator_roles)
        python_output, metadata_output, typescript_output = spec.outputs[:3]
        python_source = _normalise_json_number_types(
            spec,
            python_tmp.read_text(encoding="utf-8"),
        )
        python_source = _normalise_optional_non_nullable_defaults(spec, python_source)
        return {
            python_output: _normalise(
                spec,
                python_source,
                prefix="#",
            ),
            metadata_output: _python_metadata(spec),
            typescript_output: _normalise(
                spec,
                _typescript_metadata(
                    spec,
                    typescript_tmp.read_text(encoding="utf-8"),
                ),
                prefix="//",
            ),
            **validators,
        }


def _load_legacy_generator(generator: Path) -> Any:
    """Load a frozen compatibility generator without changing its source bytes."""

    module_name = f"_opensquilla_legacy_contract_{generator.stem}"
    module_spec = importlib.util.spec_from_file_location(module_name, generator)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"cannot load legacy Contract generator: {generator}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    try:
        module_spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def render_legacy(spec: ContractSpec) -> dict[Path, str]:
    """Render frozen legacy bytes for type generation and compatibility tests."""
    generator = next(
        generator
        for schema, generator in LEGACY_GENERATORS.items()
        if schema.resolve() == spec.schema.resolve()
    )
    legacy = _load_legacy_generator(generator)
    legacy_run = legacy._run

    def portable_run(command: list[str], *, env: dict[str, str]) -> None:
        legacy_run(_resolved_command(command), env=env)

    # Keep the historical generator byte-for-byte stable while making its
    # npm invocation work on Windows, where the executable is npm.cmd.
    legacy._run = portable_run
    rendered = legacy.render()
    return dict(
        zip(
            spec.outputs,
            (
                rendered.python,
                rendered.python_metadata,
                rendered.typescript,
                rendered.validator_javascript,
                rendered.validator_declarations,
            ),
            strict=True,
        )
    )


def _canonical_schema_bytes(spec: ContractSpec) -> bytes:
    return json.dumps(
        spec.document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _schema_digest(spec: ContractSpec) -> str:
    return hashlib.sha256(_canonical_schema_bytes(spec)).hexdigest()


def _schema_tree_digest(specs: tuple[ContractSpec, ...]) -> str:
    digest = hashlib.sha256()
    for spec in sorted(specs, key=lambda item: item.relative_schema.as_posix()):
        digest.update(spec.relative_schema.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_canonical_schema_bytes(spec))
        digest.update(b"\0")
    return digest.hexdigest()


def render_compatibility_manifest(specs: tuple[ContractSpec, ...]) -> str:
    """Render the Schema-owned method lifecycle and event-family identities."""

    method_specs = {spec.wire_name: spec for spec in specs if spec.contract_type == "method"}
    for spec in method_specs.values():
        lifecycle = str(spec.metadata.get("lifecycle", "stable"))
        if lifecycle == "legacy":
            canonical_name = str(spec.metadata["canonicalAlias"])
            canonical = method_specs.get(canonical_name)
            if canonical is None:
                raise ContractConfigurationError(
                    f"{spec.schema}: canonicalAlias {canonical_name!r} has no method Contract"
                )
            if canonical.metadata.get("lifecycle", "stable") != "stable":
                raise ContractConfigurationError(
                    f"{spec.schema}: canonicalAlias {canonical_name!r} is not stable"
                )
            aliases = canonical.metadata.get("compatibilityAliases", [])
            if spec.wire_name not in aliases:
                raise ContractConfigurationError(
                    f"{canonical.schema}: compatibilityAliases must include "
                    f"legacy method {spec.wire_name!r}"
                )
        else:
            for alias in spec.metadata.get("compatibilityAliases", []):
                legacy = method_specs.get(alias)
                if (
                    legacy is None
                    or legacy.metadata.get("lifecycle") != "legacy"
                    or legacy.metadata.get("canonicalAlias") != spec.wire_name
                ):
                    raise ContractConfigurationError(
                        f"{spec.schema}: compatibility alias {alias!r} must be backed "
                        "by a legacy method Contract"
                    )

    methods: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for spec in specs:
        schema_path = spec.relative_schema.as_posix()
        schema_digest = _schema_digest(spec)
        if spec.contract_type == "method":
            lifecycle = str(spec.metadata.get("lifecycle", "stable"))
            entry: dict[str, Any] = {
                "name": spec.wire_name,
                "lifecycle": lifecycle,
                "schema": schema_path,
                "schemaSha256": schema_digest,
            }
            if lifecycle == "legacy":
                entry["canonicalName"] = spec.metadata["canonicalAlias"]
            aliases = spec.metadata.get("compatibilityAliases", [])
            if aliases:
                entry["compatibilityAliases"] = list(aliases)
            methods.append(entry)
            continue

        declared_wire_names = spec.metadata.get("wireNames")
        wire_names = (
            list(declared_wire_names) if isinstance(declared_wire_names, list) else [spec.wire_name]
        )
        events.append(
            {
                "family": spec.wire_name,
                "wireNames": wire_names,
                "delivery": spec.metadata["delivery"],
                "schemaVersion": spec.metadata["schemaVersion"],
                "schema": schema_path,
                "schemaSha256": schema_digest,
            }
        )

    methods.sort(key=lambda entry: str(entry["name"]))
    events.sort(key=lambda entry: str(entry["family"]))
    manifest = {
        "format": 1,
        "protocol": GATEWAY_PROTOCOL,
        "wireVersion": 4,
        "source": {
            "schemaCount": len(specs),
            "methodCount": len(methods),
            "eventFamilyCount": len(events),
            "schemaTreeSha256": _schema_tree_digest(specs),
            "generatorSha256": _generator_digest(),
        },
        "methods": methods,
        "events": events,
    }
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


GENERATED_MARKERS = (
    "@generated by scripts/contracts/generate_gateway_contracts.py",
    "@generated by scripts/contracts/generate_sessions_list_contract.py",
)


def _is_marker_owned(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open(encoding="utf-8") as artifact:
            first_line = artifact.readline()
    except (OSError, UnicodeDecodeError):
        return False
    return any(marker in first_line for marker in GENERATED_MARKERS)


def marker_owned_artifacts(
    roots: tuple[Path, ...] = (PYTHON_OUTPUT_ROOT, TYPESCRIPT_OUTPUT_ROOT),
) -> frozenset[Path]:
    """Inventory artifacts that explicitly declare this runner as their owner."""

    return frozenset(
        path
        for root in roots
        if root.exists()
        for path in root.rglob("*")
        if _is_marker_owned(path)
    )


def reconcile_orphans(
    expected: frozenset[Path],
    *,
    mode: Mode,
    roots: tuple[Path, ...] = (PYTHON_OUTPUT_ROOT, TYPESCRIPT_OUTPUT_ROOT),
) -> int:
    """Reject stale owned files, or delete only owned files during ``--write``."""

    orphans = sorted(marker_owned_artifacts(roots) - expected, key=lambda path: path.as_posix())
    if not orphans:
        return 0
    if mode == "write":
        for path in orphans:
            path.unlink()
            print(f"removed orphaned Gateway Contract artifact: {path}")
        return 0
    if mode == "check":
        for path in orphans:
            print(f"orphaned generated Gateway Contract artifact: {path}", file=sys.stderr)
        return 1
    return 0


def expected_artifacts(
    specs: tuple[ContractSpec, ...],
    *,
    profile: Profile = "production",
) -> frozenset[Path]:
    targets = load_production_targets(discover_contracts()) if profile == "production" else None
    return frozenset(
        [REGISTRATION_OUTPUT, COMPATIBILITY_MANIFEST_OUTPUT]
        + [
            output
            for spec in specs
            for output in (
                spec.outputs
                if targets is None or targets.get((spec.contract_type, spec.wire_name))
                else spec.outputs[:3]
            )
        ]
    )


def build_hash_manifest(
    specs: tuple[ContractSpec, ...],
    *,
    profile: Profile = "production",
    output_root: Path | None = None,
) -> dict[str, object]:
    """Build a portable, derived manifest for cross-platform CI comparison."""

    destination = _destination_root(profile, output_root)
    expected = frozenset(
        destination / path.relative_to(ROOT) for path in expected_artifacts(specs, profile=profile)
    )
    _validate_output_tree(destination, expected)
    missing = sorted((path for path in expected if not path.exists()), key=str)
    if missing:
        raise RuntimeError(
            "cannot hash missing generated artifacts: " + ", ".join(str(path) for path in missing)
        )
    artifacts: dict[str, str] = {}
    for path in sorted(expected, key=lambda item: item.as_posix()):
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        relative = path.relative_to(destination).as_posix()
        artifacts[relative] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {"format": 1, "artifacts": artifacts}


def write_hash_manifest(
    path: Path,
    specs: tuple[ContractSpec, ...],
    *,
    profile: Profile = "production",
    output_root: Path | None = None,
) -> None:
    manifest = build_hash_manifest(specs, profile=profile, output_root=output_root)
    _write_text_lf(
        path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )


def _validate_hash_manifest(value: object, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"format", "artifacts"}:
        raise RuntimeError(f"{label} Contract hash manifest must contain only format and artifacts")
    if type(value["format"]) is not int or value["format"] != 1:
        raise RuntimeError(f"{label} Contract hash manifest format must be 1")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, dict) or not artifacts:
        raise RuntimeError(
            f"{label} Contract hash manifest artifacts must be a non-empty JSON object"
        )
    validated: dict[str, str] = {}
    for raw_path, digest in artifacts.items():
        if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
            raise RuntimeError(f"{label} Contract hash manifest has an invalid path")
        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != raw_path:
            raise RuntimeError(
                f"{label} Contract hash manifest path must be canonical and repo-relative: "
                f"{raw_path!r}"
            )
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise RuntimeError(
                f"{label} Contract hash manifest digest must be lowercase sha256: {raw_path!r}"
            )
        validated[raw_path] = digest
    return validated


def compare_hash_manifests(left: Path, right: Path) -> int:
    try:
        left_manifest = json.loads(left.read_text(encoding="utf-8"))
        right_manifest = json.loads(right.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read Contract hash manifest: {exc}") from exc
    left_artifacts = _validate_hash_manifest(left_manifest, label="left")
    right_artifacts = _validate_hash_manifest(right_manifest, label="right")
    if left_artifacts == right_artifacts:
        return 0
    paths = sorted(set(left_artifacts) | set(right_artifacts))
    for path in paths:
        if left_artifacts.get(path) != right_artifacts.get(path):
            print(
                f"cross-platform Contract hash mismatch: {path}: "
                f"{left_artifacts.get(path)} != {right_artifacts.get(path)}",
                file=sys.stderr,
            )
    return 1


def _destination_root(profile: Profile, output_root: Path | None) -> Path:
    if profile not in ("production", "verification"):
        raise ContractConfigurationError(f"unknown Contract profile: {profile}")
    if profile == "production" and output_root is None:
        return ROOT
    if output_root is None:
        raise ContractConfigurationError("verification output must be outside the production tree")
    destination = output_root.resolve()
    if destination.is_relative_to(ROOT) or ROOT.is_relative_to(destination):
        raise ContractConfigurationError("profile output must be outside the production tree")
    return destination


def _validate_output_tree(destination: Path, paths: frozenset[Path]) -> None:
    """Preflight the complete publication/cleanup surface before touching files."""
    roots = tuple(
        destination / root.relative_to(ROOT)
        for root in (PYTHON_OUTPUT_ROOT, TYPESCRIPT_OUTPUT_ROOT)
    )
    for path in (*paths, *roots):
        if not path.resolve().is_relative_to(destination):
            raise ContractConfigurationError(f"generated output is outside its tree: {path}")
        for ancestor in (path, *path.parents):
            if ancestor == destination:
                break
            if ancestor.is_symlink() or ancestor.is_junction():
                raise ContractConfigurationError(f"generated output contains a link: {ancestor}")
    pending = list(roots)
    while pending:
        path = pending.pop()
        if path.is_symlink() or path.is_junction():
            raise ContractConfigurationError(f"generated output contains a link: {path}")
        if path.is_dir():
            pending.extend(path.iterdir())


def render_tree(
    specs: tuple[ContractSpec, ...],
    *,
    profile: Profile = "production",
) -> dict[Path, str]:
    """Compile the entire tree before publishing artifacts or deleting orphans."""
    targets = load_production_targets(discover_contracts()) if profile == "production" else None
    ordered = tuple(sorted(specs, key=lambda spec: spec.relative_schema.as_posix()))
    rendered: dict[Path, str] = {}
    for spec in ordered:
        roles = None if targets is None else targets.get((spec.contract_type, spec.wire_name), ())
        if spec.uses_legacy_generator:
            frozen = render_legacy(spec)
            artifacts = {path: frozen[path] for path in spec.outputs[:3]}
            # Verification also materializes the two sessions.list roles
            # absent from the frozen generator. Old bytes have a separate
            # exact-output fixture, not a fabricated differential baseline.
            artifacts.update(_render_validators(spec, roles))
        else:
            artifacts = render_generic(spec, validator_roles=roles)
        duplicate = rendered.keys() & artifacts.keys()
        if duplicate:
            raise ContractConfigurationError(f"duplicate generated artifact: {sorted(duplicate)}")
        rendered.update(artifacts)
    rendered[REGISTRATION_OUTPUT] = render_registration_descriptor(ordered)
    rendered[COMPATIBILITY_MANIFEST_OUTPUT] = render_compatibility_manifest(ordered)
    return rendered


def run(
    mode: Mode,
    specs: tuple[ContractSpec, ...] | None = None,
    *,
    profile: Profile = "production",
    output_root: Path | None = None,
) -> int:
    """Generate, check, or independently regenerate a complete profile tree."""
    destination = _destination_root(profile, output_root)
    selected = specs if specs is not None else discover_contracts()
    rendered = render_tree(selected, profile=profile)
    expected = frozenset(destination / path.relative_to(ROOT) for path in rendered)
    _validate_output_tree(destination, expected)
    if mode == "verify-determinism":
        # Each render invokes the pinned tools in independent temporary dirs.
        second = render_tree(tuple(reversed(selected)), profile=profile)
        if rendered != second:
            print("non-deterministic Gateway Contract artifact tree", file=sys.stderr)
            return 1
        return 0
    failed = False
    for path, content in rendered.items():
        target = destination / path.relative_to(ROOT)
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == content:
            continue
        if mode == "write":
            _write_text_lf(target, content)
        else:
            print(f"stale generated Gateway Contract artifact: {target}", file=sys.stderr)
            failed = True
    roots = tuple(
        destination / root.relative_to(ROOT)
        for root in (
            PYTHON_OUTPUT_ROOT,
            TYPESCRIPT_OUTPUT_ROOT,
        )
    )
    return int(bool(reconcile_orphans(expected, mode=mode, roots=roots)) or failed)


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--verify-determinism", action="store_true")
    mode.add_argument("--hash-manifest", type=Path)
    mode.add_argument("--compare-hash-manifests", nargs=2, type=Path)
    parser.add_argument("--profile", choices=("production", "verification"), default="production")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    try:
        if args.compare_hash_manifests:
            left, right = args.compare_hash_manifests
            return compare_hash_manifests(left, right)
        specs = discover_contracts()
        if args.hash_manifest:
            write_hash_manifest(
                args.hash_manifest,
                specs,
                profile=args.profile,
                output_root=args.output_root,
            )
            return 0
        selected_mode: Mode
        if args.write:
            selected_mode = "write"
        elif args.verify_determinism:
            selected_mode = "verify-determinism"
        else:
            selected_mode = "check"
        return run(selected_mode, specs, profile=args.profile, output_root=args.output_root)
    except (ContractConfigurationError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
