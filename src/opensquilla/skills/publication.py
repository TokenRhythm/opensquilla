"""Generic installed-Skill validation behind explicit artifact publication."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from opensquilla.artifact_publication import (
    ArtifactPublicationAuthorization,
    ArtifactPublicationCandidate,
    ArtifactPublicationRequest,
)
from opensquilla.artifacts import artifact_mime_for_name
from opensquilla.skills.script_runtime import SkillScriptError, SkillScriptRunner


def _digest(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
            raise SkillScriptError("Validation input must be a bounded regular file")
        data = stream.read(64 * 1024 * 1024 + 1)
        if len(data) != info.st_size:
            raise SkillScriptError("Validation input changed while being read")
    return hashlib.sha256(data).hexdigest()


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise SkillScriptError("Validator returned duplicate object keys")
        result[key] = value
    return result


class SkillArtifactPublicationPolicy:
    """Host configuration, independent of any Skill's business or file schema."""

    def __init__(
        self,
        *,
        runner: SkillScriptRunner,
        skill_name: str,
        validator_script: str,
        validator_arguments: tuple[str, ...],
        artifact_directory: str,
        allowed_artifacts: frozenset[str],
        input_files: dict[str, Path],
        receipt_directory: Path,
    ) -> None:
        relative = PurePosixPath(artifact_directory)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise SkillScriptError("An explicit relative artifact directory is required")
        if not allowed_artifacts or not input_files:
            raise SkillScriptError("Explicit artifact and input validation scope is required")
        if any(PurePosixPath(name).name != name for name in allowed_artifacts):
            raise SkillScriptError("Granted artifacts must be file names")
        self.runner = runner
        self.skill_name = skill_name
        self.validator_script = validator_script
        self.validator_arguments = validator_arguments
        self.artifact_directory = relative
        self.allowed_artifacts = allowed_artifacts
        self.input_files = dict(input_files)
        self.receipt_directory = receipt_directory.resolve(strict=True)
        if self.receipt_directory.is_relative_to(runner.workspace):
            raise SkillScriptError("Host validation receipts must be outside the agent workspace")

    async def authorize(
        self, request: ArtifactPublicationRequest, candidate: ArtifactPublicationCandidate
    ) -> ArtifactPublicationAuthorization:
        path = PurePosixPath(request.path)
        if (
            request.execution_id != self.runner.execution_id
            or path.parent != self.artifact_directory
            or path.name not in self.allowed_artifacts
            or request.bundle != "none"
            or request.name != path.name
            or request.mime != artifact_mime_for_name(path.name)
        ):
            raise SkillScriptError("Artifact is outside the host validation scope")
        before = {name: _digest(file) for name, file in self.input_files.items()}
        result = await self.runner.run(
            self.skill_name, self.validator_script, list(self.validator_arguments), readonly=True
        )
        if result.returncode:
            raise SkillScriptError(
                f"Installed publication validator failed: {result.stderr[:1000]}"
            )
        try:
            check = json.loads(result.stdout, object_pairs_hook=_unique_object)
        except (ValueError, TypeError) as error:
            raise SkillScriptError("Installed validator returned invalid JSON") from error
        if (
            not isinstance(check, dict)
            or check.get("schemaVersion") != "skill-publication-check/1"
            or check.get("ok") is not True
            or check.get("runId") != request.execution_id
            or check.get("inputDigests") != before
        ):
            raise SkillScriptError(
                "Validator result does not bind the current execution and inputs"
            )
        artifacts = check.get("artifacts")
        if (
            not isinstance(artifacts, dict)
            or not self.allowed_artifacts.issubset(artifacts)
            or artifacts.get(path.name) != candidate.sha256
            or any(
                not isinstance(name, str)
                or PurePosixPath(name).name != name
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                for name, digest in artifacts.items()
            )
        ):
            raise SkillScriptError("Candidate bytes do not match the installed validator result")
        if before != {name: _digest(file) for name, file in self.input_files.items()}:
            raise SkillScriptError("Validation inputs changed during publication")
        validator_id = hashlib.sha256(
            json.dumps(
                [result.package_sha256, self.validator_script, self.validator_arguments],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        receipt = {
            "schemaVersion": "skill-publication-receipt/1",
            "sessionId": request.session_id,
            "sessionKey": request.session_key,
            "executionId": request.execution_id,
            "path": request.path,
            "sha256": candidate.sha256,
            "validatorId": validator_id,
            "inputDigests": before,
            "check": check,
            "startedAt": result.started_at,
            "finishedAt": result.finished_at,
        }
        data = json.dumps(
            receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        receipt_id = hashlib.sha256(data).hexdigest()
        with (self.receipt_directory / f"{receipt_id}.json").open("xb") as stream:
            stream.write(data + b"\n")
        return ArtifactPublicationAuthorization(
            session_id=request.session_id,
            session_key=request.session_key,
            execution_id=request.execution_id,
            sha256=candidate.sha256,
            validator_id=validator_id,
            receipt_id=receipt_id,
        )
