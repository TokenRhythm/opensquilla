"""Opt-in Gateway hosting for a pinned standard Skill, with no business schema."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field

from opensquilla.skills.script_runtime import SkillScriptError, SkillScriptGrant, SkillScriptRunner

if TYPE_CHECKING:
    from opensquilla.tools.types import ToolContext

PROTECTED_SKILL_TOOLS = frozenset(
    {"skill_list", "skill_view", "run_skill_script", "publish_artifact"}
)


class ScriptHTTPRouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["GET", "POST"]
    path_pattern: str
    query_keys: list[str] = Field(default_factory=list)


class ScriptHTTPServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    origin: str
    routes: list[ScriptHTTPRouteConfig] = Field(min_length=1)
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    max_request_bytes: int = Field(default=1024 * 1024, ge=1, le=1024 * 1024)
    max_response_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=32 * 1024 * 1024)


class ProtectedSkillConfig(BaseModel):
    """Operator-only grants; never accepted through task/route/model metadata."""

    model_config = ConfigDict(extra="forbid")
    agent_ids: list[str] = Field(min_length=1)
    name: str
    directory: Path
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scripts: list[str] = Field(min_length=1)
    validator_script: str
    allowed_artifacts: list[str] = Field(min_length=1)
    runtime_root: Path
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_files: dict[str, str] = Field(default_factory=dict)
    state_root: Path
    socket_root: Path
    config_environment_variable: str
    script_config: dict[str, Any] = Field(default_factory=dict)
    services: list[ScriptHTTPServiceConfig] = Field(default_factory=list)
    timeout_seconds: float = Field(default=120, gt=0, le=120)
    max_output_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=4 * 1024 * 1024)


def runtime_digest(root: Path) -> str:
    """Pin a non-editable venv, including its interpreter and symlink targets."""
    root = root.resolve(strict=True)
    entries: list[tuple[str, str]] = []
    total = 0
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if len(entries) >= 10000:
            raise SkillScriptError("Skill runtime exceeds the bounded installation")
        if stat.S_ISLNK(info.st_mode):
            target = path.resolve(strict=True)
            if target.is_dir() and target.is_relative_to(root):
                digest = hashlib.sha256(os.readlink(path).encode())
                entries.append((path.relative_to(root).as_posix(), digest.hexdigest()))
                continue
            if not target.is_file() or not (
                target.is_relative_to(root) or target.is_relative_to(Path("/usr"))
            ):
                raise SkillScriptError("Runtime symlink must target its installation or /usr")
            target_size = target.stat().st_size
            total += target_size
            if target_size > 128 * 1024 * 1024 or total > 512 * 1024 * 1024:
                raise SkillScriptError("Skill runtime exceeds the bounded installation")
            digest = hashlib.sha256(os.readlink(path).encode() + b"\0" + target.read_bytes())
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
            if total > 512 * 1024 * 1024 or info.st_size > 128 * 1024 * 1024:
                raise SkillScriptError("Skill runtime exceeds the bounded installation")
            data = path.read_bytes()
            if len(data) != info.st_size:
                raise SkillScriptError("Skill runtime changed while being pinned")
            if path.suffix == ".pth" and b"editable" in data.lower():
                raise SkillScriptError("Editable Skill runtimes are not supported")
            digest = hashlib.sha256(data)
        else:
            raise SkillScriptError("Skill runtime contains a non-regular file")
        entries.append((path.relative_to(root).as_posix(), digest.hexdigest()))
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def _directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise SkillScriptError("Skill host directories must be owned and not group/world writable")
    return path.resolve(strict=True)


def _immutable_file(path: Path, data: bytes) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or path.read_bytes() != data
        ):
            raise SkillScriptError("Existing Skill task configuration differs from the host grant")
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)


def bind_protected_skill(context: ToolContext, config: ProtectedSkillConfig) -> ToolContext:
    """Called by the ordinary Gateway turn path after durable identity resolution."""
    from opensquilla.skills.http_broker import HTTPRoute, HTTPServiceGrant, SkillHTTPBroker
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.skills.publication import SkillManifestPublicationPolicy

    if context.agent_id not in config.agent_ids:
        return context
    if (
        not context.execution_id
        or not context.artifact_session_id
        or not context.session_key
        or context.subagent_depth
        or type(context.session_epoch) is not int
        or context.session_epoch < 0
    ):
        raise SkillScriptError("Protected Skill requires a durable top-level session and execution")
    grant = SkillScriptGrant.pin(
        config.name,
        config.directory,
        frozenset([*config.scripts, config.validator_script]),
    )
    if grant.digest != config.package_sha256:
        raise SkillScriptError("Installed Skill does not match the configured package digest")
    if runtime_digest(config.runtime_root) != config.runtime_sha256:
        raise SkillScriptError("Installed runtime does not match the configured digest")
    root = _directory(config.state_root)
    runtime = config.runtime_root.resolve(strict=True)
    for installation in (grant.directory, runtime):
        if root.is_relative_to(installation) or installation.is_relative_to(root):
            raise SkillScriptError("Host task state must be separate from installed code")
    identity = [context.agent_id, context.artifact_session_id, context.session_epoch]
    binding = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    task = _directory(root / binding)
    directories = {
        name: _directory(task / name)
        for name in ("work", "private", "media", "inputs", "receipts", "validation", "host")
    }
    caller_binding = f"skill-task:{binding}"
    script_config = dict(config.script_config)
    reserved = {"workspace", "private_root", "media_root", "caller_binding"}
    if reserved.intersection(script_config):
        raise SkillScriptError("Script configuration must not override host roots or identity")
    script_config.update(
        workspace="/work",
        private_root="/private",
        media_root="/media",
        caller_binding=caller_binding,
    )
    config_file = directories["host"] / "config.toml"
    _immutable_file(config_file, tomli_w.dumps(script_config).encode())
    _immutable_file(
        directories["host"] / "identity.json",
        json.dumps(
            {
                "callerBinding": caller_binding,
                "packageSha256": grant.digest,
                "runtimeSha256": config.runtime_sha256,
                "profileSha256": hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
            },
            sort_keys=True,
        ).encode(),
    )
    services = []
    for service in config.services:
        key = os.environ.get(service.api_key_env, "") if service.api_key_env else None
        if service.api_key_env and not key:
            raise SkillScriptError("Configured Skill service credential is unavailable")
        services.append(
            HTTPServiceGrant(
                name=service.name,
                origin=service.origin,
                routes=tuple(
                    HTTPRoute(route.method, route.path_pattern, frozenset(route.query_keys))
                    for route in service.routes
                ),
                api_key=key,
                timeout_seconds=service.timeout_seconds,
                max_request_bytes=service.max_request_bytes,
                max_response_bytes=service.max_response_bytes,
            )
        )
    broker = (
        SkillHTTPBroker(
            services=tuple(services),
            socket_directory=_directory(_directory(config.socket_root) / binding[:16]),
            receipt_directory=directories["receipts"],
            execution_id=context.execution_id,
            caller_binding=caller_binding,
        )
        if services
        else None
    )
    runner = SkillScriptRunner(
        grants=(grant,),
        workspace=directories["work"],
        inputs=directories["inputs"],
        execution_id=context.execution_id,
        timeout_seconds=config.timeout_seconds,
        max_output_bytes=config.max_output_bytes,
        runtime_root=runtime,
        runtime_sha256=config.runtime_sha256,
        runtime_files=config.runtime_files,
        private_root=directories["private"],
        media_root=directories["media"],
        config_file=config_file,
        config_environment_variable=config.config_environment_variable,
        receipt_root=directories["receipts"],
        caller_binding=caller_binding,
        broker=broker,
    )
    policy = SkillManifestPublicationPolicy(
        runner=runner,
        skill_name=grant.name,
        validator_script=config.validator_script,
        allowed_artifacts=frozenset(config.allowed_artifacts),
        caller_binding=caller_binding,
        input_roots={"private": directories["private"], "receipts": directories["receipts"]},
        receipt_directory=directories["validation"],
    )
    loader = SkillLoader(
        extra_dirs=[grant.directory.parent],
        snapshot_path=directories["host"] / "skill-catalog.json",
    )
    loader.load_all()
    catalog = loader.snapshot()
    skills = tuple(skill for skill in catalog.skills if skill.name == grant.name)
    if len(skills) != 1 or Path(skills[0].base_dir).resolve() != grant.directory:
        raise SkillScriptError("The pinned standard Skill must load under its granted name")
    allowed = (
        set(PROTECTED_SKILL_TOOLS)
        if context.allowed_tools is None
        else context.allowed_tools & PROTECTED_SKILL_TOOLS
    )
    return replace(
        context,
        workspace_dir=str(directories["work"]),
        workspace_strict=True,
        workspace_lockdown=True,
        run_mode="standard",
        elevated=None,
        sandbox_mounts=[],
        sandbox_run_context=None,
        allowed_tools=set(allowed),
        surfaced_tools=set(allowed),
        skill_catalog=replace(catalog, skills=skills),
        skill_script_runner=runner,
        protected_skill_host=True,
        artifact_publication_policy=policy,
    )


def enforce_protected_tool_scope(context: ToolContext, available_tools: list[str]) -> None:
    if not context.protected_skill_host:
        return
    context.allowed_tools = (
        set(PROTECTED_SKILL_TOOLS)
        if context.allowed_tools is None
        else context.allowed_tools & PROTECTED_SKILL_TOOLS
    )
    context.surfaced_tools = set(context.allowed_tools)
    context.denied_tools.update(set(available_tools) - PROTECTED_SKILL_TOOLS)
