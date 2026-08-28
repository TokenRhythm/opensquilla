"""OpenClaw to OpenSquilla migration.

This module implements OpenSquilla-native migration behavior. It intentionally
does not copy upstream migration implementations; it maps known OpenClaw files
and config shapes into OpenSquilla's own config, workspace, skills, and env
surfaces.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

import yaml

from opensquilla.gateway.config import (
    ChannelsConfig,
    ContextOverflowPolicy,
    GatewayConfig,
    MCPServerEntry,
)
from opensquilla.gateway.config_migration import migrate_config_payload
from opensquilla.migration.env_file import merge_env_lines, write_secret_env_file
from opensquilla.onboarding.config_store import (
    load_config,
    persist_config,
    resolve_config_path,
)
from opensquilla.paths import default_opensquilla_home

SKILL_IMPORT_DIRNAME = "openclaw-imports"
SECRET_REDACTION = "[redacted]"
SKILL_CONFLICT_MODES = {"skip", "overwrite", "rename"}
# How to resolve a conflict on persona files (SOUL.md / USER.md / AGENTS.md)
# when the destination already holds real user-curated content (i.e. it is
# not the pristine OpenSquilla bootstrap template and ``--overwrite`` was
# not passed). MEMORY.md is handled separately because memory is additive
# and merges automatically. Persona files are identity definitions, so a
# silent default would either drop OpenClaw content (the original bug) or
# clobber the user's curated persona (also bad).
PERSONA_CONFLICT_MODES = {
    "prompt",
    "use-opensquilla",
    "use-openclaw",
    "merge",
    "skip",
}
_PERSONA_KIND_BY_FILENAME = {
    "SOUL.md": "soul",
    "USER.md": "user-profile",
    "AGENTS.md": "workspace-agents",
}
MAX_SKILL_FILE_BYTES = 256_000
MAX_MEMORY_CHARS = 80_000
MEMORY_OVERFLOW_DIR = "memory-overflow"

USER_DATA_OPTIONS = {
    "soul",
    "workspace-agents",
    "memory",
    "user-profile",
    "daily-memory",
    "skills",
    "shared-skills",
    "tts-assets",
}

RUNTIME_CONFIG_OPTIONS = {
    "command-allowlist",
    "model-config",
    "mcp-servers",
    "agent-config",
    "tools-config",
    "tts-config",
    "telegram-settings",
    "discord-settings",
    "slack-settings",
    "provider-keys",
    "archive",
    "plugins-config",
    "cron-jobs",
    "hooks-config",
    "gateway-config",
    "session-config",
    "browser-config",
    "approvals-config",
    "memory-backend",
    "skills-config",
    "ui-identity",
    "logging-config",
}

MIGRATION_OPTIONS = USER_DATA_OPTIONS | RUNTIME_CONFIG_OPTIONS

MIGRATION_PRESETS = {
    "user-data": USER_DATA_OPTIONS,
    "full": MIGRATION_OPTIONS,
}

SECRET_ENV_KEYS = {
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "ZAI_API_KEY",
    "MINIMAX_API_KEY",
    "BRAVE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
}

PROVIDER_ENV_KEYS = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "zai": "ZAI_API_KEY",
    "zhipu": "ZAI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
}


def _normalize_provider_id(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized == "zai":
        return "zhipu"
    return normalized


ARCHIVE_CONFIG_KEYS = {
    "plugins": "plugins-config.json",
    "cron": "cron-config.json",
    "hooks": "hooks-config.json",
    "gateway": "gateway-config.json",
    "session": "session-config.json",
    "browser": "browser-config.json",
    "approvals": "approvals-config.json",
    "memory": "memory-backend-config.json",
    "skills": "skills-registry-config.json",
    "ui": "ui-identity-config.json",
    "logging": "logging-config.json",
    "diagnostics": "logging-config.json",
}

ARCHIVE_OPTION_TO_CONFIG_KEY = {
    "plugins-config": "plugins",
    "cron-jobs": "cron",
    "hooks-config": "hooks",
    "gateway-config": "gateway",
    "session-config": "session",
    "browser-config": "browser",
    "approvals-config": "approvals",
    "memory-backend": "memory",
    "skills-config": "skills",
    "ui-identity": "ui",
    "logging-config": "logging",
}

ARCHIVE_KIND_BY_CONFIG_KEY = {
    "plugins": "plugins-config",
    "cron": "cron-jobs",
    "hooks": "hooks-config",
    "gateway": "gateway-config",
    "session": "session-config",
    "browser": "browser-config",
    "approvals": "approvals-config",
    "memory": "memory-backend",
    "skills": "skills-config",
    "ui": "ui-identity",
    "logging": "logging-config",
    "diagnostics": "logging-config",
}

ARCHIVE_WORKSPACE_FILES = (
    "IDENTITY.md",
    "TOOLS.md",
    "HEARTBEAT.md",
    "BOOTSTRAP.md",
)
ARCHIVE_WORKSPACE_DIRS = ("hooks",)
ARCHIVE_SOURCE_ARTIFACTS = (
    "extensions",
    "cron",
    "hooks",
    "webhooks",
    "bindings",
)
SKIP_SOURCE_ARTIFACTS = (
    ("credentials", "credentials"),
    ("devices", "devices"),
    ("identity", "identity"),
    ("workspace.zip", "workspace.zip"),
    ("memory/main.sqlite", "memory/main.sqlite"),
    ("auth-profiles", "agents/main/agent/auth-profiles.json"),
)
RAW_CONFIG_FILENAMES = ("openclaw.json", "clawdbot.json", "moltbot.json")

_OPENCLAW_WORKSPACE_MARKERS = ("SOUL.md", "MEMORY.md", "USER.md", "AGENTS.md", "IDENTITY.md")


def _is_valid_openclaw_home(path: Path) -> bool:
    """Return True if `path` plausibly holds an OpenClaw home.

    Mirrors hermes._is_valid_hermes_home: we accept anything that has at
    least one OpenClaw config file at the root or a workspace directory
    with persona markers inside. Used by the auto-detect entry point to
    decide whether a default ``~/.openclaw`` is worth offering.
    """
    if not path.is_dir():
        return False
    for name in RAW_CONFIG_FILENAMES:
        if (path / name).is_file():
            return True
    for candidate in (path / "workspace", *path.glob("workspace-*")):
        if candidate.is_dir() and any(
            (candidate / marker).is_file() for marker in _OPENCLAW_WORKSPACE_MARKERS
        ):
            return True
    return False


@dataclass(frozen=True)
class MigrationOptions:
    source: Path | str = field(default_factory=lambda: Path.home() / ".openclaw")
    config_path: Path | str | None = None
    apply: bool = False
    migrate_secrets: bool = False
    overwrite: bool = False
    preset: str = "full"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    skill_conflict: Literal["skip", "overwrite", "rename"] = "skip"
    persona_conflict: Literal[
        "prompt", "use-opensquilla", "use-openclaw", "merge", "skip"
    ] = "prompt"


@dataclass
class ItemResult:
    kind: str
    source: str | None
    destination: str | None
    status: str
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _BeforeImage:
    target: Path
    backup: Path | None
    kind: Literal["missing", "file", "directory", "symlink"]


class _ApplyRollback:
    """Restore paths changed by one apply run when a Python exception escapes."""

    def __init__(self) -> None:
        self._backup_root = Path(tempfile.mkdtemp(prefix="opensquilla-openclaw-rollback-"))
        self._images: dict[Path, _BeforeImage] = {}
        self._missing_parents: set[Path] = set()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exc_type, traceback
        rollback_error: OSError | None = None
        if exc is not None:
            try:
                self.rollback()
            except OSError as caught:
                rollback_error = caught
        if rollback_error is not None:
            raise OSError(
                f"{exc}; rollback failed: {rollback_error}; "
                f"before-image backup retained at {self._backup_root}"
            ) from exc
        shutil.rmtree(self._backup_root, ignore_errors=True)
        return False

    @staticmethod
    def _absolute(path: Path) -> Path:
        expanded = path.expanduser()
        if expanded.is_absolute():
            return expanded
        return Path.cwd() / expanded

    @staticmethod
    def _lexists(path: Path) -> bool:
        return os.path.lexists(path)

    def capture(self, path: Path) -> None:
        target = self._absolute(path)
        if target in self._images:
            return
        for parent in self._images:
            if target != parent and target.is_relative_to(parent):
                return

        if not self._lexists(target):
            self._images[target] = _BeforeImage(target, None, "missing")
            parent = target.parent
            while parent != parent.parent and not self._lexists(parent):
                self._missing_parents.add(parent)
                parent = parent.parent
            return

        backup = self._backup_root / str(len(self._images))
        if target.is_symlink():
            backup.symlink_to(os.readlink(target), target_is_directory=target.is_dir())
            kind: Literal["file", "directory", "symlink"] = "symlink"
        elif target.is_dir():
            shutil.copytree(target, backup, symlinks=True)
            kind = "directory"
        elif target.is_file():
            shutil.copy2(target, backup, follow_symlinks=False)
            kind = "file"
        else:
            raise OSError(f"unsupported migration target type: {target}")
        self._images[target] = _BeforeImage(target, backup, kind)

    def register_created(self, path: Path) -> None:
        target = self._absolute(path)
        if target in self._images:
            return
        for parent in self._images:
            if target != parent and target.is_relative_to(parent):
                return
        self._images[target] = _BeforeImage(target, None, "missing")

    @classmethod
    def _remove(cls, path: Path) -> None:
        if not cls._lexists(path):
            return
        if path.is_symlink() or not path.is_dir():
            path.unlink()
        else:
            shutil.rmtree(path)

    @classmethod
    def _restore(cls, image: _BeforeImage) -> None:
        cls._remove(image.target)
        if image.kind == "missing":
            return
        assert image.backup is not None
        image.target.parent.mkdir(parents=True, exist_ok=True)
        if image.kind == "directory":
            shutil.copytree(image.backup, image.target, symlinks=True)
        elif image.kind == "symlink":
            image.target.symlink_to(
                os.readlink(image.backup),
                target_is_directory=image.backup.is_dir(),
            )
        else:
            shutil.copy2(image.backup, image.target, follow_symlinks=False)

    def rollback(self) -> None:
        errors: list[str] = []
        for image in reversed(tuple(self._images.values())):
            try:
                self._restore(image)
            except OSError as exc:
                errors.append(f"restore {image.target}: {exc}")
        for parent in sorted(self._missing_parents, key=lambda path: len(path.parts), reverse=True):
            try:
                parent.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                # A parent may contain pre-existing or concurrently-created data.
                pass
        if errors:
            raise OSError("; ".join(errors))


def _as_path(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _get_nested(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, value = line.split("=", 1)
        values[key.strip().lstrip("\ufeff")] = value.strip().strip('"').strip("'")
    return values


def _is_sensitive_key(key: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(
        marker in compact
        for marker in (
            "apikey",
            "token",
            "secret",
            "password",
            "authorization",
        )
    )


def _redact_value(value: Any, key: str | None = None) -> Any:
    if key and _is_sensitive_key(key) and value not in (None, "", [], {}):
        return SECRET_REDACTION
    if isinstance(value, dict):
        return {k: _redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, str):
        if re.search(r"(api[_-]?key|token|secret|password)", value, re.IGNORECASE):
            return SECRET_REDACTION
        if value.startswith(("sk-", "xox", "ghp_", "github_pat_")):
            return SECRET_REDACTION
    return value


def _provider_from_model(model: str) -> str | None:
    normalized = model.strip().lower()
    if normalized.startswith("openrouter/"):
        return "openrouter"
    if normalized.startswith("deepseek"):
        return "deepseek"
    if normalized.startswith("gpt-") or normalized.startswith("openai/"):
        return "openai"
    if normalized.startswith("claude") or normalized.startswith("anthropic/"):
        return "anthropic"
    if normalized.startswith("gemini"):
        return "gemini"
    if normalized.startswith("zai/") or normalized.startswith("glm-"):
        return "zhipu"
    if normalized.startswith("minimax") or normalized.startswith("minimax/"):
        return "minimax"
    return None


def _model_for_opensquilla_provider(model: str, provider: str | None) -> tuple[str, dict[str, Any]]:
    """Convert OpenClaw provider-prefixed model ids to provider-native ids."""
    if provider == "openrouter" and model.lower().startswith("openrouter/"):
        native = model.split("/", 1)[1].strip()
        if native:
            return native, {"source_model": model, "normalized_provider_prefix": "openrouter"}
    if provider == "zhipu" and model.lower().startswith("zai/"):
        native = model.split("/", 1)[1].strip()
        if native:
            return native, {"source_model": model, "normalized_provider_prefix": "zai"}
    return model, {}


def _env_key_for_provider(provider: str) -> str | None:
    return PROVIDER_ENV_KEYS.get(_normalize_provider_id(provider))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _opensquilla_bootstrap_template_text(filename: str) -> str | None:
    # Read the canonical OpenSquilla bootstrap template shipped under
    # ``opensquilla.identity.templates.bootstrap`` so we can detect when a
    # destination file is still the pristine placeholder seeded by
    # ``ensure_agent_workspace`` and treat it as overwrite-safe.
    try:
        from importlib.resources import files as _resource_files

        resource = (
            _resource_files("opensquilla.identity")
            .joinpath("templates")
            .joinpath("bootstrap")
            .joinpath(filename)
        )
        return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None


def _dest_is_pristine_bootstrap_template(destination: Path, filename: str) -> bool:
    if not destination.is_file():
        return False
    template = _opensquilla_bootstrap_template_text(filename)
    if template is None:
        return False
    try:
        existing = destination.read_text(encoding="utf-8-sig")
    except OSError:
        return False
    # Compare after normalizing trailing whitespace so a stray EOL difference
    # (e.g. a one-off platform artifact) does not disqualify a pristine file.
    return existing.rstrip() == template.rstrip()


REBRAND_SKIP_REASON_MIXED = "mentions-opensquilla"
_OPENSQUILLA_MENTION_RE = re.compile(r"opensquilla", re.IGNORECASE)


def _rebrand_skip_reason(text: str) -> str | None:
    """Return a reason string when ``text`` should NOT be mechanically rebranded.

    Mirrors hermes._rebrand_skip_reason: when the source prose talks about
    both OpenClaw and OpenSquilla as distinct entities (a "what is
    installed where" note, for instance), mechanical replacement collapses
    the two subjects and produces tautologies / factual errors. Skip the
    rebrand in that case and let the user reword by hand.
    """
    if _OPENSQUILLA_MENTION_RE.search(text):
        return REBRAND_SKIP_REASON_MIXED
    return None


def _rebrand_text(text: str) -> tuple[str, bool]:
    # Mixed-subject prose: keep verbatim, let callers record the skip.
    if _rebrand_skip_reason(text) is not None:
        return text, False
    protected: dict[str, str] = {}

    def protect(match: re.Match[str]) -> str:
        key = f"__OPENSQUILLA_OPENCLAW_REF_{len(protected)}__"
        protected[key] = match.group(0)
        return key

    source_reference_patterns = (
        r"\bOPENCLAW_[A-Z0-9_]+\b",
        r"\bopenclaw\.json\b",
        r"\bclawdbot\.json\b",
        r"\bmoltbot\.json\b",
        (
            r"\bOpenClaw(?=\s*(?:CLI|Gateway|gateway|API|source|runtime|state|"
            r"config|configuration|workspace|home|directory|file|files|skill|"
            r"skills|TTS|cron|hooks|plugins|memory|branding|来源|源|运行态|"
            r"配置|工作区|目录|文件|状态|技能|品牌|的\s*branding))"
        ),
    )
    migrated = text
    for pattern in source_reference_patterns:
        migrated = re.sub(pattern, protect, migrated)

    # Use word-boundary aware regex replacements so prefix-substring
    # matches like ``.openclawrc``, ``OpenClawFlavored``, or
    # ``openclaw_pid`` are not mangled into nonsense. The path-style
    # ``.openclaw``/``.OpenClaw`` patterns additionally require a path
    # terminator (``/``, whitespace, quote, end-of-string) so paths like
    # ``.openclawd/run.sock`` stay intact.
    path_terminator = r"(?=[/\s'\"`)\],;:]|$)"
    regex_replacements = (
        (rf"\.openclaw{path_terminator}", ".opensquilla"),
        (rf"\.OpenClaw{path_terminator}", ".OpenSquilla"),
        (r"\bOpenClaw\b", "OpenSquilla"),
        (r"\bopenclaw\b", "opensquilla"),
        (r"\bClawdBot\b", "OpenSquilla"),
        (r"\bclawdbot\b", "opensquilla"),
        (r"\bMoltBot\b", "OpenSquilla"),
        (r"\bmoltbot\b", "opensquilla"),
    )
    for pattern, replacement in regex_replacements:
        migrated = re.sub(pattern, replacement, migrated)
    for key, value in protected.items():
        migrated = migrated.replace(key, value)
    return migrated, migrated != text


class OpenClawMigrator:
    """Migrate an OpenClaw home into OpenSquilla-native state."""

    def __init__(self, options: MigrationOptions) -> None:
        self.options = options
        self.source = _as_path(options.source) or Path.home() / ".openclaw"
        self.config_path = _as_path(options.config_path)
        self.home = default_opensquilla_home()
        self.timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        self.output_dir = self.home / "migration" / "openclaw" / self.timestamp
        self.items: list[ItemResult] = []
        self._config: GatewayConfig | None = None
        self._config_migration_pending = False
        self._config_paths_normalized = False
        self._config_changed = False
        self._env_additions: dict[str, str] = {}
        self._notes: list[str] = []
        self._apply_rollback: _ApplyRollback | None = None

    def migrate(self) -> dict[str, Any]:
        validation_error = self._validation_error()
        if validation_error:
            self._record("options", None, None, "error", validation_error)
            return self._report()

        if not self.source.is_dir():
            self._record(
                "source",
                self.source,
                None,
                "error",
                "OpenClaw source directory does not exist",
            )
            return self._report()

        selected = self._selected_options()
        config = self._load_openclaw_config()

        if self.options.apply:
            with _ApplyRollback() as rollback:
                self._apply_rollback = rollback
                try:
                    self._prepare_apply_rollback()
                    self._migrate_selected(selected, config)
                finally:
                    self._apply_rollback = None
        else:
            self._migrate_selected(selected, config)

        return self._report()

    def _migrate_selected(self, selected: set[str], config: dict[str, Any]) -> None:
        if "soul" in selected:
            self._migrate_workspace_file("SOUL.md", "soul")
        if "workspace-agents" in selected:
            self._migrate_workspace_file("AGENTS.md", "workspace-agents")
        if "user-profile" in selected:
            self._migrate_workspace_file("USER.md", "user-profile")
        if "memory" in selected or "daily-memory" in selected:
            self._migrate_memory()
        if "skills" in selected or "shared-skills" in selected:
            self._migrate_skills()
        if "tts-assets" in selected:
            self._migrate_tts_assets()
        if "command-allowlist" in selected:
            self._migrate_command_allowlist()
        if "model-config" in selected:
            self._migrate_model_config(config)
        if "mcp-servers" in selected:
            self._migrate_mcp_servers(config)
        if "agent-config" in selected:
            self._migrate_agent_config(config)
        if "tools-config" in selected:
            self._migrate_tools_config(config)
        if "provider-keys" in selected:
            self._migrate_provider_keys(config)
        if selected & {"telegram-settings", "discord-settings", "slack-settings"}:
            self._migrate_supported_channels(config, selected)
        if "tts-config" in selected:
            self._archive_tts_config(config)
        archive_keys = self._selected_archive_config_keys(selected)
        if archive_keys:
            self._archive_unmapped_config(config, archive_keys)
        if "archive" in selected:
            self._archive_openclaw_artifacts()

        if self.options.apply:
            self._flush_env()
            self._write_report_files()
            # Config persistence is deliberately last. persist_config merges
            # against the latest on-disk state under its shared write lock;
            # once it commits, no later fallible migration step may trigger
            # the path rollback and overwrite a concurrent settings save.
            self._flush_config()

    def _prepare_apply_rollback(self) -> None:
        assert self._apply_rollback is not None
        self._capture_apply_target(self.output_dir)

    def _capture_apply_target(self, path: Path) -> None:
        if self._apply_rollback is None:
            return
        target = path.resolve() if path.is_symlink() else path
        self._apply_rollback.capture(target)

    def _capture_apply_replace_target(self, path: Path) -> None:
        if self._apply_rollback is None:
            return
        if path.is_symlink():
            self._apply_rollback.capture(path)
        self._capture_apply_target(path)

    def _validation_error(self) -> str:
        if self.options.preset not in MIGRATION_PRESETS:
            return f"Unknown migration preset: {self.options.preset}"
        unknown_include = sorted(set(self.options.include) - MIGRATION_OPTIONS)
        if unknown_include:
            return f"Unknown migration option in include: {', '.join(unknown_include)}"
        unknown_exclude = sorted(set(self.options.exclude) - MIGRATION_OPTIONS)
        if unknown_exclude:
            return f"Unknown migration option in exclude: {', '.join(unknown_exclude)}"
        if self.options.skill_conflict not in SKILL_CONFLICT_MODES:
            return f"Unknown skill conflict behavior: {self.options.skill_conflict}"
        return ""

    def _selected_options(self) -> set[str]:
        selected = set(MIGRATION_PRESETS[self.options.preset])
        if self.options.include:
            selected |= set(self.options.include)
        if self.options.exclude:
            selected -= set(self.options.exclude)
        return selected & MIGRATION_OPTIONS

    def _selected_archive_config_keys(self, selected: set[str]) -> set[str]:
        if "archive" in selected:
            return set(ARCHIVE_CONFIG_KEYS)
        return {
            config_key
            for option, config_key in ARCHIVE_OPTION_TO_CONFIG_KEY.items()
            if option in selected
        }

    def _config_obj(self) -> GatewayConfig:
        if self._config is None:
            self._config_migration_pending = (
                self.options.apply and self._target_config_needs_migration()
            )
            # Loading must not rewrite the target config. Any schema rewrite
            # is deferred to the final persist so a later migration failure
            # never needs to restore a stale, pre-concurrency before-image.
            self._config = load_config(self.config_path, persist_migrations=False)
        if not self._config_paths_normalized:
            self._normalize_default_config_paths(self._config)
            self._config_paths_normalized = True
        return self._config

    def _target_config_needs_migration(self) -> bool:
        target, _source = resolve_config_path(self.config_path)
        if not target.is_file():
            return False
        try:
            with target.open("rb") as fh:
                payload = tomllib.load(fh)
        except FileNotFoundError:
            # A concurrent reset may remove the target between is_file() and
            # open(); load_config handles that as a fresh config as well.
            return False
        return migrate_config_payload(payload, emit_diagnostics=False).changed

    def _workspace_dir(self) -> Path:
        cfg = self._config_obj()
        return Path(cfg.workspace_dir or self.home / "workspace").expanduser()

    def _normalize_default_config_paths(self, cfg: GatewayConfig) -> None:
        if not os.environ.get("OPENSQUILLA_STATE_DIR", "").strip():
            return
        default_home = Path.home() / ".opensquilla"
        if self.home == default_home:
            return

        def normalize(value: str | None, default_child: str, target_child: str) -> str | None:
            if not value:
                return None
            path = Path(value).expanduser()
            try:
                if path.resolve(strict=False) == (default_home / default_child).resolve(
                    strict=False
                ):
                    return str(self.home / target_child)
            except OSError:
                if path == default_home / default_child:
                    return str(self.home / target_child)
            return None

        workspace = normalize(cfg.workspace_dir, "workspace", "workspace")
        if workspace:
            cfg.workspace_dir = workspace
            self._config_changed = True
        state_dir = normalize(cfg.state_dir, "state", "state")
        if state_dir:
            cfg.state_dir = state_dir
            self._config_changed = True

    def _openclaw_workspace(self) -> Path:
        config = self._load_openclaw_config()
        configured = _get_nested(config, "agents", "defaults", "workspace")
        if isinstance(configured, str) and configured.strip():
            return Path(configured).expanduser()
        workspace = self.source / "workspace"
        if workspace.is_dir():
            return workspace
        return self.source / "workspace.default"

    # Files that mark a directory as a real OpenClaw workspace. A name match
    # alone (``workspace-*``) is not enough — backup/cache directories often
    # share the prefix without being workspaces themselves.
    _WORKSPACE_MARKERS = ("SOUL.md", "MEMORY.md", "USER.md", "AGENTS.md", "IDENTITY.md")

    @classmethod
    def _looks_like_workspace(cls, path: Path) -> bool:
        return any((path / marker).is_file() for marker in cls._WORKSPACE_MARKERS)

    def _openclaw_workspaces(self) -> list[Path]:
        # OpenClaw users often keep multiple workspaces side by side under
        # ``source/`` (``workspace``, ``workspace-w1`` ... ``workspace-wN``).
        # The primary one is selected by config; the rest are discovered as
        # siblings so per-workspace files (notably daily memory) are not lost.
        # Siblings without persona marker files are rejected so unrelated
        # ``workspace-*`` directories (backups, caches) do not bleed in.
        primary = self._openclaw_workspace()
        found: list[Path] = []
        if primary.is_dir():
            found.append(primary)
        if self.source.is_dir():
            try:
                primary_real = primary.resolve() if primary.is_dir() else None
            except OSError:
                primary_real = None
            for sibling in sorted(self.source.iterdir()):
                if not sibling.is_dir():
                    continue
                name = sibling.name
                if name != "workspace" and not name.startswith("workspace-"):
                    continue
                try:
                    if primary_real is not None and sibling.resolve() == primary_real:
                        continue
                except OSError:
                    pass
                if not self._looks_like_workspace(sibling):
                    continue
                if sibling not in found:
                    found.append(sibling)
        return found

    def _load_openclaw_config(self) -> dict[str, Any]:
        for name in ("openclaw.json", "clawdbot.json", "moltbot.json"):
            data = _read_json(self.source / name)
            if data:
                return data
        return {}

    def _record(
        self,
        kind: str,
        source: Path | str | None,
        destination: Path | str | None,
        status: str,
        reason: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.items.append(
            ItemResult(
                kind=kind,
                source=str(source) if source is not None else None,
                destination=str(destination) if destination is not None else None,
                status=status,
                reason=reason,
                details=_redact_value(details or {}),
            )
        )

    def _note(self, category: str, message: str) -> None:
        entry = f"{category}: {message}"
        if entry not in self._notes:
            self._notes.append(entry)

    def _write_text_target(
        self,
        kind: str,
        source: Path,
        destination: Path,
        text: str,
        *,
        details: dict[str, Any] | None = None,
        bootstrap_template_filename: str | None = None,
    ) -> None:
        if not source.is_file():
            self._record(kind, source, destination, "skipped", "source file not found")
            return
        if not self.options.apply:
            self._record(kind, source, destination, "planned", details=details)
            return
        is_pristine_template = (
            destination.exists()
            and not self.options.overwrite
            and bootstrap_template_filename is not None
            and _dest_is_pristine_bootstrap_template(
                destination, bootstrap_template_filename
            )
        )
        if destination.exists() and not self.options.overwrite and not is_pristine_template:
            self._record(kind, source, destination, "conflict", "target exists")
            return
        self._capture_apply_target(destination)
        if destination.exists():
            self._backup_file(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        merged_details = dict(details or {})
        if is_pristine_template:
            # Make it explicit in the report that we treated a pristine
            # OpenSquilla bootstrap template as overwrite-safe — otherwise a
            # fresh ``~/.opensquilla`` (which always seeds the template at
            # init) would block every workspace-file migration with a silent
            # ``conflict: target exists``.
            merged_details["replaced_bootstrap_template"] = True
        self._record(
            kind, source, destination, "migrated", details=merged_details or None
        )

    def _migrate_workspace_file(self, filename: str, kind: str) -> None:
        source = self._openclaw_workspace() / filename
        destination = self._workspace_dir() / filename
        raw_text = (
            source.read_text(encoding="utf-8-sig", errors="replace")
            if source.is_file()
            else ""
        )
        skip_reason = _rebrand_skip_reason(raw_text) if raw_text else None
        text, changed = _rebrand_text(raw_text)
        details: dict[str, Any] = {}
        if skip_reason is not None:
            # Mixed-subject prose: keep the original wording so the user
            # can reword by hand. The destination receives the verbatim
            # text below.
            details["rebrand_skipped"] = skip_reason
        elif changed:
            details["semantic_conversions"] = ["openclaw-branding"]
        if not source.is_file():
            self._record(kind, source, destination, "skipped", "source file not found")
            return

        # Scenario-C persona conflict: dest is real user content (not the
        # pristine template, no --overwrite). Per the agreed contract,
        # never silently drop OpenClaw content and never silently clobber
        # the user's persona file. Ask the user (or honor an explicit
        # --persona-conflict mode).
        if (
            self.options.apply
            and destination.is_file()
            and not self.options.overwrite
            and not _dest_is_pristine_bootstrap_template(destination, filename)
        ):
            choice = self._resolve_persona_conflict(filename, destination, text)
            details["persona_conflict_resolution"] = choice
            if choice == "use-openclaw":
                self._capture_apply_target(destination)
                self._backup_file(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(text, encoding="utf-8")
                self._record(kind, source, destination, "migrated", details=details)
                if changed:
                    self._archive_original_workspace_file(source, filename)
                return
            if choice == "merge":
                existing = destination.read_text(encoding="utf-8-sig")
                merged = (
                    existing.rstrip()
                    + "\n\n## Imported from OpenClaw\n\n"
                    + text.lstrip()
                )
                self._capture_apply_target(destination)
                self._backup_file(destination)
                destination.write_text(merged, encoding="utf-8")
                self._record(kind, source, destination, "migrated", details=details)
                if changed:
                    self._archive_original_workspace_file(source, filename)
                return
            if choice == "use-opensquilla":
                # Keep dest unchanged. Stash the openclaw original under
                # archive/files/openclaw-orphaned/ so the user can review
                # it later instead of it being silently dropped.
                self._archive_openclaw_orphaned(source, filename)
                self._record(
                    kind,
                    source,
                    destination,
                    "skipped",
                    "kept existing opensquilla content; openclaw archived for review",
                    details=details or None,
                )
                return
            # "skip" — neither imported nor archived.
            self._record(
                kind,
                source,
                destination,
                "skipped",
                "user chose to skip this file",
                details=details or None,
            )
            return

        self._write_text_target(
            kind,
            source,
            destination,
            text,
            details=details or None,
            bootstrap_template_filename=filename,
        )
        if changed:
            self._archive_original_workspace_file(source, filename)

    def _resolve_persona_conflict(
        self, filename: str, destination: Path, incoming_text: str
    ) -> str:
        # When the user passed an explicit mode (not "prompt"), honor it.
        mode = self.options.persona_conflict
        if mode != "prompt":
            return mode
        # Interactive prompting only makes sense when stdin is a TTY. In
        # non-interactive runs (CI, pipes, --json) we default to the
        # safest option: keep the user's existing persona AND archive the
        # OpenClaw version so nothing is silently lost.
        import sys as _sys

        if not _sys.stdin.isatty():
            self._note(
                "persona-conflict",
                f"{filename}: non-interactive run, defaulted to use-opensquilla; "
                f"openclaw content archived under archive/files/openclaw-orphaned/",
            )
            return "use-opensquilla"
        return self._prompt_persona_choice(filename, destination, incoming_text)

    def _prompt_persona_choice(
        self, filename: str, destination: Path, incoming_text: str
    ) -> str:
        # Show a small side-by-side preview and offer the four resolutions
        # we already support programmatically.
        try:
            import questionary
        except ImportError:
            return "use-opensquilla"

        existing = destination.read_text(encoding="utf-8-sig")
        import sys as _sys

        def _preview(label: str, body: str, byte_count: int) -> None:
            _sys.stderr.write(f"\n  ── {label} ({byte_count} bytes) ──\n")
            lines = body.splitlines() or [""]
            for line in lines[:10]:
                _sys.stderr.write(f"    {line}\n")
            if len(lines) > 10:
                _sys.stderr.write(f"    ... ({len(lines) - 10} more lines)\n")

        _sys.stderr.write(
            f"\n⚠ Conflict on {filename}: opensquilla and openclaw both have content.\n"
        )
        _preview(f"existing opensquilla {filename}", existing, len(existing))
        _preview(f"incoming openclaw {filename} (after rebrand)", incoming_text, len(incoming_text))
        _sys.stderr.write("\n")
        answer = questionary.select(
            f"Which {filename} should opensquilla use?",
            choices=[
                questionary.Choice(
                    "Keep opensquilla (openclaw archived for review)",
                    "use-opensquilla",
                ),
                questionary.Choice(
                    "Replace with openclaw (opensquilla backed up)",
                    "use-openclaw",
                ),
                questionary.Choice(
                    "Merge: append openclaw below opensquilla",
                    "merge",
                ),
                questionary.Choice(
                    "Skip this file (neither imported nor archived)",
                    "skip",
                ),
            ],
        ).ask()
        return answer or "use-opensquilla"

    def _archive_openclaw_orphaned(self, source: Path, filename: Path | str) -> None:
        if not source.is_file():
            return
        destination = (
            self.output_dir / "archive" / "files" / "openclaw-orphaned" / filename
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        self._record(
            f"openclaw-orphaned/{filename}", source, destination, "archived"
        )

    def _archive_original_workspace_file(self, source: Path, filename: Path | str) -> None:
        if not source.is_file():
            return
        destination = self.output_dir / "archive" / "files" / "workspace-original" / filename
        if not self.options.apply:
            self._record(f"workspace-original/{filename}", source, destination, "planned")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        self._record(f"workspace-original/{filename}", source, destination, "archived")

    def _migrate_memory(self) -> None:
        primary = self._openclaw_workspace()
        workspaces = self._openclaw_workspaces() or [primary]
        source = primary / "MEMORY.md"
        destination = self._workspace_dir() / "MEMORY.md"
        parts: list[str] = []
        rebranded_sources: list[tuple[Path, Path]] = []
        read_sources: list[Path] = []
        for workspace in workspaces:
            # Primary is identified by path equality with the config-derived
            # primary path; siblings get a workspace-name prefix so their
            # daily-memory labels and archived originals do not collide.
            is_primary = workspace == primary
            memo = workspace / "MEMORY.md"
            if memo.is_file():
                body = memo.read_text(encoding="utf-8-sig", errors="replace").rstrip()
                if body:
                    parts.append(body)
                    read_sources.append(memo)
                    if _rebrand_text(body)[1]:
                        rel = (
                            Path("MEMORY.md")
                            if is_primary
                            else Path(workspace.name) / "MEMORY.md"
                        )
                        rebranded_sources.append((memo, rel))
            memory_dir = workspace / "memory"
            if memory_dir.is_dir():
                for note in sorted(memory_dir.glob("*.md")):
                    body = note.read_text(encoding="utf-8-sig", errors="replace").strip()
                    if not body:
                        continue
                    label = (
                        note.name if is_primary else f"{workspace.name}/{note.name}"
                    )
                    parts.append(f"## Imported daily memory: {label}\n\n{body}")
                    read_sources.append(note)
                    if _rebrand_text(body)[1]:
                        rel = (
                            Path("memory") / note.name
                            if is_primary
                            else Path(workspace.name) / "memory" / note.name
                        )
                        rebranded_sources.append((note, rel))
        if read_sources:
            source = read_sources[0]
        if not parts:
            self._record("memory", source, destination, "skipped", "no memory files found")
            return
        rebranded_parts: list[str] = []
        rebranded = False
        skipped_parts = 0
        for part in parts:
            if _rebrand_skip_reason(part) is not None:
                # Mixed-subject memory entry: keep verbatim and count it
                # so the migration report flags how many entries the
                # user should reword by hand.
                rebranded_parts.append(part)
                skipped_parts += 1
                continue
            converted, changed = _rebrand_text(part)
            rebranded_parts.append(converted)
            rebranded = rebranded or changed
        text, details = self._prepare_memory_text(rebranded_parts)
        details["read_sources"] = [str(path) for path in read_sources]
        if rebranded:
            details["semantic_conversions"] = ["openclaw-branding"]
            for original, relative in rebranded_sources:
                self._archive_original_workspace_file(original, relative)
        if skipped_parts:
            details["rebrand_skipped"] = REBRAND_SKIP_REASON_MIXED
            details["rebrand_skipped_block_count"] = skipped_parts
        record_details = {k: v for k, v in details.items() if k != "overflow"}
        # Memory needs special-case handling for when the destination already
        # holds real, user-curated content (not the pristine bootstrap
        # template and we are not running with --overwrite). The plain
        # ``_write_text_target`` path would record a silent ``conflict`` and
        # drop everything. Memory is additive by nature, so the right
        # default is to append the imported blocks that are not already
        # present, preserving the user's existing memory verbatim.
        if (
            self.options.apply
            and destination.is_file()
            and not self.options.overwrite
            and not _dest_is_pristine_bootstrap_template(destination, "MEMORY.md")
        ):
            existing = destination.read_text(encoding="utf-8-sig")
            merged, deduped, appended_count = (
                self._merge_blocks_preserving_existing(existing, text)
            )
            if appended_count == 0:
                record_details["deduplicated_against_existing"] = True
                self._record(
                    "memory",
                    source,
                    destination,
                    "skipped",
                    "all openclaw memory blocks already present in destination",
                    details=record_details,
                )
            else:
                self._capture_apply_target(destination)
                self._backup_file(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(merged, encoding="utf-8")
                record_details["appended_to_existing"] = True
                record_details["new_blocks_appended"] = appended_count
                if deduped:
                    record_details["deduplicated_blocks_vs_existing"] = deduped
                self._record(
                    "memory", source, destination, "migrated", details=record_details
                )
        else:
            self._write_text_target(
                "memory",
                source,
                destination,
                text,
                details=record_details,
                bootstrap_template_filename="MEMORY.md",
            )
        overflow = details.get("overflow")
        if isinstance(overflow, str):
            self._write_memory_overflow(overflow)

    def _prepare_memory_text(self, parts: list[str]) -> tuple[str, dict[str, Any]]:
        seen: set[str] = set()
        unique: list[str] = []
        duplicates = 0
        for part in parts:
            normalized = self._memory_dedupe_key(part)
            if normalized in seen:
                duplicates += 1
                continue
            seen.add(normalized)
            unique.append(part.strip())
        text = "\n\n".join(unique).rstrip() + "\n"
        details: dict[str, Any] = {
            "source_blocks": len(parts),
            "deduplicated_blocks": duplicates,
        }
        if len(text) <= MAX_MEMORY_CHARS:
            return text, details
        cutoff = text.rfind("\n\n", 0, MAX_MEMORY_CHARS)
        if cutoff < MAX_MEMORY_CHARS // 2:
            cutoff = MAX_MEMORY_CHARS
        overflow = text[cutoff:].lstrip()
        trimmed = text[:cutoff].rstrip()
        marker = (
            "\n\n## Migration overflow\n\n"
            f"Additional OpenClaw memory was archived under `{MEMORY_OVERFLOW_DIR}`.\n"
        )
        details["overflow"] = overflow
        details["overflow_chars"] = len(overflow)
        return trimmed + marker, details

    def _memory_dedupe_key(self, text: str) -> str:
        stripped = text.strip()
        match = re.match(r"^## Imported daily memory: .+?\r?\n\r?\n(.*)$", stripped, re.DOTALL)
        if match:
            stripped = match.group(1).strip()
        return re.sub(r"\s+", " ", stripped)

    def _merge_blocks_preserving_existing(
        self, existing: str, new: str
    ) -> tuple[str, int, int]:
        # Append blocks from ``new`` that are not already present in
        # ``existing``. A "block" is a paragraph, but daily-memory entries
        # of the form ``## Imported daily memory: <name>\n\n<body>`` are
        # kept glued together — otherwise the header and body, separated
        # by ``\n\n``, would be deduped independently and produce wrong
        # results when only the body happens to appear elsewhere.
        #
        # Returns ``(merged_text, n_deduplicated, n_appended)``. When all
        # logical blocks already exist the existing text is returned.
        def _logical_blocks(text: str) -> list[str]:
            raw = [b for b in re.split(r"\n{2,}", text) if b.strip()]
            glued: list[str] = []
            i = 0
            while i < len(raw):
                current = raw[i]
                if re.match(r"^## Imported daily memory: ", current) and i + 1 < len(raw):
                    glued.append(f"{current}\n\n{raw[i + 1]}")
                    i += 2
                else:
                    glued.append(current)
                    i += 1
            return glued

        def _norm(block: str) -> str:
            stripped = block.strip()
            match = re.match(
                r"^## Imported daily memory: .+?\r?\n\r?\n(.*)$",
                stripped,
                re.DOTALL,
            )
            if match:
                stripped = match.group(1).strip()
            return re.sub(r"\s+", " ", stripped)

        existing_norms = {_norm(block) for block in _logical_blocks(existing)}
        appended: list[str] = []
        deduped = 0
        seen_in_new: set[str] = set()
        for block in _logical_blocks(new):
            normalised = _norm(block)
            if normalised in existing_norms or normalised in seen_in_new:
                deduped += 1
                continue
            seen_in_new.add(normalised)
            appended.append(block)
        if not appended:
            return existing, deduped, 0
        merged = existing.rstrip() + "\n\n" + "\n\n".join(appended) + "\n"
        return merged, deduped, len(appended)

    def _write_memory_overflow(self, text: str) -> None:
        destination = self.output_dir / "archive" / MEMORY_OVERFLOW_DIR / "MEMORY.overflow.md"
        if not self.options.apply:
            self._record("memory-overflow", "OpenClaw memory", destination, "planned")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        self._record("memory-overflow", "OpenClaw memory", destination, "archived")

    def _migrate_skills(self) -> None:
        destination_root = self.home / "skills" / SKILL_IMPORT_DIRNAME
        source_roots = [
            self._openclaw_workspace() / "skills",
            self.source / "skills",
            Path.home() / ".agents" / "skills",
            self._openclaw_workspace() / ".agents" / "skills",
        ]
        copied = 0
        for source_root in source_roots:
            if not source_root.is_dir():
                continue
            for skill_dir in sorted(source_root.iterdir()):
                if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
                    continue
                target = self._skill_target(destination_root, skill_dir.name)
                details = self._skill_compatibility_details(skill_dir)
                if not self.options.apply:
                    self._record("skills", skill_dir, target, "planned", details=details)
                    copied += 1
                    continue
                if target.exists() and self.options.skill_conflict == "skip":
                    self._record(
                        "skills",
                        skill_dir,
                        target,
                        "skipped",
                        "skill target exists",
                        details,
                    )
                    continue
                if target.exists() and self.options.skill_conflict == "overwrite":
                    self._capture_apply_target(target)
                    self._backup_dir(target)
                    shutil.rmtree(target)
                else:
                    self._capture_apply_target(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(skill_dir, target)
                self._record("skills", skill_dir, target, "migrated", details=details)
                copied += 1
        if copied == 0:
            self._record("skills", None, destination_root, "skipped", "no OpenClaw skills found")
        self._add_skill_extra_dir(destination_root)

    def _migrate_tts_assets(self) -> None:
        source = self._openclaw_workspace() / "tts"
        destination = self.home / "tts"
        if not source.is_dir():
            self._record("tts-assets", source, destination, "skipped", "no TTS assets found")
            return
        if not self.options.apply:
            self._record("tts-assets", source, destination, "planned")
            return
        if destination.exists() and not self.options.overwrite:
            # Merge only missing files by default; existing files are left untouched.
            copied = 0
            for item in source.rglob("*"):
                if not item.is_file():
                    continue
                target = destination / item.relative_to(source)
                if target.exists():
                    continue
                self._capture_apply_target(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
                copied += 1
            status = "migrated" if copied else "skipped"
            reason = "" if copied else "all TTS assets already exist"
            self._record("tts-assets", source, destination, status, reason, {"copied": copied})
            return
        if destination.exists():
            self._capture_apply_target(destination)
            self._backup_dir(destination)
            shutil.rmtree(destination)
        else:
            self._capture_apply_target(destination)
        shutil.copytree(source, destination)
        self._record("tts-assets", source, destination, "migrated")

    def _skill_target(self, root: Path, name: str) -> Path:
        target = root / name
        if not target.exists() or self.options.skill_conflict != "rename":
            return target
        idx = 1
        while True:
            candidate = root / f"{name}-imported-{idx}"
            if not candidate.exists():
                return candidate
            idx += 1

    def _add_skill_extra_dir(self, path: Path) -> None:
        cfg = self._config_obj()
        extra = list(cfg.skills.extra_dirs)
        path_str = str(path)
        if path_str not in extra:
            extra.append(path_str)
            cfg.skills.extra_dirs = extra
            self._config_changed = True

    def _skill_compatibility_details(self, skill_dir: Path) -> dict[str, Any]:
        skill_file = skill_dir / "SKILL.md"
        issues: list[str] = []
        try:
            size = skill_file.stat().st_size
        except OSError:
            return {
                "opensquilla_loadable": False,
                "compatibility": "not_loadable",
                "compatibility_issues": ["SKILL.md cannot be read"],
            }
        if size > MAX_SKILL_FILE_BYTES:
            issues.append("SKILL.md exceeds OpenSquilla skill file size limit")
        try:
            text = skill_file.read_text(encoding="utf-8-sig")
        except OSError:
            return {
                "opensquilla_loadable": False,
                "compatibility": "not_loadable",
                "compatibility_issues": ["SKILL.md cannot be read"],
            }
        match = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", text, re.DOTALL)
        if not match:
            return {
                "opensquilla_loadable": False,
                "compatibility": "not_loadable",
                "compatibility_issues": ["missing YAML frontmatter"],
            }
        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return {
                "opensquilla_loadable": False,
                "compatibility": "not_loadable",
                "compatibility_issues": ["invalid YAML frontmatter"],
            }
        # YAML frontmatter may parse to None (empty), a list, or a scalar.
        # ``None.get(...)`` previously crashed the entire migration.
        if not isinstance(frontmatter, dict):
            issues.append("missing frontmatter name")
            issues.append("missing frontmatter description")
        else:
            if not frontmatter.get("name"):
                issues.append("missing frontmatter name")
            if not frontmatter.get("description"):
                issues.append("missing frontmatter description")
        loadable = not any(
            issue
            for issue in issues
            if issue
            in {
                "SKILL.md exceeds OpenSquilla skill file size limit",
                "missing frontmatter name",
            }
        )
        if loadable and not issues:
            compatibility = "loadable"
        elif loadable:
            compatibility = "needs_review"
        else:
            compatibility = "not_loadable"
        return {
            "opensquilla_loadable": loadable,
            "compatibility": compatibility,
            "compatibility_issues": issues,
        }

    def _migrate_command_allowlist(self) -> None:
        source = self.source / "exec-approvals.json"
        if not source.is_file():
            self._record("command-allowlist", source, self.home / ".env", "skipped")
            return
        data = _read_json(source)
        patterns = data.get("allow") or data.get("allowlist") or data.get("patterns") or []
        if not isinstance(patterns, list) or not patterns:
            self._record("command-allowlist", source, self.home / ".env", "skipped")
            return
        value = ",".join(str(p) for p in patterns if str(p).strip())
        if value:
            self._env_additions["OPENSQUILLA_SAFE_BIN_ALLOW"] = value
            status = "migrated" if self.options.apply else "planned"
            self._record("command-allowlist", source, self.home / ".env", status)

    def _migrate_model_config(self, config: dict[str, Any]) -> None:
        model, model_details = self._resolve_default_model(config)
        if not model:
            self._record("model-config", self.source / "openclaw.json", self.config_path, "skipped")
            return
        provider = _provider_from_model(model)
        model, normalize_details = _model_for_opensquilla_provider(model, provider)
        model_details.update(normalize_details)
        cfg = self._config_obj()
        details = {"model": model, **model_details}
        should_write_model = True
        if provider:
            env_key = _env_key_for_provider(provider)
            existing_profile = getattr(
                getattr(cfg, "squilla_router", None), "tier_profile", None
            )
            normalized_profile = (existing_profile or "").strip().lower()
            normalized_provider = provider.strip().lower()
            tier_profile_conflicts = bool(
                existing_profile
                and normalized_profile != normalized_provider
            )
            if env_key and not tier_profile_conflicts:
                cfg.llm.provider = provider
            elif env_key and tier_profile_conflicts:
                preserved = cfg.llm.provider
                preserved_model = cfg.llm.model
                should_write_model = False
                details["tier_profile_conflict"] = existing_profile
                details["llm_provider_left_unchanged"] = preserved
                details["llm_model_left_unchanged"] = preserved_model
                details["skipped_model"] = model
                details["manual_steps"] = [
                    (
                        f"OpenClaw model config implies provider={provider!r} "
                        f"and model={model!r}, but squilla_router.tier_profile "
                        f"is currently {existing_profile!r}. OpenSquilla "
                        "requires the provider and tier profile to match. "
                        f"llm.provider and llm.model were left as {preserved!r} "
                        f"and {preserved_model!r}. To switch providers, either "
                        "clear squilla_router.tier_profile or set provider, "
                        "model, and tier profile explicitly with "
                        "`opensquilla config set`."
                    )
                ]
            else:
                preserved = cfg.llm.provider
                details["unrecognized_provider"] = provider
                details["llm_provider_left_unchanged"] = preserved
                details["manual_steps"] = [
                    (
                        f"OpenClaw model config implies provider={provider!r}, which "
                        "has no OpenSquilla equivalent. llm.provider was left as "
                        f"{preserved!r}."
                    )
                ]
            if env_key and self.options.migrate_secrets and not tier_profile_conflicts:
                cfg.llm.api_key_env = env_key
        if should_write_model:
            cfg.llm.model = model
        self._config_changed = True
        self._record(
            "model-config",
            self.source / "openclaw.json",
            self.config_path or cfg.config_path,
            "migrated" if self.options.apply else "planned",
            details=details,
        )

    def _resolve_default_model(self, config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        defaults = _get_nested(config, "agents", "defaults")
        raw_model = defaults.get("model") if isinstance(defaults, dict) else None
        if isinstance(raw_model, dict):
            candidate = (
                raw_model.get("primary")
                or raw_model.get("default")
                or raw_model.get("model")
                or raw_model.get("name")
            )
        else:
            candidate = raw_model
        if not isinstance(candidate, str) or not candidate.strip():
            return "", {}
        requested = candidate.strip()
        catalogs: list[Any] = []
        if isinstance(defaults, dict):
            catalogs.append(defaults.get("models"))
        catalogs.extend(
            [
                _get_nested(config, "models", "catalog"),
            ]
        )
        resolved = requested
        for catalog in catalogs:
            resolved = self._resolve_model_alias(resolved, catalog)
            if resolved != requested:
                break
        if resolved == requested:
            aliases = _get_nested(config, "models", "aliases")
            if isinstance(aliases, dict):
                alias_target = aliases.get(requested)
                if isinstance(alias_target, str) and alias_target.strip():
                    resolved = alias_target.strip()
        details: dict[str, Any] = {}
        if resolved != requested:
            details["requested_model"] = requested
            details["resolved_from_alias"] = True
        if isinstance(raw_model, dict):
            details["source_format"] = "object"
        return resolved, details

    def _resolve_model_alias(self, model: str, catalog: Any) -> str:
        if not isinstance(catalog, dict) or not catalog:
            return model
        if model in catalog:
            value = catalog[model]
            if isinstance(value, str):
                return model if "/" in model else value.strip()
            return model
        for model_id, raw in catalog.items():
            alias: str | None = None
            if isinstance(raw, dict):
                raw_alias = raw.get("alias") or raw.get("name") or raw.get("label")
                alias = str(raw_alias).strip() if raw_alias is not None else None
            elif isinstance(raw, str):
                alias = raw.strip()
            if alias == model:
                return str(model_id)
        return model

    def _migrate_mcp_servers(self, config: dict[str, Any]) -> None:
        servers = _get_nested(config, "mcp", "servers")
        if not isinstance(servers, dict) or not servers:
            self._record("mcp-servers", self.source / "openclaw.json", self.config_path, "skipped")
            return
        cfg = self._config_obj()
        entries: list[MCPServerEntry] = []
        unsupported_fields: dict[str, list[str]] = {}
        for name, raw in servers.items():
            if not isinstance(raw, dict):
                continue
            supported = {"url", "command", "args", "env", "tool_timeout_seconds"}
            extra = sorted(str(key) for key in raw if str(key) not in supported)
            if extra:
                unsupported_fields[str(name)] = extra
                self._note(
                    "mcp-servers",
                    (
                        f"MCP server {name!r} has unsupported OpenClaw fields "
                        f"{', '.join(extra)}; native fields were migrated."
                    ),
                )
            url = raw.get("url")
            command = raw.get("command")
            transport = "sse" if url else "stdio"
            entries.append(
                MCPServerEntry(
                    name=str(name),
                    transport=transport,
                    command=str(command) if command else None,
                    args=[str(item) for item in raw.get("args", []) if item is not None],
                    url=str(url) if url else None,
                    env={
                        str(k): str(v)
                        for k, v in (raw.get("env") or {}).items()
                        if v is not None
                    },
                    tool_timeout_seconds=float(raw.get("tool_timeout_seconds", 30.0)),
                )
            )
        if not entries:
            self._record("mcp-servers", self.source / "openclaw.json", self.config_path, "skipped")
            return
        # Upsert by name into the existing list rather than replacing it
        # wholesale, so pre-existing opensquilla MCP servers survive the
        # migration. The previous assignment was a silent destructive write.
        existing_servers = list(cfg.mcp.servers)
        existing_by_name = {s.name: idx for idx, s in enumerate(existing_servers)}
        added: list[str] = []
        replaced: list[str] = []
        for entry in entries:
            if entry.name in existing_by_name:
                existing_servers[existing_by_name[entry.name]] = entry
                replaced.append(entry.name)
            else:
                existing_servers.append(entry)
                added.append(entry.name)
        # Preserve user's explicit ``mcp.enabled = false`` choice: only
        # flip to True when MCP was defaulted off (no pre-existing
        # servers). If the user had servers AND disabled MCP, keep it
        # disabled and surface a manual_steps hint.
        mcp_enabled_left_disabled = False
        if not cfg.mcp.enabled:
            if not existing_by_name:
                cfg.mcp.enabled = True
            else:
                mcp_enabled_left_disabled = True
        cfg.mcp.servers = existing_servers
        self._config_changed = True
        record_details: dict[str, Any] = {
            "count": len(entries),
            "added": added,
            "replaced": replaced,
            "preserved_existing": [
                s.name for s in existing_servers
                if s.name not in {e.name for e in entries}
            ],
            "unsupported_fields": unsupported_fields,
        }
        if mcp_enabled_left_disabled:
            record_details["mcp_enabled_left_disabled"] = True
            record_details["manual_steps"] = [
                "MCP is disabled in your OpenSquilla config but you have "
                "configured servers. Set `mcp.enabled = true` via "
                "`opensquilla config set mcp.enabled true` to activate them."
            ]
        self._record(
            "mcp-servers",
            self.source / "openclaw.json",
            self.config_path or cfg.config_path,
            "migrated" if self.options.apply else "planned",
            details=record_details,
        )

    def _migrate_agent_config(self, config: dict[str, Any]) -> None:
        defaults = _get_nested(config, "agents", "defaults")
        defaults = defaults if isinstance(defaults, dict) else {}
        timeout = defaults.get("timeoutSeconds")
        migrated: dict[str, Any] = {}
        cfg = self._config_obj()
        if isinstance(timeout, int | float) and timeout > 0:
            cfg.agent_runtime_timeout_seconds = float(timeout)
            if float(timeout).is_integer():
                cfg.agent_runtime_timeout_seconds = int(timeout)
            migrated["agent_runtime_timeout_seconds"] = timeout
            self._config_changed = True
        thinking = self._normalize_thinking(defaults.get("thinkingDefault"))
        if thinking:
            cfg.llm.thinking = thinking
            migrated["llm.thinking"] = thinking
            self._config_changed = True
        compaction = defaults.get("compaction")
        if isinstance(compaction, dict):
            policy = self._context_policy_from_openclaw(compaction.get("mode"))
            if policy:
                cfg.context_overflow_policy = policy
                migrated["context_overflow_policy"] = policy.value
                self._config_changed = True
            for key in sorted(set(compaction) - {"mode"}):
                self._note(
                    "agent-config",
                    (
                        f"OpenClaw compaction.{key}={compaction[key]!r} has no exact "
                        "OpenSquilla field."
                    ),
                )
        for key in ("verboseDefault", "humanDelay", "userTimezone"):
            if key in defaults:
                self._note(
                    "agent-config",
                    f"OpenClaw agents.defaults.{key}={defaults[key]!r} requires manual review.",
                )
        if migrated:
            self._record(
                "agent-config",
                "openclaw.json agents.defaults",
                self.config_path or cfg.config_path,
                "migrated" if self.options.apply else "planned",
                details=migrated,
            )
        else:
            self._record(
                "agent-config",
                "openclaw.json agents.defaults",
                self.config_path,
                "skipped",
            )

    def _normalize_thinking(self, value: Any) -> str | None:
        if isinstance(value, bool):
            return "medium" if value else "off"
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        aliases = {
            "false": "off",
            "none": "off",
            "no": "off",
            "true": "medium",
            "yes": "medium",
        }
        normalized = aliases.get(normalized, normalized)
        allowed = {"off", "minimal", "low", "medium", "high", "xhigh", "adaptive"}
        if normalized in allowed:
            return normalized
        self._note(
            "agent-config",
            f"OpenClaw thinkingDefault={value!r} is not an OpenSquilla thinking level.",
        )
        return None

    def _context_policy_from_openclaw(self, value: Any) -> ContextOverflowPolicy | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower().replace("-", "_")
        if normalized in {"auto", "auto_summarize", "summarize", "compact"}:
            return ContextOverflowPolicy.AUTO_SUMMARIZE
        if normalized in {"truncate", "hard_truncate"}:
            return ContextOverflowPolicy.HARD_TRUNCATE
        if normalized in {"refuse", "error", "disabled", "off", "none"}:
            return ContextOverflowPolicy.REFUSE
        self._note(
            "agent-config",
            f"OpenClaw compaction.mode={value!r} is not mapped to an OpenSquilla policy.",
        )
        return None

    def _migrate_tools_config(self, config: dict[str, Any]) -> None:
        timeout = _get_nested(config, "tools", "exec", "timeoutSec")
        if isinstance(timeout, int | float) and timeout > 0:
            self._record(
                "tools-config",
                "openclaw.json tools.exec.timeoutSec",
                self.config_path,
                "archived",
                (
                    "OpenSquilla has no exact per-shell timeout config; "
                    "agent timeout is migrated separately"
                ),
                {"timeoutSec": timeout},
            )
        else:
            self._record("tools-config", "openclaw.json tools", self.config_path, "skipped")

    def _migrate_provider_keys(self, config: dict[str, Any]) -> None:
        env_values = _load_env_file(self.source / ".env")
        providers = _get_nested(config, "models", "providers")
        config_secret_count = 0
        if isinstance(providers, dict):
            for provider, raw in providers.items():
                if not isinstance(raw, dict):
                    continue
                provider_name = _normalize_provider_id(str(provider))
                key = _env_key_for_provider(provider_name)
                if not key:
                    continue
                raw_key = raw.get("apiKey") or raw.get("api_key") or raw.get("token")
                if self.options.migrate_secrets and isinstance(raw_key, str) and raw_key.strip():
                    self._env_additions[key] = raw_key.strip()
                    config_secret_count += 1
                if (
                    self.options.migrate_secrets
                    and isinstance(raw.get("baseUrl"), str)
                    and self._config_obj().llm.provider == provider_name
                ):
                    self._config_obj().llm.base_url = str(raw["baseUrl"])
                    self._config_changed = True
        migrated = 0
        for key in sorted(SECRET_ENV_KEYS):
            value = env_values.get(key, "").strip()
            if not value:
                continue
            if self.options.migrate_secrets:
                self._env_additions[key] = value
                migrated += 1
                if key == "BRAVE_API_KEY":
                    cfg = self._config_obj()
                    cfg.search_provider = "brave"
                    cfg.search_api_key_env = "BRAVE_API_KEY"
                    self._config_changed = True
        migrated += config_secret_count
        if migrated:
            self._record(
                "provider-keys",
                "OpenClaw .env/models.providers",
                self.home / ".env",
                "migrated" if self.options.apply else "planned",
                details={"migrated_keys": [SECRET_REDACTION] * migrated},
            )
            return
        provider_keys_present = any(
            isinstance(raw, dict) and any(raw.get(name) for name in ("apiKey", "api_key", "token"))
            for raw in providers.values()
        ) if isinstance(providers, dict) else False
        reason = (
            "pass --migrate-secrets to migrate recognized secrets"
            if env_values or provider_keys_present
            else ""
        )
        self._record(
            "provider-keys",
            "OpenClaw .env/models.providers",
            self.home / ".env",
            "skipped",
            reason,
        )

    def _migrate_supported_channels(self, config: dict[str, Any], selected: set[str]) -> None:
        env_values = _load_env_file(self.source / ".env")
        raw_entries = [
            entry.model_dump(mode="python")
            for entry in self._config_obj().channels.channels
        ]
        changed = False

        def upsert(entry: dict[str, Any]) -> None:
            nonlocal changed
            for idx, existing in enumerate(raw_entries):
                if existing.get("name") == entry["name"]:
                    raw_entries[idx] = entry
                    changed = True
                    return
            raw_entries.append(entry)
            changed = True

        if "telegram-settings" in selected:
            token = env_values.get("TELEGRAM_BOT_TOKEN", "").strip()
            if token and self.options.migrate_secrets:
                upsert(
                    {
                        "type": "telegram",
                        "name": "telegram",
                        "token": token,
                        "default_chat_id": str(
                            _get_nested(config, "messages", "telegram", "defaultChatId") or ""
                        ),
                    }
                )
                self._record(
                    "telegram-settings",
                    self.source / ".env",
                    self.config_path,
                    "migrated",
                )
            else:
                reason = "pass --migrate-secrets to migrate Telegram token" if token else ""
                self._record(
                    "telegram-settings",
                    self.source / ".env",
                    self.config_path,
                    "skipped",
                    reason,
                )

        if "discord-settings" in selected:
            token = env_values.get("DISCORD_BOT_TOKEN", "").strip()
            if token and self.options.migrate_secrets:
                upsert(
                    {
                        "type": "discord",
                        "name": "discord",
                        "token": token,
                        "default_channel_id": str(
                            _get_nested(config, "messages", "discord", "defaultChannelId") or ""
                        ),
                    }
                )
                self._record(
                    "discord-settings",
                    self.source / ".env",
                    self.config_path,
                    "migrated",
                )
            else:
                reason = "pass --migrate-secrets to migrate Discord token" if token else ""
                self._record(
                    "discord-settings",
                    self.source / ".env",
                    self.config_path,
                    "skipped",
                    reason,
                )

        if "slack-settings" in selected:
            token = env_values.get("SLACK_BOT_TOKEN", "").strip()
            if token and self.options.migrate_secrets:
                upsert({"type": "slack", "name": "slack", "token": token})
                self._record("slack-settings", self.source / ".env", self.config_path, "migrated")
            else:
                reason = "pass --migrate-secrets to migrate Slack token" if token else ""
                self._record(
                    "slack-settings",
                    self.source / ".env",
                    self.config_path,
                    "skipped",
                    reason,
                )

        admin_senders = dict(self._config_obj().channel_admin_senders)
        for channel_name, option in (
            ("telegram", "telegram-settings"),
            ("discord", "discord-settings"),
            ("slack", "slack-settings"),
        ):
            if option not in selected:
                continue
            admin_users = self._channel_admin_users(config, channel_name)
            if admin_users:
                admin_senders[channel_name] = admin_users
                self._note(
                    "channel-settings",
                    (
                        f"Mapped OpenClaw {channel_name} adminUsers/admin_users to "
                        "OpenSquilla channel_admin_senders."
                    ),
                )
            allowlist_fields = self._channel_non_admin_allowlist_fields(config, channel_name)
            if allowlist_fields:
                self._note(
                    "channel-settings",
                    (
                        f"OpenClaw {channel_name} {', '.join(allowlist_fields)} controls "
                        "channel access, not OpenSquilla admin privileges; it was not "
                        "mapped to channel_admin_senders."
                    ),
                )
        if admin_senders != self._config_obj().channel_admin_senders:
            self._config_obj().channel_admin_senders = admin_senders
            self._config_changed = True

        for channel_name in ("whatsapp", "signal"):
            raw = _get_nested(config, "messages", channel_name)
            if isinstance(raw, dict) and raw:
                self._note(
                    "channel-settings",
                    (
                        f"OpenClaw {channel_name} settings were detected but OpenSquilla "
                        "does not have a native migrated channel entry for them yet."
                    ),
                )

        if changed:
            cfg = self._config_obj()
            cfg.channels = ChannelsConfig.model_validate({"channels": raw_entries})
            self._config_changed = True

    def _channel_config(self, config: dict[str, Any], channel_name: str) -> dict[str, Any]:
        raw = _get_nested(config, "messages", channel_name)
        if not isinstance(raw, dict):
            raw = _get_nested(config, "channels", channel_name)
        if not isinstance(raw, dict):
            return {}
        return raw

    def _channel_admin_users(self, config: dict[str, Any], channel_name: str) -> list[str]:
        raw = self._channel_config(config, channel_name)
        candidates: list[str] = []
        for key in ("adminUsers", "admin_users"):
            candidates.extend(_string_list(raw.get(key)))
        return list(dict.fromkeys(candidates))

    def _channel_non_admin_allowlist_fields(
        self,
        config: dict[str, Any],
        channel_name: str,
    ) -> list[str]:
        raw = self._channel_config(config, channel_name)
        fields: list[str] = []
        for key in ("allowFrom", "allowedUsers", "allowed_users", "allowlist"):
            if _string_list(raw.get(key)):
                fields.append(key)
        return fields

    def _archive_tts_config(self, config: dict[str, Any]) -> None:
        candidates = {
            "messages.tts": _get_nested(config, "messages", "tts"),
            "talk": config.get("talk") if isinstance(config.get("talk"), dict) else None,
        }
        payload = {key: value for key, value in candidates.items() if value}
        destination = self.output_dir / "archive" / "tts-config.json"
        if not payload:
            self._record("tts-config", "openclaw.json messages.tts/talk", destination, "skipped")
            return
        if not self.options.apply:
            self._record("tts-config", "openclaw.json messages.tts/talk", destination, "planned")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(_redact_value(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._note(
            "tts-config",
            "OpenClaw TTS config was archived; OpenSquilla currently migrates TTS assets only.",
        )
        self._record("tts-config", "openclaw.json messages.tts/talk", destination, "archived")

    def _archive_unmapped_config(self, config: dict[str, Any], keys: set[str]) -> None:
        payloads: dict[Path, dict[str, Any]] = {}
        records: list[tuple[str, str, Path]] = []
        for key, filename in ARCHIVE_CONFIG_KEYS.items():
            if key not in keys:
                continue
            if key not in config:
                continue
            destination = self.output_dir / "archive" / filename
            kind = ARCHIVE_KIND_BY_CONFIG_KEY[key]
            if not self.options.apply:
                self._record(kind, f"openclaw.json {key}", destination, "planned")
                continue
            payloads.setdefault(destination, {})[key] = _redact_value(config[key])
            records.append((kind, f"openclaw.json {key}", destination))

        for destination, payload in payloads.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        for kind, source, destination in records:
            self._record(
                kind,
                source,
                destination,
                "archived",
            )

    def _archive_openclaw_artifacts(self) -> None:
        workspace = self._openclaw_workspace()
        for name in ARCHIVE_WORKSPACE_FILES:
            self._archive_artifact(
                workspace / name,
                Path("workspace") / name,
                f"workspace/{name}",
            )
        for name in ARCHIVE_WORKSPACE_DIRS:
            self._archive_artifact(
                workspace / name,
                Path("workspace") / name,
                f"workspace/{name}",
            )
        for relative in ARCHIVE_SOURCE_ARTIFACTS:
            self._archive_artifact(
                self.source / relative,
                Path(relative),
                relative,
            )
        for kind, relative in SKIP_SOURCE_ARTIFACTS:
            source = self.source / relative
            if source.exists():
                self._record(
                    kind,
                    source,
                    None,
                    "skipped",
                    "OpenClaw runtime state or sensitive material is not copied",
                )
        for name in RAW_CONFIG_FILENAMES:
            source = self.source / name
            if source.exists():
                self._record(
                    "raw-config",
                    source,
                    None,
                    "skipped",
                    "raw config is parsed or ignored, not copied wholesale",
                )

    def _archive_artifact(self, source: Path, relative: Path, kind: str) -> None:
        if not source.exists():
            return
        destination = self.output_dir / "archive" / "files" / relative
        if not self.options.apply:
            self._record(kind, source, destination, "planned")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)
        self._record(kind, source, destination, "archived")

    def _flush_config(self) -> None:
        if self._config is None or (
            not self._config_changed and not self._config_migration_pending
        ):
            return
        persist_config(self._config, path=self.config_path, backup=True, restart_required=True)

    def _flush_env(self) -> None:
        if not self._env_additions:
            return
        env_path = self.home / ".env"
        self._capture_apply_replace_target(env_path)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        existing_lines = (
            env_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if env_path.exists()
            else []
        )
        lines = merge_env_lines(existing_lines, self._env_additions)
        write_secret_env_file(env_path, lines)

    def _backup_file(self, path: Path) -> None:
        backup = path.with_name(f"{path.name}.backup.{self.timestamp}")
        self._capture_apply_target(path)
        self._capture_apply_target(backup)
        backup.write_bytes(path.read_bytes())

    def _backup_dir(self, path: Path) -> None:
        backup = path.with_name(f"{path.name}.backup.{self.timestamp}")
        self._capture_apply_target(path)
        self._capture_apply_target(backup)
        if backup.exists():
            shutil.rmtree(backup)
        shutil.copytree(path, backup)

    def _report(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "target_home": str(self.home),
            "config_path": str(self.config_path) if self.config_path else None,
            "output_dir": str(self.output_dir),
            "apply": self.options.apply,
            "migrate_secrets": self.options.migrate_secrets,
            "notes": list(self._notes),
            "items": [asdict(item) for item in self.items],
        }

    def _write_report_files(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        report = self._report()
        (self.output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        lines = [
            "# OpenClaw to OpenSquilla Migration Summary",
            "",
            f"- Source: `{self.source}`",
            f"- Target home: `{self.home}`",
            f"- Apply: `{self.options.apply}`",
            f"- Migrate secrets: `{self.options.migrate_secrets}`",
            "",
            "## Counts",
            "",
        ]
        for status, count in sorted(counts.items()):
            lines.append(f"- {status}: {count}")
        lines.append("")
        (self.output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
        self._write_migration_notes()

    def _write_migration_notes(self) -> None:
        if not self._notes:
            return
        lines = [
            "# OpenClaw Migration Notes",
            "",
            "These notes list OpenClaw semantics that were transformed, partially mapped, "
            "or require review in OpenSquilla.",
            "",
        ]
        for note in self._notes:
            lines.append(f"- {note}")
        lines.append("")
        (self.output_dir / "MIGRATION_NOTES.md").write_text(
            "\n".join(lines),
            encoding="utf-8",
        )
