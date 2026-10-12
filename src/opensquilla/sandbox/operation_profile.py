"""Lightweight operation profiles for sandbox policy and prompts."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from opensquilla.sandbox.domain_validation import normalize_domain

_PYTHON_EXE_RE = re.compile(r"python(?:\d+(?:\.\d+)*)?$")
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_NODE_INSTALL_COMMANDS = frozenset({"add", "ci", "install"})
_NODE_REGISTRY_QUERY_COMMANDS = frozenset({"info", "search", "show", "view"})
_ENV_CREATE_OPTION_TOKENS = frozenset({"--help", "-h", "--version", "-V"})
_ENV_CREATE_OPTIONS_WITH_VALUE = frozenset({"--prompt", "-p", "--python"})
_PYTHON_ENV_COMMANDS = frozenset({"virtualenv"})
_PYTHON_PROJECT_INSTALL_COMMANDS = frozenset({"poetry", "rye", "pixi"})
_JAVA_BUILD_COMMANDS = frozenset({"mvn", "mvnw", "gradle", "gradlew"})
_SHELL_WRAPPERS = frozenset({"bash", "dash", "fish", "ksh", "powershell", "pwsh", "sh", "zsh"})
_PRIVILEGE_WRAPPERS = frozenset({"doas", "runas", "sudo"})
_DESTRUCTIVE_COMMANDS = frozenset({"del", "erase", "format", "mkfs", "remove-item", "rm"})
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|"})
_HOST_SOFTWARE_MANAGERS = frozenset(
    {
        "apt",
        "apt-get",
        "brew",
        "choco",
        "dnf",
        "flatpak",
        "mas",
        "msiexec",
        "pacman",
        "scoop",
        "snap",
        "winget",
        "yum",
        "zypper",
    }
)
_SERVICE_MANAGERS = frozenset(
    {
        "launchctl",
        "new-service",
        "remove-service",
        "restart-service",
        "sc",
        "service",
        "set-service",
        "start-service",
        "stop-service",
        "systemctl",
    }
)
_SOFTWARE_MANAGEMENT_COMMANDS = frozenset(
    {
        "add-appxpackage",
        "install-package",
        "remove-appxpackage",
        "uninstall-package",
    }
)
_REGISTRY_COMMANDS = frozenset(
    {
        "clear-item",
        "get-childitem",
        "get-item",
        "get-itemproperty",
        "gci",
        "gp",
        "new-item",
        "new-itemproperty",
        "reg",
        "remove-item",
        "remove-itemproperty",
        "set-item",
        "set-itemproperty",
    }
)
_SYSTEM_SETTING_COMMANDS = frozenset(
    {
        "netsh",
        "new-netfirewallrule",
        "remove-netfirewallrule",
        "set-executionpolicy",
        "set-netfirewallprofile",
        "setx",
    }
)
_DRIVER_FEATURE_COMMANDS = frozenset({"bcdedit", "dism", "pnputil", "wsl"})
_INSTALLER_EXTENSIONS = (
    ".appinstaller",
    ".deb",
    ".dmg",
    ".msi",
    ".msix",
    ".msixbundle",
    ".pkg",
    ".rpm",
)
_INSTALLER_EXE_RE = re.compile(
    r"(install|installer|setup|update|uninstall|uninstaller|unins|uninst|remove)",
    re.IGNORECASE,
)
_HOST_PROBE_COMMANDS = frozenset(
    {
        "get-appxpackage",
        "get-ciminstance",
        "get-command",
        "get-package",
        "get-process",
        "get-wmiobject",
        "tasklist",
        "test-path",
        "where",
    }
)
_PROCESS_MANAGEMENT_COMMANDS = frozenset(
    {
        "killall",
        "pkill",
        "stop-process",
        "taskkill",
    }
)
_URL_NETWORK_COMMANDS = frozenset(
    {
        "aria2",
        "aria2c",
        "curl",
        "http",
        "httpie",
        "https",
        "invoke-restmethod",
        "invoke-webrequest",
        "irm",
        "iwr",
        "wget",
    }
)
_GIT_NETWORK_COMMANDS = frozenset(
    {"clone", "fetch", "ls-remote", "pull", "push", "submodule"}
)
_READ_PATH_COMMANDS = frozenset(
    {"cat", "du", "find", "grep", "head", "ls", "rg", "tail", "tree"}
)
_COPY_PATH_COMMANDS = frozenset({"cp", "copy", "install", "rsync"})
_MOVE_PATH_COMMANDS = frozenset({"mv", "move", "ren", "rename"})
_ASSIGNMENT_RE = re.compile(r"[a-z_][a-z0-9_]*=.*")
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}\"'`>"
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[a-z]:[\\/]", re.IGNORECASE)
_WINDOWS_ABSOLUTE_PATH_IN_SCRIPT_RE = re.compile(
    r"(?<![a-z0-9_])[a-z]:[\\/]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OperationProfile:
    name: str
    needs_network: bool = False
    package_manager: str | None = None
    requested_domains: tuple[str, ...] = ()
    requested_paths: tuple[str, ...] = ()
    requested_write_paths: tuple[str, ...] = ()
    high_impact: bool = False
    host_effect: str | None = None


def classify_command(argv: tuple[str, ...] | list[str]) -> OperationProfile:
    parts = tuple(str(p) for p in argv)
    python_process_parts = _windows_invoke_python_process_parts(parts)
    if python_process_parts is not None:
        return classify_command(python_process_parts)
    windows_shell_host_command = _windows_shell_host_command(parts)
    if windows_shell_host_command is not None:
        return classify_command(("powershell", "-Command", windows_shell_host_command))
    lowered = tuple(p.lower() for p in parts)
    unwrapped = _strip_command_wrapper(lowered)
    if unwrapped != lowered:
        return classify_command(unwrapped)
    privilege_unwrapped = _strip_privilege_wrapper(lowered)
    if privilege_unwrapped != lowered:
        return classify_command(privilege_unwrapped)
    windows_cmd_script = _windows_cmd_wrapper_script(parts)
    if windows_cmd_script is not None:
        return classify_command(("sh", "-lc", windows_cmd_script))
    host_effect = _host_effect(lowered)
    if host_effect is not None and host_effect != "global_tool_install":
        return OperationProfile("host_effect_shell", host_effect=host_effect)
    if _is_python_install(lowered):
        return OperationProfile(
            "package_install",
            True,
            "python",
            host_effect=host_effect,
        )
    if _is_python_package_query(lowered):
        return OperationProfile("package_query", True, "python")
    if _is_python_env_create(lowered):
        return OperationProfile(
            "create_env",
            package_manager="python",
            requested_write_paths=_env_create_write_paths(parts),
        )
    if _is_python_project_install(lowered):
        return OperationProfile("package_install", True, "python")
    if _is_php_package_install(lowered):
        return OperationProfile("package_install", True, "php")
    if _is_java_package_install(lowered):
        return OperationProfile("package_install", True, "java")
    if _is_node_install(lowered):
        return OperationProfile(
            "package_install",
            True,
            "node",
            host_effect=host_effect,
        )
    if _is_node_package_query(lowered):
        return OperationProfile("package_query", True, "node")
    if _is_rust_package_install(lowered):
        return OperationProfile(
            "package_install",
            True,
            "rust",
            host_effect=host_effect,
        )
    if _is_go_package_install(lowered):
        return OperationProfile(
            "package_install",
            True,
            "go",
            host_effect=host_effect,
        )
    if host_effect is not None:
        return OperationProfile("host_effect_shell", host_effect=host_effect)
    if _is_shell_wrapper(lowered):
        script_profile = _classify_shell_script(parts)
        if _shell_script_is_destructive(lowered):
            return OperationProfile(
                "destructive_shell",
                needs_network=script_profile.needs_network,
                package_manager=script_profile.package_manager,
                requested_domains=script_profile.requested_domains,
                requested_paths=script_profile.requested_paths,
                requested_write_paths=script_profile.requested_write_paths,
                high_impact=True,
            )
        if (
            script_profile.needs_network
            or script_profile.requested_paths
            or script_profile.requested_write_paths
            or script_profile.host_effect
        ):
            return script_profile
        return OperationProfile("unknown_shell")
    if _is_destructive(lowered):
        return OperationProfile(
            "destructive_shell",
            requested_write_paths=_path_args_from_argv(parts),
            high_impact=True,
        )
    domains = _domains_from_argv(parts)
    if domains and _command_uses_http_url(lowered):
        return OperationProfile(
            "url_fetch",
            True,
            requested_domains=domains,
            host_effect=_installer_download_host_effect(lowered),
        )
    if lowered and _command_name(lowered[0]) in _READ_PATH_COMMANDS:
        return OperationProfile("workspace_read", requested_paths=_path_args_from_argv(parts))
    if lowered and _command_name(lowered[0]) in _COPY_PATH_COMMANDS:
        read_paths, write_paths = _copy_path_args_from_argv(parts)
        if read_paths or write_paths:
            return OperationProfile(
                "path_transfer",
                requested_paths=read_paths,
                requested_write_paths=write_paths,
            )
    if lowered and _command_name(lowered[0]) in _MOVE_PATH_COMMANDS:
        write_paths = _path_args_from_argv(parts)
        if write_paths:
            return OperationProfile("path_transfer", requested_write_paths=write_paths)
    return OperationProfile("unknown_shell")


def package_bundle_for_manager(package_manager: str | None) -> str | None:
    return {
        "python": "python-package-install",
        "node": "node-package-install",
        "rust": "rust-package-install",
        "go": "go-package-install",
        "java": "java-package-install",
        "php": "php-package-install",
    }.get(package_manager or "")


def shell_command_approval_variants(command: str) -> tuple[str, ...]:
    variants: list[str] = []

    def add_text(text: str) -> None:
        cleaned = text.strip()
        if cleaned and cleaned not in variants:
            variants.append(cleaned)

    def visit(parts: tuple[str, ...]) -> None:
        if not parts:
            return
        joined = " ".join(parts)
        add_text(joined)
        quoted = shlex.join(parts)
        if quoted != joined:
            add_text(quoted)

        lowered = tuple(p.lower() for p in parts)
        unwrapped = _strip_command_wrapper(lowered)
        if unwrapped != lowered:
            visit(unwrapped)
            return
        privilege_unwrapped = _strip_privilege_wrapper(lowered)
        if privilege_unwrapped != lowered:
            visit(privilege_unwrapped)
            return
        windows_cmd_script = _windows_cmd_wrapper_script(parts)
        if windows_cmd_script is not None:
            visit(("sh", "-lc", windows_cmd_script))
            return
        if _is_shell_wrapper(lowered):
            for command_parts in _shell_commands(parts):
                visit(command_parts)

    add_text(command)
    visit(("sh", "-lc", command))
    return tuple(variants)


def _is_python_install(lowered: tuple[str, ...]) -> bool:
    return (
        len(lowered) >= 4
        and _PYTHON_EXE_RE.fullmatch(_command_name(lowered[0])) is not None
        and lowered[1:3] == ("-m", "pip")
        and lowered[3] == "install"
    ) or (
        len(lowered) >= 2
        and _command_name(lowered[0]) in {"pip", "pip3"}
        and lowered[1] == "install"
    ) or (
        len(lowered) >= 3
        and _command_name(lowered[0]) == "uv"
        and lowered[1] == "pip"
        and lowered[2] == "install"
    )


def _is_python_package_query(lowered: tuple[str, ...]) -> bool:
    return (
        len(lowered) >= 5
        and _PYTHON_EXE_RE.fullmatch(_command_name(lowered[0])) is not None
        and lowered[1:4] == ("-m", "pip", "index")
    ) or (
        len(lowered) >= 4
        and _command_name(lowered[0]) in {"pip", "pip3"}
        and lowered[1] == "index"
    ) or (
        len(lowered) >= 4
        and _command_name(lowered[0]) == "uv"
        and lowered[1:3] == ("pip", "index")
    )


def _is_python_env_create(lowered: tuple[str, ...]) -> bool:
    if (
        len(lowered) >= 4
        and _PYTHON_EXE_RE.fullmatch(_command_name(lowered[0])) is not None
        and lowered[1:3] == ("-m", "venv")
    ):
        return _env_create_path_from_argv(lowered, lowered, 3) is not None
    if len(lowered) >= 2 and _command_name(lowered[0]) in _PYTHON_ENV_COMMANDS:
        return _env_create_path_from_argv(lowered, lowered, 1) is not None
    if (
        len(lowered) >= 2
        and _command_name(lowered[0]) == "uv"
        and lowered[1] == "venv"
    ):
        return _env_create_path_from_argv(lowered, lowered, 2) is not None
    return False


def _env_create_write_paths(parts: tuple[str, ...]) -> tuple[str, ...]:
    lowered = tuple(part.lower() for part in parts)
    if (
        len(lowered) >= 4
        and _PYTHON_EXE_RE.fullmatch(_command_name(lowered[0])) is not None
        and lowered[1:3] == ("-m", "venv")
    ):
        path = _env_create_path_from_argv(parts, lowered, 3)
        return (path,) if path is not None else ()
    if len(lowered) >= 2 and _command_name(lowered[0]) in _PYTHON_ENV_COMMANDS:
        path = _env_create_path_from_argv(parts, lowered, 1)
        return (path,) if path is not None else ()
    if (
        len(lowered) >= 2
        and _command_name(lowered[0]) == "uv"
        and lowered[1] == "venv"
    ):
        path = _env_create_path_from_argv(parts, lowered, 2)
        return (path,) if path is not None else ()
    return ()


def _env_create_path_from_argv(
    parts: tuple[str, ...], lowered_parts: tuple[str, ...], start: int
) -> str | None:
    if start >= len(parts):
        return None
    ignore_next = False
    for index, part in enumerate(parts[start:], start=start):
        if ignore_next:
            ignore_next = False
            continue
        lowered = lowered_parts[index]
        if lowered in _ENV_CREATE_OPTION_TOKENS:
            return None
        if lowered in _ENV_CREATE_OPTIONS_WITH_VALUE:
            ignore_next = True
            continue
        if lowered.startswith("--prompt="):
            continue
        if part.startswith("-"):
            continue
        return part
    return None


def _is_python_project_install(lowered: tuple[str, ...]) -> bool:
    if not lowered:
        return False
    command = _command_name(lowered[0])
    if command == "poetry":
        return len(lowered) >= 2 and lowered[1] in {"install", "sync"}
    if command == "rye":
        return len(lowered) >= 2 and lowered[1] in {"sync", "install"}
    if command == "pixi":
        return len(lowered) >= 2 and lowered[1] in {"install", "update"}
    return False


def _is_php_package_install(lowered: tuple[str, ...]) -> bool:
    return (
        len(lowered) >= 2
        and _command_name(lowered[0]) == "composer"
        and lowered[1] in {"install", "update", "require"}
    )


def _is_java_package_install(lowered: tuple[str, ...]) -> bool:
    if len(lowered) < 2:
        return False
    command = _command_name(lowered[0])
    if command in {"mvn", "mvnw"}:
        return lowered[1] in {"package", "install", "test", "verify", "dependency:resolve"}
    if command in {"gradle", "gradlew"}:
        return lowered[1] in {"build", "test", "assemble", "dependencies"}
    return False


def _is_node_install(lowered: tuple[str, ...]) -> bool:
    if not lowered or _command_name(lowered[0]) not in {"bun", "npm", "pnpm", "yarn"}:
        return False
    return len(lowered) >= 2 and lowered[1] in _NODE_INSTALL_COMMANDS


def _is_node_package_query(lowered: tuple[str, ...]) -> bool:
    if not lowered or _command_name(lowered[0]) not in {"bun", "npm", "pnpm", "yarn"}:
        return False
    return len(lowered) >= 2 and lowered[1] in _NODE_REGISTRY_QUERY_COMMANDS


def _is_rust_package_install(lowered: tuple[str, ...]) -> bool:
    return (
        len(lowered) >= 2
        and _command_name(lowered[0]) == "cargo"
        and lowered[1] in {"build", "install", "test"}
    )


def _is_go_package_install(lowered: tuple[str, ...]) -> bool:
    if len(lowered) < 2 or _command_name(lowered[0]) != "go":
        return False
    if lowered[1] in {"get", "install"}:
        return True
    return len(lowered) >= 3 and lowered[1] == "mod" and lowered[2] in {
        "download",
        "tidy",
    }


def _command_uses_http_url(lowered: tuple[str, ...]) -> bool:
    if not lowered:
        return False
    command = _command_name(lowered[0])
    if command in _URL_NETWORK_COMMANDS:
        return True
    if command == "git" and len(lowered) >= 2:
        if lowered[1] in _GIT_NETWORK_COMMANDS:
            return True
        return len(lowered) >= 3 and lowered[1] == "submodule" and lowered[2] == "update"
    return False


def _installer_download_host_effect(lowered: tuple[str, ...]) -> str | None:
    if not _command_uses_http_url(lowered):
        return None
    if any(_looks_like_downloaded_installer_artifact(part) for part in lowered[1:]):
        return "software_management"
    return None


def _looks_like_downloaded_installer_artifact(value: str) -> bool:
    cleaned = _strip_outer_quotes(value).rstrip(_TRAILING_URL_PUNCTUATION)
    cleaned = cleaned.split("?", 1)[0].split("#", 1)[0]
    lowered = cleaned.lower()
    if lowered.endswith(_INSTALLER_EXTENSIONS):
        return True
    return lowered.endswith(".exe")


def _domains_from_argv(parts: tuple[str, ...]) -> tuple[str, ...]:
    domains: list[str] = []
    for part in parts:
        for match in _URL_RE.finditer(part):
            domain = normalize_domain(match.group(0).rstrip(_TRAILING_URL_PUNCTUATION))
            if domain and domain not in domains:
                domains.append(domain)
    return tuple(domains)


def _is_destructive(lowered: tuple[str, ...]) -> bool:
    if not lowered:
        return False
    return _command_name(lowered[0]) in _DESTRUCTIVE_COMMANDS


def _is_shell_wrapper(lowered: tuple[str, ...]) -> bool:
    return bool(lowered) and _command_name(lowered[0]) in _SHELL_WRAPPERS


def _shell_script_is_destructive(lowered: tuple[str, ...]) -> bool:
    command_expected = True
    for token in _shell_tokens(_shell_script(lowered)):
        if token in _SHELL_SEPARATORS:
            command_expected = True
            continue
        if command_expected:
            lowered_token = token.lower()
            if _ASSIGNMENT_RE.fullmatch(lowered_token):
                continue
            if _command_name(lowered_token) in _DESTRUCTIVE_COMMANDS:
                return True
            command_expected = False
    return False


def _shell_tokens(script: str) -> tuple[str, ...]:
    script = _strip_outer_quotes(script.strip())
    try:
        lexer = shlex.shlex(
            script,
            posix=_WINDOWS_ABSOLUTE_PATH_IN_SCRIPT_RE.search(script) is None,
            punctuation_chars=";&|",
        )
        lexer.whitespace_split = True
        return tuple(lexer)
    except ValueError:
        return tuple(script.split())


def _classify_shell_script(parts: tuple[str, ...]) -> OperationProfile:
    requested_paths: list[str] = []
    requested_write_paths: list[str] = []
    requested_domains: list[str] = []
    network_profile: OperationProfile | None = None
    host_effect: str | None = None
    for command_parts in _shell_commands(parts):
        profile = classify_command(command_parts)
        if profile.host_effect is not None and host_effect is None:
            host_effect = profile.host_effect
        if profile.needs_network:
            if network_profile is None:
                network_profile = profile
            for domain in profile.requested_domains:
                if domain not in requested_domains:
                    requested_domains.append(domain)
        for path in profile.requested_paths:
            if path not in requested_paths:
                requested_paths.append(path)
        for path in profile.requested_write_paths:
            if path not in requested_write_paths:
                requested_write_paths.append(path)
    if network_profile is not None:
        return OperationProfile(
            network_profile.name,
            needs_network=True,
            package_manager=network_profile.package_manager,
            requested_domains=tuple(requested_domains),
            requested_paths=tuple(requested_paths),
            requested_write_paths=tuple(requested_write_paths),
            high_impact=network_profile.high_impact,
            host_effect=host_effect,
        )
    script = _shell_script(parts)
    script_domains = _domains_from_argv((script,))
    if script_domains and _script_uses_python_network_runtime(script):
        return OperationProfile(
            "url_fetch",
            needs_network=True,
            requested_domains=script_domains,
            host_effect=host_effect,
        )
    if host_effect is not None:
        return OperationProfile(
            "host_effect_shell",
            requested_paths=tuple(requested_paths),
            requested_write_paths=tuple(requested_write_paths),
            host_effect=host_effect,
        )
    if requested_paths or requested_write_paths:
        return OperationProfile(
            "path_transfer" if requested_write_paths else "workspace_read",
            requested_paths=tuple(requested_paths),
            requested_write_paths=tuple(requested_write_paths),
        )
    return OperationProfile("unknown_shell")


def _script_uses_python_network_runtime(script: str) -> bool:
    if re.search(r"(?m)(?:^|[;&|]\s*)python(?:3(?:\.\d+)?)?\b", script) is None:
        return False
    lowered = script.lower()
    return any(
        marker in lowered
        for marker in (
            "urllib.request",
            "requests.",
            "http.client",
            "aiohttp.",
            "httpx.",
        )
    )


def _shell_commands(parts: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    commands: list[tuple[str, ...]] = []
    current: list[str] = []
    for token in _shell_tokens(_shell_script(parts)):
        if token in _SHELL_SEPARATORS:
            cleaned = _strip_assignment_prefix(tuple(current))
            if cleaned:
                commands.append(cleaned)
            current = []
            continue
        current.append(token)
    cleaned = _strip_assignment_prefix(tuple(current))
    if cleaned:
        commands.append(cleaned)
    return tuple(commands)


def _strip_assignment_prefix(parts: tuple[str, ...]) -> tuple[str, ...]:
    index = 0
    while index < len(parts):
        token = _strip_outer_quotes(parts[index]).lower()
        if token == "&" or _ASSIGNMENT_RE.fullmatch(token):
            index += 1
            continue
        break
    return parts[index:]


def _windows_shell_host_command(parts: tuple[str, ...]) -> str | None:
    if len(parts) < 5:
        return None
    if parts[1].lower() != "-c":
        return None
    if "windows sandbox shell host expects powershell path and command" not in parts[2].lower():
        return None
    command = parts[4].strip()
    return command or None


def _windows_invoke_python_process_parts(parts: tuple[str, ...]) -> tuple[str, ...] | None:
    if not parts or _command_name(parts[0]).lower() != "invoke-opensquillapythonprocess":
        return None
    executable: str | None = None
    arguments: tuple[str, ...] | None = None
    index = 1
    while index < len(parts):
        flag = parts[index].lower()
        if flag == "-filepath" and index + 1 < len(parts):
            executable = _unescape_powershell_single_quoted(parts[index + 1])
            index += 2
            continue
        if flag == "-arguments" and index + 1 < len(parts):
            arguments = _powershell_single_quoted_array(parts[index + 1])
            index += 2
            continue
        index += 1
    if not executable or arguments is None:
        return None
    return (executable, *arguments)


def _unescape_powershell_single_quoted(value: str) -> str:
    return _strip_outer_quotes(value).replace("''", "'")


def _powershell_single_quoted_array(value: str) -> tuple[str, ...] | None:
    value = value.strip()
    if not value.startswith("@(") or not value.endswith(")"):
        return None
    body = value[2:-1]
    values: list[str] = []
    index = 0
    length = len(body)
    while index < length:
        while index < length and body[index].isspace():
            index += 1
        if index < length and body[index] == ",":
            index += 1
            continue
        while index < length and body[index].isspace():
            index += 1
        if index >= length:
            break
        if body[index] != "'":
            return None
        index += 1
        chars: list[str] = []
        while index < length:
            char = body[index]
            if char != "'":
                chars.append(char)
                index += 1
                continue
            if index + 1 < length and body[index + 1] == "'":
                chars.append("'")
                index += 2
                continue
            index += 1
            break
        else:
            return None
        while index < length and body[index].isspace():
            index += 1
        if index < length and body[index] not in ",":
            return None
        values.append("".join(chars))
    return tuple(values)


def _strip_outer_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _strip_privilege_wrapper(lowered: tuple[str, ...]) -> tuple[str, ...]:
    if not lowered or _command_name(lowered[0]) not in _PRIVILEGE_WRAPPERS:
        return lowered
    index = 1
    while index < len(lowered):
        token = lowered[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-"):
            break
        index += 1
        if token in {"-g", "--group", "-h", "--host", "-p", "--prompt", "-u", "--user"}:
            index += 1
    return lowered[index:] if index < len(lowered) else lowered


def _windows_cmd_wrapper_script(parts: tuple[str, ...]) -> str | None:
    if not parts or _command_name(parts[0]).lower() != "cmd":
        return None
    for index, part in enumerate(parts[1:], start=1):
        lowered = part.lower()
        if lowered in {"/c", "/k"}:
            script = " ".join(parts[index + 1 :]).strip()
            return _strip_outer_quotes(script) or None
        if not lowered.startswith("/"):
            return None
    return None


def _strip_command_wrapper(lowered: tuple[str, ...]) -> tuple[str, ...]:
    if not lowered or _command_name(lowered[0]) != "timeout":
        return lowered

    index = 1
    while index < len(lowered):
        token = lowered[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-"):
            break
        index += 1
        if token in {"-k", "--kill-after", "-s", "--signal"} and index < len(lowered):
            index += 1

    if index >= len(lowered):
        return lowered
    # GNU timeout syntax is: timeout [OPTION] DURATION COMMAND [ARG]...
    index += 1
    if index >= len(lowered):
        return lowered
    return lowered[index:]


def _host_effect(lowered: tuple[str, ...]) -> str | None:
    if not lowered:
        return None
    command = _command_name(lowered[0])
    if _invokes_windows_uninstall(lowered):
        return "software_management"
    if _is_host_probe(lowered):
        return "host_probe"
    if (
        command in _HOST_SOFTWARE_MANAGERS
        or command in _SOFTWARE_MANAGEMENT_COMMANDS
        or _uses_installer_artifact(lowered)
        or _opens_software_management_ui(lowered)
        or _touches_installed_software_path(lowered)
    ):
        return "software_management"
    if command in _PROCESS_MANAGEMENT_COMMANDS:
        return "process_management"
    if command in _SERVICE_MANAGERS:
        return "service_management"
    if _is_registry_write(lowered):
        return "registry_write"
    if command in _SYSTEM_SETTING_COMMANDS:
        return "system_settings"
    if command in _DRIVER_FEATURE_COMMANDS:
        return "driver_management"
    if _is_global_tool_install(lowered):
        return "global_tool_install"
    return None


def _is_host_probe(lowered: tuple[str, ...]) -> bool:
    command = _command_name(lowered[0])
    if command in {"get-process", "tasklist"}:
        return True
    if command in {"get-wmiobject", "get-ciminstance"}:
        return _is_windows_installed_app_query(lowered)
    if command in {"get-package", "get-appxpackage"}:
        return True
    if command in {"get-item", "get-itemproperty", "get-childitem", "gp", "gci"}:
        return _contains_registry_path(lowered)
    if command == "get-command":
        return True
    if command not in _HOST_PROBE_COMMANDS and not (
        command == "reg" and len(lowered) >= 2 and lowered[1] == "query"
    ):
        return False
    body = " ".join(lowered[1:])
    if any(manager in lowered[1:] for manager in _HOST_SOFTWARE_MANAGERS):
        return True
    return any(
        marker in body
        for marker in (
            "program files",
            "windowsapps",
            "hklm\\",
            "hkcu\\",
            "hkcr\\",
            "hku\\",
            "hkcc\\",
            "hkey_local_machine",
            "hkey_current_user",
            "hkey_classes_root",
            "hkey_users",
            "hkey_current_config",
            "win32_product",
            "installedwin32program",
            "uninstall",
        )
    )


def _is_windows_installed_app_query(lowered: tuple[str, ...]) -> bool:
    body = " ".join(lowered[1:])
    return any(
        marker in body
        for marker in (
            "win32_product",
            "installedwin32program",
            "win32reg_addremoveprograms",
            "cim_installedwin32program",
        )
    )


def _contains_registry_path(lowered: tuple[str, ...]) -> bool:
    body = " ".join(lowered[1:])
    return any(
        marker in body
        for marker in (
            "hklm:",
            "hkcu:",
            "hkcr:",
            "hku:",
            "hkcc:",
            "hklm\\",
            "hkcu\\",
            "hkcr\\",
            "hku\\",
            "hkcc\\",
            "registry::",
            "hkey_local_machine",
            "hkey_current_user",
            "hkey_classes_root",
            "hkey_users",
            "hkey_current_config",
        )
    )


def _is_registry_write(lowered: tuple[str, ...]) -> bool:
    command = _command_name(lowered[0])
    if command in {
        "clear-item",
        "new-item",
        "new-itemproperty",
        "remove-item",
        "remove-itemproperty",
        "set-item",
        "set-itemproperty",
    }:
        return _contains_registry_path(lowered)
    return command == "reg" and len(lowered) >= 2 and lowered[1] in {
        "add",
        "delete",
        "import",
        "load",
        "restore",
        "save",
        "unload",
    }


def _invokes_windows_uninstall(lowered: tuple[str, ...]) -> bool:
    body = " ".join(lowered)
    if ".uninstall(" in body or ".uninstall()" in body:
        markers = ("win32_product", "get-wmiobject", "get-ciminstance")
        return any(marker in body for marker in markers)
    return False


def _is_global_tool_install(lowered: tuple[str, ...]) -> bool:
    command = _command_name(lowered[0])
    if command in {"bun", "npm", "pnpm", "yarn"} and _is_node_install(lowered):
        return _has_global_flag(lowered)
    if _is_python_install(lowered):
        return "--user" in lowered or any(part.startswith("--user=") for part in lowered)
    if command == "pipx" and len(lowered) >= 2 and lowered[1] == "install":
        return True
    if command == "gem" and len(lowered) >= 2 and lowered[1] == "install":
        return True
    if command == "cargo" and len(lowered) >= 2 and lowered[1] == "install":
        return True
    if command == "go" and len(lowered) >= 2 and lowered[1] == "install":
        return True
    return (
        command == "dotnet"
        and len(lowered) >= 4
        and lowered[1:3] == ("tool", "install")
        and _has_global_flag(lowered)
    )


def _has_global_flag(lowered: tuple[str, ...]) -> bool:
    return any(part in {"-g", "--global"} or part.startswith("--global=") for part in lowered)


def _uses_installer_artifact(lowered: tuple[str, ...]) -> bool:
    command = _command_name(lowered[0])
    if command in {"open", "start-process"}:
        return any(_looks_like_installer_artifact(part) for part in lowered[1:])
    return _looks_like_installer_artifact(lowered[0]) or _contains_windows_installer_path_fragment(
        lowered
    )


def _contains_windows_installer_path_fragment(lowered: tuple[str, ...]) -> bool:
    if not lowered or not any(separator in lowered[0] for separator in (":", "\\")):
        return False
    first_basename = _strip_outer_quotes(lowered[0]).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if first_basename.endswith((".exe", ".cmd", ".bat", ".ps1")):
        return False
    return any(_looks_like_installer_artifact(part) for part in lowered[1:])


def _opens_software_management_ui(lowered: tuple[str, ...]) -> bool:
    command = _command_name(lowered[0])
    if command == "appwiz.cpl":
        return True
    if command == "control":
        return any(_strip_outer_quotes(part).lower() == "appwiz.cpl" for part in lowered[1:])
    if command in {"open", "start-process", "start"}:
        return any(_strip_outer_quotes(part).lower() == "appwiz.cpl" for part in lowered[1:])
    return False


def _touches_installed_software_path(lowered: tuple[str, ...]) -> bool:
    command = _command_name(lowered[0])
    if command not in {"del", "erase", "remove-item", "rm", "rmdir"}:
        return False
    body = " ".join(lowered[1:])
    return any(
        marker in body
        for marker in (
            "program files",
            "\\appdata\\local\\programs\\",
            "/applications/",
            "/library/application support/",
            "/opt/",
            "/usr/local/",
        )
    )


def _looks_like_installer_artifact(value: str) -> bool:
    cleaned = _strip_outer_quotes(value).rstrip(".,;")
    lowered = cleaned.lower()
    if lowered.endswith(_INSTALLER_EXTENSIONS):
        return True
    if not lowered.endswith(".exe"):
        return False
    basename = lowered.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return _INSTALLER_EXE_RE.search(basename) is not None


def _shell_script(parts: tuple[str, ...]) -> str:
    for index, part in enumerate(parts[1:], start=1):
        if _is_shell_command_option(part.lower()):
            return " ".join(parts[index + 1 :])
    return " ".join(parts[1:])


def _is_shell_command_option(part: str) -> bool:
    if part == "-c":
        return True
    if not part.startswith("-") or part.startswith("--"):
        return False
    return "c" in part[1:]


def _command_name(value: str) -> str:
    value = _strip_outer_quotes(value)
    name = value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    lowered = name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _path_args_from_argv(parts: tuple[str, ...]) -> tuple[str, ...]:
    paths: list[str] = []
    for part in parts[1:]:
        cleaned = part.strip("'\"")
        if cleaned.startswith("-"):
            continue
        if not _looks_like_path_arg(cleaned):
            continue
        if cleaned not in paths:
            paths.append(cleaned)
    return tuple(paths)


def _copy_path_args_from_argv(parts: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    path_args = _path_args_from_argv(parts)
    if len(path_args) < 2:
        return path_args, ()
    return path_args[:-1], path_args[-1:]


def _looks_like_path_arg(part: str) -> bool:
    if not part or part == "-":
        return False
    lowered = part.lower()
    if lowered.startswith(("http://", "https://")):
        return False
    return (
        part.startswith(("/", "~/", "~\\", "../", "..\\", "./", ".\\"))
        or _WINDOWS_ABSOLUTE_PATH_RE.match(part) is not None
    )


__all__ = [
    "OperationProfile",
    "classify_command",
    "package_bundle_for_manager",
    "shell_command_approval_variants",
]
