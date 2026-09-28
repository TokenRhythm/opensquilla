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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opensquilla.skills.http_broker import SkillHTTPBroker

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_MAX_PACKAGE_BYTES = 8 * 1024 * 1024


class SkillScriptError(ValueError):
    """An installed-script grant or its bounded execution failed."""


def runtime_files_digest(paths: Iterable[str]) -> dict[str, str]:
    """Hash an explicit small set of mounted system runtime dependencies."""
    result: dict[str, str] = {}
    total = 0
    for number, name in enumerate(paths):
        if number >= 128:
            raise SkillScriptError("External runtime file list exceeds its bound")
        if not isinstance(name, str):
            raise SkillScriptError("External runtime paths must be absolute strings")
        path = Path(name)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or not (path.is_relative_to("/usr") or path.is_relative_to("/etc/fonts"))
        ):
            raise SkillScriptError("External runtime files must be under /usr or /etc/fonts")
        try:
            original = path.lstat()
            resolved = path.resolve(strict=True)
            if not (
                resolved.is_relative_to("/usr") or resolved.is_relative_to("/etc/fonts")
            ) or not (stat.S_ISLNK(original.st_mode) or stat.S_ISREG(original.st_mode)):
                raise SkillScriptError(
                    "External runtime files must resolve to allowed regular files"
                )
            info = resolved.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
                raise SkillScriptError("External runtime file is not bounded and regular")
            total += info.st_size
            if total > 128 * 1024 * 1024:
                raise SkillScriptError("External runtime files exceed the total byte bound")
            descriptor = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
                    raise SkillScriptError("External runtime file changed while being read")
                digest = hashlib.sha256()
                count = 0
                while data := stream.read(65536):
                    count += len(data)
                    if count > info.st_size:
                        raise SkillScriptError("External runtime file changed while being read")
                    digest.update(data)
                after = os.fstat(stream.fileno())
                if (
                    count != info.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                    or path.resolve(strict=True) != resolved
                    or resolved.lstat().st_ino != before.st_ino
                ):
                    raise SkillScriptError("External runtime file changed while being read")
        except (OSError, RuntimeError) as error:
            raise SkillScriptError("External runtime file is missing or unavailable") from error
        result[name] = digest.hexdigest()
    return result


def check_runtime_files(pins: Mapping[str, str]) -> None:
    """Fail closed when any declared system dependency no longer matches its pin."""
    if len(pins) > 128 or any(
        not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        for digest in pins.values()
    ):
        raise SkillScriptError("External runtime file pins must be bounded SHA256 values")
    if runtime_files_digest(pins) != dict(pins):
        raise SkillScriptError("External runtime files changed after the host grant")


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


def private_inventory(directory: Path) -> dict[str, str]:
    """Bounded host inventory; linked, changing or special private files fail closed."""
    result: dict[str, str] = {}
    total = 0

    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    for number, path in enumerate(directory.rglob("*")):
        if number >= 8192:
            raise SkillScriptError("Private state exceeds the host entry budget")
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SkillScriptError("Private state must contain only unlinked regular files")
        total += info.st_size
        if info.st_size > 64 * 1024 * 1024 or total > 256 * 1024 * 1024 or len(result) >= 4096:
            raise SkillScriptError("Private state exceeds the host inventory budget")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if identity(before) != identity(info):
                raise SkillScriptError("Private state changed while inventorying")
            digest = hashlib.sha256()
            count = 0
            while data := stream.read(65536):
                count += len(data)
                if count > info.st_size:
                    raise SkillScriptError("Private state changed while inventorying")
                digest.update(data)
            after = os.fstat(stream.fileno())
            if (
                count != info.st_size
                or identity(after) != identity(before)
                or identity(path.lstat()) != identity(before)
            ):
                raise SkillScriptError("Private state changed while inventorying")
        result[path.relative_to(directory).as_posix()] = digest.hexdigest()
    return result


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
    stdout_sha256: str = ""
    stderr_sha256: str = ""


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
        max_input_bytes: int = 16 * 1024 * 1024,
        runtime_root: Path | None = None,
        runtime_sha256: str | None = None,
        runtime_files: dict[str, str] | None = None,
        private_root: Path | None = None,
        media_root: Path | None = None,
        config_file: Path | None = None,
        config_environment_variable: str | None = None,
        receipt_root: Path | None = None,
        broker: SkillHTTPBroker | None = None,
        caller_binding: str | None = None,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.inputs = inputs.resolve(strict=True)
        if not execution_id or not grants or len({g.name for g in grants}) != len(grants):
            raise SkillScriptError("Execution identity and unique explicit grants are required")
        self.runtime_root = runtime_root.resolve(strict=True) if runtime_root else None
        self.runtime_sha256 = runtime_sha256
        self.runtime_files = dict(runtime_files or {})
        if runtime_sha256 is not None and (
            self.runtime_root is None or not re.fullmatch(r"[0-9a-f]{64}", runtime_sha256)
        ):
            raise SkillScriptError("Runtime digest requires a valid installed runtime")
        self.private_root = private_root.resolve(strict=True) if private_root else None
        self.media_root = media_root.resolve(strict=True) if media_root else None
        self.receipt_root = receipt_root.resolve(strict=True) if receipt_root else None
        self.config_file = config_file.resolve(strict=True) if config_file else None
        self.config_environment_variable = config_environment_variable
        self.broker = broker
        self.caller_binding = caller_binding
        if self.private_root is not None and self.receipt_root is not None and not caller_binding:
            raise SkillScriptError("Private invocation proofs require a host caller identity")
        optional_roots = tuple(
            root
            for root in (self.runtime_root, self.private_root, self.media_root, self.receipt_root)
            if root is not None
        )
        roots = (self.workspace, self.inputs, *(g.directory for g in grants), *optional_roots)
        for index, left in enumerate(roots):
            if not left.is_dir():
                raise SkillScriptError("Script mount roots must be directories")
            for right in roots[index + 1 :]:
                if left.is_relative_to(right) or right.is_relative_to(left):
                    raise SkillScriptError("Script mount roots must be disjoint")
        if bool(self.config_file) != bool(config_environment_variable) or (
            config_environment_variable is not None
            and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", config_environment_variable)
        ):
            raise SkillScriptError("Host configuration needs an explicit environment variable")
        if self.config_file is not None and (
            not self.config_file.is_file()
            or any(self.config_file.is_relative_to(root) for root in roots)
        ):
            raise SkillScriptError("Host configuration must be a separate regular file")
        if self.runtime_root is not None and not (self.runtime_root / "bin/python").is_file():
            raise SkillScriptError("Runtime must contain a fixed bin/python interpreter")
        if broker is not None and (
            broker.execution_id != execution_id
            or broker.caller_binding != caller_binding
            or broker.receipt_directory != self.receipt_root
            or any(
                broker.socket_directory.is_relative_to(root)
                or root.is_relative_to(broker.socket_directory)
                for root in roots
            )
        ):
            raise SkillScriptError("Broker identity and private mount scope do not match")
        if not 0 < timeout_seconds <= 120 or not 1024 <= max_output_bytes <= 4 * 1024 * 1024:
            raise SkillScriptError("Invalid script timeout or output budget")
        if type(max_input_bytes) is not int or not 1 <= max_input_bytes <= 16 * 1024 * 1024:
            raise SkillScriptError("Invalid script input budget")
        self.execution_id = execution_id
        self.grants = {grant.name: grant for grant in grants}
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.max_input_bytes = max_input_bytes
        self._lock = asyncio.Lock()

    def command(
        self,
        grant: SkillScriptGrant,
        script: str,
        arguments: list[str],
        *,
        readonly: bool,
        broker_socket: Path | None = None,
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
        self._check_runtime()
        mounts: list[str] = []
        if Path("/etc/fonts").is_dir():
            mounts.extend(["--ro-bind", "/etc/fonts", "/etc/fonts"])
        for root, target in (
            (self.runtime_root, "/runtime"),
            (self.private_root, "/private"),
            (self.media_root, "/media"),
        ):
            if root is not None:
                mode = "--ro-bind" if readonly or target == "/runtime" else "--bind"
                mounts.extend([mode, str(root), target])
        if self.config_file is not None and self.config_environment_variable:
            mounts.extend(
                [
                    "--ro-bind",
                    str(self.config_file),
                    "/host/config.toml",
                    "--setenv",
                    self.config_environment_variable,
                    "/host/config.toml",
                ]
            )
        if readonly and self.receipt_root is not None:
            mounts.extend(["--ro-bind", str(self.receipt_root), "/receipts"])
        if broker_socket is not None and not readonly:
            mounts.extend(["--ro-bind", str(broker_socket), "/host/broker.sock"])
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
            *mounts,
            "--chdir",
            "/work",
            "--",
            "/usr/bin/prlimit",
            "--as=536870912",
            "--cpu=30",
            "--nofile=128",
            "--nproc=64",
            "--",
            "/runtime/bin/python" if self.runtime_root else "/usr/bin/python3",
            "-I",
            f"/skill/{script}",
            *arguments,
        ]

    def _check_runtime(self) -> None:
        check_runtime_files(self.runtime_files)
        if self.runtime_root is not None and self.runtime_sha256 is not None:
            from opensquilla.skills.host import runtime_digest

            if runtime_digest(self.runtime_root) != self.runtime_sha256:
                raise SkillScriptError("Installed runtime changed after the host grant")

    async def run(
        self,
        skill_name: str,
        script: str,
        arguments: list[str],
        *,
        readonly: bool = False,
        stdin: bytes | None = None,
    ) -> SkillScriptResult:
        if stdin is not None and (
            not isinstance(stdin, bytes) or len(stdin) > self.max_input_bytes
        ):
            raise SkillScriptError("Script input exceeds the bounded interface")
        async with self._lock:
            grant = self.grants.get(skill_name)
            if grant is None:
                raise SkillScriptError("Skill script execution is not granted by the host")
            async with contextlib.AsyncExitStack() as stack:
                socket_path = None
                if self.broker is not None and not readonly:
                    socket_path = await stack.enter_async_context(self.broker.serve())
                command = self.command(
                    grant, script, arguments, readonly=readonly, broker_socket=socket_path
                )
                started = datetime.now(UTC).isoformat()
                try:
                    result = await self._execute(command, grant, stdin)
                    if not readonly:
                        self._invocation_receipt(grant, script, arguments, stdin, started, result)
                    return result
                except BaseException:
                    if not readonly:
                        self._invocation_receipt(grant, script, arguments, stdin, started, None)
                    raise

    def _invocation_receipt(
        self,
        grant: SkillScriptGrant,
        script: str,
        arguments: list[str],
        stdin: bytes | None,
        started: str,
        result: SkillScriptResult | None,
    ) -> None:
        if self.private_root is None or self.receipt_root is None:
            return
        inventory = private_inventory(self.private_root) if result is not None else None
        receipt = {
            "schemaVersion": "skill-script-invocation/1",
            "callerBinding": self.caller_binding,
            "executionId": self.execution_id,
            "skillName": grant.name,
            "script": script,
            "argv": arguments,
            "stdinSha256": hashlib.sha256(stdin or b"").hexdigest(),
            "stdoutSha256": (
                result.stdout_sha256 or hashlib.sha256(result.stdout.encode()).hexdigest()
            )
            if result
            else None,
            "stderrSha256": (
                result.stderr_sha256 or hashlib.sha256(result.stderr.encode()).hexdigest()
            )
            if result
            else None,
            "packageSha256": grant.digest,
            "runtimeSha256": self.runtime_sha256,
            "returncode": result.returncode if result else None,
            "startedAt": result.started_at if result else started,
            "finishedAt": result.finished_at if result else datetime.now(UTC).isoformat(),
            "privateDigests": inventory,
        }
        data = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        directory = self.receipt_root / "invocations"
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise SkillScriptError("Invocation receipts must use a private host directory")
        descriptor = os.open(
            directory / f"{hashlib.sha256(data).hexdigest()}.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    async def _execute(
        self, command: list[str], grant: SkillScriptGrant, stdin: bytes | None
    ) -> SkillScriptResult:
        started = datetime.now(UTC).isoformat()
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
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

        async def write_input() -> None:
            assert process.stdin is not None
            try:
                if stdin:
                    process.stdin.write(stdin)
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        writer = asyncio.create_task(write_input())
        try:
            async with asyncio.timeout(self.timeout_seconds):
                stdout, stderr = await asyncio.gather(*readers)
                await writer
                await process.wait()
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            for task in [*readers, writer]:
                task.cancel()
            await asyncio.gather(*readers, writer, return_exceptions=True)
            await process.wait()
            raise
        if package_digest(grant.directory) != grant.digest:
            raise SkillScriptError("Installed Skill changed during execution")
        self._check_runtime()
        return SkillScriptResult(
            process.returncode if process.returncode is not None else -1,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            started,
            datetime.now(UTC).isoformat(),
            grant.digest,
            hashlib.sha256(stdout).hexdigest(),
            hashlib.sha256(stderr).hexdigest(),
        )
