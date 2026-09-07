"""Production validator selection must not remove full Contract verification."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from scripts.contracts import generate_gateway_contracts as runner

ROOT = Path(__file__).resolve().parents[2]
AJV_GENERATOR = ROOT / "scripts/contracts/generate_gateway_contract_ajv.mjs"
FIXTURE = ROOT / "tests/fixtures/contracts/gateway/v4/toolchain/toolchain-ping.schema.json"


def test_production_targets_preserve_every_approved_validator_role() -> None:
    specs = runner.discover_contracts()
    targets = runner.load_production_targets(specs)

    assert len(targets) == 195
    assert Counter(role for roles in targets.values() for role in roles) == {
        "result": 186,
        "params": 15,
        "payload": 8,
        "frame": 1,
    }
    assert sum(len(spec.targets) for spec in specs) == 869
    assert ("method", "sessions.list") not in targets


@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "method", "wireName": "config.get", "roles": ["result", "result"]},
        {"kind": "method", "wireName": "config.get", "roles": ["payload"]},
        {"kind": "method", "wireName": "missing.method", "roles": ["result"]},
        {"kind": "method", "wireName": "config.get", "roles": []},
        {"kind": "method", "wireName": "config.get", "roles": [False]},
    ],
)
def test_production_policy_rejects_ambiguous_or_unknown_targets(
    tmp_path: Path,
    entry: dict[str, object],
) -> None:
    path = tmp_path / "targets.json"
    path.write_text(json.dumps({"format": 1, "targets": [entry]}), encoding="utf-8")
    with pytest.raises(runner.ContractConfigurationError):
        runner.load_production_targets(runner.discover_contracts(), path)


def test_production_render_keeps_types_but_not_unselected_validators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = runner.load_contract(FIXTURE, contract_root=FIXTURE.parents[2])

    def emit_tool_output(command: list[str], **_: object) -> None:
        output = Path(command[command.index("--output") + 1])
        output.write_text("unchanged type output\n", encoding="utf-8")

    monkeypatch.setattr(runner, "_run", emit_tool_output)
    monkeypatch.setattr(runner, "_capture", lambda *args, **kwargs: "standalone validator\n")
    full = runner.render_generic(spec)
    selected = runner.render_generic(spec, validator_roles=("result",))
    types_only = runner.render_generic(spec, validator_roles=())

    assert set(full) == set(selected) == set(spec.outputs)
    assert set(types_only) == set(spec.outputs[:3])
    assert all(full[path] == selected[path] == types_only[path] for path in spec.outputs[:3])
    declarations = selected[spec.outputs[-1]]
    assert "validateToolchainPingResult" in declarations
    assert "validateToolchainPingRequestFrame" not in declarations


def test_verification_profile_checks_a_separate_complete_output_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = runner.load_contract(FIXTURE, contract_root=FIXTURE.parents[2])
    monkeypatch.setattr(
        runner,
        "render_generic",
        lambda selected, **kwargs: {path: f"artifact {path.name}\n" for path in selected.outputs},
    )
    assert runner.run("write", (spec,), profile="verification", output_root=tmp_path) == 0
    assert runner.run("check", (spec,), profile="verification", output_root=tmp_path) == 0
    validator = tmp_path / spec.outputs[3].relative_to(ROOT)
    assert validator.read_text(encoding="utf-8") == "artifact toolchainPingValidators.mjs\n"
    assert not spec.outputs[3].exists()
    validator.write_text("stale\n", encoding="utf-8")
    assert runner.run("check", (spec,), profile="verification", output_root=tmp_path) == 1


@pytest.mark.parametrize("output_root", [None, ROOT, ROOT / "opensquilla-webui"])
def test_verification_profile_cannot_publish_into_production(output_root: Path | None) -> None:
    with pytest.raises(runner.ContractConfigurationError, match="outside"):
        runner.run("write", (), profile="verification", output_root=output_root)


def test_failed_tree_compile_neither_publishes_nor_removes_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specs = tuple(spec for spec in runner.discover_contracts() if not spec.uses_legacy_generator)[
        :2
    ]
    previous = tmp_path / specs[0].outputs[0].relative_to(ROOT)
    previous.parent.mkdir(parents=True)
    previous.write_text("preserved output\n", encoding="utf-8")

    def compile_contract(spec: runner.ContractSpec, **_: object) -> dict[Path, str]:
        if spec == specs[1]:
            raise RuntimeError("synthetic compiler failure")
        return {path: "new output\n" for path in spec.outputs}

    monkeypatch.setattr(runner, "render_generic", compile_contract)
    with pytest.raises(RuntimeError, match="compiler failure"):
        runner.run("write", specs, profile="verification", output_root=tmp_path)
    assert previous.read_text(encoding="utf-8") == "preserved output\n"
    assert list(previous.parent.iterdir()) == [previous]


@pytest.mark.skipif(
    shutil.which("node") is None or not (ROOT / "opensquilla-webui/node_modules/ajv").exists(),
    reason="the Contract validation job installs the pinned Node toolchain",
)
def test_validator_cli_selects_result_without_removing_full_verification() -> None:
    def compile_roles(*arguments: str) -> set[str]:
        completed = subprocess.run(
            ["node", str(AJV_GENERATOR), str(FIXTURE), "--esm", *arguments],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return set(re.findall(r"export const (validate\w+)\s*=", completed.stdout))

    assert compile_roles("--roles", "result") == {"validateToolchainPingResult"}
    assert compile_roles() == {
        "validateToolchainPingRequestFrame",
        "validateToolchainPingParams",
        "validateToolchainPingResponseFrame",
        "validateToolchainPingResult",
    }


@pytest.mark.parametrize("operation", ["write", "check", "hash"])
@pytest.mark.parametrize("link_kind", ["directory", "artifact", "orphan"])
def test_output_links_cannot_read_overwrite_or_remove_external_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    link_kind: str,
) -> None:
    destination = tmp_path / "verification"
    protected = tmp_path / "protected"
    protected.mkdir()
    source = runner.PYTHON_OUTPUT_ROOT / "review_probe.py"
    target = destination / source.relative_to(ROOT)
    victim = protected / source.name
    original = "# @generated by scripts/contracts/generate_gateway_contracts.py\npreserved\n"
    victim.write_text(original)
    try:
        if link_kind == "directory":
            target.parent.parent.mkdir(parents=True)
            target.parent.symlink_to(protected, target_is_directory=True)
        else:
            target.parent.mkdir(parents=True)
            if link_kind == "artifact":
                target.symlink_to(victim)
            else:
                target.write_text(original)
                (target.parent / "stale.py").symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"filesystem link creation is unavailable: {exc}")
    monkeypatch.setattr(runner, "render_tree", lambda *args, **kwargs: {source: "replacement\n"})
    monkeypatch.setattr(runner, "expected_artifacts", lambda *args, **kwargs: frozenset({source}))
    with pytest.raises(runner.ContractConfigurationError, match="link|outside"):
        if operation == "hash":
            runner.build_hash_manifest((), profile="verification", output_root=destination)
        else:
            runner.run(operation, (), profile="verification", output_root=destination)
    assert victim.read_text() == original
    assert target.read_text() == original
    if link_kind == "orphan":
        assert (target.parent / "stale.py").is_symlink()
