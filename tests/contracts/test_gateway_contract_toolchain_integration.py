"""Real Python/TypeScript/Ajv integration test for the generic Contract runner."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

from scripts.contracts import generate_gateway_contracts as runner

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    ROOT
    / "tests/fixtures/contracts/gateway/v4/toolchain/toolchain-ping.schema.json"
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENSQUILLA_RUN_CONTRACT_TOOLCHAIN_INTEGRATION") != "1",
    reason="real Contract toolchain dependencies are installed by frontend-validation",
)


def _artifact(rendered: dict[Path, str], suffix: str) -> str:
    return next(content for path, content in rendered.items() if path.name.endswith(suffix))


def _import_module(path: Path) -> ModuleType:
    module_name = "opensquilla_contract_toolchain_integration_generated"
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    module_spec.loader.exec_module(module)
    return module


def test_generic_contract_toolchain_is_real_and_deterministic(tmp_path: Path) -> None:
    spec = runner.load_contract(FIXTURE, contract_root=FIXTURE.parents[2])

    first = runner.render_generic(spec)
    second = runner.render_generic(spec)
    first_hashes = {
        path.name: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in first.items()
    }
    second_hashes = {
        path.name: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in second.items()
    }
    assert first_hashes == second_hashes
    assert len(first_hashes) == 5

    python_path = tmp_path / "toolchain_ping.py"
    python_path.write_text(_artifact(first, "toolchain_ping.py"), encoding="utf-8")
    generated = _import_module(python_path)
    valid_request = {
        "type": "req",
        "id": "integration",
        "method": "toolchain.ping",
        "params": {"message": "hello"},
    }
    parsed = generated.ToolchainPingRequestFrame.model_validate(valid_request)
    assert parsed.model_dump(mode="json") == valid_request
    with pytest.raises(ValidationError):
        generated.ToolchainPingRequestFrame.model_validate(
            {**valid_request, "method": "toolchain.wrong"}
        )
    generated.ToolchainPingParams.model_validate({"message": "hello"})
    with pytest.raises(ValidationError):
        generated.ToolchainPingParams.model_validate({})
    generated.ToolchainPingResult.model_validate({"echoed": "hello"})
    with pytest.raises(ValidationError):
        generated.ToolchainPingResult.model_validate({})

    typescript_path = tmp_path / "toolchainPing.ts"
    typescript_path.write_text(_artifact(first, "toolchainPing.ts"), encoding="utf-8")
    usage_path = tmp_path / "toolchainPing.typecheck.ts"
    usage_path.write_text(
        "import type { ToolchainPingRequestFrame } from './toolchainPing'\n"
        "const valid: ToolchainPingRequestFrame = {\n"
        "  type: 'req', id: 'integration', method: 'toolchain.ping',\n"
        "  params: { message: 'hello' },\n"
        "}\n"
        "void valid\n"
        "// @ts-expect-error method is a generated string literal\n"
        "const invalid: ToolchainPingRequestFrame = "
        "{ type: 'req', id: 'integration', method: 'toolchain.wrong', "
        "params: { message: 'hello' } }\n"
        "void invalid\n",
        encoding="utf-8",
    )
    subprocess.run(
        runner._resolved_command(
            [
                "npm",
                "--prefix",
                "opensquilla-webui",
                "exec",
                "--",
                "tsc",
                "--noEmit",
                "--strict",
                "--skipLibCheck",
                "--target",
                "ES2022",
                "--module",
                "ESNext",
                "--moduleResolution",
                "Bundler",
                str(typescript_path),
                str(usage_path),
            ]
        ),
        cwd=ROOT,
        check=True,
        text=True,
    )

    validator_source = _artifact(first, "toolchainPingValidators.mjs")
    # ESM output is imported directly by Vite/browser code.  AJV must not
    # leave a CommonJS runtime require in that artifact.
    assert "require(" not in validator_source
    validator_path = tmp_path / "toolchainPingValidators.mjs"
    validator_path.write_text(
        validator_source,
        encoding="utf-8",
    )
    node_check = tmp_path / "verify-toolchain.mjs"
    node_check.write_text(
        "import { pathToFileURL } from 'node:url'\n"
        "const validators = await import(pathToFileURL(process.argv[2]).href)\n"
        f"const valid = {json.dumps(valid_request)}\n"
        "if (!validators.validateToolchainPingRequestFrame(valid)) process.exit(11)\n"
        "const wrong = {...valid, method: 'wrong'}\n"
        "if (validators.validateToolchainPingRequestFrame(wrong)) process.exit(12)\n"
        "if (!validators.validateToolchainPingParams({message: 'hello'})) process.exit(13)\n"
        "if (validators.validateToolchainPingParams({})) process.exit(14)\n"
        "if (!validators.validateToolchainPingResult({echoed: 'hello'})) process.exit(15)\n"
        "if (validators.validateToolchainPingResult({})) process.exit(16)\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["node", str(node_check), str(validator_path)],
        cwd=ROOT,
        env={
            **os.environ,
            "NODE_PATH": str(ROOT / "opensquilla-webui/node_modules"),
        },
        check=True,
        text=True,
    )


def test_required_alternatives_preserve_actual_params_types(tmp_path: Path) -> None:
    goal_revision = {
        "expectedGoalId": "synthetic-goal",
        "expectedStateRevision": 1,
        "clientRequestId": "00000000-0000-4000-8000-000000000001",
    }
    session_keys = ("sessionKey", "session_key", "key")
    cases = [
        ("agents.create", {}, ("id", "agentId", "name")),
        ("cron.runs", {"limit": 1}, ("id", "job_id")),
        ("goals.capabilities", {}, session_keys),
        ("goals.clear", goal_revision, session_keys),
        ("goals.edit", {**goal_revision, "objective": "synthetic edit"}, session_keys),
        ("goals.pause", goal_revision, session_keys),
        ("goals.resume", goal_revision, session_keys),
        (
            "goals.set",
            {
                "objective": "synthetic objective",
                "clientRequestId": "00000000-0000-4000-8000-000000000001",
                "clientMessageId": "00000000-0000-4000-8000-000000000002",
            },
            session_keys,
        ),
        ("goals.status", {}, session_keys),
        ("config.patch", {}, ("patch", "patches")),
        ("sessions.routing.get", {}, session_keys),
        ("skills.install.cancel", {}, ("operationId", "operation_id")),
        ("skills.uninstall", {"allowDrift": False}, ("name", "installId", "install_id")),
    ]
    specs = {spec.wire_name: spec for spec in runner.discover_contracts()}
    usage: list[str] = []
    for index, (method, required, alternatives) in enumerate(cases):
        spec = specs[method]
        original = json.dumps(spec.document, sort_keys=True)
        rendered = runner.render_generic(spec, validator_roles=())
        assert json.dumps(spec.document, sort_keys=True) == original
        (tmp_path / f"{spec.typescript_stem}.ts").write_text(
            _artifact(rendered, f"{spec.typescript_stem}.ts"), encoding="utf-8",
        )
        name = f"Params{index}"
        usage.append(
            f"import type {{ {spec.target('params')} as {name} }} "
            f"from './{spec.typescript_stem}'"
        )
        values = [
            {**required, key: {} if method == "config.patch" else "synthetic-id"}
            for key in alternatives
        ]
        invalid_values = [required, {**values[0], alternatives[0]: 42}]
        if spec.document["$defs"][spec.target("params")].get("additionalProperties") is False:
            invalid_values.append({**values[0], "futureField": True})
        else:
            values.append({**values[0], "futureField": True})
        if method == "agents.create":
            values.append({"id": None, "enabled": None})
        if method == "config.patch":
            values.append({"patch": {}, "patches": {}})
            invalid_values.append({"patch": None})
        if method.startswith("goals.") and "clientRequestId" in required:
            invalid_values.append({key: value for key, value in values[0].items()
                                   if key != "clientRequestId"})
        for number, value in enumerate(values):
            usage.append(f"const valid{index}_{number}: {name} = {json.dumps(value)}")
        for number, value in enumerate(invalid_values):
            usage.extend([
                "// @ts-expect-error Params must retain required aliases and property types",
                f"const invalid{index}_{number}: {name} = {json.dumps(value)}",
            ])
    usage_path = tmp_path / "params.typecheck.ts"
    usage_path.write_text("\n".join(usage) + "\n", encoding="utf-8")
    subprocess.run(
        runner._resolved_command([
            "npm", "--prefix", "opensquilla-webui", "exec", "--", "tsc",
            "--noEmit", "--strict", "--skipLibCheck", "--target", "ES2022",
            "--module", "ESNext", "--moduleResolution", "Bundler", str(usage_path),
        ]),
        cwd=ROOT, check=True, text=True,
    )
