"""Hermes Agent to OpenSquilla migration."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from opensquilla.gateway.config import ChannelsConfig, GatewayConfig, MCPServerEntry
from opensquilla.migration.env_file import merge_env_lines, write_secret_env_file
from opensquilla.onboarding.config_store import load_config, persist_config
from opensquilla.paths import default_opensquilla_home

SKILL_IMPORT_DIRNAME = "hermes-imports"
SECRET_REDACTION = "[redacted]"
SKILL_CONFLICT_MODES = {"skip", "overwrite", "rename"}
MAX_SKILL_FILE_BYTES = 256_000
MAX_MEMORY_CHARS = 80_000
MEMORY_OVERFLOW_DIR = "memory-overflow"

USER_DATA_OPTIONS = {"soul", "memory", "user-profile", "skills", "workspace-files"}
RUNTIME_CONFIG_OPTIONS = {
    "model-config",
    "provider-keys",
    "search-config",
    "telegram-settings",
    "discord-settings",
    "slack-settings",
    "mcp-servers",
    "tools-config",
    "archive",
    "browser-config",
    "session-config",
    "cron-jobs",
    "plugins-config",
    "gateway-config",
    "memory-backend",
    "approvals-config",
    "logging-config",
}

# Option ids accepted by the CLI but not yet wired to a real migration handler.
# Migrate() emits a `deferred` record for each so users can see exactly which
# selections are no-ops instead of getting an empty success report.
DEFERRED_OPTIONS = {
    "workspace-files",
    "tools-config",
    "browser-config",
    "session-config",
    "gateway-config",
    "approvals-config",
    "logging-config",
    "memory-backend",
}
MIGRATION_OPTIONS = USER_DATA_OPTIONS | RUNTIME_CONFIG_OPTIONS
MIGRATION_PRESETS = {"user-data": USER_DATA_OPTIONS, "full": MIGRATION_OPTIONS}

SECRET_ENV_KEYS = {
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "BRAVE_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "TAVILY_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
}

NON_SECRET_ENV_KEYS = {
    "OPENAI_BASE_URL",
    "OPENROUTER_BASE_URL",
    "ANTHROPIC_BASE_URL",
}

PROVIDER_ENV_KEYS = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

# Hermes itself validates profile names against this regex (see hermes_cli/
# profiles.py). Enforcing it here prevents `--profile "../secrets"` from
# escaping the hermes home via path traversal.
_HERMES_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

ARCHIVE_SOURCE_ARTIFACTS = {"cron": "cron-jobs", "plugins": "plugins-config"}
SKIP_SOURCE_ARTIFACTS = {
    "state.db",
    "state.db-wal",
    "state.db-shm",
    "kanban.db",
    "sessions",
    "logs",
    "auth.json",
    "checkpoints",
    "cache",
}


@dataclass(frozen=True)
class HermesMigrationOptions:
    source: Path | str | None = None
    profile: str | None = None
    config_path: Path | str | None = None
    apply: bool = False
    migrate_secrets: bool = False
    overwrite: bool = False
    preset: str = "full"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    skill_conflict: Literal["skip", "overwrite", "rename"] = "skip"


@dataclass
class ItemResult:
    kind: str
    source: str | None
    destination: str | None
    status: str
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _as_path(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser()


def _is_valid_hermes_home(path: Path) -> bool:
    return any(
        (path / name).exists()
        for name in ("config.yaml", ".env", "SOUL.md", "memories", "skills")
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    # Returns ({}, None) on missing file or unparseable YAML so a hand-edited
    # config.yaml with a syntax error cannot crash the entire migration.
    # Callers that need to surface the parse failure should use
    # _load_yaml_with_error.
    data, _ = _load_yaml_with_error(path)
    return data


def _load_yaml_with_error(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as exc:
        return {}, str(exc).splitlines()[0]
    if loaded is None:
        return {}, None
    if not isinstance(loaded, dict):
        return {}, None
    return loaded, None


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        # bash-style `export FOO=bar` is common in hand-written .env files.
        # Strip the leading `export ` keyword so the key matches the known
        # SECRET_ENV_KEYS set instead of being lost as "export FOO".
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


REBRAND_SKIP_REASON_MIXED = "mentions-opensquilla"
_OPENSQUILLA_MENTION_RE = re.compile(r"opensquilla", re.IGNORECASE)


def _rebrand_skip_reason(text: str) -> str | None:
    """Return a reason string when the source text should NOT be mechanically
    rebranded, otherwise ``None``.

    Mechanical "Hermes" -> "OpenSquilla" replacement assumes the source is
    *single-subject* prose (notes written from inside Hermes, about Hermes).
    Real users keep "what is installed where" notes that talk about Hermes
    AND OpenSquilla as distinct entities — e.g. "Hermes Agent v0.13.0
    installed at ~/.local/bin/hermes. OpenSquilla also installed at
    ~/.local/bin/opensquilla. Has `migrate hermes` subcommand." A
    mechanical rebrand collapses the two subjects into one, producing
    tautologies ("OpenSquilla skills loadable by OpenSquilla"), self-
    referential nonsense ("migrate OpenSquilla skills to OpenSquilla"),
    and factual errors (OpenSquilla "installed at ~/.local/bin/hermes").

    When the source already contains any case-variant of "opensquilla",
    treat it as mixed-subject prose and skip rebrand entirely. Callers
    write the original verbatim and record ``rebrand_skipped`` in the
    migration report so the user can review.
    """
    if _OPENSQUILLA_MENTION_RE.search(text):
        return REBRAND_SKIP_REASON_MIXED
    return None


def _hermes_rebrand_text(text: str) -> tuple[str, bool]:
    # Skip mechanical rebrand for mixed-subject prose: when the source
    # already mentions OpenSquilla, collapsing "Hermes" into "OpenSquilla"
    # turns previously-distinct subjects into the same subject and
    # corrupts the text (see _rebrand_skip_reason for the failure modes).
    # Callers consult _rebrand_skip_reason separately to record the skip
    # in the migration report.
    if _rebrand_skip_reason(text) is not None:
        return text, False

    # Rewrite Hermes-Agent branding in workspace prose to OpenSquilla. The
    # transformation is intentionally narrow: bare "Hermes" is only rewritten
    # when followed by a workspace-context word, and source-reference tokens
    # (project URLs, env var names, module names) are protected so the
    # migration archive still points back at the original source clearly.
    protected: dict[str, str] = {}

    def protect(match: re.Match[str]) -> str:
        key = f"__OPENSQUILLA_HERMES_REF_{len(protected)}__"
        protected[key] = match.group(0)
        return key

    source_reference_patterns = (
        r"\bHERMES_HOME\b",
        r"\bhermes-agent\b",
        r"\bNousResearch\b",
        r"\bhermes_constants\b",
        r"\bhermes_state\b",
        r"\bhermes_cli\b",
    )
    migrated = text
    for pattern in source_reference_patterns:
        migrated = re.sub(pattern, protect, migrated)

    # Brand-token replacements run before contextual lookahead rules so that
    # the multi-word brand "Hermes Agent" collapses to "OpenSquilla" cleanly
    # instead of leaving a dangling "Agent" behind. The "Hermes Agent"
    # variant uses a regex so multiple spaces, tabs, or a newline between
    # the two words still match.
    migrated = re.sub(r"\bHermes\s+Agent\b", "OpenSquilla", migrated)
    # Only rewrite ``.hermes`` when it ends a path token. A plain substring
    # replacement turned ``.hermesrc`` into ``.opensquillarc`` and
    # ``.hermes_backup`` into ``.opensquilla_backup``, both of which are
    # meaningless. The lookahead permits ``/``, whitespace, end-of-string,
    # or a quote char (so ``"~/.hermes"`` still rebrands).
    migrated = re.sub(r"\.hermes(?=[/\s'\"`)\],;:]|$)", ".opensquilla", migrated)

    contextual_replacements = (
        (
            r"\bHermes(?=\s+(?:home|workspace|skills?|config|configuration|"
            r"memory|gateway|state|runtime|来源|源|配置|工作区|状态|技能|内存))",
            "OpenSquilla",
        ),
        (
            r"\bhermes(?=\s+(?:home|workspace|skills?|config|memory|gateway|"
            r"state))",
            "opensquilla",
        ),
    )
    for pattern, replacement in contextual_replacements:
        migrated = re.sub(pattern, replacement, migrated)

    for key, value in protected.items():
        migrated = migrated.replace(key, value)
    return migrated, migrated != text


class HermesMigrator:
    def __init__(self, options: HermesMigrationOptions) -> None:
        self.options = options
        self.source = self._resolve_source()
        self.home = default_opensquilla_home()
        self.timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        self.output_dir = self.home / "migration" / "hermes" / self.timestamp
        self.items: list[ItemResult] = []
        self.config_path = _as_path(options.config_path)
        self._config_obj: GatewayConfig | None = None
        self._config_changed = False
        self._env_additions: dict[str, str] = {}
        self._last_merge_details: dict[str, Any] = {}

    def _resolve_source(self) -> Path:
        explicit = _as_path(self.options.source)
        if explicit is not None:
            return explicit

        env_home = os.environ.get("HERMES_HOME")
        root = Path(env_home).expanduser() if env_home else Path.home() / ".hermes"
        if self.options.profile:
            return root / "profiles" / self.options.profile
        return root

    def migrate(self) -> dict[str, Any]:
        if (
            self.options.profile is not None
            and not _HERMES_PROFILE_NAME_RE.match(self.options.profile)
        ):
            # Reject up front so we never construct a source path that
            # escapes the hermes home via ``..`` segments or hidden chars.
            self._record(
                "profile",
                None,
                None,
                "error",
                f"invalid profile name: {self.options.profile!r}",
            )
            return self._report()
        if not _is_valid_hermes_home(self.source):
            self._record("source", self.source, None, "error", "not a Hermes home")
            return self._report()
        selected = self._selected_options()
        self._plan_user_data(selected)
        self._migrate_config_and_env(selected)
        self._write_config()
        self._write_env()
        self._archive_unsupported(selected)
        self._record_deferred(selected)
        self._write_reports()
        return self._report()

    def _record_deferred(self, selected: set[str]) -> None:
        for option_id in sorted(DEFERRED_OPTIONS & selected):
            self._record(
                option_id,
                None,
                None,
                "deferred",
                "handler not implemented yet",
            )

    def _config(self) -> GatewayConfig:
        if self._config_obj is None:
            self._config_obj = load_config(self.config_path)
        return self._config_obj

    def _selected_options(self) -> set[str]:
        selected = set(MIGRATION_PRESETS.get(self.options.preset, MIGRATION_PRESETS["full"]))
        selected.update(self.options.include)
        selected.difference_update(self.options.exclude)
        return selected

    def _workspace_dir(self) -> Path:
        return self.home / "workspace"

    def _plan_user_data(self, selected: set[str]) -> None:
        if "soul" in selected:
            self._plan_file("soul", self.source / "SOUL.md", self._workspace_dir() / "SOUL.md")
        if "memory" in selected:
            self._plan_file(
                "memory",
                self.source / "memories" / "MEMORY.md",
                self._workspace_dir() / "MEMORY.md",
            )
        if "user-profile" in selected:
            self._plan_file(
                "user-profile",
                self.source / "memories" / "USER.md",
                self._workspace_dir() / "USER.md",
            )
        if "skills" in selected:
            self._plan_skills()

    def _plan_file(self, kind: str, source: Path, destination: Path) -> None:
        if not source.exists():
            self._record(kind, source, destination, "skipped", "source missing")
            return
        status = "migrated" if self.options.apply else "planned"
        reason = ""
        details: dict[str, Any] = {}
        if self.options.apply:
            self._write_text_merge(source, destination)
            details = dict(self._last_merge_details)
            # When the source content already matches an existing destination
            # block the merge is a no-op. Reflect that in the status so the
            # report doesn't claim "migrated" for a write that never happened.
            if details.get("deduplicated"):
                status = "skipped"
                reason = "duplicate of existing destination block"
        self._record(kind, source, destination, status, reason, details=details or None)

    def _plan_skills(self) -> None:
        skills_dir = self.source / "skills"
        destination_root = self.home / "skills" / SKILL_IMPORT_DIRNAME
        # is_dir() instead of exists() — if `skills` happens to be a regular
        # file the migrator must not crash on iterdir(). Treat both
        # "missing" and "not a directory" as a clean skip.
        if not skills_dir.is_dir():
            reason = "source missing" if not skills_dir.exists() else "source is not a directory"
            self._record("skills", skills_dir, destination_root, "skipped", reason)
            return
        skill_subdirs = sorted(path for path in skills_dir.iterdir() if path.is_dir())
        if not skill_subdirs:
            # An empty skills/ directory used to produce no record at all,
            # leaving users unable to distinguish "checked but empty" from
            # "skipped/never checked".
            self._record(
                "skills", skills_dir, destination_root, "skipped", "no skills to migrate"
            )
            return
        for skill_dir in skill_subdirs:
            target = destination_root / skill_dir.name
            compat = self._skill_compatibility_details(skill_dir)
            status = "migrated" if self.options.apply else "planned"
            reason = ""
            if self.options.apply:
                copied = self._copy_skill_dir(skill_dir, target)
                if copied is None:
                    status = "skipped"
                    reason = "target exists"
                else:
                    target = copied
            self._record("skills", skill_dir, target, status, reason, details=compat or None)

    def _write_text_merge(self, source: Path, destination: Path) -> None:
        # Reset per-call details so _plan_file's record reflects only this merge.
        self._last_merge_details = {}
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Use ``errors="replace"`` so a hand-edited source with stray bad
        # bytes (BOM markers, mixed CP1252 fragments, accidental binary
        # paste) does not crash the entire migration with
        # ``UnicodeDecodeError``. Openclaw already uses this strategy;
        # hermes was inconsistent. Replaced characters land as U+FFFD
        # so the user can spot them after migration.
        source_text = source.read_text(encoding="utf-8-sig", errors="replace")
        skip_reason = _rebrand_skip_reason(source_text)
        rebranded_text, rebranded = _hermes_rebrand_text(source_text)
        if skip_reason is not None:
            # Source is mixed-subject prose. The text is written verbatim
            # (no Hermes->OpenSquilla mangling) so the user can decide
            # themselves which mentions need rewording.
            self._last_merge_details["rebrand_skipped"] = skip_reason
        elif rebranded:
            self._last_merge_details["semantic_conversions"] = ["hermes-branding"]
            self._archive_original_workspace_file(source, destination.name)

        if destination.exists() and not self.options.overwrite:
            existing = destination.read_text(encoding="utf-8")
            if self._is_substantively_present(rebranded_text, existing):
                self._last_merge_details["deduplicated"] = True
                return
            combined = existing.rstrip() + "\n\n" + rebranded_text.lstrip()
        else:
            if destination.exists():
                # --overwrite: keep an item-level backup before destroying the
                # existing target so the user can roll back. The CLI help has
                # always advertised this; the implementation now keeps that
                # promise.
                self._backup_file(destination)
                self._last_merge_details["backup_created"] = True
            combined = rebranded_text

        # Memory overflow archival. The merged text may exceed the size limit
        # OpenSquilla applies to a single MEMORY.md; split at a paragraph
        # boundary and archive the tail rather than truncating silently.
        if destination.name == "MEMORY.md" and len(combined) > MAX_MEMORY_CHARS:
            trimmed, overflow = self._split_memory_overflow(combined)
            if overflow:
                self._write_memory_overflow(overflow)
                self._last_merge_details["overflow_chars"] = len(overflow)
            combined = trimmed

        destination.write_text(combined, encoding="utf-8")

    @staticmethod
    def _is_substantively_present(source_text: str, existing: str) -> bool:
        # Block-level subset check after whitespace normalization. Source is
        # considered "already present" only if EVERY non-empty source block
        # has an equivalent block in the destination. Comparing the
        # normalized whole-source string against individual destination
        # blocks (the previous approach) silently failed for multi-block
        # MEMORY.md because the concatenated form never matched any single
        # destination block, causing re-migration to append duplicates.
        def _blocks(text: str) -> list[str]:
            return [
                re.sub(r"\s+", " ", block.strip())
                for block in re.split(r"\n{2,}", text)
                if block.strip()
            ]

        source_blocks = _blocks(source_text)
        if not source_blocks:
            return True
        existing_blocks = set(_blocks(existing))
        return all(block in existing_blocks for block in source_blocks)

    @staticmethod
    def _split_memory_overflow(text: str) -> tuple[str, str]:
        # No-op fast path when the input already fits — the previous
        # implementation appended an overflow marker even when the overflow
        # slice was empty, so callers that defensively invoked the helper got
        # a marker-laden text with no archive to point at.
        if len(text) <= MAX_MEMORY_CHARS:
            return text, ""
        cutoff = text.rfind("\n\n", 0, MAX_MEMORY_CHARS)
        if cutoff < MAX_MEMORY_CHARS // 2:
            cutoff = MAX_MEMORY_CHARS
        overflow = text[cutoff:].lstrip()
        trimmed = text[:cutoff].rstrip()
        if not overflow:
            return trimmed, ""
        marker = (
            "\n\n## Migration overflow\n\n"
            f"Additional Hermes memory was archived under `{MEMORY_OVERFLOW_DIR}`.\n"
        )
        return trimmed + marker, overflow

    def _write_memory_overflow(self, text: str) -> None:
        destination = self.output_dir / "archive" / MEMORY_OVERFLOW_DIR / "MEMORY.overflow.md"
        if not self.options.apply:
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        self._record("memory-overflow", "Hermes memory", destination, "archived")

    def _archive_original_workspace_file(self, source: Path, filename: str) -> None:
        # Only invoked from _write_text_merge under apply=True, so no dry-run
        # branch is needed here.
        if not source.is_file():
            return
        destination = (
            self.output_dir / "archive" / "files" / "workspace-original" / filename
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        self._record(f"workspace-original/{filename}", source, destination, "archived")

    def _backup_file(self, path: Path) -> None:
        backup = path.with_name(f"{path.name}.backup.{self.timestamp}")
        backup.write_bytes(path.read_bytes())

    def _backup_dir(self, path: Path) -> None:
        backup = path.with_name(f"{path.name}.backup.{self.timestamp}")
        if backup.exists():
            shutil.rmtree(backup)
        shutil.copytree(path, backup)

    def _skill_compatibility_details(self, skill_dir: Path) -> dict[str, Any]:
        # Mirror the openclaw migrator's compatibility shape so downstream
        # tooling can read either report uniformly.
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
        # In any non-dict case we can't pluck name/description out of it.
        # Treat it as missing both fields rather than crashing the whole
        # migration with AttributeError on ``None.get("description")``.
        if not isinstance(frontmatter, dict):
            issues.append("missing frontmatter name")
            issues.append("missing frontmatter description")
        else:
            if not frontmatter.get("name"):
                issues.append("missing frontmatter name")
            if not frontmatter.get("description"):
                issues.append("missing frontmatter description")
        blocking = {
            "SKILL.md exceeds OpenSquilla skill file size limit",
            "missing frontmatter name",
        }
        loadable = not any(issue in blocking for issue in issues)
        # Keep the string and the bool in agreement. The previous variant
        # returned ``needs_review`` even when ``opensquilla_loadable`` was
        # ``False``, contradicting itself in the report.
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

    def _copy_skill_dir(self, source: Path, destination: Path) -> Path | None:
        target = destination
        if target.exists():
            if self.options.skill_conflict == "skip":
                return None
            if self.options.skill_conflict == "rename":
                index = 1
                while target.exists():
                    target = destination.with_name(f"{destination.name}-imported-{index}")
                    index += 1
            elif self.options.skill_conflict == "overwrite":
                # Keep an item-level backup so a skill that turns out to have
                # local edits is not lost to rmtree.
                self._backup_dir(target)
                shutil.rmtree(target)
        shutil.copytree(source, target)
        return target

    def _write_reports(self) -> None:
        if not self.options.apply:
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        report = self._report()
        (self.output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        counts: dict[str, int] = {}
        for item in report["items"]:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        lines = ["# Hermes Migration Summary", ""]
        lines.extend(f"- {key}: {value}" for key, value in sorted(counts.items()))
        (self.output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _migrate_config_and_env(self, selected: set[str]) -> None:
        config_path = self.source / "config.yaml"
        raw_config, yaml_error = _load_yaml_with_error(config_path)
        if yaml_error is not None:
            # Record an explicit error item instead of letting a hand-edited
            # config.yaml with bad syntax crash the whole migration. raw_config
            # is empty so downstream sections simply find nothing to migrate.
            self._record(
                "config.yaml",
                config_path,
                self.config_path,
                "error",
                f"could not parse config.yaml: {yaml_error}",
            )
        env_values = _load_env_file(self.source / ".env")
        if "skills" in selected:
            self._ensure_skills_extra_dir()
        if "model-config" in selected:
            self._migrate_model_config(raw_config)
        if "provider-keys" in selected or "search-config" in selected:
            self._migrate_env_values(env_values)
        if "mcp-servers" in selected:
            self._migrate_mcp_servers(raw_config)
        self._migrate_channels(raw_config, env_values, selected)

    @staticmethod
    def _extract_model_id(model_cfg: dict[str, Any]) -> str:
        # Hermes configs that came from OpenClaw or were authored against a
        # multi-model setup nest the model id inside a dict like
        # ``{primary: ..., fallback: ...}``. The previous implementation
        # stringified the whole dict via ``str()``, producing a Python
        # ``repr`` (``{'primary': 'claude', ...}``) that OpenSquilla cannot
        # use as a model id.
        raw = model_cfg.get("model")
        if raw is None:
            raw = model_cfg.get("default")
        if isinstance(raw, dict):
            for key in ("primary", "default", "main", "active"):
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return ""
        if isinstance(raw, str):
            return raw.strip()
        return ""

    def _migrate_model_config(self, raw_config: dict[str, Any]) -> None:
        raw_model = raw_config.get("model")
        model_cfg = raw_model if isinstance(raw_model, dict) else {}
        provider = str(model_cfg.get("provider") or "").strip()
        model = self._extract_model_id(model_cfg)
        if not provider and not model:
            self._record("model-config", self.source / "config.yaml", self.config_path, "skipped")
            return
        cfg = self._config()
        base_url = model_cfg.get("base_url") or model_cfg.get("baseUrl")
        target_provider = "openai" if provider == "custom" and base_url else provider
        details: dict[str, Any] = {}
        if provider:
            env_key = PROVIDER_ENV_KEYS.get(target_provider)
            # ``squilla_router.tier_profile`` (if set) must agree with
            # ``llm.provider`` after case normalisation, per
            # GatewayConfig._validate_squilla_router_tier_profile_provider.
            # When the new provider would violate that invariant we skip
            # the write — silently overwriting llm.provider here used to
            # make persist_config raise pydantic ValidationError and abort
            # the entire migration.
            existing_profile = getattr(
                getattr(cfg, "squilla_router", None), "tier_profile", None
            )
            normalized_profile = (existing_profile or "").strip().lower()
            normalized_new_provider = (target_provider or "").strip().lower()
            tier_profile_conflicts = bool(
                existing_profile
                and normalized_profile != normalized_new_provider
            )
            if env_key is not None and not tier_profile_conflicts:
                # Recognized OpenSquilla provider, no router-profile clash —
                # write provider and the corresponding env-var name.
                cfg.llm.provider = target_provider
                cfg.llm.api_key_env = env_key
            elif env_key is not None and tier_profile_conflicts:
                # Provider is known to OpenSquilla but the existing
                # squilla_router.tier_profile pins the home to a
                # different provider. Don't break that invariant; tell
                # the user what we skipped and why.
                preserved = cfg.llm.provider
                details["llm_provider_left_unchanged"] = preserved
                details["tier_profile_conflict"] = existing_profile
                details["manual_steps"] = [
                    (
                        f"hermes config.yaml asks for "
                        f"model.provider={target_provider!r}, but "
                        f"squilla_router.tier_profile is currently "
                        f"{existing_profile!r}. OpenSquilla requires these "
                        f"two to match. llm.provider was left as "
                        f"{preserved!r}. To switch providers, either clear "
                        f"squilla_router.tier_profile or set both fields "
                        f"explicitly with `opensquilla config set`."
                    )
                ]
            else:
                # Unrecognized provider: do NOT write it into
                # ``cfg.llm.provider``. Hermes uses values like ``auto``
                # (runtime auto-detect) that have no opensquilla-native
                # equivalent, and writing them verbatim used to break
                # ``persist_config`` because:
                #   - GatewayConfig validates ``llm.provider`` is one of
                #     the known providers; and
                #   - ``squilla_router.tier_profile`` (preserved across
                #     migrations) must agree with ``llm.provider`` after
                #     case normalisation.
                # Leaving the existing provider in place keeps both
                # invariants satisfied and lets the rest of the migration
                # apply. The user is told what was skipped and why.
                preserved = cfg.llm.provider
                details["unrecognized_provider"] = target_provider
                details["llm_provider_left_unchanged"] = preserved
                details["manual_steps"] = [
                    (
                        f"hermes config.yaml declared model.provider="
                        f"{target_provider!r}, which has no opensquilla "
                        f"equivalent (only known providers can be written "
                        f"to llm.provider). llm.provider was left as "
                        f"{preserved!r}. Set it explicitly via "
                        f"`opensquilla config set llm.provider <name>` "
                        f"if you want to switch."
                    )
                ]
        if model:
            cfg.llm.model = model
        if base_url:
            cfg.llm.base_url = str(base_url)
        self._config_changed = True
        self._record(
            "model-config",
            self.source / "config.yaml",
            self.config_path,
            "migrated" if self.options.apply else "planned",
            details=details or None,
        )

    def _migrate_env_values(self, env_values: dict[str, str]) -> None:
        migrated = 0
        for key, value in env_values.items():
            target_key = "BRAVE_SEARCH_API_KEY" if key == "BRAVE_API_KEY" else key
            if target_key not in SECRET_ENV_KEYS and target_key not in NON_SECRET_ENV_KEYS:
                continue
            if not value:
                continue
            is_secret = target_key in SECRET_ENV_KEYS
            if is_secret and not self.options.migrate_secrets:
                continue
            self._env_additions[target_key] = value
            migrated += 1
            if target_key == "BRAVE_SEARCH_API_KEY":
                cfg = self._config()
                cfg.search_provider = "brave"
                cfg.search_api_key_env = "BRAVE_SEARCH_API_KEY"
                self._config_changed = True
        self._record(
            "provider-keys",
            self.source / ".env",
            self.home / ".env",
            "migrated" if migrated and self.options.apply else "planned" if migrated else "skipped",
            (
                "pass --migrate-secrets to migrate recognized secrets"
                if any(key in SECRET_ENV_KEYS for key in env_values)
                and not self.options.migrate_secrets
                else ""
            ),
            {"migrated_keys": [SECRET_REDACTION] * migrated},
        )

    def _ensure_skills_extra_dir(self) -> None:
        destination_root = str(self.home / "skills" / SKILL_IMPORT_DIRNAME)
        cfg = self._config()
        extra_dirs = list(cfg.skills.extra_dirs)
        if destination_root not in extra_dirs:
            extra_dirs.append(destination_root)
            cfg.skills.extra_dirs = extra_dirs
            self._config_changed = True
            self._record(
                "skills-config",
                self.source / "skills",
                self.config_path,
                "migrated" if self.options.apply else "planned",
            )

    def _migrate_mcp_servers(self, raw_config: dict[str, Any]) -> None:
        raw_mcp = raw_config.get("mcp")
        raw_servers = raw_mcp.get("servers") if isinstance(raw_mcp, dict) else None
        # Hermes config.yaml supports both shapes:
        #   mcp.servers: {name: {command: ...}}        (dict-of-dicts)
        #   mcp.servers: [{name: x, command: y}, ...]  (list-of-dicts)
        # The earlier implementation only accepted the dict form and
        # silently dropped the list form along with every server in it.
        servers: dict[str, dict[str, Any]] = {}
        dropped: list[str] = []
        if isinstance(raw_servers, dict):
            for name, raw in raw_servers.items():
                if isinstance(raw, dict):
                    servers[str(name)] = raw
                else:
                    dropped.append(str(name))
        elif isinstance(raw_servers, list):
            for idx, entry in enumerate(raw_servers):
                if not isinstance(entry, dict):
                    dropped.append(f"index {idx}")
                    continue
                name = entry.get("name") or entry.get("id")
                if not isinstance(name, str) or not name.strip():
                    dropped.append(f"index {idx} (missing name)")
                    continue
                servers[name] = {k: v for k, v in entry.items() if k not in ("name", "id")}
        if not servers:
            reason = "no MCP servers found" if not dropped else (
                f"all {len(dropped)} server entries were malformed"
            )
            self._record(
                "mcp-servers",
                self.source / "config.yaml",
                self.config_path,
                "skipped",
                reason,
                details={"dropped_entries": dropped} if dropped else None,
            )
            return
        # Build the imported entries first, then upsert by name into the
        # existing list. The previous implementation replaced
        # ``cfg.mcp.servers`` wholesale, silently destroying any
        # pre-existing opensquilla MCP servers the user had configured.
        imported: list[MCPServerEntry] = []
        for name, raw in servers.items():
            payload: dict[str, Any] = {"name": name}
            for key in ("command", "args", "env", "url"):
                if key in raw:
                    payload[key] = raw[key]
            if payload.get("url") and not payload.get("command"):
                payload["transport"] = "sse"
            elif payload.get("command"):
                payload["transport"] = "stdio"
            imported.append(MCPServerEntry.model_validate(payload))
        cfg = self._config()
        existing_servers = list(cfg.mcp.servers)
        existing_by_name = {s.name: idx for idx, s in enumerate(existing_servers)}
        replaced: list[str] = []
        added: list[str] = []
        for entry in imported:
            if entry.name in existing_by_name:
                existing_servers[existing_by_name[entry.name]] = entry
                replaced.append(entry.name)
            else:
                existing_servers.append(entry)
                added.append(entry.name)
        # Preserve the user's explicit choice on ``mcp.enabled``. A
        # previous implementation flipped it to ``True`` unconditionally,
        # which silently re-enabled MCP for users who had deliberately
        # turned it off. We only flip ``False`` -> ``True`` when MCP
        # has never been configured (defaulted False) and report any
        # leave-as-is in the migration record.
        mcp_was_disabled = not cfg.mcp.enabled
        mcp_enabled_left_disabled = False
        if mcp_was_disabled:
            # Heuristic: if the user previously had no servers AND mcp
            # was disabled, treat that as "MCP defaulted off" and turn
            # it on along with the imported servers. If they HAD
            # servers and explicitly disabled MCP, respect that choice.
            had_servers = bool(existing_by_name)
            if not had_servers:
                cfg.mcp.enabled = True
            else:
                mcp_enabled_left_disabled = True
        cfg.mcp.servers = existing_servers
        self._config_changed = True
        details: dict[str, Any] = {
            "server_count": len(imported),
            "added": added,
            "replaced": replaced,
            "preserved_existing": [
                s.name for s in existing_servers if s.name not in {e.name for e in imported}
            ],
        }
        if mcp_enabled_left_disabled:
            details["mcp_enabled_left_disabled"] = True
            details["manual_steps"] = [
                "MCP is disabled in your OpenSquilla config but you have "
                "configured servers. Set `mcp.enabled = true` via "
                "`opensquilla config set mcp.enabled true` to activate them."
            ]
        if dropped:
            details["dropped_entries"] = dropped
        self._record(
            "mcp-servers",
            self.source / "config.yaml",
            self.config_path,
            "migrated" if self.options.apply else "planned",
            details=details,
        )

    def _migrate_channels(
        self,
        raw_config: dict[str, Any],
        env_values: dict[str, str],
        selected: set[str],
    ) -> None:
        if not self.options.migrate_secrets:
            self._record(
                "channels",
                self.source / ".env",
                self.config_path,
                "skipped",
                "pass --migrate-secrets to migrate channel tokens",
            )
            return
        raw_entries = [
            entry.model_dump(mode="python") for entry in self._config().channels.channels
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

        if "telegram-settings" in selected and env_values.get("TELEGRAM_BOT_TOKEN"):
            telegram = raw_config.get("telegram", {})
            telegram_cfg = telegram if isinstance(telegram, dict) else {}
            upsert(
                {
                    "name": "hermes-telegram",
                    "type": "telegram",
                    "token": env_values["TELEGRAM_BOT_TOKEN"],
                    "default_chat_id": str(telegram_cfg.get("default_chat_id", "")),
                }
            )
        if "discord-settings" in selected and env_values.get("DISCORD_BOT_TOKEN"):
            discord = raw_config.get("discord", {})
            discord_cfg = discord if isinstance(discord, dict) else {}
            upsert(
                {
                    "name": "hermes-discord",
                    "type": "discord",
                    "token": env_values["DISCORD_BOT_TOKEN"],
                    "default_channel_id": str(discord_cfg.get("default_channel_id", "")),
                }
            )
        if "slack-settings" in selected and env_values.get("SLACK_BOT_TOKEN"):
            slack = raw_config.get("slack", {})
            slack_cfg = slack if isinstance(slack, dict) else {}
            upsert(
                {
                    "name": "hermes-slack",
                    "type": "slack",
                    "token": env_values["SLACK_BOT_TOKEN"],
                    "slack_channel_id": str(slack_cfg.get("channel_id", "")),
                }
            )
        if changed:
            cfg = self._config()
            cfg.channels = ChannelsConfig.model_validate({"channels": raw_entries})
            self._config_changed = True
            self._record(
                "channels",
                self.source / ".env",
                self.config_path,
                "migrated" if self.options.apply else "planned",
            )
        else:
            # Always emit a channels record so the user sees the migrator
            # checked even if no tokens were found or all channel options
            # were excluded. Previously a `--migrate-secrets` run against a
            # source with no `.env` (or no channel tokens) produced silence,
            # leaving users unsure whether channels were considered at all.
            self._record(
                "channels",
                self.source / ".env",
                self.config_path,
                "skipped",
                "no channel tokens found in source",
            )

    def _write_env(self) -> None:
        if not self.options.apply or not self._env_additions:
            return
        env_path = self.home / ".env"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        existing_lines = (
            env_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if env_path.exists()
            else []
        )
        write_secret_env_file(
            env_path, merge_env_lines(existing_lines, self._env_additions)
        )

    def _write_config(self) -> None:
        if self.options.apply and self._config_changed and self._config_obj is not None:
            persist_config(self._config_obj, path=self.config_path, backup=True)

    def _archive_unsupported(self, selected: set[str]) -> None:
        if "archive" not in selected:
            return
        for name, kind in ARCHIVE_SOURCE_ARTIFACTS.items():
            source = self.source / name
            if not source.exists():
                continue
            destination = self.output_dir / "archive" / "files" / name
            if self.options.apply:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, destination)
            self._record(
                kind,
                source,
                destination,
                "archived" if self.options.apply else "planned",
            )
        for name in sorted(SKIP_SOURCE_ARTIFACTS):
            source = self.source / name
            if source.exists():
                self._record(
                    name,
                    source,
                    None,
                    "skipped",
                    "runtime or credential artifact is not imported",
                )

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
                details=details or {},
            )
        )

    def _report(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "target_home": str(self.home),
            "output_dir": str(self.output_dir),
            "apply": self.options.apply,
            "items": [asdict(item) for item in self.items],
        }
