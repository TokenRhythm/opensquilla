"""Workspace-local cache planning for Windows sandboxed commands."""

from __future__ import annotations

from pathlib import Path

from opensquilla.sandbox.backend.windows_default_roots import workspace_cache_root

CACHE_ENV_PATHS: dict[str, tuple[str, ...]] = {
    "TEMP": ("temp",),
    "TMP": ("temp",),
    "HOME": ("home",),
    "USERPROFILE": ("home",),
    "APPDATA": ("home", "AppData", "Roaming"),
    "LOCALAPPDATA": ("home", "AppData", "Local"),
    "XDG_CONFIG_HOME": ("home", ".config"),
    "PIP_CACHE_DIR": ("pip",),
    "UV_CACHE_DIR": ("uv",),
    "npm_config_cache": ("npm-cache",),
    "npm_config_prefix": ("npm", "prefix"),
    "PNPM_HOME": ("pnpm", "home"),
    "PNPM_STORE_DIR": ("pnpm", "store"),
    "YARN_CACHE_FOLDER": ("yarn",),
    "CARGO_HOME": ("cargo",),
    "RUSTUP_HOME": ("rustup",),
    "GOMODCACHE": ("go", "pkg", "mod"),
    "GOCACHE": ("go", "build"),
    "MAVEN_USER_HOME": ("maven",),
    "GRADLE_USER_HOME": ("gradle",),
    "NUGET_PACKAGES": ("nuget",),
    "DOTNET_CLI_HOME": ("dotnet",),
    "COMPOSER_CACHE_DIR": ("composer",),
    "GEM_HOME": ("ruby", "gems"),
    "GEM_SPEC_CACHE": ("ruby", "specs"),
}

_EXTRA_CACHE_DIRS: tuple[tuple[str, ...], ...] = (("git",),)


def planned_cache_dirs(workspace: Path) -> tuple[Path, ...]:
    root = workspace_cache_root(workspace)
    return tuple(
        dict.fromkeys(
            root.joinpath(*parts)
            for parts in (*CACHE_ENV_PATHS.values(), *_EXTRA_CACHE_DIRS)
        )
    )


def ensure_cache_dirs(workspace: Path) -> tuple[Path, ...]:
    dirs = planned_cache_dirs(workspace)
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
    return dirs


def build_cache_env(
    workspace: Path,
    *,
    base_env: dict[str, str],
    override_user: bool = True,
) -> dict[str, str]:
    root = workspace_cache_root(workspace)
    home = root / "home"
    env = dict(base_env)
    for key, parts in CACHE_ENV_PATHS.items():
        if not override_user and key in env:
            continue
        env[key] = str(root.joinpath(*parts))
    env["HOMEDRIVE"] = home.drive
    env["HOMEPATH"] = str(home)[len(home.drive) :] if home.drive else str(home)
    env["GIT_CONFIG_GLOBAL"] = str(root / "git" / "config")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


__all__ = [
    "CACHE_ENV_PATHS",
    "build_cache_env",
    "ensure_cache_dirs",
    "planned_cache_dirs",
]
