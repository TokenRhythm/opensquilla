"""Explicit host grants for standard Skill scripts, isolated from host services."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_MAX_PACKAGE_BYTES = 8 * 1024 * 1024


class SkillScriptError(ValueError):
    """An installed-script grant or its bounded execution failed."""


def package_digest(directory: Path) -> str:
    """Pin the whole small installation, including imported helpers and assets."""
    entries: list[tuple[str, str]] = []
    total = 0
    for path in sorted(directory.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise SkillScriptError("Skill installation contains a non-regular file")
        total += info.st_size
        if total > _MAX_PACKAGE_BYTES or len(entries) >= 256:
            raise SkillScriptError("Skill installation exceeds the bounded grant")
        with path.open("rb") as stream:
            data = stream.read(_MAX_PACKAGE_BYTES + 1)
        if len(data) != info.st_size:
            raise SkillScriptError("Skill installation changed while being read")
        entries.append((path.relative_to(directory).as_posix(), hashlib.sha256(data).hexdigest()))
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class SkillScriptGrant:
    name: str
    directory: Path
    scripts: frozenset[str]
    digest: str

    @classmethod
    def pin(cls, name: str, directory: Path, scripts: frozenset[str]) -> SkillScriptGrant:
        root = directory.resolve(strict=True)
        if not _NAME.fullmatch(name) or not (root / "SKILL.md").is_file() or not scripts:
            raise SkillScriptError("A named installed Skill and explicit scripts are required")
        for script in scripts:
            relative = Path(script)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.parts[:1] != ("scripts",)
            ):
                raise SkillScriptError("Script must be a relative installed scripts entry")
            target = root / relative
            if target.suffix != ".py" or not target.is_file() or target.is_symlink():
                raise SkillScriptError("Only installed Python script files can be granted")
        return cls(name, root, scripts, package_digest(root))


@dataclass(frozen=True)
class SkillScriptResult:
    returncode: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    package_sha256: str


class SkillScriptRunner:
    """Runtime-only authority; never constructed from model tool arguments."""

    def __init__(
        self,
        *,
        grants: tuple[SkillScriptGrant, ...],
        workspace: Path,
        inputs: Path,
        execution_id: str,
        timeout_seconds: float = 30,
        max_output_bytes: int = 1024 * 1024,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.inputs = inputs.resolve(strict=True)
        if not execution_id or not grants or len({g.name for g in grants}) != len(grants):
            raise SkillScriptError("Execution identity and unique explicit grants are required")
        roots = (self.workspace, self.inputs, *(g.directory for g in grants))
        for index, left in enumerate(roots):
            if not left.is_dir():
                raise SkillScriptError("Script mount roots must be directories")
            for right in roots[index + 1 :]:
                if left.is_relative_to(right) or right.is_relative_to(left):
                    raise SkillScriptError("Workspace, inputs and installations must be disjoint")
        if not 0 < timeout_seconds <= 120 or not 1024 <= max_output_bytes <= 4 * 1024 * 1024:
            raise SkillScriptError("Invalid script timeout or output budget")
        self.execution_id = execution_id
        self.grants = {grant.name: grant for grant in grants}
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self._lock = asyncio.Lock()

    def command(
        self, grant: SkillScriptGrant, script: str, arguments: list[str], *, readonly: bool
    ) -> list[str]:
        if shutil.which("bwrap") is None or not Path("/usr/bin/prlimit").is_file():
            raise SkillScriptError("Required script isolation backend is unavailable")
        if script not in grant.scripts:
            raise SkillScriptError("Script is not granted by the host")
        if (
            not isinstance(arguments, list)
            or len(arguments) > 64
            or any(not isinstance(value, str) or "\0" in value for value in arguments)
            or sum(len(value.encode()) for value in arguments) > 16_384
        ):
            raise SkillScriptError("Script arguments exceed the bounded interface")
        if package_digest(grant.directory) != grant.digest:
            raise SkillScriptError("Installed Skill changed after the host grant")
        return [
            "bwrap",
            "--die-with-parent",
            "--unshare-all",
            "--cap-drop",
            "ALL",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--ro-bind",
            str(grant.directory),
            "/skill",
            "--ro-bind",
            str(self.inputs),
            "/inputs",
            "--ro-bind" if readonly else "--bind",
            str(self.workspace),
            "/work",
            "--chdir",
            "/work",
            "--",
            "/usr/bin/prlimit",
            "--as=536870912",
            "--cpu=30",
            "--nofile=128",
            "--nproc=64",
            "--",
            "/usr/bin/python3",
            "-I",
            f"/skill/{script}",
            *arguments,
        ]

    async def run(
        self, skill_name: str, script: str, arguments: list[str], *, readonly: bool = False
    ) -> SkillScriptResult:
        async with self._lock:
            grant = self.grants.get(skill_name)
            if grant is None:
                raise SkillScriptError("Skill script execution is not granted by the host")
            command = self.command(grant, script, arguments, readonly=readonly)
            started = datetime.now(UTC).isoformat()
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin"},
                start_new_session=True,
            )

            output_bytes = 0

            async def read(stream: asyncio.StreamReader | None) -> bytes:
                nonlocal output_bytes
                assert stream is not None
                data = bytearray()
                while chunk := await stream.read(8192):
                    output_bytes += len(chunk)
                    if output_bytes > self.max_output_bytes:
                        raise SkillScriptError("Script output exceeds the host limit")
                    data.extend(chunk)
                return bytes(data)

            readers = [
                asyncio.create_task(read(process.stdout)),
                asyncio.create_task(read(process.stderr)),
            ]
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    stdout, stderr = await asyncio.gather(*readers)
                    await process.wait()
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                for task in readers:
                    task.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                await process.wait()
                raise
            if package_digest(grant.directory) != grant.digest:
                raise SkillScriptError("Installed Skill changed during execution")
            return SkillScriptResult(
                process.returncode if process.returncode is not None else -1,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                started,
                datetime.now(UTC).isoformat(),
                grant.digest,
            )
