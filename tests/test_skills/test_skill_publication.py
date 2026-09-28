from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.artifact_publication import (
    ArtifactPublicationCandidate,
    ArtifactPublicationRequest,
    authorize_publication,
)
from opensquilla.skills.publication import (
    SkillArtifactPublicationPolicy,
    SkillManifestPublicationPolicy,
)
from opensquilla.skills.script_runtime import (
    SkillScriptError,
    SkillScriptGrant,
    SkillScriptResult,
    SkillScriptRunner,
)

REPORT = b"<html><body>host-validated example</body></html>"
ARTIFACTS = frozenset({"report.html", "provenance.json", "validation.json"})
RUN_ID = "publication-test"


@pytest.fixture
def publication(
    tmp_path: Path,
) -> tuple[
    SkillArtifactPublicationPolicy, ArtifactPublicationRequest, ArtifactPublicationCandidate
]:
    installation = tmp_path / "skill"
    (installation / "scripts").mkdir(parents=True)
    (installation / "SKILL.md").write_text("---\nname: demo\ndescription: test\n---\n")
    (installation / "scripts/check.py").write_text(
        "import hashlib,json\nfrom pathlib import Path\n"
        "def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()\n"
        "actual=Path('/work/report/report.html').read_bytes()\n"
        "assert actual == Path('/work/draft.json').read_bytes()\n"
        f"check={{'schemaVersion':'skill-publication-check/1','ok':True,'runId':{RUN_ID!r}}}\n"
        "check['inputDigests']={'receipts':digest('/inputs/receipts.json'),'draft':digest('/work/draft.json')}\n"
        f"names={sorted(ARTIFACTS)!r}\n"
        "check['artifacts']={name:digest('/work/report/'+name) for name in names}\n"
        "print(json.dumps(check))\n"
    )
    workspace, inputs, receipts = (tmp_path / name for name in ("work", "inputs", "receipts"))
    for directory in (workspace, inputs, receipts):
        directory.mkdir()
    (workspace / "report").mkdir()
    (workspace / "draft.json").write_bytes(REPORT)
    (inputs / "receipts.json").write_text('{"source":"trusted host"}')
    for name in ARTIFACTS:
        (workspace / "report" / name).write_bytes(REPORT if name == "report.html" else b"{}")
    runner = SkillScriptRunner(
        grants=(SkillScriptGrant.pin("demo", installation, frozenset({"scripts/check.py"})),),
        workspace=workspace,
        inputs=inputs,
        execution_id=RUN_ID,
    )
    policy = SkillArtifactPublicationPolicy(
        runner=runner,
        skill_name="demo",
        validator_script="scripts/check.py",
        validator_arguments=(),
        artifact_directory="report",
        allowed_artifacts=ARTIFACTS,
        input_files={"receipts": inputs / "receipts.json", "draft": workspace / "draft.json"},
        receipt_directory=receipts,
    )
    request = ArtifactPublicationRequest(
        session_id="session-1",
        session_key="agent:main:webchat:session-1",
        execution_id=RUN_ID,
        path="report/report.html",
        name="report.html",
        mime="text/html",
        bundle="none",
    )
    return policy, request, ArtifactPublicationCandidate(REPORT)


def _manifest(policy: SkillArtifactPublicationPolicy) -> dict[str, Any]:
    return {
        "schemaVersion": "skill-publication-check/1",
        "ok": True,
        "runId": RUN_ID,
        "inputDigests": {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in policy.input_files.items()
        },
        "artifacts": {
            name: hashlib.sha256(
                (policy.runner.workspace / "report" / name).read_bytes()
            ).hexdigest()
            for name in ARTIFACTS
        },
    }


def _result(policy: SkillArtifactPublicationPolicy, manifest: dict[str, Any]) -> SkillScriptResult:
    return SkillScriptResult(
        returncode=0,
        stdout=json.dumps(manifest),
        stderr="",
        started_at="2026-09-28T10:00:00+00:00",
        finished_at="2026-09-28T10:00:01+00:00",
        package_sha256=policy.runner.grants["demo"].digest,
    )


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_real_installed_validator_authorizes_immutable_candidate(publication) -> None:
    policy, request, candidate = publication
    authorization = await authorize_publication(policy, request, candidate)
    assert authorization.sha256 == candidate.sha256
    receipt_path = policy.receipt_directory / f"{authorization.receipt_id}.json"
    receipt_bytes = receipt_path.read_bytes()
    assert hashlib.sha256(receipt_bytes[:-1]).hexdigest() == authorization.receipt_id
    receipt = json.loads(receipt_bytes)
    assert receipt["check"] == _manifest(policy)
    assert receipt["executionId"] == RUN_ID
    assert receipt["validatorId"] == authorization.validator_id
    assert "/" not in authorization.validator_id
    assert receipt["check"]["artifacts"]["report.html"] == candidate.sha256


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_id": "another-run"},
        {"path": "other/report.html"},
        {"path": "report/unapproved.html"},
        {"path": "/report/report.html"},
        {"path": "report/../report.html"},
        {"bundle": "auto"},
        {"name": "renamed.html"},
        {"name": "report.svg", "mime": "image/svg+xml"},
        {"mime": "application/octet-stream"},
        {"mime": "text/html; charset=utf-8"},
    ],
)
async def test_invalid_scope_never_invokes_validator(
    publication, monkeypatch: pytest.MonkeyPatch, changes: dict[str, str]
) -> None:
    policy, request, candidate = publication
    run = AsyncMock()
    monkeypatch.setattr(policy.runner, "run", run)
    with pytest.raises(SkillScriptError, match="scope"):
        await policy.authorize(replace(request, **changes), candidate)
    run.assert_not_awaited()
    assert not list(policy.receipt_directory.iterdir())


@pytest.mark.parametrize(
    "mutation",
    [
        "exit",
        "json",
        "duplicate_key",
        "schema",
        "false",
        "truthy",
        "run",
        "input_digest",
        "missing_input",
        "extra_input",
        "artifact_hash",
        "missing_artifact",
        "unsafe_companion_name",
        "invalid_companion_hash",
    ],
)
async def test_untrusted_validator_output_cannot_create_authorization(
    publication, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    policy, request, candidate = publication
    manifest = _manifest(policy)
    if mutation == "schema":
        manifest["schemaVersion"] = "unsupported/1"
    elif mutation in {"false", "truthy"}:
        manifest["ok"] = False if mutation == "false" else 1
    elif mutation == "run":
        manifest["runId"] = "old-run"
    elif mutation == "input_digest":
        manifest["inputDigests"]["draft"] = "0" * 64
    elif mutation == "missing_input":
        del manifest["inputDigests"]["receipts"]
    elif mutation == "extra_input":
        manifest["inputDigests"]["unbound"] = "0" * 64
    elif mutation == "artifact_hash":
        manifest["artifacts"]["report.html"] = "0" * 64
    elif mutation == "missing_artifact":
        del manifest["artifacts"]["provenance.json"]
    elif mutation == "unsafe_companion_name":
        manifest["artifacts"]["../unapproved.html"] = "0" * 64
    elif mutation == "invalid_companion_hash":
        manifest["artifacts"]["validation.json"] = "not-a-sha256"
    result = _result(policy, manifest)
    if mutation == "exit":
        result = replace(result, returncode=1, stderr="untrusted success payload")
    elif mutation == "json":
        result = replace(result, stdout="not json")
    elif mutation == "duplicate_key":
        result = replace(result, stdout=result.stdout[:-1] + ',"ok":true}')
    monkeypatch.setattr(policy.runner, "run", AsyncMock(return_value=result))
    with pytest.raises(SkillScriptError):
        await policy.authorize(request, candidate)
    assert not list(policy.receipt_directory.iterdir())


async def test_additional_companion_does_not_extend_publication_scope(
    publication, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, request, candidate = publication
    manifest = _manifest(policy)
    manifest["artifacts"]["unapproved.html"] = candidate.sha256
    run = AsyncMock(return_value=_result(policy, manifest))
    monkeypatch.setattr(policy.runner, "run", run)
    authorization = await authorize_publication(policy, request, candidate)
    assert authorization.sha256 == candidate.sha256
    with pytest.raises(SkillScriptError, match="scope"):
        await policy.authorize(
            replace(request, path="report/unapproved.html", name="unapproved.html"), candidate
        )
    assert run.await_count == 1


async def test_changed_input_during_validation_cannot_be_authorized(
    publication, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, request, candidate = publication
    result = _result(policy, _manifest(policy))

    async def change_input(*args: Any, **kwargs: Any) -> SkillScriptResult:
        assert kwargs == {"readonly": True}
        policy.input_files["draft"].write_bytes(b"changed while validator runs")
        return result

    monkeypatch.setattr(policy.runner, "run", change_input)
    with pytest.raises(SkillScriptError, match="inputs changed"):
        await policy.authorize(request, candidate)
    assert not list(policy.receipt_directory.iterdir())


async def test_candidate_and_fixed_validator_arguments_are_bound(
    publication, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, request, _ = publication
    run = AsyncMock(return_value=_result(policy, _manifest(policy)))
    monkeypatch.setattr(policy.runner, "run", run)
    with pytest.raises(SkillScriptError, match="Candidate bytes"):
        await policy.authorize(request, ArtifactPublicationCandidate(b"modified artifact"))
    run.assert_awaited_once_with("demo", "scripts/check.py", [], readonly=True)
    assert not list(policy.receipt_directory.iterdir())


async def test_receipts_cannot_be_written_in_agent_workspace(publication) -> None:
    policy, _, _ = publication
    with pytest.raises(SkillScriptError, match="outside"):
        SkillArtifactPublicationPolicy(
            runner=policy.runner,
            skill_name=policy.skill_name,
            validator_script=policy.validator_script,
            validator_arguments=policy.validator_arguments,
            artifact_directory="report",
            allowed_artifacts=ARTIFACTS,
            input_files=policy.input_files,
            receipt_directory=policy.runner.workspace,
        )


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap required")
async def test_changed_installed_validator_cannot_authorize(publication) -> None:
    policy, request, candidate = publication
    script = policy.runner.grants["demo"].directory / "scripts/check.py"
    script.write_text("print('forged success')")
    with pytest.raises(SkillScriptError, match="changed"):
        await policy.authorize(request, candidate)
    assert not list(policy.receipt_directory.iterdir())


@pytest.fixture
def dynamic_publication(publication, tmp_path: Path):
    old, request, candidate = publication
    private = tmp_path / "private"
    raw_receipts = tmp_path / "raw-receipts"
    private.mkdir()
    raw_receipts.mkdir()
    (private / "state.json").write_text('{"status":"finalized"}')
    (raw_receipts / "raw.json").write_text('{"source":"host"}')
    policy = SkillManifestPublicationPolicy(
        runner=old.runner,
        skill_name=old.skill_name,
        validator_script=old.validator_script,
        allowed_artifacts=ARTIFACTS,
        caller_binding="caller",
        input_roots={"private": private, "receipts": raw_receipts},
        receipt_directory=old.receipt_directory,
    )
    return policy, replace(request, path="reports/example-collision-suffix/report.html"), candidate


def _dynamic_manifest(
    policy: SkillManifestPublicationPolicy,
    request: ArtifactPublicationRequest,
    candidate: ArtifactPublicationCandidate,
) -> dict[str, Any]:
    return {
        "schemaVersion": "skill-publication-check/1",
        "ok": True,
        "runId": RUN_ID,
        "artifactDirectory": str(Path(request.path).parent),
        "inputDigests": policy._inventory(),
        "artifacts": {name: candidate.sha256 for name in ARTIFACTS},
    }


def _dynamic_result(
    policy: SkillManifestPublicationPolicy, manifest: dict[str, Any]
) -> SkillScriptResult:
    return SkillScriptResult(
        0,
        json.dumps(manifest),
        "",
        "2026-09-28T10:00:00+00:00",
        "2026-09-28T10:00:01+00:00",
        policy.runner.grants["demo"].digest,
    )


async def test_dynamic_directory_and_input_inventory_are_host_bound(
    dynamic_publication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, request, candidate = dynamic_publication
    manifest = _dynamic_manifest(policy, request, candidate)
    run = AsyncMock(return_value=_dynamic_result(policy, manifest))
    monkeypatch.setattr(policy.runner, "run", run)
    authorization = await policy.authorize(request, candidate)
    assert authorization.sha256 == candidate.sha256
    assert run.await_args is not None
    kwargs = run.await_args.kwargs
    assert kwargs["readonly"] is True
    stdin = json.loads(kwargs["stdin"])
    assert stdin["callerBinding"] == "caller"
    assert stdin["inputDigests"] == policy._inventory()
    assert stdin["path"] == request.path


@pytest.mark.parametrize("mutation", ["directory", "extra", "missing", "candidate", "run", "input"])
async def test_dynamic_manifest_rejects_wrong_scope_and_nonexact_artifact_set(
    dynamic_publication,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    policy, request, candidate = dynamic_publication
    manifest = _dynamic_manifest(policy, request, candidate)
    if mutation == "directory":
        manifest["artifactDirectory"] = "another/report"
    elif mutation == "extra":
        manifest["artifacts"]["private.json"] = "0" * 64
    elif mutation == "missing":
        manifest["artifacts"].pop("provenance.json")
    elif mutation == "candidate":
        manifest["artifacts"]["report.html"] = "0" * 64
    elif mutation == "run":
        manifest["runId"] = "another-execution"
    else:
        manifest["inputDigests"]["private/state.json"] = "0" * 64
    monkeypatch.setattr(
        policy.runner, "run", AsyncMock(return_value=_dynamic_result(policy, manifest))
    )
    with pytest.raises(SkillScriptError):
        await policy.authorize(request, candidate)
    assert not list(policy.receipt_directory.iterdir())


@pytest.mark.parametrize("mutation", ["add", "remove", "change"])
async def test_dynamic_inventory_changes_during_validation_are_rejected(
    dynamic_publication,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    policy, request, candidate = dynamic_publication
    manifest = _dynamic_manifest(policy, request, candidate)

    async def mutate(*_args: Any, **_kwargs: Any) -> SkillScriptResult:
        root = policy.input_roots["private"]
        if mutation == "add":
            (root / "extra.json").write_text("new data")
        elif mutation == "remove":
            (root / "state.json").unlink()
        else:
            (root / "state.json").write_text("replaced data")
        return _dynamic_result(policy, manifest)

    monkeypatch.setattr(policy.runner, "run", mutate)
    with pytest.raises((SkillScriptError, OSError)):
        await policy.authorize(request, candidate)
    assert not list(policy.receipt_directory.iterdir())


@pytest.mark.parametrize("root_name", ["private", "receipts"])
def test_publication_receipts_cannot_overlap_dynamic_input_roots(
    dynamic_publication, root_name: str
) -> None:
    policy, _, _ = dynamic_publication
    with pytest.raises(SkillScriptError, match="isolated"):
        SkillManifestPublicationPolicy(
            runner=policy.runner,
            skill_name=policy.skill_name,
            validator_script=policy.validator_script,
            allowed_artifacts=ARTIFACTS,
            caller_binding="caller",
            input_roots=policy.input_roots,
            receipt_directory=policy.input_roots[root_name],
        )


@pytest.mark.parametrize("change", ["directory", "inputs"])
async def test_one_execution_cannot_mix_different_finalized_manifests(
    dynamic_publication,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    policy, request, candidate = dynamic_publication
    first_manifest = _dynamic_manifest(policy, request, candidate)
    run = AsyncMock(return_value=_dynamic_result(policy, first_manifest))
    monkeypatch.setattr(policy.runner, "run", run)
    await policy.authorize(request, candidate)
    assert len(list(policy.receipt_directory.iterdir())) == 1
    if change == "directory":
        second_request = replace(request, path="reports/different-research/report.html")
    else:
        (policy.input_roots["private"] / "state.json").write_text(
            '{"status":"new-finalized-state"}'
        )
        second_request = request
    second_manifest = _dynamic_manifest(policy, second_request, candidate)
    run.return_value = _dynamic_result(policy, second_manifest)
    with pytest.raises(SkillScriptError):
        await policy.authorize(second_request, candidate)
    assert len(list(policy.receipt_directory.iterdir())) == 1
