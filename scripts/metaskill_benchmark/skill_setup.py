"""Adapt writable Skill copies without modifying pinned benchmark inputs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any


def _bun_json(text: str) -> dict[str, Any]:
    """Read Bun's JSON-with-comments format, preserving quoted string contents."""
    cleaned = []
    index = 0
    quoted = escaped = False
    while index < len(text):
        char = text[index]
        if quoted:
            cleaned.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
        elif char == '"':
            quoted = True
            cleaned.append(char)
            index += 1
        elif text.startswith("//", index):
            end = text.find("\n", index + 2)
            index = len(text) if end < 0 else end
            cleaned.append(" ")
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("Unterminated comment in dev-browser bun.lock")
            cleaned.append(" " + "\n" * text[index:end].count("\n"))
            index = end + 2
        else:
            cleaned.append(char)
            index += 1
    text = "".join(cleaned)
    cleaned = []
    quoted = escaped = False
    for index, char in enumerate(text):
        if quoted:
            cleaned.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            cleaned.append(char)
        elif char == ",":
            following = index + 1
            while following < len(text) and text[following].isspace():
                following += 1
            if following == len(text) or text[following] not in "}]":
                cleaned.append(char)
        else:
            cleaned.append(char)
    value = json.loads("".join(cleaned))
    if not isinstance(value, dict):
        raise ValueError("dev-browser bun.lock must contain an object")
    return value


def _package_from_lock(raw: bytes) -> dict[str, Any]:
    lock = _bun_json(raw.decode("utf-8-sig"))
    workspaces = lock.get("workspaces")
    workspace = workspaces.get("") if isinstance(workspaces, dict) else None
    if not isinstance(workspace, dict) or not isinstance(workspace.get("name"), str) or not workspace["name"]:
        raise ValueError("dev-browser bun.lock is missing its root workspace name")
    dependencies = {}
    for field in ("dependencies", "devDependencies"):
        values = workspace.get(field, {})
        if (not isinstance(values, dict) or any(not isinstance(name, str) or not name
                or not isinstance(version, str) or not version for name, version in values.items())):
            raise ValueError(f"dev-browser bun.lock contains invalid {field}")
        dependencies[field] = values
    if "tsx" not in dependencies["dependencies"] and "tsx" not in dependencies["devDependencies"]:
        raise ValueError("dev-browser bun.lock is missing its tsx runtime dependency")
    return {
        "name": workspace["name"], "private": True, "type": "module",
        "scripts": {"start": "tsx scripts/start-server.ts",
                    "start-extension": "tsx scripts/start-relay.ts"},
        **dependencies,
    }


def _ensure_shell_executable(skills: Path) -> list[str]:
    changed = []
    for script in sorted(skills.rglob("*.sh")):
        if script.is_symlink():
            raise ValueError("Writable Skill copies must not contain shell-script links")
        if script.is_file():
            mode = script.stat().st_mode
            if not mode & stat.S_IXUSR:
                script.chmod(stat.S_IMODE(mode) | stat.S_IXUSR)
                changed.append(script.relative_to(skills).as_posix())
    return changed


def prepare_skill_runtime(skills: Path) -> dict[str, Any]:
    """Prepare this attempt's writable packages; never pass the frozen cache.

    The pinned dev-browser source lacks package.json. Restore its dependency
    declarations from its own Bun lock so its existing npm/tsx launcher works.
    No dependencies are installed here and an existing manifest is unchanged.
    """
    skills = skills.expanduser().resolve()
    if not skills.is_dir():
        raise ValueError("Writable Skill directory is missing")
    result: dict[str, Any] = {"adaptations": [], "executable_scripts": []}
    browser = skills / "dev-browser"
    manifest = browser / "package.json"
    if browser.is_dir() and not manifest.exists():
        lock = browser / "bun.lock"
        if not lock.is_file():
            raise ValueError("dev-browser lacks both package.json and bun.lock")
        for script in ("start-server.ts", "start-relay.ts"):
            if not (browser / "scripts" / script).is_file():
                raise ValueError(f"dev-browser is missing scripts/{script}")
        raw = lock.read_bytes()
        package = _package_from_lock(raw)
        generated = (json.dumps(package, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        manifest.write_bytes(generated)
        result["adaptations"].append({
            "skill": "dev-browser", "kind": "package_manifest_from_bun_lock",
            "path": manifest.relative_to(skills).as_posix(),
            "source_path": lock.relative_to(skills).as_posix(),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "generated_sha256": hashlib.sha256(generated).hexdigest(),
        })
    if os.name == "posix":
        result["executable_scripts"] = _ensure_shell_executable(skills)
    return result
