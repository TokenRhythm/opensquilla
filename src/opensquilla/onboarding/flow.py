"""Coordinate interactive and non-interactive onboarding flows."""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import os
import re
import sys
import tempfile
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, cast

from opensquilla.onboarding.channel_specs import (
    ChannelSetupField,
    ChannelSetupSpec,
    get_channel_setup_spec,
    list_channel_setup_specs,
)
from opensquilla.onboarding.config_store import (
    PersistResult,
    default_config_path,
    load_config,
    persist_config,
    resolve_config_path,
)
from opensquilla.onboarding.errors import UserCancelledError
from opensquilla.onboarding.image_generation_specs import (
    ImageGenerationProviderSetupSpec,
    get_image_generation_provider_setup_spec,
    list_image_generation_provider_setup_specs,
)
from opensquilla.onboarding.memory_embedding_specs import (
    MemoryEmbeddingProviderSetupSpec,
    get_memory_embedding_provider_setup_spec,
    list_memory_embedding_provider_setup_specs,
)
from opensquilla.onboarding.mutations import (
    _router_tiers_hand_customized,
    upsert_channel,
    upsert_image_generation_provider,
    upsert_llm_ensemble,
    upsert_llm_provider,
    upsert_memory_embedding,
    upsert_router,
    upsert_search_provider,
)
from opensquilla.onboarding.next_steps import (
    headless_setup_command,
    headless_setup_commands,
    quote_cli_arg,
    setup_catalog_command,
)
from opensquilla.onboarding.provider_specs import (
    get_provider_setup_spec,
    list_provider_setup_specs,
)
from opensquilla.onboarding.search_specs import (
    get_search_provider_setup_spec,
    list_search_provider_setup_specs,
)
from opensquilla.onboarding.section_status import SECTION_STATUS_DISPLAY
from opensquilla.onboarding.setup_engine import (
    ENSEMBLE_SECTION_ALIASES,
    IMAGE_GENERATION_SECTION_ALIASES,
    MEMORY_EMBEDDING_SECTION_ALIASES,
)
from opensquilla.onboarding.setup_paths import web_setup_url
from opensquilla.onboarding.status import get_onboarding_status
from opensquilla.router_tiers import (
    DEFAULT_TEXT_TIER,
    IMAGE_TIER,
    TEXT_TIERS,
    normalize_text_tier,
)
from opensquilla.search.types import DEFAULT_SEARCH_MAX_RESULTS
from opensquilla.ui import (
    ACCENT,
    ACCENT_DIM,
    ACCENT_SOFT,
    banner_panel,
    console,
    markup_escape,
    questionary_style,
    warning_panel,
)

_QSTYLE = None


def _qs():
    global _QSTYLE
    if _QSTYLE is None:
        built = questionary_style()
        if built is None:
            return None
        _QSTYLE = built
    return _QSTYLE


def _styled(q):
    """Wrap the questionary module so every prompt inherits the brand style.

    When ``questionary_style()`` returns ``None`` (e.g. test stub or missing
    optional dep) the wrapper passes calls through unchanged.
    """
    from types import SimpleNamespace

    style = _qs()
    if style is None:
        return q
    try:
        import questionary.prompts.common as questionary_common

        questionary_common.INDICATOR_SELECTED = "☑"
        questionary_common.INDICATOR_UNSELECTED = "☐"
    except Exception:
        pass

    def _wrap(name):
        fn = getattr(q, name)
        return lambda *a, **kw: fn(*a, **{"style": style, **kw})

    return SimpleNamespace(
        select=_wrap("select"),
        text=_wrap("text"),
        confirm=_wrap("confirm"),
        password=_wrap("password"),
        checkbox=_wrap("checkbox"),
        Choice=getattr(q, "Choice", None),
    )


@dataclass(frozen=True)
class OnboardOptions:
    skip_channels: bool = False
    skip_search: bool = False
    skip_image_generation: bool = False
    if_needed: bool = False
    provider_id: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    proxy: str | None = None
    # ``None`` = not passed (an omitted ``--router``): keep the stored router
    # state on a configured install. The wizard applies the recommended
    # profile only when no working provider is stored yet (first-run walk).
    router_mode: str | None = None
    minimal: bool = False
    skip_migration: bool = False
    config_path: str | Path | None = None
    # Internal: a scoped section entry (``onboard configure provider`` or the
    # hub's Provider item). Skips the first-run banner, the "Press Enter"
    # start gate, and the trailing action-required prompts of the full walk.
    scoped_section: bool = False


def _is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _flush_stdin_typeahead() -> None:
    """Drop keys typed before the visible setup prompt was flushed."""
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            return
        msvcrt_mod = cast(Any, msvcrt)
        while msvcrt_mod.kbhit():
            msvcrt_mod.getwch()
        return

    if not sys.stdin.isatty():
        return
    try:
        import termios
    except ImportError:
        return
    termios_mod = cast(Any, termios)
    termios_mod.tcflush(sys.stdin, termios_mod.TCIFLUSH)


def _wait_for_setup_start() -> None:
    console.print(f"[{ACCENT}]◆[/] Press Enter to start setup")
    flush = getattr(getattr(console, "file", None), "flush", None)
    if callable(flush):
        flush()
    _flush_stdin_typeahead()
    input()


def run_noninteractive_provider_configure(
    provider_id: str,
    values: dict[str, Any],
    *,
    path: str | Path | None = None,
) -> PersistResult:
    from opensquilla.onboarding.setup_engine import SetupEngine

    engine = SetupEngine(path=path)
    engine.apply(
        "provider",
        {
            "providerId": provider_id,
            "model": values.get("model", ""),
            "apiKey": values.get("api_key", ""),
            "apiKeyEnv": values.get("api_key_env", ""),
            "baseUrl": values.get("base_url", ""),
            "proxy": values.get("proxy", ""),
        },
    )
    router_mode = values.get("router", "")
    if router_mode:
        engine.apply("router", {"mode": router_mode})
    return engine.persist()


def _config_cli_arg(config_path: str | Path | None) -> str:
    if config_path is None:
        return ""
    # quote_cli_arg is platform-aware (PowerShell on Windows): the resume
    # hints printed here must paste cleanly in the same shell as the
    # next-steps/status output built from the sibling helper in next_steps.
    return f" --config {quote_cli_arg(config_path)}"


def _first_blocking_setup_section(cfg: Any) -> str:
    status = get_onboarding_status(cfg)
    for name in status.sections:
        if status.section_details.get(name, {}).get("blocking"):
            return name
    return "provider"


def _print_noninteractive_hint(
    cfg: Any,
    config_path: str | Path | None = None,
    *,
    section: str | None = None,
) -> PersistResult:
    if config_path is None and isinstance(cfg, (str, Path)):
        config_path = cfg
        cfg = load_config(config_path)
    config_arg = _config_cli_arg(config_path)
    normalized = (section or _first_blocking_setup_section(cfg)).replace("_", "-")
    headless_commands = headless_setup_commands(normalized)
    if not headless_commands:
        headless_commands = [
            headless_setup_command("provider")
            or (
                "Provider recipes",
                "opensquilla onboard catalog providers",
            )
        ]
    guided_command = (
        f"opensquilla onboard configure {normalized}{config_arg}"
        if section
        else f"opensquilla onboard --if-needed{config_arg}"
    )
    lines = [
        "Onboarding requires a TTY-compatible interactive terminal for the guided wizard.",
        "Use a runnable setup path from this shell:",
    ]
    setup_url = web_setup_url(cfg)
    if setup_url:
        lines.append(f"  Web UI: opensquilla gateway run{config_arg} -> {setup_url}")
    catalog_label, catalog_command = setup_catalog_command(config_arg)
    lines.append(f"  {catalog_label}: {catalog_command}")
    for label, command in headless_commands:
        lines.append(f"  {label}: {command}{config_arg}")
    lines.extend(
        [
            f"  Guided CLI: {guided_command} (interactive terminal only)",
            f"  Check status: opensquilla onboard status{config_arg}",
        ]
    )
    print("\n".join(lines))
    return PersistResult(
        path=default_config_path(),
        backup_path=None,
        restart_required=False,
        warnings=["tty_required"],
    )


def _ask_or_cancel(prompt, section: str) -> Any:
    """Run a questionary prompt and convert a ``None`` answer into ``UserCancelledError``.

    ``questionary`` returns ``None`` when the user aborts (Ctrl+C / Esc). Letting
    that flow into downstream validation or upsert calls produces misleading
    error messages — convert it to a typed cancellation at the input boundary
    so callers can route the user back to a resumable state.
    """
    value = prompt.ask()
    if value is None:
        raise UserCancelledError(section=section)
    return value


def _ask_provider_choice(questionary, options: OnboardOptions):
    if options.provider_id:
        spec = get_provider_setup_spec(options.provider_id)
        return spec, spec.provider_id
    supported = [s for s in list_provider_setup_specs() if s.runtime_supported]
    choices = [f"{s.provider_id} ({s.label})" for s in supported]
    default = next(
        (choice for choice in choices if choice.startswith("tokenrhythm ")), None
    ) or next((choice for choice in choices if choice.startswith("openrouter ")), None)
    pid = _ask_or_cancel(
        questionary.select(
            "LLM provider",
            choices=choices,
            default=default,
            # ~30 entries: let the operator type to filter instead of
            # arrowing through the list (requires questionary >= 2.1).
            use_search_filter=True,
            use_jk_keys=False,
        ),
        section="provider",
    )
    pid_clean = pid.split(" ")[0]
    return get_provider_setup_spec(pid_clean), pid_clean


def _required_value(label: str):
    def _validate(value: str) -> bool | str:
        if str(value or "").strip():
            return True
        return f"{label} is required"

    return _validate


_PASTE_API_KEY_CHOICE = "Paste API key now"
_DETECTED_ENV_SUFFIX = " (detected)"
_TERMINAL_ESCAPE_RE = re.compile(r"\x1b")
_LEADING_TERMINAL_KEY_RE = re.compile(r"^\[[0-9;?]+~")


def _api_key_env_choice(env_key: str, *, detected: bool = False) -> str:
    suffix = _DETECTED_ENV_SUFFIX if detected else ""
    return f"Use environment variable {env_key}{suffix}"


def _api_key_env_from_choice(choice: str) -> str:
    prefix = "Use environment variable "
    if not choice.startswith(prefix):
        return ""
    env_key = choice[len(prefix) :]
    if env_key.endswith(_DETECTED_ENV_SUFFIX):
        env_key = env_key[: -len(_DETECTED_ENV_SUFFIX)]
    return env_key


def _api_key_source_choices(env_key: str) -> list[str]:
    choices = [_PASTE_API_KEY_CHOICE]
    if env_key:
        choices.append(
            _api_key_env_choice(env_key, detected=bool(os.environ.get(env_key)))
        )
    return choices


def _api_key_source_default(env_key: str) -> str:
    if env_key and os.environ.get(env_key):
        return _api_key_env_choice(env_key, detected=True)
    return _PASTE_API_KEY_CHOICE


def _secret_paste_error(value: str) -> str | None:
    stripped = str(value or "").strip()
    if _TERMINAL_ESCAPE_RE.search(stripped) or _LEADING_TERMINAL_KEY_RE.search(
        stripped
    ):
        return (
            "Paste was not read correctly by this terminal. Use right-click, "
            "Shift+Insert, or the environment variable option."
        )
    return None


def _secret_value_validator(label: str):
    required = _required_value(label)

    def _validate(value: str) -> bool | str:
        result = cast(bool | str, required(value))
        if result is not True:
            return result
        paste_error = _secret_paste_error(value)
        if paste_error is not None:
            return paste_error
        return True

    return _validate


def _secret_keep_current_validator(label: str):
    """Secret validator for edit prompts: blank keeps the stored value."""

    def _validate(value: str) -> bool | str:
        if not str(value or "").strip():
            return True
        paste_error = _secret_paste_error(value)
        if paste_error is not None:
            return paste_error
        return True

    return _validate


def _int_value_validator(label: str, *, minimum: int | None = None):
    """Re-prompt on non-numeric input instead of crashing at ``int()`` time.

    Blank input stays valid — callers coerce it to their own default, so an
    accepted-as-is Enter keeps the quick-start path free of new friction.
    """

    def _validate(value: str) -> bool | str:
        stripped = str(value or "").strip()
        if not stripped:
            return True
        try:
            parsed = int(stripped)
        except ValueError:
            return f"{label} must be a whole number"
        if minimum is not None and parsed < minimum:
            return f"{label} must be at least {minimum}"
        return True

    return _validate


def _coerce_channel_prompt_value(
    field: ChannelSetupField, raw: str
) -> tuple[Any, str | None]:
    """Coerce one wizard answer with the headless ``--field`` semantics.

    The wizard and ``onboard configure channels --field`` must accept exactly
    the same spellings — and reject with the same wording — for the same spec
    field. The headless coercer (``cli.channel_fields.coerce_channel_field_value``)
    lives in the CLI package, which the architecture contract forbids importing
    from ``onboarding``, so this is its value-for-value mirror, held in lockstep
    by a parity test; the only deliberate difference is that rejections name the
    field label instead of the ``--field`` flag the wizard user never typed.

    Returns ``(value, None)`` on success, ``(None, message)`` with the
    re-prompt message on rejection.
    """
    if field.field_type == "int":
        try:
            return int(raw), None
        except ValueError:
            return None, f"{field.label} expects an integer, got {raw!r}"
    if field.field_type == "float":
        try:
            return float(raw), None
        except ValueError:
            return None, f"{field.label} expects a number, got {raw!r}"
    return raw, None


def _channel_number_validator(field: ChannelSetupField):
    """Re-prompt on input the channel-field coercer rejects instead of crashing.

    Blank input stays valid — the caller keeps the prompt's displayed
    default, matching the headless contract where an omitted ``--field``
    keeps the stored/spec value.
    """

    def _validate(value: str) -> bool | str:
        stripped = str(value or "").strip()
        if not stripped:
            return True
        _value, error = _coerce_channel_prompt_value(field, stripped)
        return True if error is None else error

    return _validate


def _base_url_validator(label: str = "Base URL"):
    def _validate(value: str) -> bool | str:
        stripped = str(value or "").strip()
        if not stripped:
            return f"{label} is required for this provider"
        if not stripped.lower().startswith(("http://", "https://")):
            return f"{label} must start with http:// or https://"
        return True

    return _validate


def _search_api_key_prompt(spec) -> str:
    if getattr(spec, "provider_id", "") == "brave":
        return (
            "Brave Search API key "
            "(create one at https://api-dashboard.search.brave.com/app/keys)"
        )
    return "Search API key"


def _stored_llm_entry(config: Any, provider_id: str) -> Any | None:
    """Return ``config.llm`` when it already holds this provider's entry.

    A wizard re-run must seed prompt defaults from what the operator stored
    last time, not from spec defaults — otherwise accepting the defaults
    silently wipes a custom ``base_url``/``proxy``.
    """
    llm = getattr(config, "llm", None)
    if str(getattr(llm, "provider", "") or "") == provider_id:
        return llm
    return None


def _ask_provider_fields(
    questionary, spec, options: OnboardOptions, *, config: Any = None
) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    stored = _stored_llm_entry(config, spec.provider_id) if config is not None else None
    stored_base_url = str(getattr(stored, "base_url", "") or "")
    stored_proxy = str(getattr(stored, "proxy", "") or "")
    stored_model = str(getattr(stored, "model", "") or "")
    # Resolve credentials and endpoint BEFORE the model so an optional live
    # discovery can enumerate the candidate provider's models for the picker.
    if spec.requires_api_key:
        env_key = options.api_key_env or spec.env_key
        if options.api_key:
            answers["api_key"] = options.api_key
            answers["api_key_env"] = ""
        elif options.api_key_env:
            answers["api_key"] = ""
            answers["api_key_env"] = options.api_key_env
        else:
            key_source = _ask_or_cancel(
                questionary.select(
                    "LLM API key source",
                    choices=_api_key_source_choices(env_key or ""),
                    default=_api_key_source_default(env_key or ""),
                ),
                section="provider",
            )
            selected_env_key = _api_key_env_from_choice(key_source or "")
            answers["api_key_env"] = selected_env_key
            answers["api_key"] = ""
            if not selected_env_key:
                answers["api_key"] = _ask_or_cancel(
                    questionary.password(
                        "API key",
                        validate=_secret_value_validator("API key"),
                    ),
                    section="provider",
                )
                answers["api_key_env"] = ""
    else:
        answers["api_key"] = options.api_key or ""
        answers["api_key_env"] = ""
    if spec.requires_base_url:
        answers["base_url"] = options.base_url or str(
            _ask_or_cancel(
                questionary.text(
                    "Base URL",
                    default=stored_base_url or spec.default_base_url,
                    validate=_base_url_validator("Base URL"),
                ),
                section="provider",
            )
        ).strip()
    else:
        answers["base_url"] = options.base_url or stored_base_url or spec.default_base_url
    answers["proxy"] = options.proxy or stored_proxy

    if options.model:
        answers["model"] = options.model
    elif getattr(spec, "router_supported", False):
        _verify_router_provider_connection(questionary, spec, answers)
        # Same-provider re-save: ``None`` engages the mutation layer's
        # keep-current so an Enter-through re-run never resets a hand-set
        # ``llm.model`` to the derived tier default. A first-time save keeps
        # the legacy ``""`` sentinel (derive the provider's default model).
        answers["model"] = None if stored is not None else ""
    else:
        answers["model"] = _ask_direct_provider_model(
            questionary, spec, answers, stored_model=stored_model
        )
    return answers


@contextlib.contextmanager
def _quiet_provider_logs():
    """Silence structlog output while a wizard-run probe/discovery is in flight.

    The provider layer logs request/response details at debug/warning level.
    The wizard process leaves structlog unconfigured, so those records would
    print raw into the interactive prompt stream; the probe outcome is already
    surfaced to the user through the redacted result panel.
    """
    import structlog  # deferred: only probe/discovery paths need it

    saved = structlog.get_config()
    structlog.configure(logger_factory=structlog.ReturnLoggerFactory())
    try:
        yield
    finally:
        structlog.configure(**saved)


def _run_provider_probe(
    *,
    provider_id: str,
    model: str,
    api_key: str = "",
    api_key_env: str = "",
    base_url: str = "",
    proxy: str = "",
):
    """Run the async provider probe from the sync onboarding flow.

    Returns a ``ProviderProbeResult`` or ``None`` when the probe cannot even
    be attempted (validation-level rejection or an unexpected error). A probe
    NEVER blocks setup, so any failure degrades to ``None`` here and the caller
    falls back to the offline free-text path. Ctrl+C during the network wait
    skips the check (never the whole wizard): the operator interrupted the
    probe, not their setup session.
    """
    import asyncio

    from opensquilla.onboarding.probe import probe_llm_provider

    try:
        with _quiet_provider_logs():
            return asyncio.run(
                probe_llm_provider(
                    provider_id=provider_id,
                    model=model,
                    api_key=api_key,
                    api_key_env=api_key_env,
                    base_url=base_url,
                    proxy=proxy,
                )
            )
    except KeyboardInterrupt:
        console.print("[dim]connection check skipped[/dim]")
        return None
    except Exception:  # noqa: BLE001 - verification is best-effort, never fatal
        return None


def _run_provider_discovery(
    *,
    provider_id: str,
    api_key: str = "",
    api_key_env: str = "",
    base_url: str = "",
    proxy: str = "",
):
    """Run the async model discovery from the sync onboarding flow.

    Returns a ``ProviderModelsDiscoverResult`` or ``None`` when discovery could
    not be attempted; the caller then keeps the free-text model prompt.
    Ctrl+C during the network wait skips discovery (never the whole wizard).
    """
    import asyncio

    from opensquilla.onboarding.probe import discover_selectable_provider_models

    try:
        with _quiet_provider_logs():
            return asyncio.run(
                discover_selectable_provider_models(
                    provider_id=provider_id,
                    api_key=api_key,
                    api_key_env=api_key_env,
                    base_url=base_url,
                    proxy=proxy,
                )
            )
    except KeyboardInterrupt:
        console.print("[dim]model discovery skipped[/dim]")
        return None
    except Exception:  # noqa: BLE001 - discovery is best-effort, never fatal
        return None


_TYPE_MODEL_ID_CHOICE = "Type a model id…"


def _discovered_model_label(model: dict[str, Any]) -> str:
    model_id = str(model.get("id") or "")
    context_window = model.get("contextWindow")
    try:
        ctx = int(context_window or 0)
    except (TypeError, ValueError):
        ctx = 0
    if ctx > 0:
        return f"{model_id}  ·  {ctx:,} ctx"
    return model_id


def _prompt_free_text_model(questionary, *, stored_model: str = "") -> str:
    # Non-empty validation: providers without a derivable default model raise
    # "model is required" deep in the mutation layer, so an empty submit must
    # re-prompt here instead of surfacing a raw error after the operator has
    # already typed the API key. On a same-provider re-run the stored model
    # seeds the prompt so plain Enter keeps it instead of retyping it.
    kwargs: dict[str, Any] = {"validate": _required_value("Model id")}
    if stored_model:
        kwargs["default"] = stored_model
    return str(
        _ask_or_cancel(
            questionary.text("Model id", **kwargs),
            section="provider",
        )
        or ""
    )


def _ask_direct_provider_model(
    questionary, spec, answers: dict[str, Any], *, stored_model: str = ""
) -> str:
    """Free-text model path, optionally upgraded by live verify + discovery.

    Scoped to providers that expose a direct model prompt (``router_supported``
    is False): a router-driven provider never types a direct model, so there is
    nothing to verify or discover for it. The verification is entirely optional
    and adds ZERO new required prompts on the quick-start (happy) path — a probe
    failure only surfaces a default-yes "Save anyway?" confirmation, and
    discovery merely swaps the existing free-text prompt for a select of live
    models (with a "type a model id…" escape back to the free-text prompt).

    A probe needs a model id; the provider's ``default_direct_model`` is used
    when present. Direct providers that ship no default model try trusted live
    discovery first. When that yields no selectable rows, the free-text model
    is probed after entry so credential verification remains intact.

    On a same-provider re-run ``stored_model`` seeds the free-text prompt and
    pre-selects the stored model in the discovery picker, so Enter-through
    keeps the operator's model instead of switching to the first discovered
    one.
    """
    if not getattr(spec, "can_probe", False):
        return _prompt_free_text_model(questionary, stored_model=stored_model)

    outcome = _probe_and_confirm(questionary, spec, answers)
    if outcome == "no_model":
        # No probe model yet: trusted discovery may verify the connection;
        # otherwise the free-text model is probed below.
        console.print("[dim]Checking the connection…[/dim]")
    elif outcome == "failed":
        return _prompt_free_text_model(questionary, stored_model=stored_model)
    probed = outcome == "verified"
    needs_typed_probe = outcome == "no_model"

    discovery = _run_provider_discovery(
        provider_id=spec.provider_id,
        api_key=answers.get("api_key", ""),
        api_key_env=answers.get("api_key_env", ""),
        base_url=answers.get("base_url", ""),
        proxy=answers.get("proxy", ""),
    )
    if discovery is not None and not discovery.ok:
        # When no probe ran (provider ships no default model), a failed
        # discovery is the connection check: surface the redacted detail and
        # offer the same non-blocking "Save anyway?" escape.
        if not probed:
            if not _confirm_save_after_failed_check(questionary, spec, discovery.detail):
                raise UserCancelledError(section="provider")
        return _prompt_free_text_model(questionary, stored_model=stored_model)

    if probed:
        console.print(f"[{ACCENT_SOFT}]◆[/] [dim]connection verified[/dim]")
    if discovery is None:
        return _prompt_free_text_model(questionary, stored_model=stored_model)
    models = list(getattr(discovery, "models", []) or []) if discovery else []
    if not models:
        model = _prompt_free_text_model(questionary, stored_model=stored_model)
        if needs_typed_probe:
            typed_outcome = _probe_and_confirm(
                questionary,
                spec,
                answers,
                probe_model=model,
                announce=False,
            )
            if typed_outcome == "verified":
                console.print(f"[{ACCENT_SOFT}]◆[/] [dim]connection verified[/dim]")
        return model

    choices = [_discovered_model_label(model) for model in models] + [
        _TYPE_MODEL_ID_CHOICE
    ]
    stored_choice = next(
        (
            choice
            for choice, model in zip(choices, models)
            if stored_model and str(model.get("id") or "") == stored_model
        ),
        None,
    )
    selected = _ask_or_cancel(
        questionary.select("Model", choices=choices, default=stored_choice or choices[0]),
        section="provider",
    )
    if selected == _TYPE_MODEL_ID_CHOICE:
        return _prompt_free_text_model(questionary, stored_model=stored_model)
    return str(selected).split(" ")[0]


def _confirm_save_after_failed_check(questionary, spec, detail: str) -> bool:
    """Show a redacted failure detail and ask the default-yes "Save anyway?".

    A verification failure NEVER blocks offline setup — the default is yes so a
    plain Enter keeps setup moving. Returns whether the user chose to continue.
    """
    message = detail or "the provider did not accept the request"
    console.print(
        warning_panel(
            f"Could not verify {markup_escape(spec.provider_id)}: "
            f"{markup_escape(message)}"
        )
    )
    return bool(
        _ask_or_cancel(
            questionary.confirm("Save anyway?", default=True),
            section="provider",
        )
    )


def _probe_and_confirm(
    questionary,
    spec,
    answers: dict[str, Any],
    *,
    probe_model: str = "",
    announce: bool = True,
) -> str:
    """The shared probe-then-"Save anyway?" core of both provider checks.

    Direct and router-supported providers verify with the exact same UX —
    the same "Checking the connection…" line, the same redacted failure
    panel, the same default-yes "Save anyway?" escape — so a UX change made
    here reaches both paths at once. Returns one of:

    - ``"no_model"``: neither the caller nor the spec supplied a probe model;
      nothing was attempted or printed (the caller decides what verification,
      if any, replaces the probe).
    - ``"skipped"``: the probe could not even be attempted — degrade
      silently, verification never blocks setup.
    - ``"verified"``: the probe succeeded (the caller prints the verified
      line at the point its own flow is actually done verifying).
    - ``"failed"``: the probe failed and the operator chose to save anyway.

    Declining the save raises ``UserCancelledError(section="provider")``.
    """
    model = str(probe_model or getattr(spec, "default_direct_model", "") or "").strip()
    if not model:
        return "no_model"
    if announce:
        console.print("[dim]Checking the connection…[/dim]")
    probe = _run_provider_probe(
        provider_id=spec.provider_id,
        model=model,
        api_key=answers.get("api_key", ""),
        api_key_env=answers.get("api_key_env", ""),
        base_url=answers.get("base_url", ""),
        proxy=answers.get("proxy", ""),
    )
    if probe is None:
        return "skipped"
    if not probe.ok:
        if not _confirm_save_after_failed_check(questionary, spec, probe.message):
            raise UserCancelledError(section="provider")
        return "failed"
    return "verified"


def _verify_router_provider_connection(questionary, spec, answers: dict[str, Any]) -> None:
    """Pre-save connection check for router-supported providers.

    A router-driven provider never types a direct model, so the free-text
    verify/discover path never runs for it — historically a bad API key
    surfaced only as an HTTP error in the middle of the first chat. The save
    applies the provider's router tier profile, and the spec's
    ``default_direct_model`` is that profile's default-tier model — i.e. the
    model the first routed turn will actually use — so probe with it.

    The check is the shared ``_probe_and_confirm`` sequence: a failed probe
    surfaces the redacted detail plus the default-yes "Save anyway?" escape,
    an unattemptable probe degrades silently, and a spec without a
    determinable model (or without probe support) keeps the old behavior of
    verifying nothing.
    """
    if not getattr(spec, "can_probe", False):
        return
    if _probe_and_confirm(questionary, spec, answers) == "verified":
        console.print(f"[{ACCENT_SOFT}]◆[/] [dim]connection verified[/dim]")


def _ask_search_choice(questionary):
    supported = [s for s in list_search_provider_setup_specs() if s.runtime_supported]
    provider_id = _ask_or_cancel(
        questionary.select(
            "Search provider",
            choices=[f"{s.provider_id} ({s.label})" for s in supported],
        ),
        section="search",
    )
    provider_id_clean = provider_id.split(" ")[0]
    return get_search_provider_setup_spec(provider_id_clean), provider_id_clean


def _ask_search_fields(questionary, spec, config: Any = None) -> dict[str, Any]:
    """Collect search settings, seeding every default from the stored config.

    A wizard re-run (e.g. rotating a key) must not reset the stored global
    search settings to factory defaults: pressing Enter through the prompts
    keeps ``max_results``/``proxy``/``use_env_proxy``/``fallback_policy``/
    ``diagnostics`` exactly as persisted, matching the headless keep-current
    contract.
    """
    stored_max_results = int(
        getattr(config, "search_max_results", DEFAULT_SEARCH_MAX_RESULTS)
        or DEFAULT_SEARCH_MAX_RESULTS
    )
    stored_proxy = str(getattr(config, "search_proxy", "") or "")
    stored_use_env_proxy = bool(getattr(config, "search_use_env_proxy", False))
    stored_fallback = str(getattr(config, "search_fallback_policy", "off") or "off")
    if stored_fallback not in _SEARCH_FALLBACK_LABELS:
        stored_fallback = "off"
    stored_diagnostics = bool(getattr(config, "search_diagnostics", False))

    answers: dict[str, Any] = {}
    if spec.requires_api_key:
        env_key = spec.env_key or ""
        use_env_key = False
        if env_key and os.environ.get(env_key):
            use_env_key = bool(
                _ask_or_cancel(
                    questionary.confirm(
                        f"Use {env_key} from environment?",
                        default=True,
                    ),
                    section="search",
                )
            )
        if use_env_key:
            answers["api_key"] = ""
            answers["api_key_env"] = env_key
        else:
            answers["api_key"] = _ask_or_cancel(
                questionary.password(
                    _search_api_key_prompt(spec),
                    validate=_secret_value_validator("Search API key"),
                ),
                section="search",
            )
            answers["api_key_env"] = ""
    else:
        answers["api_key"] = ""
        answers["api_key_env"] = ""
    max_results_raw = _ask_or_cancel(
        questionary.text(
            "Max search results",
            default=str(stored_max_results),
            validate=_int_value_validator("Max search results", minimum=1),
        ),
        section="search",
    )
    max_results_clean = str(max_results_raw or "").strip()
    answers["max_results"] = (
        int(max_results_clean) if max_results_clean else stored_max_results
    )
    answers["proxy"] = _ask_or_cancel(
        questionary.text("Search HTTP proxy", default=stored_proxy), section="search"
    )
    answers["use_env_proxy"] = bool(
        _ask_or_cancel(
            questionary.confirm(
                "Use environment proxy for search?", default=stored_use_env_proxy
            ),
            section="search",
        )
    )
    fallback_choice = _ask_or_cancel(
        questionary.select(
            "Search fallback policy",
            choices=list(_SEARCH_FALLBACK_LABELS.values()),
            default=_SEARCH_FALLBACK_LABELS[stored_fallback],
        ),
        section="search",
    )
    answers["fallback_policy"] = _search_fallback_choice_to_value(fallback_choice)
    answers["diagnostics"] = bool(
        _ask_or_cancel(
            questionary.confirm(_SEARCH_DIAGNOSTICS_PROMPT, default=stored_diagnostics),
            section="search",
        )
    )
    return answers


def run_interactive_search_configure(
    config_path: str | Path | None = None,
) -> PersistResult:
    if not _is_tty():
        return _print_noninteractive_hint(config_path, section="search")

    import questionary as _qmod
    questionary = _styled(_qmod)

    console.print(banner_panel("Search Setup", "Wire a web search provider"))
    cfg = load_config(config_path)
    spec, provider_id = _ask_search_choice(questionary)
    answers = _ask_search_fields(questionary, spec, cfg)
    result = upsert_search_provider(
        cfg,
        provider_id=provider_id,
        api_key=answers.get("api_key", ""),
        api_key_env=answers.get("api_key_env", ""),
        max_results=answers["max_results"],
        proxy=answers.get("proxy", ""),
        use_env_proxy=answers.get("use_env_proxy", False),
        fallback_policy=answers.get("fallback_policy", "off"),
        diagnostics=answers.get("diagnostics", False),
    )
    return persist_config(result.config, path=config_path, restart_required=False)


def _image_generation_choice_label(spec: ImageGenerationProviderSetupSpec) -> str:
    return f"{spec.provider_id} ({spec.label})"


def _image_generation_choice_to_provider_id(choice: str) -> str:
    return choice.split(" ")[0]


def _preferred_image_generation_provider_id(config) -> str | None:
    provider_id = str(getattr(config.llm, "provider", "") or "")
    supported = {
        spec.provider_id
        for spec in list_image_generation_provider_setup_specs()
        if spec.runtime_supported
    }
    return provider_id if provider_id in supported else None


def _ask_image_generation_choice(questionary, config):
    supported = [
        spec
        for spec in list_image_generation_provider_setup_specs()
        if spec.runtime_supported
    ]
    preferred = _preferred_image_generation_provider_id(config)
    default_spec = next(
        (spec for spec in supported if spec.provider_id == preferred),
        supported[0],
    )
    selected = _ask_or_cancel(
        questionary.select(
            "Image generation provider",
            choices=[_image_generation_choice_label(spec) for spec in supported],
            default=_image_generation_choice_label(default_spec),
        ),
        section="image-generation",
    )
    provider_id = _image_generation_choice_to_provider_id(selected)
    return get_image_generation_provider_setup_spec(provider_id), provider_id


def _ask_image_generation_fields(
    questionary,
    spec: ImageGenerationProviderSetupSpec,
    config,
) -> dict[str, Any]:
    """Collect image settings, seeding defaults from the stored config.

    A wizard re-run (e.g. rotating a key) must not reset a stored custom
    primary model, base URL, or a deliberate ``enabled = false`` back to
    factory defaults: pressing Enter through the prompts keeps what the
    operator persisted, matching the headless keep-current contract.
    """
    image_cfg = getattr(config, "image_generation", None)
    stored_primary = str(getattr(image_cfg, "primary", "") or "")
    if not stored_primary.startswith(f"{spec.provider_id}/"):
        stored_primary = ""
    provider_cfg = getattr(
        getattr(image_cfg, "providers", None), spec.provider_id, None
    )
    stored_base_url = str(getattr(provider_cfg, "base_url", "") or "")
    if image_cfg is not None and "enabled" in image_cfg.model_fields_set:
        stored_enabled = bool(image_cfg.enabled)
    else:
        stored_enabled = True

    answers: dict[str, Any] = {}
    answers["primary"] = (
        _ask_or_cancel(
            questionary.text(
                "Primary image model",
                default=stored_primary or spec.default_model,
            ),
            section="image-generation",
        )
        or stored_primary
        or spec.default_model
    )

    key_choices: list[str] = []
    llm_choice = "Reuse matching LLM provider key"
    if config.llm.provider == spec.provider_id and config.llm.api_key:
        key_choices.append(llm_choice)
    env_choice = (
        _api_key_env_choice(spec.env_key)
        if spec.env_key
        else ""
    )
    if env_choice and os.environ.get(spec.env_key):
        key_choices.append(env_choice)
    key_choices.append(_PASTE_API_KEY_CHOICE)
    if env_choice and not os.environ.get(spec.env_key):
        key_choices.append(env_choice)

    key_source = _ask_or_cancel(
        questionary.select(
            "Image API key source",
            choices=key_choices,
            default=key_choices[0],
        ),
        section="image-generation",
    )
    selected_env_key = _api_key_env_from_choice(key_source or "")
    if key_source == _PASTE_API_KEY_CHOICE:
        answers["api_key"] = _ask_or_cancel(
            questionary.password(
                "Image API key",
                validate=_secret_value_validator("Image API key"),
            ),
            section="image-generation",
        )
        answers["api_key_env"] = ""
    elif selected_env_key:
        answers["api_key"] = ""
        answers["api_key_env"] = selected_env_key
    else:
        answers["api_key"] = ""
        answers["api_key_env"] = ""

    answers["base_url"] = (
        _ask_or_cancel(
            questionary.text(
                "Image base URL",
                default=stored_base_url or spec.default_base_url,
            ),
            section="image-generation",
        )
        or stored_base_url
        or spec.default_base_url
    )
    # A cancel at the consent confirm must cancel the section — coercing the
    # ``None`` answer through ``bool()`` would silently persist enabled=false
    # while the wizard prints a success message.
    answers["enabled"] = bool(
        _ask_or_cancel(
            questionary.confirm("Image generation enabled?", default=stored_enabled),
            section="image-generation",
        )
    )
    return answers


def _print_image_generation_intro(spec: ImageGenerationProviderSetupSpec) -> None:
    console.print(
        f"[bold {ACCENT}]▌[/] [bold]Image generation[/]"
        f" [dim]· {markup_escape(spec.label)}[/dim]"
    )
    console.print(
        f"  [dim]Enables the [{ACCENT_SOFT}]image_generate[/] tool for new turns "
        "when the gateway can see the selected provider key.[/dim]"
    )


def _print_image_generation_saved(provider_id: str) -> None:
    console.print(
        f"[bold {ACCENT}]◆[/] [bold]Image generation configured.[/]"
    )
    console.print(
        f"  [dim]Provider:[/dim] [{ACCENT_SOFT}]{markup_escape(provider_id)}[/]"
        " [dim]· start a new turn after the gateway can see the key[/dim]"
    )


def run_interactive_image_generation_configure(
    config_path: str | Path | None = None,
) -> PersistResult:
    if not _is_tty():
        return _print_noninteractive_hint(config_path, section="image-generation")

    import questionary as _qmod
    questionary = _styled(_qmod)

    cfg = load_config(config_path)
    spec, provider_id = _ask_image_generation_choice(questionary, cfg)
    _print_image_generation_intro(spec)
    answers = _ask_image_generation_fields(questionary, spec, cfg)
    result = upsert_image_generation_provider(
        cfg,
        provider_id=provider_id,
        primary=answers.get("primary", ""),
        api_key=answers.get("api_key", ""),
        api_key_env=answers.get("api_key_env", ""),
        base_url=answers.get("base_url", ""),
        enabled=bool(answers.get("enabled", True)),
    )
    persisted = persist_config(result.config, path=config_path, restart_required=False)
    _print_image_generation_saved(provider_id)
    return persisted


def _memory_embedding_choice_label(spec: MemoryEmbeddingProviderSetupSpec) -> str:
    return f"{spec.provider_id} ({spec.label})"


def _memory_embedding_choice_to_provider_id(choice: str | None) -> str:
    return (choice or "").split(" ", 1)[0]


def _ask_memory_embedding_choice(
    questionary,
    config,
) -> tuple[MemoryEmbeddingProviderSetupSpec, str]:
    providers = [
        s
        for s in list_memory_embedding_provider_setup_specs()
        if s.runtime_supported
    ]
    current_provider = getattr(config.memory.embedding, "requested_provider", "auto")
    default_spec = next(
        (s for s in providers if s.provider_id == current_provider),
        providers[0],
    )
    choice = _ask_or_cancel(
        questionary.select(
            "Memory embedding provider",
            choices=[_memory_embedding_choice_label(s) for s in providers],
            default=_memory_embedding_choice_label(default_spec),
        ),
        section="memory embedding",
    )
    provider_id = _memory_embedding_choice_to_provider_id(choice)
    return get_memory_embedding_provider_setup_spec(provider_id), provider_id


def _memory_embedding_key_choices(
    spec: MemoryEmbeddingProviderSetupSpec,
    config,
) -> list[str]:
    embedding = config.memory.embedding
    current_env = str(getattr(embedding.remote, "api_key_env", "") or "").strip()
    current_key = str(
        getattr(embedding.remote, "api_key", "")
        or getattr(embedding, "api_key", "")
        or ""
    ).strip()
    choices: list[str] = []
    if current_key:
        choices.append("Keep stored memory API key")
    env_key = current_env or spec.env_key
    if env_key:
        choices.append(
            _api_key_env_choice(env_key, detected=bool(os.environ.get(env_key)))
        )
    choices.append(_PASTE_API_KEY_CHOICE)
    return choices


def _ask_memory_embedding_fields(
    questionary,
    spec: MemoryEmbeddingProviderSetupSpec,
    config,
) -> dict[str, Any]:
    embedding = config.memory.embedding
    answers: dict[str, Any] = {}
    if spec.provider_id == "none":
        return answers
    if spec.provider_id == "local":
        answers["onnx_dir"] = _ask_or_cancel(
            questionary.text(
                "Local ONNX directory",
                default=(
                    embedding.local.onnx_dir
                    if embedding.requested_provider == "local"
                    else ""
                ),
            ),
            section="memory embedding",
        )
        return answers
    if spec.provider_id in {"openai", "openai-compatible"}:
        answers["model"] = _ask_or_cancel(
            questionary.text(
                "Memory embedding model",
                default=embedding.remote.model
                or embedding.model
                or "text-embedding-3-small",
            ),
            section="memory embedding",
        )
        key_source = _ask_or_cancel(
            questionary.select(
                "Memory API key source",
                choices=_memory_embedding_key_choices(spec, config),
            ),
            section="memory embedding",
        )
        selected_env_key = _api_key_env_from_choice(key_source or "")
        if key_source == _PASTE_API_KEY_CHOICE:
            answers["api_key"] = _ask_or_cancel(
                questionary.password(
                    "Memory embedding API key",
                    validate=_secret_value_validator("Memory embedding API key"),
                ),
                section="memory embedding",
            )
            answers["api_key_env"] = ""
        elif selected_env_key:
            answers["api_key"] = ""
            answers["api_key_env"] = selected_env_key
        else:
            answers["api_key"] = ""
            answers["api_key_env"] = ""
        answers["base_url"] = _ask_or_cancel(
            questionary.text(
                "Memory embedding base URL",
                default=embedding.remote.base_url
                or embedding.base_url
                or "https://api.openai.com/v1",
            ),
            section="memory embedding",
        )
        return answers
    if spec.provider_id == "ollama":
        answers["model"] = _ask_or_cancel(
            questionary.text(
                "Memory embedding model",
                default=embedding.ollama.model or "nomic-embed-text",
            ),
            section="memory embedding",
        )
        answers["base_url"] = _ask_or_cancel(
            questionary.text(
                "Memory embedding base URL",
                default=embedding.ollama.base_url or "http://localhost:11434",
            ),
            section="memory embedding",
        )
    return answers


def run_interactive_memory_embedding_configure(
    config_path: str | Path | None = None,
) -> PersistResult:
    if not _is_tty():
        cfg = load_config(config_path)
        return _print_noninteractive_hint(cfg, config_path, section="memory-embedding")

    import questionary as _qmod
    questionary = _styled(_qmod)

    cfg = load_config(config_path)
    spec, provider_id = _ask_memory_embedding_choice(questionary, cfg)
    answers = _ask_memory_embedding_fields(questionary, spec, cfg)
    result = upsert_memory_embedding(
        cfg,
        provider=provider_id,
        model=answers.get("model"),
        api_key=answers.get("api_key"),
        api_key_env=answers.get("api_key_env"),
        base_url=answers.get("base_url"),
        onnx_dir=answers.get("onnx_dir"),
    )
    # The mutation knows whether the edit actually changed the embedding
    # setup (embedding changes need a full gateway restart to take effect);
    # hardcoding False here silently dropped that signal.
    persisted = persist_config(
        result.config,
        path=config_path,
        restart_required=result.restart_required,
    )
    console.print(
        f"[bold {ACCENT}]◆[/] [bold]Memory embedding configured.[/]"
    )
    console.print(
        f"  [dim]Provider:[/dim] [{ACCENT_SOFT}]{markup_escape(provider_id)}[/]"
        " [dim]· new memory indexing will use this setting[/dim]"
    )
    return persisted


_TEXT_ROUTER_TIERS = TEXT_TIERS
_EXPOSED_ROUTER_TIERS = (*TEXT_TIERS, IMAGE_TIER)
_TEXT_TIER_LABELS = {
    "c0": "Route c0",
    "c1": "Route c1",
    "c2": "Route c2",
    "c3": "Route c3",
}
_IMAGE_TIER_LABEL = "Image model"
_DONE_LABEL = "Done"


_ROUTER_MODE_LABEL = "SquillaRouter"
_ROUTER_DISABLED_LABEL = "Disabled"
_SEARCH_FALLBACK_LABELS = {
    "off": "off - no fallback; surface the original provider error",
    "network": "network - retry with DuckDuckGo on timeout/network errors",
}
_SEARCH_DIAGNOSTICS_PROMPT = (
    "Enable search diagnostics? Include provider attempt/error details "
    "for troubleshooting?"
)


def _search_fallback_choice_to_value(choice: str | None) -> str:
    for value, label in _SEARCH_FALLBACK_LABELS.items():
        if choice == label or choice == value:
            return value
    return "off"


def _router_mode_choices(provider_id: str) -> list[str]:
    return [_ROUTER_MODE_LABEL, _ROUTER_DISABLED_LABEL]


def _router_mode_default(provider_id: str, requested: str) -> str:
    if requested == "disabled":
        return _ROUTER_DISABLED_LABEL
    return _ROUTER_MODE_LABEL


def _router_mode_to_internal(selected: str | None) -> str:
    if selected == _ROUTER_DISABLED_LABEL:
        return "disabled"
    return "recommended"


def _text_tier_label(tier: str | None) -> str:
    normalized = normalize_text_tier(tier) or DEFAULT_TEXT_TIER
    return _TEXT_TIER_LABELS.get(normalized, _TEXT_TIER_LABELS[DEFAULT_TEXT_TIER])


def _text_tier_to_internal(selected: str | None) -> str:
    normalized = normalize_text_tier(selected)
    if normalized:
        return normalized
    if selected in _TEXT_ROUTER_TIERS:
        return str(selected)
    for tier, label in _TEXT_TIER_LABELS.items():
        if selected == label:
            return tier
    return DEFAULT_TEXT_TIER


def _tier_choice_label(tier: str) -> str:
    if tier == "image_model":
        return _IMAGE_TIER_LABEL
    return _text_tier_label(tier)


def _tier_choice_to_internal(selected: str | None) -> str | None:
    if not selected or selected == _DONE_LABEL:
        return None
    if selected == _IMAGE_TIER_LABEL:
        return "image_model"
    if selected in _EXPOSED_ROUTER_TIERS:
        return str(selected)
    for tier_name in _EXPOSED_ROUTER_TIERS:
        if selected == _tier_choice_label(tier_name):
            return tier_name
    return None


def _print_router_defaults(config) -> None:
    router = config.squilla_router
    if not getattr(router, "enabled", True):
        console.print(
            f"[{ACCENT_DIM}]router[/] [dim]disabled — requests bypass tier routing[/dim]"
        )
        return
    default_tier = _text_tier_to_internal(getattr(router, "default_tier", None))
    default = router.tiers.get(default_tier, {})
    console.print(
        f"[bold {ACCENT}]◆ router[/] "
        f"[dim]default[/] [{ACCENT_SOFT}]{default_tier}[/] "
        f"[dim]→[/] {markup_escape(default.get('provider', ''))}"
        f"[dim]/[/]{markup_escape(default.get('model', ''))}"
    )
    for tier_name in _EXPOSED_ROUTER_TIERS:
        tier = router.tiers.get(tier_name)
        if not isinstance(tier, dict):
            continue
        marker = (
            f"[{ACCENT}]●[/]" if tier_name == default_tier else f"[{ACCENT_DIM}]○[/]"
        )
        console.print(
            f"  {marker} [{ACCENT_SOFT}]{tier_name:<11}[/]"
            f" [dim]{markup_escape(tier.get('provider', ''))}/"
            f"{markup_escape(tier.get('model', ''))}[/dim]"
        )


def _router_tier_overrides(questionary, config) -> dict[str, dict[str, Any]]:
    overrides: dict[str, dict[str, Any]] = {}
    choices = [_DONE_LABEL] + [
        _tier_choice_label(tier_name)
        for tier_name in _EXPOSED_ROUTER_TIERS
        if isinstance(config.squilla_router.tiers.get(tier_name), dict)
    ]
    while True:
        # Cancel contract: Esc/Ctrl+C must abort the router section, not map
        # to "Done"/keep-current — the callers persist the returned payload,
        # so a swallowed cancel would rewrite config.toml on explicit abort.
        selected = _ask_or_cancel(
            questionary.select(
                "Tier to edit",
                choices=choices,
                default=_DONE_LABEL,
            ),
            section="router",
        )
        tier_name = _tier_choice_to_internal(selected)
        if not tier_name:
            break
        tier = config.squilla_router.tiers.get(tier_name)
        if not isinstance(tier, dict):
            continue
        provider = _ask_or_cancel(
            questionary.text(
                f"{tier_name} provider",
                default=str(tier.get("provider") or ""),
            ),
            section="router",
        ) or str(tier.get("provider") or "")
        model = _ask_or_cancel(
            questionary.text(
                f"{tier_name} model",
                default=str(tier.get("model") or ""),
            ),
            section="router",
        ) or str(tier.get("model") or "")
        overrides[tier_name] = {"provider": provider, "model": model}
        if tier_name == "image_model":
            overrides[tier_name]["supportsImage"] = True
    return overrides


def _ask_router_fields(
    questionary,
    config,
    *,
    provider_id: str,
    requested_mode: str,
    explicit_mode: bool = False,
) -> dict[str, Any]:
    choices = _router_mode_choices(provider_id)
    # A cancel here must never be read as consent: mapping the ``None`` answer
    # through ``_router_mode_to_internal`` would silently persist
    # ``squilla_router.enabled = true`` and print the success handoff.
    selected_mode = _ask_or_cancel(
        questionary.select(
            "Router mode",
            choices=choices,
            default=_router_mode_default(provider_id, requested_mode),
        ),
        section="router",
    )
    mode = _router_mode_to_internal(selected_mode)
    if mode == "disabled":
        preview = upsert_router(config, mode=mode).config
        _print_router_defaults(preview)
        return {"mode": mode}

    if (
        mode == "recommended"
        and not explicit_mode
        and _router_tiers_hand_customized(config)
    ):
        # (Re-)enabling routing over an operator-customized inline ladder —
        # including one preserved across a disable — must keep that ladder;
        # the custom mode restores it instead of resetting to the packaged
        # profile. An explicit ``--router recommended`` still resets.
        mode = "custom"

    preview = upsert_router(config, mode=mode).config
    _print_router_defaults(preview)
    default_tier_choice = _ask_or_cancel(
        questionary.select(
            "Default text model",
            choices=[_TEXT_TIER_LABELS[tier] for tier in _TEXT_ROUTER_TIERS],
            default=_text_tier_label(str(preview.squilla_router.default_tier or "c1")),
        ),
        section="router",
    )
    default_tier = _text_tier_to_internal(default_tier_choice)
    preview = upsert_router(config, mode=mode, default_tier=default_tier).config
    _print_router_defaults(preview)

    payload: dict[str, Any] = {"mode": mode, "defaultTier": default_tier}
    if bool(
        _ask_or_cancel(
            questionary.confirm("Edit router tier models now?", default=False),
            section="router",
        )
    ):
        overrides = _router_tier_overrides(questionary, preview)
        if mode == "custom":
            # The tier editor showed the preserved ladder; passing only the
            # edited tiers would merge them onto the packaged preset base and
            # wipe the untouched stored tiers. Send the full effective ladder.
            effective = {
                name: dict(tier)
                for name, tier in preview.squilla_router.tiers.items()
                if isinstance(tier, dict)
            }
            effective.update(overrides)
            overrides = effective
        payload["tiers"] = overrides
    return payload


def _apply_router_section(
    questionary,
    config,
    *,
    provider_id: str,
    requested_mode: str,
    explicit_mode: bool = False,
    config_path: str | Path | None = None,
):
    """Run the inline router step of the full wizard; a cancel skips it.

    The router prompts follow directly after the provider credentials in
    ``run_interactive_onboard``. Letting a router-stage cancel propagate would
    discard the API key the user just pasted, so the cancel is scoped to the
    router section here: nothing router-related is persisted and the caller
    keeps the provider config it already collected.
    """
    try:
        payload = _ask_router_fields(
            questionary,
            config,
            provider_id=provider_id,
            requested_mode=requested_mode,
            explicit_mode=explicit_mode,
        )
    except UserCancelledError:
        config_arg = _config_cli_arg(config_path)
        console.print("[yellow]router setup cancelled — keeping current router settings.[/yellow]")
        console.print(
            f"  [dim]Resume later with[/dim] "
            f"[{ACCENT_SOFT}]opensquilla onboard configure router{config_arg}[/]"
        )
        return config
    result = upsert_router(
        config,
        mode=payload["mode"],
        default_tier=payload.get("defaultTier"),
        tiers=payload.get("tiers"),
    )
    return result.config


def run_interactive_router_configure(
    *, config_path: str | Path | None = None
) -> PersistResult:
    cfg = load_config(config_path)
    if not _is_tty():
        return _print_noninteractive_hint(cfg, config_path, section="router")

    import questionary as _qmod

    questionary = _styled(_qmod)
    provider_id = _provider_id_from_config(cfg)
    requested_mode = "recommended" if cfg.squilla_router.enabled else "disabled"
    payload = _ask_router_fields(
        questionary,
        cfg,
        provider_id=provider_id,
        requested_mode=requested_mode,
    )
    result = upsert_router(
        cfg,
        mode=payload["mode"],
        default_tier=payload.get("defaultTier"),
        tiers=payload.get("tiers"),
    )
    return persist_config(result.config, path=config_path, restart_required=False)


def _ensemble_selection_modes() -> tuple[str, ...]:
    from opensquilla.onboarding.mutations import _LLM_ENSEMBLE_SELECTION_MODES

    return _LLM_ENSEMBLE_SELECTION_MODES


def _ensemble_all_failed_policies() -> tuple[str, ...]:
    from opensquilla.onboarding.mutations import _LLM_ENSEMBLE_ALL_FAILED_POLICIES

    return _LLM_ENSEMBLE_ALL_FAILED_POLICIES


def _ask_ensemble_fields(questionary, cfg) -> dict[str, Any]:
    """Collect [llm_ensemble] settings, seeding every default from the config.

    Every prompt defaults to the stored value, so accepting all defaults is a
    no-op edit — the ensemble step is only ever reached when the operator opts
    in, and it adds no required prompts to the quick-start sequence. Disabling
    is a one-answer operation: the tuning prompts are skipped and the stored
    values stay untouched for a later re-enable.
    """
    ensemble = cfg.llm_ensemble
    answers: dict[str, Any] = {}
    answers["enabled"] = bool(
        _ask_or_cancel(
            questionary.confirm(
                "Enable the LLM ensemble?",
                default=bool(getattr(ensemble, "enabled", True)),
            ),
            section="ensemble",
        )
    )
    if not answers["enabled"]:
        return answers

    modes = list(_ensemble_selection_modes())
    current_mode = str(getattr(ensemble, "selection_mode", "") or "")
    selection_mode = _ask_or_cancel(
        questionary.select(
            "Ensemble selection mode",
            choices=modes,
            default=current_mode if current_mode in modes else modes[0],
        ),
        section="ensemble",
    )
    answers["selection_mode"] = selection_mode

    current_options = list(getattr(ensemble, "model_options", []) or [])
    options_raw = _ask_or_cancel(
        questionary.text(
            "Ensemble model options (comma-separated; blank keeps current)",
            default="",
        ),
        section="ensemble",
    )
    parsed_options = [
        piece.strip() for piece in str(options_raw or "").split(",") if piece.strip()
    ]
    # Blank input keeps the stored options untouched (keep-current semantics).
    answers["model_options"] = parsed_options if parsed_options else None
    if answers["model_options"] is None and current_options:
        console.print(
            f"  [dim]Keeping {len(current_options)} configured model option(s).[/dim]"
        )

    min_raw = _ask_or_cancel(
        questionary.text(
            "Minimum successful proposers",
            default=str(getattr(ensemble, "min_successful_proposers", 1)),
            validate=_int_value_validator("Minimum successful proposers", minimum=1),
        ),
        section="ensemble",
    )
    answers["min_successful_proposers"] = str(min_raw or "").strip() or None

    policies = list(_ensemble_all_failed_policies())
    current_policy = str(getattr(ensemble, "all_failed_policy", "") or "")
    answers["all_failed_policy"] = _ask_or_cancel(
        questionary.select(
            "Policy when all proposers fail",
            choices=policies,
            default=current_policy if current_policy in policies else policies[0],
        ),
        section="ensemble",
    )
    return answers


def _print_ensemble_saved(cfg) -> None:
    ensemble = cfg.llm_ensemble
    state = "enabled" if getattr(ensemble, "enabled", False) else "disabled"
    console.print(f"[bold {ACCENT}]◆[/] [bold]LLM ensemble {state}.[/]")
    # This runner writes the config file on disk, which the gateway only
    # reads at boot — unlike the RPC hot-apply path, the change is NOT live
    # on the next turn until a reload/restart picks the file up.
    console.print(
        f"  [dim]Selection mode:[/dim] "
        f"[{ACCENT_SOFT}]{markup_escape(getattr(ensemble, 'selection_mode', ''))}[/]"
        " [dim]· saved to the config file — run[/dim]"
        f" [{ACCENT_SOFT}]opensquilla gateway reload[/]"
        " [dim](or restart) to apply it[/dim]"
    )


def run_interactive_ensemble_configure(
    config_path: str | Path | None = None,
) -> PersistResult:
    if not _is_tty():
        cfg = load_config(config_path)
        return _print_noninteractive_hint(cfg, config_path, section="ensemble")

    import questionary as _qmod

    questionary = _styled(_qmod)

    cfg = load_config(config_path)
    console.print(
        banner_panel("LLM Ensemble Setup", "Tune the multi-model routing surface")
    )
    answers = _ask_ensemble_fields(questionary, cfg)
    result = upsert_llm_ensemble(
        cfg,
        enabled=answers.get("enabled"),
        selection_mode=answers.get("selection_mode"),
        model_options=answers.get("model_options"),
        min_successful_proposers=answers.get("min_successful_proposers"),
        all_failed_policy=answers.get("all_failed_policy"),
    )
    persisted = persist_config(result.config, path=config_path, restart_required=False)
    _print_ensemble_saved(result.config)
    return persisted


def _channel_control_fields(spec: ChannelSetupSpec) -> set[str]:
    controls: set[str] = set()
    for field in spec.fields:
        controls.update((field.show_when or {}).keys())
    return controls


def _channel_field_visible(field: ChannelSetupField, answers: dict[str, Any]) -> bool:
    return all(
        str(answers.get(key, "")) == str(expected)
        for key, expected in (field.show_when or {}).items()
    )


def _should_prompt_channel_field(
    field: ChannelSetupField,
    *,
    controls: set[str],
    answers: dict[str, Any],
) -> bool:
    if not _channel_field_visible(field, answers):
        return False
    if field.name == "name":
        return True
    if field.required:
        return True
    # The interactive channel flow is intentionally a minimal-field wizard.
    # Optional advanced controls still seed their safe defaults so dependent
    # fields are evaluated correctly, but they are configured later rather
    # than expanding every existing add flow with another prompt.
    if field.advanced:
        return False
    if field.name in controls:
        return True
    if field.show_when and field.default in (None, ""):
        return not field.advanced
    return False


def _channel_prompt_default(
    field: ChannelSetupField,
    *,
    current: Any,
    type_name: str,
) -> Any:
    if current not in (None, ""):
        return current
    if field.name == "name":
        return type_name
    return field.default


def _ask_channel_field(questionary, field: ChannelSetupField, default: Any) -> Any:
    if field.help:
        console.print(
            f"  [dim]{markup_escape(field.label)}: {markup_escape(field.help)}[/dim]"
        )
    elif field.placeholder:
        console.print(
            f"  [dim]{markup_escape(field.label)}: "
            f"{markup_escape(field.placeholder)}[/dim]"
        )
    if field.field_type == "select":
        select_default = default if isinstance(default, str) else None
        return _ask_or_cancel(
            questionary.select(
                field.label, choices=list(field.choices), default=select_default
            ),
            section="channels",
        )
    if field.field_type == "bool":
        return bool(
            _ask_or_cancel(
                questionary.confirm(field.label, default=bool(default)),
                section="channels",
            )
        )
    if field.field_type == "password":
        if field.secret and default not in (None, ""):
            # Editing an entry that already stores this secret: the wizard
            # keeps the stored value for a blank submit. Resolving it here
            # (instead of relying on the mutation's by-name merge) keeps the
            # promise even when the operator renames the entry in the same
            # edit — a blank submit then must not crash with "requires a
            # non-empty value".
            console.print(
                f"  [dim]{markup_escape(field.label)}: "
                "leave blank to keep the stored value[/dim]"
            )
            answer = _ask_or_cancel(
                questionary.password(
                    field.label,
                    validate=_secret_keep_current_validator(field.label),
                ),
                section="channels",
            )
            return answer if str(answer or "").strip() else default
        return (
            _ask_or_cancel(
                questionary.password(
                    field.label,
                    validate=_secret_value_validator(field.label),
                ),
                section="channels",
            )
            or ""
        )
    if field.field_type in ("int", "float"):
        fallback = default if default is not None else (0 if field.field_type == "int" else 0.0)
        raw = _ask_or_cancel(
            questionary.text(
                field.label,
                default=str(fallback),
                validate=_channel_number_validator(field),
            ),
            section="channels",
        )
        stripped = str(raw or "").strip()
        if not stripped:
            # Blank keeps the displayed default — the wizard twin of the
            # headless contract where an omitted --field keeps the value.
            stripped = str(fallback)
        value, error = _coerce_channel_prompt_value(field, stripped)
        if error is not None:
            raise ValueError(error)
        return value
    return (
        _ask_or_cancel(
            questionary.text(field.label, default=str(default or "")),
            section="channels",
        )
        or ""
    )


def _ask_channel_fields(
    questionary,
    spec: ChannelSetupSpec,
    *,
    type_name: str,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    answers: dict[str, Any] = {"type": type_name, **(current or {})}
    for field in spec.fields:
        if field.default is not None and field.name not in answers:
            answers[field.name] = field.default

    controls = _channel_control_fields(spec)
    for field in spec.fields:
        if field.show_when:
            continue
        if not _should_prompt_channel_field(field, controls=controls, answers=answers):
            continue
        default = _channel_prompt_default(
            field,
            current=answers.get(field.name),
            type_name=type_name,
        )
        answers[field.name] = _ask_channel_field(questionary, field, default)

    for field in spec.fields:
        if not field.show_when:
            continue
        if not _should_prompt_channel_field(field, controls=controls, answers=answers):
            continue
        default = _channel_prompt_default(
            field,
            current=answers.get(field.name),
            type_name=type_name,
        )
        answers[field.name] = _ask_channel_field(questionary, field, default)

    return answers


def _print_channel_intro(spec: ChannelSetupSpec) -> None:
    console.print(
        f"[bold {ACCENT}]▌[/] [bold]{markup_escape(spec.label)}[/]"
        f" [dim]· {markup_escape(spec.description)}[/dim]"
    )
    if spec.help:
        console.print(f"  [dim]{markup_escape(spec.help)}[/dim]")
    if spec.requires_public_url:
        console.print(
            f"  [{ACCENT_SOFT}]webhook[/] "
            "[dim]needs a public HTTPS URL reachable by the platform[/dim]"
        )
    console.print(
        "  [dim]minimal-field wizard · advanced/webhook-only fields editable later[/dim]"
    )


_RELEASE_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?$")


def _installed_reinstall_command_lines() -> str:
    """Reinstall lines for the running release, never a stale pinned tag.

    A release build reinstalls its own wheel with the ``recommended`` extra so
    the command adds the missing optional dependency without changing version.
    Dev or unknown builds cannot map to a published wheel, so they are pointed
    at the latest-release page instead of a guessed URL.
    """
    from opensquilla import __version__

    if _RELEASE_VERSION_RE.match(__version__):
        wheel_url = (
            "https://github.com/opensquilla/opensquilla/releases/download/"
            f"v{__version__}/opensquilla-{__version__}-py3-none-any.whl"
        )
        return (
            "  uv tool install --python 3.12 --force "
            f"\"opensquilla[recommended] @ {wheel_url}\"\n"
        )
    return (
        "  uv tool install --python 3.12 --force "
        "\"opensquilla[recommended] @ <wheel-url>\"\n"
        "  (use the wheel from the latest release: "
        "https://github.com/opensquilla/opensquilla/releases/latest)\n"
    )


def _warn_channel_dependency_gaps(spec: ChannelSetupSpec, answers: dict[str, Any]) -> None:
    """Warn about optional channel dependencies that will fail at gateway start."""
    if spec.type == "feishu" and answers.get("connection_mode") == "websocket":
        if importlib.util.find_spec("lark_oapi") is None:
            console.print(
                warning_panel(
                    "Feishu websocket mode requires the base lark-oapi dependency "
                    "(lark-oapi).\n\n"
                    "[bold]Portable zip:[/]\n"
                    "  Use the latest recommended portable package, then restart.\n\n"
                    "[bold]Installed command:[/]\n"
                    + _installed_reinstall_command_lines() +
                    "  opensquilla gateway restart\n\n"
                    "[bold]Development checkout:[/]\n"
                    "  uv sync --extra recommended\n"
                    "  uv run opensquilla gateway restart --json\n\n"
                    "[bold yellow]Restarting alone will not install Python packages.[/]",
                    title="Channel dependency missing",
                )
            )


def _print_channel_saved(name: str) -> None:
    console.print(
        f"[bold {ACCENT}]◆[/] [bold]Channel configured, not connected yet.[/]"
    )
    console.print(
        "  [dim]Restart the gateway process to load the channel adapter.[/dim]"
    )
    console.print(
        f"  [dim]Verify after restart:[/dim] "
        f"[{ACCENT_SOFT}]opensquilla channels status "
        f"{markup_escape(name)} --json[/]"
    )


_MIGRATION_SOURCE_LABELS = {
    "openclaw": "OpenClaw",
    "hermes": "Hermes Agent",
}


@dataclass(frozen=True)
class DetectedMigrationSource:
    name: str
    path: Path


@dataclass(frozen=True)
class MigrationBatchOptions:
    config: Path
    apply: bool
    migrate_secrets: bool
    overwrite: bool
    preset: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    skill_conflict: str
    persona_conflict: str


@dataclass(frozen=True)
class MigrationBatchResult:
    selected: tuple[str, ...]
    reports: dict[str, dict[str, Any]]
    apply: bool

    @property
    def has_error(self) -> bool:
        return any(
            item.get("status") == "error"
            for report in self.reports.values()
            for item in report.get("items", [])
            if isinstance(item, dict)
        )


def _config_path_from_loaded_config(cfg: Any) -> Path:
    raw = getattr(cfg, "config_path", "") or default_config_path()
    return Path(raw).expanduser()


def _migration_orchestrator() -> Any:
    return importlib.import_module("opensquilla.migration.orchestrator")


def detect_default_sources() -> list[Any]:
    return cast(list[Any], _migration_orchestrator().detect_default_sources())


def run_migration_batch(
    detected: list[Any], selected: list[str] | tuple[str, ...], options: Any
) -> Any:
    migration = _migration_orchestrator()
    if isinstance(options, MigrationBatchOptions):
        options = migration.MigrationBatchOptions(
            config=options.config,
            apply=options.apply,
            migrate_secrets=options.migrate_secrets,
            overwrite=options.overwrite,
            preset=options.preset,
            include=options.include,
            exclude=options.exclude,
            skill_conflict=options.skill_conflict,
            persona_conflict=options.persona_conflict,
        )
    return migration.run_migration_batch(detected, selected, options)


def report_status_counts(report: dict[str, Any]) -> dict[str, int]:
    return cast(dict[str, int], _migration_orchestrator().report_status_counts(report))


def _run_onboard_migration_step(
    questionary,
    *,
    config_path: Path,
) -> Any | None:
    """Run the interactive onboarding migration pre-step.

    Migration is intentionally isolated from the rest of onboarding: detection,
    dry-run, apply, and report rendering failures all degrade to "skip migration"
    so provider setup can continue normally.
    """

    migration = None
    try:
        migration = _migration_orchestrator()
        detected = detect_default_sources()
        if not detected:
            return None
        _print_detected_migration_sources(detected)
        should_migrate = bool(
            _ask_or_cancel(
                questionary.confirm(
                    "Review migration options now?",
                    default=True,
                ),
                section="migration",
            )
        )
        if not should_migrate:
            console.print("[dim]Migration skipped.[/dim]")
            return None

        selected = _ask_migration_sources(questionary, detected)
        if not selected:
            console.print("[yellow]No migration source selected; skipping migration.[/yellow]")
            return None
        _print_selected_migration_sources(detected, selected)

        migrate_secrets = bool(
            _ask_or_cancel(
                questionary.confirm(
                    "Import saved API keys/tokens from detected legacy .env files?",
                    default=False,
                ),
                section="migration",
            )
        )
        dry_run_options = _onboard_migration_options(
            migration=migration,
            config_path=config_path,
            apply=False,
            migrate_secrets=migrate_secrets,
        )
        dry_run = run_migration_batch(detected, selected, dry_run_options)
        _print_migration_summary(dry_run, title="Migration preview")
        if dry_run.has_error:
            console.print(
                warning_panel(
                    "Migration preview found errors. Onboarding will continue without "
                    "applying migration; retry later with `opensquilla migrate --apply`."
                )
            )
            return None

        apply_now = bool(
            _ask_or_cancel(
                questionary.confirm("Apply this migration now?", default=True),
                section="migration",
            )
        )
        if not apply_now:
            console.print("[dim]Migration not applied.[/dim]")
            return None

        applied_options = _onboard_migration_options(
            migration=migration,
            config_path=config_path,
            apply=True,
            migrate_secrets=migrate_secrets,
        )
        applied = run_migration_batch(detected, selected, applied_options)
        _print_migration_summary(applied, title="Migration complete")
        if applied.has_error:
            console.print(
                warning_panel(
                    "Migration reported errors after apply. Onboarding will continue; "
                    "review the migration report before relying on imported data."
                )
            )
            return None
        return applied
    except UserCancelledError:
        console.print("[yellow]Migration setup cancelled — continuing onboarding.[/yellow]")
        return None
    except KeyboardInterrupt:
        console.print("[yellow]Migration interrupted — continuing onboarding.[/yellow]")
        return None
    except Exception as exc:
        option_error = getattr(migration, "MigrationOptionError", None)
        if isinstance(option_error, type) and isinstance(exc, option_error):
            console.print(
                warning_panel(
                    f"Migration options were rejected: {exc}. "
                    "Onboarding will continue without migration."
                )
            )
            return None
        console.print(
            warning_panel(
                f"Migration failed before onboarding completed: {exc}. "
                "Onboarding will continue without migration."
            )
        )
        return None


def _onboard_migration_options(
    *,
    migration: Any,
    config_path: Path,
    apply: bool,
    migrate_secrets: bool,
) -> Any:
    return MigrationBatchOptions(
        config=config_path,
        apply=apply,
        migrate_secrets=migrate_secrets,
        overwrite=False,
        preset="full",
        include=(),
        exclude=(),
        skill_conflict="skip",
        persona_conflict="use-opensquilla",
    )


def _print_detected_migration_sources(detected: list[Any]) -> None:
    console.print(f"[bold {ACCENT}]◆[/] [bold]Existing agent data detected[/]")
    for source in detected:
        label = _MIGRATION_SOURCE_LABELS.get(source.name, source.name)
        console.print(f"  [{ACCENT_SOFT}]✓[/] {label} [dim]{source.path}[/dim]")


def _print_selected_migration_sources(
    detected: list[Any],
    selected: list[str],
) -> None:
    selected_names = set(selected)
    console.print(f"[bold {ACCENT}]◆[/] [bold]Selected migration sources[/]")
    for source in detected:
        if source.name not in selected_names:
            continue
        label = _MIGRATION_SOURCE_LABELS.get(source.name, source.name)
        console.print(f"  [{ACCENT_SOFT}]☑[/] {label} [dim]{source.path}[/dim]")


def _ask_migration_sources(
    questionary,
    detected: list[Any],
) -> list[str]:
    if len(detected) == 1:
        return [detected[0].name]
    choice_cls = getattr(questionary, "Choice", None)
    if choice_cls is None:
        choices = [
            f"{_MIGRATION_SOURCE_LABELS.get(source.name, source.name)} - {source.path}"
            for source in detected
        ]
        selected = _ask_or_cancel(
            questionary.checkbox(
                "Select sources to import",
                choices=choices,
                instruction="Space select | Enter continue | A toggle all",
            ),
            section="migration",
        )
        selected_text = {str(value).split(" ", 1)[0].lower() for value in selected}
        return [source.name for source in detected if source.name in selected_text]
    choices = [
        choice_cls(
            title=_MIGRATION_SOURCE_LABELS.get(source.name, source.name),
            value=source.name,
            checked=True,
            description=str(source.path),
        )
        for source in detected
    ]
    selected = _ask_or_cancel(
        questionary.checkbox(
            "Select sources to import",
            choices=choices,
            instruction="Space select | Enter continue | A toggle all",
        ),
        section="migration",
    )
    return [str(value) for value in selected]


def _print_migration_summary(result: Any, *, title: str) -> None:
    console.print(f"[bold {ACCENT}]◆[/] [bold]{title}[/]")
    mode = "applied" if result.apply else "dry-run"
    for name in result.selected:
        report = result.reports.get(name, {})
        label = _MIGRATION_SOURCE_LABELS.get(name, name)
        counts = report_status_counts(report)
        pieces = [
            f"{status}={count}"
            for status, count in sorted(counts.items())
            if count
        ]
        summary = ", ".join(pieces) if pieces else "no changes"
        console.print(f"  {label}: {mode}; {summary}")
        output_dir = str(report.get("output_dir") or "")
        report_file = Path(output_dir) / "report.json" if output_dir else None
        if output_dir and (result.apply or (report_file is not None and report_file.is_file())):
            console.print(f"    [dim]Report:[/dim] {output_dir}")


def _migration_result_path(
    cfg: Any,
    migration_result: Any | None,
    *,
    config_path: Path,
) -> PersistResult:
    if migration_result is None:
        return PersistResult(
            path=_config_path_from_loaded_config(cfg),
            backup_path=None,
            restart_required=False,
        )
    return PersistResult(
        path=config_path,
        backup_path=None,
        restart_required=bool(migration_result.apply),
    )


def _reload_after_migration(config_path: Path, fallback: Any):
    try:
        return load_config(config_path)
    except Exception as exc:
        console.print(
            warning_panel(
                f"Imported configuration could not be reloaded: {exc}. "
                "Continuing with the pre-migration onboarding state."
            )
        )
        return fallback


def _keep_imported_provider(questionary, cfg: Any) -> bool:
    llm = getattr(cfg, "llm", None)
    provider = str(getattr(llm, "provider", "") or "")
    model = str(getattr(llm, "model", "") or "")
    router_supported = _imported_provider_router_supported(cfg)
    if provider:
        console.print(
            f"[bold {ACCENT}]◆[/] [bold]Imported provider settings found[/]"
        )
        console.print(f"  Provider: [{ACCENT_SOFT}]{markup_escape(provider)}[/]")
        if router_supported:
            console.print(
                "  Model: [dim]will use SquillaRouter defaults; "
                "old direct model is not imported[/dim]"
            )
        elif model:
            console.print(f"  Model: [{ACCENT_SOFT}]{markup_escape(model)}[/]")
    return bool(
        _ask_or_cancel(
            questionary.confirm("Use imported provider credentials?", default=True),
            section="provider",
        )
    )


def _imported_provider_router_supported(cfg: Any) -> bool:
    llm = getattr(cfg, "llm", None)
    provider = str(getattr(llm, "provider", "") or "")
    if not provider:
        return False
    try:
        spec = get_provider_setup_spec(provider)
    except KeyError:
        return False
    return bool(getattr(spec, "router_supported", False))


def _provider_id_from_config(cfg: Any) -> str:
    llm = getattr(cfg, "llm", None)
    return str(getattr(llm, "provider", "") or "")


def _imported_provider_key_payload(llm: Any) -> dict[str, str]:
    api_key = str(getattr(llm, "api_key", "") or "")
    api_key_env = str(getattr(llm, "api_key_env", "") or "")
    if api_key:
        api_key_env = ""
    return {"api_key": api_key, "api_key_env": api_key_env}


def _use_imported_provider_credentials_with_router_defaults(
    questionary,
    cfg: Any,
    *,
    requested_mode: str,
    explicit_mode: bool = False,
    config_path: str | Path | None = None,
):
    llm = getattr(cfg, "llm", None)
    provider = _provider_id_from_config(cfg)
    key_payload = _imported_provider_key_payload(llm)
    res = upsert_llm_provider(
        cfg,
        provider_id=provider,
        model="",
        api_key=key_payload["api_key"],
        api_key_env=key_payload["api_key_env"],
        base_url=str(getattr(llm, "base_url", "") or ""),
        proxy=str(getattr(llm, "proxy", "") or ""),
        provider_routing=dict(getattr(llm, "provider_routing", {}) or {}),
    )
    cfg_after_provider = res.config
    if requested_mode:
        cfg_after_provider = _apply_router_section(
            questionary,
            cfg_after_provider,
            provider_id=provider,
            requested_mode=requested_mode,
            explicit_mode=explicit_mode,
            config_path=config_path,
        )
    return cfg_after_provider


def _complete_imported_provider_credentials(questionary, cfg: Any):
    llm = getattr(cfg, "llm", None)
    provider = str(getattr(llm, "provider", "") or "")
    model = str(getattr(llm, "model", "") or "")
    base_url = str(getattr(llm, "base_url", "") or "")
    imported_env_key = str(getattr(llm, "api_key_env", "") or "")
    if not provider or not model:
        return None
    try:
        spec = get_provider_setup_spec(provider)
    except KeyError:
        return None
    if not spec.runtime_supported or not spec.requires_api_key:
        return None
    if spec.requires_base_url and not base_url:
        return None

    console.print(
        warning_panel(
            "Provider settings were imported, but no usable API key is available. "
            "Set the key now to finish onboarding."
        )
    )
    credentials = _ask_imported_provider_credentials(
        questionary,
        spec,
        imported_env_key=imported_env_key,
    )
    res = upsert_llm_provider(
        cfg,
        provider_id=provider,
        model="" if _imported_provider_router_supported(cfg) else model,
        api_key=credentials["api_key"],
        api_key_env=credentials["api_key_env"],
        base_url=base_url,
    )
    return res.config


def _ask_imported_provider_credentials(
    questionary,
    spec,
    *,
    imported_env_key: str,
) -> dict[str, str]:
    choices = [_PASTE_API_KEY_CHOICE]
    seen_env_keys: set[str] = set()
    for env_key in (imported_env_key, spec.env_key):
        if env_key and env_key not in seen_env_keys:
            seen_env_keys.add(env_key)
            choices.append(
                _api_key_env_choice(env_key, detected=bool(os.environ.get(env_key)))
            )
    detected_choice = next((choice for choice in choices if _DETECTED_ENV_SUFFIX in choice), None)
    key_source = _ask_or_cancel(
        questionary.select(
            "LLM API key source",
            choices=choices,
            default=detected_choice or _PASTE_API_KEY_CHOICE,
        ),
        section="provider",
    )
    selected_env_key = _api_key_env_from_choice(key_source or "")
    if selected_env_key:
        return {"api_key": "", "api_key_env": selected_env_key}
    return {
        "api_key": _ask_or_cancel(
            questionary.password(
                "API key",
                validate=_secret_value_validator("API key"),
            ),
            section="provider",
        ),
        "api_key_env": "",
    }


def _ensure_config_dir_writable(config_path: str | Path | None) -> None:
    """Fail fast (exit code 2) when the config directory cannot be written.

    The wizard saves only after every prompt has been answered, so an
    unwritable state/config directory used to surface as a raw
    ``PermissionError`` traceback after the operator had already typed
    everything — including the API key. Probe writability before the first
    prompt so the failure is actionable and costs no input.
    """
    target, _source = resolve_config_path(config_path)
    if target.is_symlink():
        # persist_config writes through the symlink into the parent of the
        # TARGET file; probing the link's own parent would pass even when
        # the directory that actually receives the temp file is read-only.
        target = target.resolve()
    directory = target.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, probe_name = tempfile.mkstemp(
            prefix=".opensquilla-setup-probe-", dir=str(directory)
        )
        os.close(fd)
        os.unlink(probe_name)
    except OSError as exc:
        console.print(
            warning_panel(
                f"Cannot write the configuration directory "
                f"{markup_escape(str(directory))}: {markup_escape(str(exc))}\n\n"
                "Fix the directory permissions, or point --config / "
                "OPENSQUILLA_GATEWAY_CONFIG_PATH at a writable location, "
                "then re-run setup.",
                title="Setup directory not writable",
            )
        )
        raise SystemExit(2) from exc


_ONBOARD_UPDATE_CHOICE = "Update settings (keep current values as defaults)"
_ONBOARD_SECTIONS_CHOICE = "Change specific sections"
_ONBOARD_RESET_CHOICE = "Start fresh (back up current config first)"


# The only OnboardOptions fields whose non-default values still describe a
# plain full re-run: an explicit --config targets a different file but not a
# different walk. EVERY other field (including future additions) scopes the
# run automatically — a new option can never silently trigger the re-run
# fork without being reviewed onto this allowlist.
_FORK_COMPATIBLE_OPTION_FIELDS = frozenset({"config_path"})


def _is_full_interactive_rerun(options: OnboardOptions) -> bool:
    """True only for a plain `opensquilla onboard` over a working install.

    Scoped invocations (configure provider re-runs, --if-needed, --minimal,
    any skip flag, an explicit --router, or headless-shaped
    provider/model/key options) keep the linear walk so their pinned prompt
    sequences stay unchanged. Detection compares field-by-field against a
    fresh ``OnboardOptions()`` so a future field cannot drift out of the
    check unnoticed.
    """
    defaults = OnboardOptions()
    return all(
        getattr(options, fld.name) == getattr(defaults, fld.name)
        for fld in fields(OnboardOptions)
        if fld.name not in _FORK_COMPATIBLE_OPTION_FIELDS
    )


def _ask_existing_setup_action(
    questionary,
    cfg,
    status,
    options: OnboardOptions,
) -> str | None:
    """Re-run fork: 'update' (linear walk), 'sections' (hub), or 'reset'.

    Returns ``None`` when the fork does not apply (fresh install, unfinished
    setup, or a scoped run) — the caller then proceeds exactly as before.
    Declining the reset confirmation falls back to 'update' rather than
    aborting; Esc anywhere cancels the whole run like every wizard prompt.
    """
    if not status.has_config or not status.llm_configured:
        return None
    if not _is_full_interactive_rerun(options):
        return None
    resolved, _source = resolve_config_path(options.config_path)
    provider = str(getattr(cfg.llm, "provider", "") or "")
    model = str(getattr(cfg.llm, "model", "") or "")
    router_word = "SquillaRouter" if cfg.squilla_router.enabled else "router disabled"
    llm_part = f"{provider} / {model}".strip(" /")
    summary = " · ".join(part for part in (llm_part, router_word) if part)
    console.print(
        f"[{ACCENT_DIM}]Existing setup detected:[/] [{ACCENT_SOFT}]{markup_escape(summary)}[/]"
        f" [dim]({markup_escape(str(resolved))})[/dim]"
    )
    choice = _ask_or_cancel(
        questionary.select(
            "This install is already configured — what would you like to do?",
            choices=[
                _ONBOARD_UPDATE_CHOICE,
                _ONBOARD_SECTIONS_CHOICE,
                _ONBOARD_RESET_CHOICE,
            ],
            default=_ONBOARD_UPDATE_CHOICE,
        ),
        section="onboard",
    )
    if choice == _ONBOARD_SECTIONS_CHOICE:
        return "sections"
    if choice == _ONBOARD_RESET_CHOICE:
        confirmed = _ask_or_cancel(
            questionary.confirm(
                f"Back up {resolved} and restart setup from defaults?",
                default=False,
            ),
            section="onboard",
        )
        return "reset" if confirmed else "update"
    return "update"


def _backup_and_reset_config(config_path: str | Path | None) -> tuple[Path, Path] | None:
    """Move the existing config aside (never delete) before a fresh walk.

    The backup is created by the shared ``make_config_backup`` helper — the
    same collision-safe (O_EXCL) writer the persist/migration paths use — so
    every config backup in the state dir carries one naming scheme
    (``config.toml.backup.<stamp>``) that backup-aware tooling such as the
    uninstall purge inventory already recognizes.

    Returns ``(original_target, backup_path)`` so a cancelled fresh walk can
    restore the previous config, or ``None`` when nothing existed to back up.
    """
    from opensquilla.gateway.config_migration import make_config_backup

    resolved, _source = resolve_config_path(config_path)
    # Back up the real file next to itself and leave the symlink in place:
    # the fresh walk then writes through the untouched link as before.
    target = resolved.resolve() if resolved.is_symlink() else resolved
    if not target.exists():
        return None
    backup = make_config_backup(target)
    target.unlink()
    console.print(
        f"[dim]Backed up existing config to {markup_escape(str(backup))}[/dim]"
    )
    return target, backup


def _restore_reset_backup(reset_backup: tuple[Path, Path]) -> None:
    """Put the pre-"Start fresh" config back after a cancelled fresh walk.

    Cancelling after the reset must leave the previous config active — an
    Esc'd walk otherwise strands the install unconfigured with only a dim
    backup notice. The restore is skipped when the fresh walk already
    persisted a new config (the operator's new answers win over the backup).
    """
    target, backup = reset_backup
    if target.exists() or not backup.exists():
        return
    os.replace(backup, target)
    console.print(
        f"[dim]Setup cancelled — restored previous config to "
        f"{markup_escape(str(target))}[/dim]"
    )


def run_interactive_onboard(options: OnboardOptions) -> PersistResult:
    cfg = load_config(options.config_path)
    status = get_onboarding_status(cfg)
    if options.if_needed and status.has_config and not status.needs_onboarding:
        return persist_config(
            cfg,
            path=options.config_path,
            restart_required=False,
            backup=False,
        )

    if not _is_tty():
        return _print_noninteractive_hint(cfg, options.config_path)

    _ensure_config_dir_writable(options.config_path)

    import questionary as _qmod
    questionary = _styled(_qmod)

    if not options.scoped_section:
        # A scoped section edit (configure provider, the hub's Provider item)
        # is not a first run: re-printing the banner and blocking on the raw
        # "Press Enter" gate inside a one-section edit is wrong, and Ctrl+C at
        # that raw input() would escape the hub's cancel handling entirely.
        console.print(
            banner_panel(
                "OpenSquilla Onboarding",
                "Migration · Provider · SquillaRouter · Channels · Capabilities",
            )
        )
        _wait_for_setup_start()
    if options.if_needed and status.has_config and status.llm_configured:
        sections_result = _run_action_required_optional_sections(
            questionary,
            status,
            options=options,
        )
        persist = persist_config(
            load_config(options.config_path),
            path=options.config_path,
            restart_required=False,
            backup=False,
        )
        if sections_result is not None:
            persist = _merge_persist_results(sections_result, persist)
        return persist

    config_path = _config_path_from_loaded_config(cfg)
    rerun_action = _ask_existing_setup_action(questionary, cfg, status, options)
    if rerun_action == "sections":
        hub_result = _run_configure_hub(questionary, config_path=options.config_path)
        if hub_result is not None:
            return hub_result
        # Explicit "Exit (nothing changed)": report the resolved path without
        # rewriting the file — a no-change exit must not normalize a
        # hand-maintained config.toml (comments, key order, mode, mtime).
        resolved, _source = resolve_config_path(options.config_path)
        return PersistResult(
            path=resolved,
            backup_path=None,
            restart_required=False,
            warnings=[],
        )
    if rerun_action == "reset":
        # Pin the pre-reset path: after the backup renames a cwd-resolved
        # ./opensquilla.toml away, a dynamic re-resolve would silently switch
        # the rest of the walk to the HOME config — seeding from and
        # overwriting a different file than the one just backed up.
        reset_backup = _backup_and_reset_config(config_path)
        options = replace(options, config_path=config_path)
        cfg = load_config(config_path)
        status = get_onboarding_status(cfg)
        if reset_backup is not None:
            try:
                return _run_onboard_walk(
                    questionary, cfg, status, options, config_path=config_path
                )
            except (UserCancelledError, KeyboardInterrupt):
                # Cancelling after "Start fresh" must leave the previous
                # config active, not an unconfigured install.
                _restore_reset_backup(reset_backup)
                raise

    return _run_onboard_walk(questionary, cfg, status, options, config_path=config_path)


def _effective_router_mode(options: OnboardOptions, cfg: Any) -> str:
    """Resolve the walk's router mode when ``--router`` was not passed.

    An omitted flag is keep-current: the router consent prompt defaults to
    the stored state (a re-run over a disabled router must not steer the
    operator toward re-enabling it). A fresh install keeps the historical
    first-run default of the recommended profile.
    """
    if options.router_mode:
        return options.router_mode
    enabled = bool(getattr(getattr(cfg, "squilla_router", None), "enabled", True))
    return "recommended" if enabled else "disabled"


def _run_onboard_walk(
    questionary,
    cfg: Any,
    status: Any,
    options: OnboardOptions,
    *,
    config_path: Path,
) -> PersistResult:
    """The linear wizard walk: migration, provider, router, optional sections."""
    requested_router_mode = _effective_router_mode(options, cfg)
    explicit_router_mode = options.router_mode is not None

    migration_result: Any | None = None
    if not options.skip_migration:
        migration_result = _run_onboard_migration_step(
            questionary,
            config_path=config_path,
        )
        if migration_result is not None:
            cfg = _reload_after_migration(config_path, cfg)
            status = get_onboarding_status(cfg)

    keep_imported = (
        migration_result is not None
        and not migration_result.has_error
        and status.llm_configured
        and _keep_imported_provider(questionary, cfg)
    )
    if keep_imported:
        try:
            if _imported_provider_router_supported(cfg):
                cfg_after_provider = _use_imported_provider_credentials_with_router_defaults(
                    questionary,
                    cfg,
                    requested_mode=requested_router_mode,
                    explicit_mode=explicit_router_mode,
                    config_path=options.config_path,
                )
                persist = persist_config(
                    cfg_after_provider,
                    path=options.config_path,
                    restart_required=False,
                )
            else:
                cfg_after_provider = cfg
                persist = _migration_result_path(cfg, migration_result, config_path=config_path)
        except Exception as exc:
            keep_imported = False
            console.print(
                warning_panel(
                    f"Imported provider settings could not be finalized: {exc}. "
                    "Continue provider setup to finish onboarding."
                )
            )
    if not keep_imported:
        completed_imported = (
            _complete_imported_provider_credentials(questionary, cfg)
            if migration_result is not None and not status.llm_configured
            else None
        )
        if completed_imported is not None:
            cfg_after_provider = completed_imported
            if _imported_provider_router_supported(cfg_after_provider):
                cfg_after_provider = _apply_router_section(
                    questionary,
                    cfg_after_provider,
                    provider_id=_provider_id_from_config(cfg_after_provider),
                    requested_mode=requested_router_mode,
                    explicit_mode=explicit_router_mode,
                    config_path=options.config_path,
                )
        else:
            if migration_result is not None and not status.llm_configured:
                console.print(
                    warning_panel(
                        "Provider settings were not fully usable after migration. "
                        "Continue provider setup to finish onboarding."
                    )
                )
            spec, provider_id = _ask_provider_choice(questionary, options)
            answers = _ask_provider_fields(questionary, spec, options, config=cfg)
            res = upsert_llm_provider(
                cfg,
                provider_id=provider_id,
                model=answers["model"],
                api_key=answers.get("api_key", ""),
                api_key_env=answers.get("api_key_env", ""),
                base_url=answers.get("base_url", ""),
                proxy=answers.get("proxy", ""),
            )
            cfg_after_provider = res.config
            cfg_after_provider = _apply_router_section(
                questionary,
                cfg_after_provider,
                provider_id=provider_id,
                requested_mode=requested_router_mode,
                explicit_mode=explicit_router_mode,
                config_path=options.config_path,
            )
        persist = persist_config(
            cfg_after_provider,
            path=options.config_path,
            restart_required=False,
        )

    if options.minimal:
        return persist

    if options.scoped_section:
        # A scoped section edit stops at its own section: the trailing
        # optional and action-required prompts belong to the full walk only.
        return persist

    if not options.skip_channels and questionary.confirm(
        "Configure a messaging channel now?", default=False
    ).ask():
        persist = _fold_persist_result(
            persist,
            _run_optional_section(
                section="channel",
                label="channel",
                runner=run_interactive_channel_add,
                args=(None,),
                config_path=options.config_path,
            ),
        )

    if not options.skip_search and questionary.confirm(
        "Configure web search now?", default=False
    ).ask():
        persist = _fold_persist_result(
            persist,
            _run_optional_section(
                section="search",
                label="search",
                runner=run_interactive_search_configure,
                config_path=options.config_path,
            ),
        )

    if not options.skip_image_generation and questionary.confirm(
        "Enable image generation now?", default=False
    ).ask():
        persist = _fold_persist_result(
            persist,
            _run_optional_section(
                section="image-generation",
                label="image generation",
                runner=run_interactive_image_generation_configure,
                config_path=options.config_path,
            ),
        )

    refreshed_status = get_onboarding_status(load_config(options.config_path))
    persist = _fold_persist_result(
        persist,
        _run_action_required_optional_sections(
            questionary,
            refreshed_status,
            options=options,
            sections=("memory_embedding",),
        ),
    )

    return persist


def _fold_persist_result(
    persist: PersistResult, latest: PersistResult | None
) -> PersistResult:
    """Fold an optional section's save into the walk's returned result.

    Optional sections persist on their own; dropping their ``PersistResult``
    used to lose a ``restart_required`` signal before it reached the CLI's
    restart guidance.
    """
    if latest is None:
        return persist
    return _merge_persist_results(persist, latest)


def _run_optional_section(
    *,
    section: str,
    label: str,
    runner,
    args: tuple[Any, ...] = (),
    kwargs: dict | None = None,
    config_path: str | Path | None = None,
) -> PersistResult | None:
    """Run an optional onboarding step, isolating cancellation from siblings.

    ``section`` is the slug consumed by ``opensquilla onboard configure <section>``;
    ``label`` is the user-facing wording (which can contain spaces). Only
    cancellation-shaped exceptions are caught here — real validation or
    programming errors propagate so they surface in the operator's terminal
    instead of being silently buried alongside the "skipping" message.

    Returns the runner's ``PersistResult`` (``None`` on cancel or for legacy
    runners without one) so a section's ``restart_required`` reaches the CLI
    boundary instead of being discarded here.
    """
    try:
        runner_kwargs = {**(kwargs or {})}
        if config_path is not None:
            runner_kwargs.setdefault("config_path", config_path)
        result = runner(*args, **runner_kwargs)
        return result if isinstance(result, PersistResult) else None
    except UserCancelledError:
        config_arg = _config_cli_arg(config_path)
        console.print(
            f"[yellow]{label} setup cancelled — skipping.[/yellow]"
        )
        console.print(
            f"  [dim]Resume later with[/dim] "
            f"[{ACCENT_SOFT}]opensquilla onboard configure {section}{config_arg}[/]"
        )
        return None
    except KeyboardInterrupt:
        console.print(
            f"[yellow]{label} setup interrupted — skipping.[/yellow]"
        )
        return None


def _section_needs_action(status, section: str) -> bool:
    detail = status.section_details.get(section, {})
    return bool(detail.get("blocking") or detail.get("actionRequired"))


def _run_action_required_optional_sections(
    questionary,
    status,
    *,
    options: OnboardOptions,
    sections: tuple[str, ...] = (
        "router",
        "ensemble",
        "channels",
        "search",
        "image_generation",
        "memory_embedding",
    ),
) -> PersistResult | None:
    actions = {
        "router": {
            "prompt": "Configure SquillaRouter now?",
            "section": "router",
            "label": "router",
            "runner": run_interactive_router_configure,
            "skip": False,
        },
        # The ensemble step is opt-in only: its verifier never reports an
        # action-required state today (ok when enabled, optional when
        # disabled), so it never adds a prompt to the default quick-start
        # sequence — it is reachable via `onboard configure ensemble` and
        # would surface here only if a future verifier state demands action.
        "ensemble": {
            "prompt": "Configure the LLM ensemble now?",
            "section": "ensemble",
            "label": "ensemble",
            "runner": run_interactive_ensemble_configure,
            "skip": False,
        },
        "channels": {
            "prompt": "Configure messaging channels now?",
            "section": "channels",
            "label": "channel",
            "runner": run_interactive_channel_add,
            "args": (None,),
            "skip": options.skip_channels,
        },
        "search": {
            "prompt": "Configure web search now?",
            "section": "search",
            "label": "search",
            "runner": run_interactive_search_configure,
            "skip": options.skip_search,
        },
        "image_generation": {
            "prompt": "Fix image generation now?",
            "section": "image-generation",
            "label": "image generation",
            "runner": run_interactive_image_generation_configure,
            "skip": options.skip_image_generation,
        },
        "memory_embedding": {
            "prompt": "Configure memory embeddings now?",
            "section": "memory-embedding",
            "label": "memory embedding",
            "runner": run_interactive_memory_embedding_configure,
            "skip": False,
        },
    }
    aggregated: PersistResult | None = None
    for name in sections:
        action = actions.get(name)
        if not action or action.get("skip") or not _section_needs_action(status, name):
            continue
        if not questionary.confirm(str(action["prompt"]), default=True).ask():
            continue
        result = _run_optional_section(
            section=str(action["section"]),
            label=str(action["label"]),
            runner=action["runner"],
            args=cast(tuple[Any, ...], action.get("args", ())),
            config_path=options.config_path,
        )
        if result is not None:
            aggregated = _merge_persist_results(aggregated, result)
    return aggregated


def run_interactive_channel_add(
    type_name: str | None,
    *,
    config_path: str | Path | None = None,
) -> PersistResult:
    if not _is_tty():
        return _print_noninteractive_hint(config_path, section="channels")

    import questionary as _qmod
    questionary = _styled(_qmod)

    if type_name is None:
        type_name = _ask_or_cancel(
            questionary.select(
                "Channel type",
                choices=[s.type for s in list_channel_setup_specs()],
            ),
            section="channels",
        )
    spec = get_channel_setup_spec(type_name)
    _print_channel_intro(spec)
    answers = _ask_channel_fields(questionary, spec, type_name=type_name)
    _warn_channel_dependency_gaps(spec, answers)

    cfg = load_config(config_path)
    res = upsert_channel(cfg, entry_payload=answers)
    persisted = persist_config(res.config, path=config_path, restart_required=True)
    _print_channel_saved(str(res.public_payload.get("name") or answers.get("name")))
    return persisted


def run_interactive_channel_edit(
    name: str | None = None,
    *,
    config_path: str | Path | None = None,
) -> PersistResult:
    if not _is_tty():
        return _print_noninteractive_hint(config_path, section="channels")

    import questionary as _qmod
    questionary = _styled(_qmod)

    cfg = load_config(config_path)
    existing_entries = [e.model_dump(mode="python") for e in cfg.channels.channels]
    if not existing_entries:
        console.print(
            f"[{ACCENT_DIM}]no channels to edit[/]"
            " [dim]· run `opensquilla onboard configure channels` to add one[/dim]"
        )
        return persist_config(
            cfg,
            path=config_path,
            restart_required=False,
            backup=False,
        )

    if name is None:
        name = _ask_or_cancel(
            questionary.select(
                "Channel to edit",
                choices=[e["name"] for e in existing_entries],
            ),
            section="channels",
        )
    target_entry = next(e for e in existing_entries if e["name"] == name)
    type_name = target_entry["type"]
    spec = get_channel_setup_spec(type_name)

    _print_channel_intro(spec)
    answers = _ask_channel_fields(
        questionary,
        spec,
        type_name=type_name,
        current={**target_entry, "name": name},
    )
    _warn_channel_dependency_gaps(spec, answers)

    res = upsert_channel(cfg, entry_payload=answers)
    persisted = persist_config(res.config, path=config_path, restart_required=True)
    _print_channel_saved(str(res.public_payload.get("name") or name))
    return persisted


_CONFIGURE_MENU_SECTIONS: tuple[tuple[str, str, str], ...] = (
    # (menu label, configure slug, key in OnboardingStatus.sections)
    ("Provider", "provider", "llm"),
    ("Router", "router", "router"),
    ("Ensemble", "ensemble", "ensemble"),
    ("Channels", "channels", "channels"),
    ("Web search", "search", "search"),
    ("Image generation", "image-generation", "image_generation"),
    ("Memory embedding", "memory-embedding", "memory_embedding"),
)


def _merge_persist_results(
    previous: PersistResult | None, latest: PersistResult
) -> PersistResult:
    """Fold one hub section's save into the running result.

    The hub can persist several sections in one sitting; the CLI boundary
    prints restart guidance from a single ``PersistResult``, so the flag must
    stay sticky once any section required a restart.
    """
    if previous is None:
        return latest
    merged_warnings = list(previous.warnings)
    merged_warnings.extend(w for w in latest.warnings if w not in merged_warnings)
    return PersistResult(
        path=latest.path,
        backup_path=latest.backup_path or previous.backup_path,
        restart_required=previous.restart_required or latest.restart_required,
        warnings=merged_warnings,
    )


def _run_configure_hub(
    questionary,
    *,
    config_path: str | Path | None = None,
) -> PersistResult | None:
    """Menu loop over the configure sections: edit one, return to the menu.

    Each section persists as soon as it completes, so progress survives a
    later cancel. Cancelling INSIDE a section (Esc or Ctrl+C) returns to the
    menu (the section is simply left unchanged). Cancelling AT the menu
    raises ``UserCancelledError`` like every other wizard prompt — unless
    sections were already persisted this sitting, in which case the cancel
    exits like "Done": the saved changes (and their restart guidance) are
    on disk and must not be reported as "Setup cancelled".
    """
    console.print(
        f"[{ACCENT_DIM}]Voice audio is configured from the Web UI setup page; "
        "`opensquilla onboard catalog audio` lists the options.[/]"
    )
    aggregated: PersistResult | None = None
    while True:
        status = get_onboarding_status(load_config(config_path))
        title_to_slug: dict[str, str] = {}
        choices: list[str] = []
        for label, slug, status_key in _CONFIGURE_MENU_SECTIONS:
            state = status.sections.get(status_key)
            word = SECTION_STATUS_DISPLAY.get(state, "") if state is not None else ""
            title = f"{label} — {word}" if word else label
            title_to_slug[title] = slug
            choices.append(title)
        done_title = "Done" if aggregated is not None else "Exit (nothing changed)"
        choices.append(done_title)
        try:
            picked = _ask_or_cancel(
                questionary.select("Section", choices=choices),
                section="configure",
            )
        except (UserCancelledError, KeyboardInterrupt):
            if aggregated is None:
                raise
            console.print(
                "[dim]Exiting — the sections saved this sitting are kept.[/dim]"
            )
            return aggregated
        picked_slug = title_to_slug.get(picked)
        if picked_slug is None:
            return aggregated
        try:
            result = _run_configure_section(
                picked_slug, questionary, config_path=config_path
            )
        except (UserCancelledError, KeyboardInterrupt):
            # Ctrl+C inside a section is the same operator intent as Esc:
            # leave the section unchanged and return to the menu instead of
            # aborting the whole hub sitting.
            console.print(
                f"[dim]{picked_slug} left unchanged — back to the section menu.[/dim]"
            )
            continue
        if result is not None:
            aggregated = _merge_persist_results(aggregated, result)


def run_interactive_configure(
    section: str | None = None,
    *,
    config_path: str | Path | None = None,
) -> PersistResult | None:
    if not _is_tty():
        cfg = load_config(config_path)
        _print_noninteractive_hint(cfg, config_path, section=section)
        return None

    import questionary as _qmod
    questionary = _styled(_qmod)

    if section is not None:
        # Explicit section: one-shot edit, exactly like `configure <section>`
        # has always behaved.
        return _run_configure_section(section, questionary, config_path=config_path)
    return _run_configure_hub(questionary, config_path=config_path)


def _run_configure_section(
    section: str,
    questionary,
    *,
    config_path: str | Path | None = None,
) -> PersistResult | None:
    if section in {"provider", "providers"}:
        # A scoped provider re-run: swapping a key must not re-trigger the
        # legacy-migration pre-step, the first-run banner and "Press Enter"
        # start gate, or the optional capability prompts the full first-run
        # wizard walks through.
        return run_interactive_onboard(
            OnboardOptions(
                skip_channels=True,
                skip_search=True,
                skip_image_generation=True,
                skip_migration=True,
                config_path=config_path,
                scoped_section=True,
            )
        )
    if section == "router":
        return run_interactive_router_configure(config_path=config_path)
    if section in ENSEMBLE_SECTION_ALIASES:
        return run_interactive_ensemble_configure(config_path=config_path)
    if section in {"channel", "channels"}:
        existing = load_config(config_path).channels.channels
        if existing:
            mode = _ask_or_cancel(
                questionary.select(
                    "Channel action",
                    choices=["add", "edit"],
                    default="add",
                ),
                section="channels",
            )
            if mode == "edit":
                return run_interactive_channel_edit(None, config_path=config_path)
        return run_interactive_channel_add(None, config_path=config_path)
    if section == "search":
        return run_interactive_search_configure(config_path=config_path)
    # The alias sets are shared with the setup engine and the CLI help, so
    # every advertised spelling (including the short "image"/"memory"
    # aliases) reaches the wizard instead of the unsupported-section notice.
    if section in IMAGE_GENERATION_SECTION_ALIASES:
        return run_interactive_image_generation_configure(config_path=config_path)
    if section in MEMORY_EMBEDDING_SECTION_ALIASES:
        return run_interactive_memory_embedding_configure(config_path=config_path)
    fallback_path, _source = resolve_config_path(config_path)
    console.print(
        f"[{ACCENT_DIM}]section[/] [{ACCENT_SOFT}]{markup_escape(repr(section))}[/]"
        " [dim]not yet supported in the wizard · edit "
        f"{markup_escape(str(fallback_path))} directly[/dim]"
    )
    return None
