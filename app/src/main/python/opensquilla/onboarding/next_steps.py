"""Secret-safe next-step text for onboarding output."""

from __future__ import annotations

import os
import platform
import re
import shlex
from pathlib import Path
from typing import Any

from opensquilla.onboarding.image_generation_specs import (
    get_image_generation_provider_setup_spec,
)
from opensquilla.onboarding.search_specs import get_search_provider_setup_spec
from opensquilla.onboarding.section_status import SECTION_STATUS_DISPLAY
from opensquilla.onboarding.setup_paths import web_setup_url
from opensquilla.onboarding.status import get_onboarding_status
from opensquilla.paths import default_opensquilla_home

_KEY_URLS = {
    "tokenrhythm": "https://tokenrhythm.studio/account/keys",
    "openrouter": "https://openrouter.ai/keys",
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
}
_CAPABILITY_SECTIONS = (
    "search",
    "channels",
    "image_generation",
    "audio",
    "memory_embedding",
)
# String-keyed view of the shared status words (section_status is the single
# source of truth); derived so the summary lookup below can use raw ``.value``
# strings without drifting from the ``onboard status`` table wording.
_CAPABILITY_STATUS_DISPLAY: dict[str, str] = {
    status.value: display for status, display in SECTION_STATUS_DISPLAY.items()
}
_HEADLESS_SECTION_ALIASES = {
    "llm": "provider",
    "providers": "provider",
    "channel": "channels",
    "image_generation": "image-generation",
    "audio": "audio",
    "llm-ensemble": "ensemble",
    "llm_ensemble": "ensemble",
    "memory_embedding": "memory-embedding",
}
_HEADLESS_SETUP_COMMANDS = {
    "provider": (
        "Provider recipes",
        "opensquilla onboard catalog providers",
    ),
    "router": (
        "Headless router",
        "opensquilla onboard configure router --router recommended --default-tier c1",
    ),
    "ensemble": (
        "Headless ensemble",
        "opensquilla onboard configure ensemble --enabled",
    ),
    "channels": (
        "Channel recipes",
        "opensquilla onboard catalog channels",
    ),
    "search": (
        "Headless search",
        "opensquilla onboard configure search --search-provider duckduckgo",
    ),
    "image-generation": (
        "Image recipes",
        "opensquilla onboard catalog image",
    ),
    "audio": (
        "Audio recipes",
        "opensquilla onboard catalog audio",
    ),
    "memory-embedding": (
        "Headless memory embedding",
        "opensquilla onboard configure memory --memory-provider auto",
    ),
}


def _normalize_headless_section(section: str) -> str:
    normalized = section.strip().lower().replace("_", "-")
    return _HEADLESS_SECTION_ALIASES.get(normalized, normalized)


def headless_setup_commands(section: str) -> list[tuple[str, str]]:
    normalized = _normalize_headless_section(section)
    commands: list[tuple[str, str]] = []
    entry = _HEADLESS_SETUP_COMMANDS.get(normalized)
    if entry:
        commands.append(entry)
    return commands


def headless_setup_command(section: str) -> tuple[str, str] | None:
    commands = headless_setup_commands(section)
    return commands[-1] if commands else None


def setup_catalog_command(config_arg: str = "") -> tuple[str, str]:
    return "Explore options", f"opensquilla onboard catalog{config_arg}"


def _is_windows() -> bool:
    return platform.system().lower().startswith("win")


# Mirror of the character class ``shlex.quote`` treats as safe, so POSIX and
# PowerShell quoting agree on *when* to quote and only differ in *how*.
_SHELL_UNSAFE_RE = re.compile(r"[^\w@%+=:,./-]", re.ASCII)

# PowerShell's tokenizer treats the Unicode smart quotes U+2018-U+201B as
# single-quote delimiters too; doubling is the only escape for any of them.
_POWERSHELL_QUOTE_RE = re.compile(r"['‘’‚‛]")


def quote_cli_arg(value: str | Path) -> str:
    """Quote one copy-paste CLI argument for the operator's shell.

    POSIX shells get ``shlex.quote``. On Windows the copyable commands are
    presented as PowerShell (matching the env hints below), whose
    single-quoted strings are literal except for doubled single quotes —
    ``shlex.quote``'s ``'"'"'`` escape is not valid there.
    """
    text = str(value)
    if not _is_windows():
        return shlex.quote(text)
    if not text:
        return "''"
    if _SHELL_UNSAFE_RE.search(text) is None:
        return text
    return "'" + _POWERSHELL_QUOTE_RE.sub(lambda match: match.group(0) * 2, text) + "'"


def persistent_env_file() -> str:
    """Path of the supported persistent ``.env`` file (names only, no values)."""
    return str(default_opensquilla_home() / ".env")


def set_env_command(env_key: str) -> str:
    """The bare set-this-env-var command for the operator's shell.

    Machine-readable surfaces (``onboard status --json`` and the RPC status
    payload) must carry only this command — no human labels.
    """
    if _is_windows():
        return f'$env:{env_key} = "<your-key>"'
    return f'export {env_key}="<your-key>"'


def human_env_command(command: str) -> str:
    """Label a bare environment command for human-facing output."""
    return f"PowerShell: {command}" if _is_windows() else command


def set_env_hint(env_key: str) -> str:
    """Human-facing variant of :func:`set_env_command` (labels the shell)."""
    return human_env_command(set_env_command(env_key))


def _set_env_hint(env_key: str) -> str:
    return set_env_hint(env_key)


def env_recovery_commands(status: Any) -> list[dict[str, str]]:
    candidates = [
        ("llm", "Set provider key", status.llm_source, status.llm_env_key),
        ("search", "Set search key", status.search_source, status.search_env_key),
        (
            "image_generation",
            "Set image key",
            status.image_generation_source,
            status.image_generation_env_key,
        ),
        ("audio", "Set audio key", status.audio_source, status.audio_env_key),
        (
            "memory_embedding",
            "Set memory key",
            status.memory_embedding_source,
            status.memory_embedding_env_key,
        ),
    ]
    seen_env_keys: set[str] = set()

    def priority(
        item: tuple[int, tuple[str, str, str, str]],
    ) -> tuple[int, int]:
        index, (section, _label, _source, _env_key) = item
        detail = status.section_details.get(section, {})
        return (0 if detail.get("blocking") else 1, index)

    commands: list[dict[str, str]] = []
    for _index, (section, label, source, env_key) in sorted(
        enumerate(candidates),
        key=priority,
    ):
        if source != "missing_env" or not env_key or env_key in seen_env_keys:
            continue
        seen_env_keys.add(env_key)
        commands.append(
            {
                "section": section,
                "label": label,
                # Machine-readable field: the bare command only. Human
                # renderers wanting a shell label wrap it via human_env_command.
                "command": set_env_command(env_key),
            }
        )
    return commands


def _missing_env_warning(surface: str, env_key: str) -> str:
    return (
        f"{surface}: ${env_key} is not set in this shell. "
        "The config saved the environment-variable reference, but this feature "
        "will not work until the gateway is started with that variable set. "
        f"Persist it by adding {env_key}=<your-key> to {persistent_env_file()}."
    )


def _config_cli_arg(config_path: str | Path | None) -> str:
    if not config_path:
        return ""
    return f" --config {quote_cli_arg(config_path)}"


def _image_generation_provider_id(config: Any) -> str:
    primary = str(getattr(config.image_generation, "primary", "") or "")
    provider_id, sep, _model = primary.partition("/")
    if sep and provider_id:
        return provider_id
    return "openai"


def _capability_section_view(status: Any, name: str) -> tuple[str, str, str, bool]:
    """Resolve one capability section to ``(label, display, value, needs_action)``.

    Single source for the "Capabilities:" summary and the "Fix next:"
    checklist: both lines print in the same next-steps block from the same
    status object, so their label and status wording must come from one
    resolver — a one-sided edit must not make the two lines disagree.
    """
    detail = status.section_details.get(name, {})
    label = str(detail.get("label") or name.replace("_", " ").title())
    state = status.sections.get(name)
    value = str(getattr(state, "value", detail.get("status") or ""))
    needs_action = bool(detail.get("blocking") or detail.get("actionRequired"))
    if needs_action:
        display = "Needs action"
    else:
        display_value = value or "optional"
        display = _CAPABILITY_STATUS_DISPLAY.get(
            display_value, display_value.replace("_", " ").title()
        )
    return label, display, value, needs_action


def _capabilities_summary(status: Any) -> str:
    parts: list[str] = []
    for name in _CAPABILITY_SECTIONS:
        label, display, _value, _needs_action = _capability_section_view(status, name)
        parts.append(f"{label}={display}")
    return " | ".join(parts)


def _capability_fix_lines(status: Any, config_arg: str) -> list[str]:
    """One actionable line per capability that needs operator attention.

    Deliberate opt-outs ("Later") are not nagged about; only blocking,
    missing, degraded, or unverifiable capabilities get a line, and each
    names the exact command that fixes it. Audio has no `configure audio`
    path, so it points at the catalog command recorded once in
    ``_HEADLESS_SETUP_COMMANDS`` instead of a command that exits 2.
    """
    fix_lines: list[str] = []
    for name in _CAPABILITY_SECTIONS:
        label, display, value, needs_action = _capability_section_view(status, name)
        if not needs_action and value not in {"missing", "degraded", "unknown"}:
            continue
        slug = _normalize_headless_section(name)
        if slug == "audio":
            command = f"{_HEADLESS_SETUP_COMMANDS['audio'][1]}{config_arg}"
        else:
            command = f"opensquilla onboard configure {slug}{config_arg}"
        fix_lines.append(f"  {label} ({display}): {command}")
    return fix_lines


def env_reference_warnings(config: Any) -> list[str]:
    """Return operator-facing warnings for saved env references not visible now."""
    warnings: list[str] = []
    status = get_onboarding_status(config)

    llm = config.llm
    llm_env_key = str(getattr(llm, "api_key_env", "") or "")
    if status.llm_source == "missing_env" and llm_env_key:
        warnings.append(_missing_env_warning("LLM provider", llm_env_key))

    search_provider = str(getattr(config, "search_provider", "") or "")
    search_env_key = str(getattr(config, "search_api_key_env", "") or "")
    if search_provider and search_env_key and not getattr(config, "search_api_key", ""):
        try:
            search_spec = get_search_provider_setup_spec(search_provider)
        except KeyError:
            search_spec = None
        if (
            search_spec is not None
            and search_spec.requires_api_key
            and not os.environ.get(search_env_key)
        ):
            warnings.append(_missing_env_warning("Search provider", search_env_key))

    if status.image_generation_enabled and not status.image_generation_configured:
        provider_id = _image_generation_provider_id(config)
        try:
            image_spec = get_image_generation_provider_setup_spec(provider_id)
        except KeyError:
            image_spec = None
        providers = getattr(getattr(config, "image_generation", None), "providers", None)
        provider_cfg = getattr(providers, provider_id, None) if providers is not None else None
        image_env_key = str(getattr(provider_cfg, "api_key_env", "") or "").strip()
        if not image_env_key and image_spec is not None:
            image_env_key = str(getattr(image_spec, "env_key", "") or "").strip()
        if image_env_key and not os.environ.get(image_env_key):
            warnings.append(_missing_env_warning("Image generation provider", image_env_key))

    audio = getattr(config, "audio", None)
    if getattr(audio, "enabled", False) and not status.audio_configured:
        providers = getattr(audio, "providers", None)
        provider_cfg = getattr(providers, "elevenlabs", None) if providers is not None else None
        audio_env_key = str(getattr(provider_cfg, "api_key_env", "") or "").strip()
        if audio_env_key and not os.environ.get(audio_env_key):
            warnings.append(_missing_env_warning("Audio provider", audio_env_key))

    embedding = getattr(getattr(config, "memory", None), "embedding", None)
    embedding_provider = str(getattr(embedding, "requested_provider", "") or "")
    if embedding_provider in {"openai", "openai-compatible"}:
        remote = getattr(embedding, "remote", None)
        memory_env_key = str(getattr(remote, "api_key_env", "") or "").strip()
        memory_key = str(getattr(remote, "api_key", "") or "") or str(
            getattr(embedding, "api_key", "") or ""
        )
        if memory_env_key and not memory_key and not os.environ.get(memory_env_key):
            warnings.append(_missing_env_warning("Memory embedding", memory_env_key))

    return warnings


def format_next_steps(config: Any, *, config_path: str | Path | None = None) -> str:
    status = get_onboarding_status(config)
    llm = config.llm
    router = config.squilla_router
    path = str(config_path or status.config_path or getattr(config, "config_path", ""))
    provider = str(getattr(llm, "provider", "") or "")
    model = str(getattr(llm, "model", "") or "")
    env_key = str(getattr(llm, "api_key_env", "") or "")
    router_default = str(getattr(router, "default_tier", "") or "c1")
    router_line = (
        "  Router: disabled"
        if not router.enabled
        else f"  Router: SquillaRouter, default={router_default}"
    )
    config_arg = _config_cli_arg(config_path)
    key_source = status.llm_source
    if key_source == "env" and env_key:
        key_line = f"Key: ${env_key}"
    elif key_source == "missing_env" and env_key:
        key_line = f"Key: ${env_key} is not set in this shell"
    elif key_source == "explicit":
        key_line = "Key: stored in config"
    else:
        key_line = "Key: not required" if status.llm_configured else "Key: not configured"

    lines = [
        "Configuration summary:",
        f"  Config: {path}",
        f"  LLM: {provider} / {model}",
        f"  {key_line}",
        router_line,
        f"  Capabilities: {_capabilities_summary(status)}",
        "",
        "Commands:",
        f"  Run gateway now: opensquilla gateway run{config_arg}",
        f"  Start gateway in background: opensquilla gateway start --json{config_arg}",
        f"  Restart running gateway: opensquilla gateway restart --json{config_arg}",
        f"  Change settings anytime: opensquilla onboard configure{config_arg}",
    ]
    if key_source == "missing_env" and env_key:
        lines.append(f"  Set key before starting gateway: {set_env_hint(env_key)}")
        lines.append(
            f"  Persist key across restarts: add {env_key}=<your-key> to "
            f"{persistent_env_file()}"
        )
    fix_lines = _capability_fix_lines(status, config_arg)
    if fix_lines:
        lines.extend(["", "Fix next:"])
        lines.extend(fix_lines)
    lines.extend(["", "Reference:"])
    setup_url = web_setup_url(config)
    if setup_url:
        lines.append(f"  Web UI: {setup_url}")
    key_url = _KEY_URLS.get(provider)
    if key_url:
        lines.append(f"  Provider keys: {key_url}")
    return "\n".join(lines)
