"""Tests for the aggregate Gateway v4 Contract generation runner."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.contracts import generate_gateway_contracts as runner


def _method_schema(name: str) -> dict[str, Any]:
    pascal = "".join(part.title() for part in name.replace("_", ".").split("."))
    request = f"{pascal}RequestFrame"
    params = f"{pascal}Params"
    response = f"{pascal}ResponseFrame"
    result = f"{pascal}Result"
    return {
        "$schema": runner.JSON_SCHEMA_2020_12,
        "$id": f"https://opensquilla.dev/contracts/gateway/v4/{name}.schema.json",
        "type": "object",
        "x-opensquilla-wire": {
            "protocol": "opensquilla-websocket-json",
            "version": 4,
            "compatibility": "exact-json-tree",
        },
        "x-opensquilla-codegen": copy.deepcopy(runner.PINNED_CODEGEN),
        "x-opensquilla-method": {
            "name": name,
            "kind": "query",
            "scope": "operator.read",
            "guestAllowed": False,
            "idempotency": "read-only",
            "timeout": {"policy": "caller"},
            "capability": {"kind": "method-availability", "name": name},
            "errors": [{"code": "INTERNAL_ERROR"}],
            "request": f"#/$defs/{request}",
            "params": f"#/$defs/{params}",
            "response": f"#/$defs/{response}",
            "result": f"#/$defs/{result}",
        },
        "$defs": {
            request: {"type": "object"},
            params: {"type": "object"},
            response: {"type": "object"},
            result: {"type": "object"},
        },
    }


def _write_schema(root: Path, relative: str, document: dict[str, Any]) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_discovery_scans_every_schema_in_stable_path_order(tmp_path: Path) -> None:
    _write_schema(
        tmp_path,
        "workspaces/workspaces-list.schema.json",
        _method_schema("workspaces.list"),
    )
    _write_schema(
        tmp_path,
        "sessions/sessions-resolve.schema.json",
        _method_schema("sessions.resolve"),
    )

    specs = runner.discover_contracts(tmp_path)

    assert [spec.wire_name for spec in specs] == [
        "sessions.resolve",
        "workspaces.list",
    ]
    assert specs[0].python_stem == "sessions_resolve"
    assert specs[0].typescript_stem == "sessionsResolve"
    assert specs[0].semantic_kind == "query"
    assert specs[0].targets == (
        ("request", "SessionsResolveRequestFrame"),
        ("params", "SessionsResolveParams"),
        ("response", "SessionsResolveResponseFrame"),
        ("result", "SessionsResolveResult"),
    )


def test_event_contract_uses_declared_frame_as_validator_target(tmp_path: Path) -> None:
    document = _method_schema("sessions.changed")
    document.pop("x-opensquilla-method")
    document["x-opensquilla-event"] = {
        "name": "sessions.changed",
        "frame": "#/$defs/SessionsChangedEventFrame",
        "delivery": "live-only-best-effort",
        "schemaVersion": 1,
    }
    document["$defs"] = {"SessionsChangedEventFrame": {"type": "object"}}
    schema = _write_schema(
        tmp_path,
        "sessions/sessions-changed.schema.json",
        document,
    )

    spec = runner.load_contract(schema, contract_root=tmp_path)

    assert spec.contract_type == "event"
    assert spec.semantic_kind == "event"
    assert spec.targets == (
        ("frame", "SessionsChangedEventFrame"),
    )


def test_event_contract_rejects_boolean_schema_version(tmp_path: Path) -> None:
    document = _method_schema("sessions.changed")
    document.pop("x-opensquilla-method")
    document["x-opensquilla-event"] = {
        "name": "sessions.changed",
        "frame": "#/$defs/SessionsChangedEventFrame",
        "delivery": "live-only-best-effort",
        "schemaVersion": True,
    }
    document["$defs"] = {"SessionsChangedEventFrame": {"type": "object"}}
    schema = _write_schema(
        tmp_path,
        "sessions/sessions-changed.schema.json",
        document,
    )

    with pytest.raises(runner.ContractConfigurationError, match="positive integer"):
        runner.load_contract(schema, contract_root=tmp_path)


def test_event_python_renderer_tightens_reachable_optional_fields() -> None:
    spec = next(
        spec
        for spec in runner.discover_contracts()
        if spec.wire_name == "sessions.changed"
    )
    generated = (
        "from pydantic import BaseModel\n\n"
        "class SessionsChangedCanonicalPayload(BaseModel):\n"
        "    schema_version: StrictInt\n"
        "    run_status: StrictStr | None = None\n\n"
        "class SessionsChangedLegacyPayload(BaseModel):\n"
        "    run_status: StrictStr | None = None\n"
    )

    rendered = runner._normalise_optional_non_nullable_defaults(spec, generated)

    assert rendered.count("run_status: StrictStr = None  # type: ignore[assignment]") == 2


def test_json_number_renderer_accepts_one_line_pydantic_imports() -> None:
    spec = next(
        spec
        for spec in runner.discover_contracts()
        if spec.wire_name == "sessions.changed"
    )
    generated = (
        "from __future__ import annotations\n"
        "\n"
        "from pydantic import BaseModel, StrictInt\n\n"
        "class SessionsChangedCanonicalPayload(BaseModel):\n"
        "    schema_version: StrictInt = Field(..., ge=1, le=1)\n"
    )

    rendered = runner._normalise_json_number_types(spec, generated)

    assert "from typing import Annotated, Any" in rendered
    assert "_JsonInteger = Annotated[" in rendered
    assert "schema_version: _JsonInteger" in rendered


def test_duplicate_wire_names_are_rejected(tmp_path: Path) -> None:
    _write_schema(tmp_path, "a/first.schema.json", _method_schema("sessions.resolve"))
    duplicate = _method_schema("sessions.resolve")
    duplicate["$id"] = "https://opensquilla.dev/contracts/gateway/v4/duplicate.schema.json"
    _write_schema(tmp_path, "b/second.schema.json", duplicate)

    with pytest.raises(runner.ContractConfigurationError, match="duplicate method Contract"):
        runner.discover_contracts(tmp_path)


def test_contract_cannot_overwrite_aggregate_registry(tmp_path: Path) -> None:
    _write_schema(
        tmp_path,
        "gateway-contract-registry.schema.json",
        _method_schema("gateway.registry"),
    )

    with pytest.raises(runner.ContractConfigurationError, match="aggregate registry"):
        runner.discover_contracts(tmp_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda schema: schema.update(
                {"$schema": "http://json-schema.org/draft-07/schema#"}
            ),
            "2020-12",
        ),
        (lambda schema: schema.pop("x-opensquilla-codegen"), "pinned toolchain"),
        (lambda schema: schema["x-opensquilla-method"].pop("scope"), "scope"),
        (
            lambda schema: schema["x-opensquilla-method"].update(
                {"guestAllowed": "yes"}
            ),
            "guestAllowed",
        ),
        (lambda schema: schema["x-opensquilla-method"].pop("errors"), "errors"),
        (
            lambda schema: schema["x-opensquilla-wire"].update(
                {"protocol": "invented-wire"}
            ),
            "protocol",
        ),
        (
            lambda schema: schema["x-opensquilla-method"].update(
                {"name": "Sessions Invalid"}
            ),
            "method name",
        ),
        (
            lambda schema: schema["x-opensquilla-method"].update(
                {"scope": "operator/read"}
            ),
            "scope",
        ),
        (
            lambda schema: schema["x-opensquilla-method"].update(
                {"kind": "stream"}
            ),
            "method kind",
        ),
        (
            lambda schema: schema["x-opensquilla-method"].update(
                {"idempotency": "sometimes"}
            ),
            "idempotency",
        ),
        (
            lambda schema: schema["x-opensquilla-method"].pop("timeout"),
            "timeout",
        ),
        (
            lambda schema: schema["x-opensquilla-method"].update(
                {"timeout": {"policy": "whenever"}}
            ),
            "timeout policy",
        ),
        (
            lambda schema: schema["x-opensquilla-method"].pop("capability"),
            "capability",
        ),
        (
            lambda schema: schema["x-opensquilla-method"].update(
                {"capability": {"kind": "method-availability", "name": "bad name"}}
            ),
            "capability name",
        ),
        (
            lambda schema: schema["x-opensquilla-method"].update(
                {"errors": [{"code": "bad-code"}]}
            ),
            "error code",
        ),
        (
            lambda schema: schema["x-opensquilla-method"].pop("params"),
            "local \\$defs reference",
        ),
        (
            lambda schema: schema["x-opensquilla-method"].pop("result"),
            "local \\$defs reference",
        ),
        (
            lambda schema: schema["x-opensquilla-method"].update(
                {"result": "#/$defs/not-legal"}
            ),
            "legal identifier",
        ),
    ],
)
def test_invalid_generation_metadata_fails_before_tool_execution(
    tmp_path: Path,
    mutation: object,
    message: str,
) -> None:
    document = _method_schema("sessions.resolve")
    assert callable(mutation)
    mutation(document)
    schema = _write_schema(tmp_path, "sessions/sessions-resolve.schema.json", document)

    with pytest.raises(runner.ContractConfigurationError, match=message):
        runner.load_contract(schema, contract_root=tmp_path)


def test_only_the_exact_production_sessions_list_schema_is_grandfathered(
    tmp_path: Path,
) -> None:
    production = next(
        spec for spec in runner.discover_contracts() if spec.wire_name == "sessions.list"
    )
    assert production.semantic_kind == "query"
    assert production.metadata.get("timeout") is None
    assert production.metadata.get("capability") is None

    copied_document = json.loads(production.schema.read_text(encoding="utf-8"))
    copied_schema = _write_schema(
        tmp_path,
        "sessions/sessions-list.schema.json",
        copied_document,
    )
    with pytest.raises(runner.ContractConfigurationError, match="declare kind"):
        runner.load_contract(copied_schema, contract_root=tmp_path)


@pytest.mark.parametrize(
    "event_targets",
    [
        {},
        {
            "frame": "#/$defs/SessionsChangedEventFrame",
            "payload": "#/$defs/SessionsChangedEventFrame",
        },
    ],
)
def test_event_requires_exactly_one_frame_or_payload(
    tmp_path: Path,
    event_targets: dict[str, str],
) -> None:
    document = _method_schema("sessions.changed")
    document.pop("x-opensquilla-method")
    document["x-opensquilla-event"] = {
        "name": "sessions.changed",
        "delivery": "live-only-best-effort",
        "schemaVersion": 1,
        **event_targets,
    }
    document["$defs"] = {"SessionsChangedEventFrame": {"type": "object"}}
    schema = _write_schema(
        tmp_path,
        "sessions/sessions-changed.schema.json",
        document,
    )

    with pytest.raises(runner.ContractConfigurationError, match="exactly one"):
        runner.load_contract(schema, contract_root=tmp_path)


def test_aggregate_run_dispatches_every_discovered_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generic_schema = _write_schema(
        tmp_path,
        "sessions/sessions-resolve.schema.json",
        _method_schema("sessions.resolve"),
    )
    legacy = next(
        spec for spec in runner.discover_contracts() if spec.wire_name == "sessions.list"
    )
    generic = runner.load_contract(generic_schema, contract_root=tmp_path)
    calls: list[tuple[str, str]] = []

    def record_call(spec: runner.ContractSpec, mode: runner.Mode) -> int:
        calls.append((spec.wire_name, mode))
        return 0

    monkeypatch.setattr(
        runner,
        "_run_legacy",
        record_call,
    )
    monkeypatch.setattr(
        runner,
        "_run_generic",
        record_call,
    )
    monkeypatch.setattr(
        runner,
        "_run_registration_descriptor",
        lambda specs, mode: 0,
    )
    monkeypatch.setattr(
        runner,
        "reconcile_orphans",
        lambda expected, mode: 0,
    )

    assert runner.run("check", (legacy, generic)) == 0
    assert calls == [("sessions.list", "check"), ("sessions.resolve", "check")]


def test_generic_renderer_derives_all_adapter_only_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = _write_schema(
        tmp_path,
        "sessions/sessions-resolve.schema.json",
        _method_schema("sessions.resolve"),
    )
    spec = runner.load_contract(schema, contract_root=tmp_path)

    def fake_run(
        command: list[str],
        *,
        env: dict[str, str],
        purpose: str,
    ) -> None:
        del env, purpose
        output = Path(command[command.index("--output") + 1])
        if "datamodel_code_generator" in command:
            output.write_text("class SessionsResolveRequestFrame: ...\n", encoding="utf-8")
        else:
            output.write_text("export interface SessionsResolveRequestFrame {}\n", encoding="utf-8")

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(
        runner,
        "_capture",
        lambda command, env, purpose: "export const validateSessionsResolveRequestFrame = true\n",
    )

    rendered = runner.render_generic(spec)

    assert {path.name for path in rendered} == {
        "sessions_resolve.py",
        "sessions_resolve_metadata.py",
        "sessionsResolve.ts",
        "sessionsResolveValidators.mjs",
        "sessionsResolveValidators.d.mts",
    }
    combined = "\n".join(rendered.values())
    assert "SESSIONS_RESOLVE_METHOD" in combined
    assert '"method-availability"' in combined
    assert "validateSessionsResolveRequestFrame" in combined
    assert all(
        "@generated by scripts/contracts/generate_gateway_contracts.py" in content
        for content in rendered.values()
    )


def test_python_renderer_preserves_optional_non_nullable_semantics(
    tmp_path: Path,
) -> None:
    document = _method_schema("sessions.resolve")
    result_name = "SessionsResolveResult"
    document["$defs"][result_name] = {
        "type": "object",
        "required": ["session_key"],
        "properties": {
            "session_key": {"type": "string"},
            "status": {"type": "string"},
            "model": {"type": ["string", "null"]},
        },
    }
    schema = _write_schema(
        tmp_path,
        "sessions/sessions-resolve.schema.json",
        document,
    )
    spec = runner.load_contract(schema, contract_root=tmp_path)
    generated = (
        "class SessionsResolveResult(BaseModel):\n"
        "    session_key: StrictStr\n"
        "    status: StrictStr | None = None\n"
        "    model: StrictStr | None = None\n"
        "\n"
        "class OtherModel(BaseModel):\n"
        "    status: StrictStr | None = None\n"
    )

    rendered = runner._normalise_optional_non_nullable_defaults(spec, generated)

    assert "    status: StrictStr = None  # type: ignore[assignment]" in rendered
    assert "    model: StrictStr | None = None" in rendered
    assert "class OtherModel(BaseModel):\n    status: StrictStr | None = None" in rendered


def test_python_renderer_keeps_field_alias_metadata_when_tightening_nullability(
    tmp_path: Path,
) -> None:
    document = _method_schema("sessions.resolve")
    result_name = "SessionsResolveResult"
    document["$defs"][result_name] = {
        "type": "object",
        "properties": {"wireStatus": {"type": "string"}},
    }
    schema = _write_schema(
        tmp_path,
        "sessions/sessions-resolve.schema.json",
        document,
    )
    spec = runner.load_contract(schema, contract_root=tmp_path)
    generated = (
        "from pydantic import BaseModel, Field\n\n"
        "class SessionsResolveResult(BaseModel):\n"
        "    status: StrictStr | None = Field(None, alias='wireStatus')\n"
    )

    rendered = runner._normalise_optional_non_nullable_defaults(spec, generated)

    assert (
        "status: StrictStr = Field(None, alias='wireStatus')  # type: ignore[assignment]"
        in rendered
    )


def test_python_renderer_aligns_json_integer_acceptance_with_ajv(
    tmp_path: Path,
) -> None:
    document = _method_schema("sessions.resolve")
    result_name = "SessionsResolveResult"
    document["$defs"][result_name] = {
        "type": "object",
        "properties": {"created_at": {"type": "integer"}},
    }
    schema = _write_schema(
        tmp_path,
        "sessions/sessions-resolve.schema.json",
        document,
    )
    spec = runner.load_contract(schema, contract_root=tmp_path)
    generated = (
        "from typing import Any, Literal\n"
        "from pydantic import (\n"
        "    BaseModel,\n"
        "    StrictInt,\n"
        ")\n\n"
        "class SessionsResolveResult(BaseModel):\n"
        "    created_at: StrictInt\n"
    )

    rendered = runner._normalise_json_number_types(spec, generated)

    assert "_JsonInteger = Annotated[" in rendered
    assert "    int | float," in rendered
    assert "created_at: _JsonInteger" in rendered
    assert "StrictInt" not in rendered


def test_python_number_postprocessor_is_shape_guarded_and_executable(
    tmp_path: Path,
) -> None:
    document = _method_schema("sessions.resolve")
    result_name = "SessionsResolveResult"
    document["$defs"][result_name] = {
        "type": "object",
        "properties": {"score": {"type": "number"}},
    }
    schema = _write_schema(
        tmp_path,
        "sessions/sessions-resolve.schema.json",
        document,
    )
    spec = runner.load_contract(schema, contract_root=tmp_path)
    # Deliberately omit Literal and keep the import block on one line to guard
    # against offset-sensitive source rewriting.
    generated = (
        "from __future__ import annotations\n\n"
        "from typing import Any\n"
        "from pydantic import (\n"
        "    BaseModel,\n"
        "    StrictFloat,\n"
        ")\n\n"
        "class SessionsResolveResult(BaseModel):\n"
        "    score: StrictFloat\n"
    )

    rendered = runner._normalise_json_number_types(spec, generated)
    namespace: dict[str, Any] = {}
    exec(rendered, namespace)
    model = namespace["SessionsResolveResult"]
    model.model_rebuild(_types_namespace=namespace)

    assert model.model_validate({"score": 1}).score == 1
    assert model.model_validate({"score": 1.5}).score == 1.5
    with pytest.raises(ValueError):
        model.model_validate({"score": float("nan")})
    assert "StrictFloat" not in rendered
    assert "BeforeValidator" in rendered


def test_registration_descriptor_exposes_uniform_validation_models() -> None:
    specs = runner.discover_contracts()

    rendered = runner.render_registration_descriptor(specs)

    assert "class GatewayMethodContract:" in rendered
    assert (
        "GATEWAY_METHOD_CONTRACTS: Final[dict[str, GatewayMethodContract]]"
        in rendered
    )
    assert (
        "GATEWAY_EVENT_CONTRACTS: Final[dict[str, GatewayEventContract]]"
        in rendered
    )
    assert "request_model: type[Any]" in rendered
    assert "params_model: type[Any]" in rendered
    assert "response_model: type[Any]" in rendered
    assert "result_model: type[Any]" in rendered
    assert "'sessions.list': GatewayMethodContract(" in rendered
    assert "scope='operator.read'" in rendered
    assert "guest_allowed=True" in rendered
    assert "timeout=None" in rendered
    assert "capability=None" in rendered
    assert "params_model=_sessions_list_params_model" in rendered
    assert "result_model=_sessions_list_result_model" in rendered


def test_generated_registration_descriptor_is_importable_by_gateway_adapter() -> None:
    from opensquilla.contracts.generated.v4.gateway_contract_registry import (  # type: ignore[import-untyped]
        GATEWAY_METHOD_CONTRACTS,
    )

    descriptor = GATEWAY_METHOD_CONTRACTS["sessions.list"]
    assert descriptor.name == "sessions.list"
    assert descriptor.kind == "query"
    assert descriptor.scope == "operator.read"
    assert descriptor.guest_allowed is True
    assert descriptor.protocol == runner.GATEWAY_PROTOCOL
    assert descriptor.wire_version == 4
    assert descriptor.params_model.__name__ == "SessionsListParams"
    assert descriptor.result_model.__name__ == "SessionsListResult"


@pytest.mark.parametrize("expected_name", ["renamed.py", None])
def test_orphan_reconciliation_handles_contract_rename_and_delete(
    tmp_path: Path,
    expected_name: str | None,
) -> None:
    python_root = tmp_path / "python"
    typescript_root = tmp_path / "typescript"
    python_root.mkdir()
    typescript_root.mkdir()
    orphan = python_root / "old_contract.py"
    orphan.write_text(
        "# @generated by scripts/contracts/generate_gateway_contracts.py; do not edit.\n",
        encoding="utf-8",
    )
    unowned = python_root / "keep_me.py"
    unowned.write_text("# maintained by a human\n", encoding="utf-8")
    expected = (
        frozenset({python_root / expected_name})
        if expected_name is not None
        else frozenset()
    )
    roots = (python_root, typescript_root)

    assert runner.reconcile_orphans(expected, mode="check", roots=roots) == 1
    assert orphan.exists()
    assert unowned.exists()
    assert runner.reconcile_orphans(expected, mode="write", roots=roots) == 0
    assert not orphan.exists()
    assert unowned.exists()


def test_hash_manifest_comparison_is_portable_and_fail_closed(tmp_path: Path) -> None:
    left = tmp_path / "linux.json"
    right = tmp_path / "windows.json"
    manifest: dict[str, Any] = {
        "format": 1,
        "artifacts": {
            "src/opensquilla/contracts/generated/v4/example.py": "a" * 64
        },
    }
    left.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    right.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    assert runner.compare_hash_manifests(left, right) == 0

    changed = copy.deepcopy(manifest)
    changed["artifacts"]["src/opensquilla/contracts/generated/v4/example.py"] = (
        "b" * 64
    )
    right.write_text(json.dumps(changed), encoding="utf-8")
    assert runner.compare_hash_manifests(left, right) == 1


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"format": True, "artifacts": {"generated/example.py": "a" * 64}},
        {"format": 2, "artifacts": {"generated/example.py": "a" * 64}},
        {"format": 1, "artifacts": {}},
        {"format": 1, "artifacts": {"/absolute.py": "a" * 64}},
        {"format": 1, "artifacts": {"../escape.py": "a" * 64}},
        {"format": 1, "artifacts": {"generated/example.py": "abc123"}},
        {
            "format": 1,
            "artifacts": {"generated/example.py": "a" * 64},
            "unexpected": True,
        },
    ],
)
def test_equal_invalid_hash_manifests_fail_closed(
    tmp_path: Path,
    manifest: dict[str, object],
) -> None:
    left = tmp_path / "linux.json"
    right = tmp_path / "windows.json"
    left.write_text(json.dumps(manifest), encoding="utf-8")
    right.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Contract hash manifest"):
        runner.compare_hash_manifests(left, right)


def test_command_resolution_uses_windows_cli_shim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner.shutil,
        "which",
        lambda name: "C:/Program Files/nodejs/npm.cmd" if name == "npm" else None,
    )

    assert runner._resolved_command(["npm", "--version"]) == [
        "C:/Program Files/nodejs/npm.cmd",
        "--version",
    ]


def test_generated_writer_disables_platform_newline_translation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "generated.py"
    original_open = Path.open
    observed_newline: list[str | None] = []

    def open_spy(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == output and args and args[0] == "w":
            observed_newline.append(kwargs.get("newline"))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_spy)

    runner._write_text_lf(output, "first\nsecond\n")

    assert observed_newline == ["\n"]
    assert output.read_bytes() == b"first\nsecond\n"


def test_sessions_list_legacy_artifacts_remain_byte_exact() -> None:
    specs = runner.discover_contracts()
    sessions_list = next(spec for spec in specs if spec.wire_name == "sessions.list")
    assert sessions_list.uses_legacy_generator is True

    expected = {
        "sessions_list.py": "00a9ad628f2331690ef6db62ba1d6c8c6c5986dcef40564b32407fa3d8199c5d",
        "sessions_list_metadata.py": (
            "d17a5ac76fbede14262c1f0c404059f9bf6ec870ab2974c24009b5de5916d122"
        ),
        "sessionsList.ts": "03c17fa7bf72d8be25e562451478c614f4151c7658efaf4f3a0b003cefc41e2f",
        "sessionsListValidators.cjs": (
            "49a3dbb6d0cfc648273768d56036e1e8f4882fe1651b0c88034b0846190ef802"
        ),
        "sessionsListValidators.d.cts": (
            "86e783b3d1b504cf356ae6c1feeddffaccec12ac8b1db2d808bde2d20fa697d3"
        ),
    }
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sessions_list.outputs
    } == expected
