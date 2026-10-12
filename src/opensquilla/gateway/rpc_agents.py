"""RPC handlers for the agents domain."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, cast

from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.identity.bootstrap import (
    CORE_BOOTSTRAP_TEMPLATE_FILENAMES,
    ONE_SHOT_BOOTSTRAP_FILENAME,
    ensure_agent_workspace,
)
from opensquilla.session.keys import normalize_agent_id

_d = get_dispatcher()

_ALLOWED_FILE_EXTENSIONS = frozenset({".md", ".txt", ".yaml", ".yml", ".j2"})
_WORKSPACE_AGENT_FILE_NAMES = (
    *CORE_BOOTSTRAP_TEMPLATE_FILENAMES,
    ONE_SHOT_BOOTSTRAP_FILENAME,
    "MEMORY.md",
    "memory.md",
)
_WORKSPACE_AGENT_FILE_NAME_SET = frozenset(_WORKSPACE_AGENT_FILE_NAMES)


def _slugify(name: str) -> str:
    """Generate a slug-based ID from a name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "agent"


def _get_agent_registry(ctx: RpcContext):
    agent_registry = getattr(ctx, "agent_registry", None)
    if agent_registry is None:
        raise RpcUnavailableError("Agent registry not available")
    return agent_registry


def _workspace_file_root(ctx: RpcContext, agent_id: str) -> Path | None:
    config = getattr(ctx, "config", None)
    if not getattr(config, "workspace_dir", None):
        return None
    return ensure_agent_workspace(resolve_agent_workspace_dir(agent_id, config)).workspace_dir


def _validate_workspace_file_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("params.name is required")
    if name != Path(name).name or "/" in name or "\\" in name:
        raise ValueError("workspace file name must not contain path separators")
    if name not in _WORKSPACE_AGENT_FILE_NAME_SET:
        raise ValueError(f"Unsupported workspace agent file: {name}")
    return name


def _workspace_file_entry(root: Path, name: str) -> dict[str, Any]:
    path = root / name
    entry: dict[str, Any] = {
        "name": name,
        "path": name,
        "exists": False,
        "missing": True,
        "status": "missing",
    }
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return entry
    entry.update({"exists": True, "missing": False})
    if stat.S_ISLNK(file_stat.st_mode):
        entry.update({"status": "unsafe", "unsafeReason": "symlink"})
        return entry
    if not stat.S_ISREG(file_stat.st_mode):
        entry.update({"status": "unsafe", "unsafeReason": "not-regular-file"})
        return entry
    if getattr(file_stat, "st_nlink", 1) > 1:
        entry.update({"status": "unsafe", "unsafeReason": "hardlink"})
        return entry
    entry.update({"status": "present", "size": file_stat.st_size})
    return entry


def _list_workspace_agent_files(root: Path) -> list[dict[str, Any]]:
    return [_workspace_file_entry(root, name) for name in _WORKSPACE_AGENT_FILE_NAMES]


def _resolve_workspace_agent_file(root: Path, name: str) -> tuple[str, Path]:
    safe_name = _validate_workspace_file_name(name)
    path = root / safe_name
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("workspace file escapes workspace root") from exc
    return safe_name, path


def _validate_safe_file_stat(file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("workspace agent file must be a regular file")
    if getattr(file_stat, "st_nlink", 1) > 1:
        raise ValueError("workspace agent file must not be hardlinked")


def _read_workspace_agent_file(root: Path, name: str) -> tuple[str, str]:
    safe_name, path = _resolve_workspace_agent_file(root, name)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, os.O_RDONLY | nofollow)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError("workspace agent file must not be a symlink") from exc

    try:
        _validate_safe_file_stat(os.fstat(fd))
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            content = handle.read()
    finally:
        if fd != -1:
            os.close(fd)
    return safe_name, content


def _open_workspace_agent_file_for_write(path: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        try:
            return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        except FileExistsError:
            file_stat = path.lstat()
    if stat.S_ISLNK(file_stat.st_mode):
        raise ValueError("workspace agent file must not be a symlink")
    _validate_safe_file_stat(file_stat)
    try:
        return os.open(path, os.O_WRONLY | nofollow)
    except OSError as exc:
        raise ValueError("workspace agent file must not be a symlink") from exc


def _write_workspace_agent_file(root: Path, name: str, content: Any) -> dict[str, Any]:
    safe_name, path = _resolve_workspace_agent_file(root, name)
    text = content if isinstance(content, str) else str(content)
    data = text.encode("utf-8")
    fd = _open_workspace_agent_file_for_write(path)
    try:
        _validate_safe_file_stat(os.fstat(fd))
        os.ftruncate(fd, 0)
        os.write(fd, data)
    finally:
        os.close(fd)
    return {"name": safe_name, "path": safe_name, "size": len(data)}


@_d.method("agents.list", scope="operator.read")
async def _handle_agents_list(params: dict | None, ctx: RpcContext) -> dict:
    include_builtin = (params or {}).get("includeBuiltin", True)

    agent_registry = getattr(ctx, "agent_registry", None)
    if agent_registry is not None:
        agents = await agent_registry.list_agents(include_builtin=include_builtin)
        return {"agents": agents}

    return {"agents": []}


_UPDATE_FIELD_MAP: tuple[tuple[str, ...], ...] = (
    ("name",),
    ("description",),
    ("model",),
    ("systemPrompt", "system_prompt"),
    ("tools",),
    ("workspace",),
    ("agentDir", "agent_dir"),
    ("enabled",),
)


@_d.method("agents.create", scope="operator.admin")
async def _handle_agents_create(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params.id or params.name is required")

    name = params.get("name")
    raw_agent_id = params.get("id") or params.get("agentId") or (_slugify(name) if name else None)
    if not raw_agent_id:
        raise ValueError("params.id or params.name is required")
    agent_id = normalize_agent_id(raw_agent_id)

    agent_registry = _get_agent_registry(ctx)
    try:
        result = await agent_registry.create_agent(
            agent_id=agent_id,
            name=name or agent_id,
            description=params.get("description"),
            model=params.get("model"),
            workspace=params.get("workspace"),
            agent_dir=params.get("agentDir") or params.get("agent_dir"),
            enabled=params.get("enabled", True),
            system_prompt=params.get("systemPrompt"),
            tools=params.get("tools"),
        )
    except ValueError as exc:
        msg = str(exc)
        if "already exists" in msg:
            raise RpcHandlerError(
                "agent.exists", msg, details={"agentId": agent_id}
            ) from exc
        if agent_id == "main" or "builtin" in msg.lower():
            raise RpcHandlerError(
                "agent.builtin_immutable", msg, details={"agentId": agent_id}
            ) from exc
        raise
    return cast(dict, result)


@_d.method("agents.update", scope="operator.admin")
async def _handle_agents_update(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")

    agent_id = normalize_agent_id(params["id"])
    updated_fields: list[str] = []
    for aliases in _UPDATE_FIELD_MAP:
        if any(alias in params for alias in aliases):
            updated_fields.append(aliases[0])

    if not updated_fields:
        raise ValueError("No fields to update")

    agent_registry = _get_agent_registry(ctx)
    try:
        result = await agent_registry.update_agent(agent_id, **{**params, "id": agent_id})
    except ValueError as exc:
        msg = str(exc)
        if "builtin" in msg.lower() or agent_id == "main":
            raise RpcHandlerError(
                "agent.builtin_immutable", msg, details={"agentId": agent_id}
            ) from exc
        raise
    except KeyError as exc:
        raise RpcHandlerError(
            "agent.not_found",
            f"Agent '{agent_id}' does not exist",
            details={"agentId": agent_id},
        ) from exc
    return cast(dict, result)


@_d.method("agents.delete", scope="operator.admin")
async def _handle_agents_delete(params: dict | None, ctx: RpcContext) -> None:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")

    agent_id = normalize_agent_id(params["id"])

    # Refuse to delete builtin agents
    if agent_id == "main":
        raise RpcHandlerError(
            "agent.builtin_immutable",
            "Cannot delete builtin agent: main",
            details={"agentId": agent_id},
        )

    agent_registry = _get_agent_registry(ctx)
    try:
        await agent_registry.delete_agent(agent_id)
    except KeyError as exc:
        raise RpcHandlerError(
            "agent.not_found",
            f"Agent '{agent_id}' does not exist",
            details={"agentId": agent_id},
        ) from exc
    return None


@_d.method("agents.files.list", scope="operator.read")
async def _handle_agents_files_list(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict) or "agentId" not in params:
        raise ValueError("params.agentId is required")

    agent_id = normalize_agent_id(params["agentId"])

    agent_registry = getattr(ctx, "agent_registry", None)
    if agent_registry is None:
        root = _workspace_file_root(ctx, agent_id)
        if root is None:
            raise RpcUnavailableError("Agent registry not available")
        return {"files": _list_workspace_agent_files(root)}
    files = await agent_registry.list_agent_files(agent_id)
    return {"files": files}


@_d.method("agents.files.get", scope="operator.read")
async def _handle_agents_files_get(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params required: agentId, name")
    if "agentId" not in params:
        raise ValueError("params.agentId is required")
    if "name" not in params:
        raise ValueError("params.name is required")

    agent_id = normalize_agent_id(params["agentId"])
    name = _validate_workspace_file_name(params["name"])

    agent_registry = getattr(ctx, "agent_registry", None)
    if agent_registry is None:
        root = _workspace_file_root(ctx, agent_id)
        if root is None:
            raise RpcUnavailableError("Agent registry not available")
        safe_name, content = _read_workspace_agent_file(root, name)
        return {"name": safe_name, "content": content}
    content = await agent_registry.get_agent_file(agent_id, name)
    return cast(dict, content)


@_d.method("agents.files.set", scope="operator.admin")
async def _handle_agents_files_set(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params required: agentId, name, content")
    if "agentId" not in params:
        raise ValueError("params.agentId is required")
    if "name" not in params:
        raise ValueError("params.name is required")
    if "content" not in params:
        raise ValueError("params.content is required")

    name = _validate_workspace_file_name(params["name"])
    # Validate file extension
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    if ext not in _ALLOWED_FILE_EXTENSIONS:
        raise ValueError(
            f"File extension not allowed: {ext}. Allowed: {sorted(_ALLOWED_FILE_EXTENSIONS)}"
        )

    content = params["content"]

    agent_registry = getattr(ctx, "agent_registry", None)
    if agent_registry is None:
        agent_id = normalize_agent_id(params["agentId"])
        root = _workspace_file_root(ctx, agent_id)
        if root is None:
            raise RpcUnavailableError("Agent registry not available")
        return _write_workspace_agent_file(root, name, content)
    result = await agent_registry.set_agent_file(
        normalize_agent_id(params["agentId"]),
        name,
        content,
    )
    return cast(dict, result)


@_d.method("agent.identity.get", scope="operator.read")
async def _handle_agent_identity_get(params: dict | None, ctx: RpcContext) -> dict:
    agent_id = normalize_agent_id((params or {}).get("agentId", "main"))

    agent_registry = _get_agent_registry(ctx)
    identity = await agent_registry.get_identity(agent_id)
    return cast(dict, identity)
