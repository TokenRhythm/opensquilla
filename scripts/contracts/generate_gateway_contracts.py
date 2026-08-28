#!/usr/bin/env python3
"""Discover and generate every language-neutral Gateway v4 Contract.

``sessions.list`` predates this aggregate runner.  It deliberately keeps its
original entry point so that adopting the runner does not rewrite its already
reviewed generated artifacts.  New Contracts use the generic JSON Schema
2020-12 path below.
"""

from __future__ import annotations

import argparse
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

WIRE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FILE_STEM_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
METHOD_KINDS = frozenset({"query", "command"})
IDEMPOTENCY_KINDS = frozenset({"read-only", "idempotent", "non-idempotent"})
TIMEOUT_POLICIES = frozenset({"caller", "server", "transport"})
CAPABILITY_KINDS = frozenset({"method-availability"})

Mode = Literal["write", "check", "verify-determinism"]


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
        return (
            PYTHON_OUTPUT_ROOT / f"{self.python_stem}.py",
            PYTHON_OUTPUT_ROOT / f"{self.python_stem}_metadata.py",
            TYPESCRIPT_OUTPUT_ROOT / f"{self.typescript_stem}.ts",
            TYPESCRIPT_OUTPUT_ROOT / f"{self.typescript_stem}Validators.cjs",
            TYPESCRIPT_OUTPUT_ROOT / f"{self.typescript_stem}Validators.d.cts",
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
        raise ContractConfigurationError(
            f"{schema}: Contract must use JSON Schema 2020-12"
        )
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
        raise ContractConfigurationError(
            f"{schema}: Gateway protocol must be {GATEWAY_PROTOCOL!r}"
        )
    wire_version = wire.get("version")
    if wire_version != 4:
        raise ContractConfigurationError(f"{schema}: only Gateway wire version 4 is supported")
    if wire.get("compatibility") != "exact-json-tree":
        raise ContractConfigurationError(
            f"{schema}: Gateway Contract compatibility must be 'exact-json-tree'"
        )
    file_stem = schema.name.removesuffix(".schema.json")
    if not FILE_STEM_PATTERN.fullmatch(file_stem):
        raise ContractConfigurationError(
            f"{schema}: Contract filename must use lower-kebab-case"
        )

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
        if not WIRE_NAME_PATTERN.fullmatch(wire_name):
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
            raise ContractConfigurationError(
                f"{schema}: unsupported idempotency {idempotency!r}"
            )
        errors = metadata.get("errors")
        if not isinstance(errors, list):
            raise ContractConfigurationError(
                f"{schema}: method Contract errors must be an array"
            )
        for error in errors:
            error_metadata = _require_mapping(
                error,
                label="method Contract error",
                schema=schema,
            )
            error_code = _require_string(error_metadata, "code", schema=schema)
            if not ERROR_CODE_PATTERN.fullmatch(error_code):
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
            raise ContractConfigurationError(
                f"{schema}: unsupported method kind {semantic_kind!r}"
            )
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
            if not WIRE_NAME_PATTERN.fullmatch(capability_name):
                raise ContractConfigurationError(
                    f"{schema}: capability name {capability_name!r} is not legal"
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
        schema_version = metadata.get("schemaVersion")
        if type(schema_version) is not int or schema_version < 1:
            raise ContractConfigurationError(
                f"{schema}: event Contract schemaVersion must be a positive integer"
            )
        declared_event_targets = [
            role for role in ("frame", "payload") if role in metadata
        ]
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
    source = Path(__file__).read_bytes() + b"\0" + AJV_GENERATOR.read_bytes()
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


def _registration_header(specs: tuple[ContractSpec, ...]) -> str:
    sources = b"\0".join(
        spec.relative_schema.as_posix().encode("utf-8")
        + b"\0"
        + spec.schema.read_bytes()
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
            model_aliases = {
                role: f"{alias_prefix}_{role}_model" for role, _ in spec.targets
            }
            for role, definition in spec.targets:
                imports.append(
                    f"from {module} import {definition} as {model_aliases[role]}"
                )
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
    body = f'''from dataclasses import dataclass
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
'''
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
            f"export const {prefix}_EVENT_METADATA = {_json_literal(spec.metadata)} as const"
        )
    return text.rstrip() + "\n\n" + "\n".join(lines) + "\n"


def _validator_declarations(spec: ContractSpec) -> str:
    exports = "\n".join(
        f"export const validate{definition}: ContractValidator"
        for _, definition in spec.targets
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


def render_generic(spec: ContractSpec) -> dict[Path, str]:
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
                str(spec.schema),
                "--output",
                str(typescript_tmp),
                "--unreachableDefinitions",
                "--bannerComment",
                "",
            ],
            env=env,
            purpose=f"TypeScript generation for {spec.wire_name}",
        )
        validator = _capture(
            ["node", str(AJV_GENERATOR), str(spec.schema)],
            env=env,
            purpose=f"validator generation for {spec.wire_name}",
        )
        python_output, metadata_output, typescript_output, validator_output, declarations_output = (
            spec.outputs
        )
        return {
            python_output: _normalise(
                spec,
                python_tmp.read_text(encoding="utf-8"),
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
            validator_output: _normalise(spec, validator, prefix="//"),
            declarations_output: _validator_declarations(spec),
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


def _run_legacy(spec: ContractSpec, mode: Mode) -> int:
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
    first = legacy.render()
    if mode == "verify-determinism":
        return int(first != legacy.render())
    if mode == "write":
        for path, content in (
            (legacy.PYTHON_OUTPUT, first.python),
            (legacy.PYTHON_METADATA_OUTPUT, first.python_metadata),
            (legacy.TYPESCRIPT_OUTPUT, first.typescript),
            (legacy.VALIDATOR_OUTPUT, first.validator_javascript),
            (legacy.VALIDATOR_DECLARATIONS_OUTPUT, first.validator_declarations),
        ):
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != content:
                _write_text_lf(path, content)
        return 0
    return int(legacy._check(first))


def _run_generic(spec: ContractSpec, mode: Mode) -> int:
    rendered = render_generic(spec)
    if mode == "verify-determinism":
        if rendered != render_generic(spec):
            print(f"non-deterministic Contract generation: {spec.schema}", file=sys.stderr)
            return 1
        return 0
    if mode == "write":
        for path, content in rendered.items():
            _write_text_lf(path, content)
        return 0

    stale = []
    for path, content in rendered.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != content:
            stale.append(path)
    for path in stale:
        print(f"stale generated Gateway Contract artifact: {path}", file=sys.stderr)
    return int(bool(stale))


def _run_registration_descriptor(specs: tuple[ContractSpec, ...], mode: Mode) -> int:
    rendered = render_registration_descriptor(specs)
    if mode == "verify-determinism":
        return int(rendered != render_registration_descriptor(specs))
    if mode == "write":
        _write_text_lf(REGISTRATION_OUTPUT, rendered)
        return 0
    current = (
        REGISTRATION_OUTPUT.read_text(encoding="utf-8")
        if REGISTRATION_OUTPUT.exists()
        else None
    )
    if current != rendered:
        print(
            f"stale generated Gateway Contract artifact: {REGISTRATION_OUTPUT}",
            file=sys.stderr,
        )
        return 1
    return 0


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


def expected_artifacts(specs: tuple[ContractSpec, ...]) -> frozenset[Path]:
    return frozenset(
        [REGISTRATION_OUTPUT]
        + [output for spec in specs for output in spec.outputs]
    )


def build_hash_manifest(specs: tuple[ContractSpec, ...]) -> dict[str, object]:
    """Build a portable, derived manifest for cross-platform CI comparison."""

    expected = expected_artifacts(specs)
    missing = sorted((path for path in expected if not path.exists()), key=str)
    if missing:
        raise RuntimeError(
            "cannot hash missing generated artifacts: "
            + ", ".join(str(path) for path in missing)
        )
    artifacts: dict[str, str] = {}
    for path in sorted(expected, key=lambda item: item.as_posix()):
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        relative = path.relative_to(ROOT).as_posix()
        artifacts[relative] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {"format": 1, "artifacts": artifacts}


def write_hash_manifest(path: Path, specs: tuple[ContractSpec, ...]) -> None:
    manifest = build_hash_manifest(specs)
    _write_text_lf(
        path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )


def _validate_hash_manifest(value: object, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"format", "artifacts"}:
        raise RuntimeError(
            f"{label} Contract hash manifest must contain only format and artifacts"
        )
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
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != raw_path
        ):
            raise RuntimeError(
                f"{label} Contract hash manifest path must be canonical and repo-relative: "
                f"{raw_path!r}"
            )
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise RuntimeError(
                f"{label} Contract hash manifest digest must be lowercase sha256: "
                f"{raw_path!r}"
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


def run(mode: Mode, specs: tuple[ContractSpec, ...] | None = None) -> int:
    """Run the requested mode for all discovered Contracts."""

    selected = specs if specs is not None else discover_contracts()
    failed = False
    for spec in selected:
        result = _run_legacy(spec, mode) if spec.uses_legacy_generator else _run_generic(spec, mode)
        failed = bool(result) or failed
    failed = bool(_run_registration_descriptor(selected, mode)) or failed
    failed = bool(
        reconcile_orphans(expected_artifacts(selected), mode=mode)
    ) or failed
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--verify-determinism", action="store_true")
    mode.add_argument("--hash-manifest", type=Path)
    mode.add_argument("--compare-hash-manifests", nargs=2, type=Path)
    args = parser.parse_args()
    try:
        if args.compare_hash_manifests:
            left, right = args.compare_hash_manifests
            return compare_hash_manifests(left, right)
        specs = discover_contracts()
        if args.hash_manifest:
            write_hash_manifest(args.hash_manifest, specs)
            return 0
        selected_mode: Mode
        if args.write:
            selected_mode = "write"
        elif args.verify_determinism:
            selected_mode = "verify-determinism"
        else:
            selected_mode = "check"
        return run(selected_mode, specs)
    except (ContractConfigurationError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
