"""Discovery-backed registry for gateway-managed channel entry models/builders."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, cast

import structlog
from pydantic import BaseModel

if TYPE_CHECKING:
    from opensquilla.channels.types import ManagedChannel

log = structlog.get_logger(__name__)

_COMMON_ENTRY_FIELDS = frozenset(
    {
        "name",
        "type",
        "enabled",
        "agent_id",
        "group_session_scope",
        "busy_input_mode",
        "dm_access",
        "allowed_senders",
    }
)
_INTERNAL = frozenset({"manager", "registry", "transports", "types"})
# Adapters that exist on disk but are intentionally hidden from
# auto-discovery. The implementation is kept for future first-class
# promotion; until then the runtime does not advertise them and there is
# no packaging extra installable for their third-party SDK.
_HIDDEN = frozenset({"msteams"})
_PLAIN_TEXT_MARKDOWN_HINT = (
    "This channel renders Markdown markers as literal text. Reply in plain text: "
    "avoid Markdown headings, bold/italic markers, tables, and fenced code unless "
    "the user explicitly asks for raw Markdown."
)
_MARKDOWN_RENDER_HINTS = {
    "whatsapp": _PLAIN_TEXT_MARKDOWN_HINT,
    "signal": _PLAIN_TEXT_MARKDOWN_HINT,
    "sms": _PLAIN_TEXT_MARKDOWN_HINT,
}


@dataclass(frozen=True)
class ChannelRegistration:
    """Resolved config model plus adapter factory for one channel type."""

    type_name: str
    entry_model: type[BaseModel]
    build_channel: Callable[[BaseModel], ManagedChannel]


def markdown_render_hint_for(type_name: str) -> str | None:
    """Return the dynamic prompt hint for channels that do not render Markdown."""

    return _MARKDOWN_RENDER_HINTS.get(type_name.strip().lower())


def discover_channel_names() -> list[str]:
    """Return builtin channel module names that may contribute registrations."""
    import opensquilla.channels as pkg

    return [
        name
        for _, name, ispkg in pkgutil.iter_modules(pkg.__path__)
        if not ispkg
        and not name.startswith("_")
        and name not in _INTERNAL
        and name not in _HIDDEN
    ]


def parse_channel_entry(value: Any) -> BaseModel:
    """Validate a raw channel entry dict against the registered entry model."""
    if isinstance(value, BaseModel):
        channel_type = getattr(value, "type", None)
    elif isinstance(value, dict):
        channel_type = value.get("type")
    else:
        raise TypeError("channel entries must be dicts or pydantic models")

    if not isinstance(channel_type, str) or not channel_type:
        raise ValueError("channel entries require a non-empty string type")

    registration = get_channel_registration(channel_type)
    if registration is None:
        raise ValueError(f"unknown channel type: {channel_type}")

    if isinstance(value, registration.entry_model):
        return value
    return registration.entry_model.model_validate(value)


def build_managed_channel(entry: BaseModel) -> ManagedChannel | None:
    """Build a managed adapter instance for ``entry`` from the registry."""
    channel_type = getattr(entry, "type", None)
    if not isinstance(channel_type, str) or not channel_type:
        return None
    registration = get_channel_registration(channel_type)
    if registration is None:
        return None
    return registration.build_channel(entry)


def get_channel_registration(type_name: str) -> ChannelRegistration | None:
    """Look up a registration by channel type."""
    return discover_all().get(type_name)


@lru_cache(maxsize=1)
def discover_all() -> dict[str, ChannelRegistration]:
    """Return builtin registrations merged with external plugins."""
    builtin: dict[str, ChannelRegistration] = {}
    for module_name in discover_channel_names():
        try:
            registration = _discover_builtin(module_name)
        except ImportError as exc:
            log.warning("channel_registry.builtin_load_failed", module=module_name, error=str(exc))
            continue
        if registration is not None:
            builtin[registration.type_name] = registration

    external = discover_plugins()
    shadowed = set(external) & set(builtin)
    if shadowed:
        log.warning("channel_registry.plugin_shadowed", types=sorted(shadowed))

    return {**external, **builtin}


def discover_plugins() -> dict[str, ChannelRegistration]:
    """Discover external channel registrations from entry points."""
    plugins: dict[str, ChannelRegistration] = {}
    for ep in entry_points(group="opensquilla.channels"):
        try:
            registration = _coerce_registration(ep.load())
        except Exception as exc:
            log.warning("channel_registry.plugin_load_failed", name=ep.name, error=str(exc))
            continue
        if registration is not None:
            plugins[registration.type_name] = registration
    return plugins


def _coerce_registration(candidate: Any) -> ChannelRegistration | None:
    if isinstance(candidate, ChannelRegistration):
        return candidate
    if callable(candidate):
        built = candidate()
        if isinstance(built, ChannelRegistration):
            return built
    return None


def _discover_builtin(module_name: str) -> ChannelRegistration | None:
    module = importlib.import_module(f"opensquilla.channels.{module_name}")
    explicit = _coerce_registration(getattr(module, "CHANNEL_REGISTRATION", None))
    if explicit is not None:
        return explicit

    entry_model = _resolve_entry_model(module_name, module)
    channel_class = _resolve_local_class(module, "Channel")
    if entry_model is None or channel_class is None:
        return None

    custom_builder = getattr(module, "build_channel_from_entry", None)
    if callable(custom_builder):
        return ChannelRegistration(module_name, entry_model, custom_builder)

    config_class = _resolve_local_class(module, "ChannelConfig")
    return ChannelRegistration(
        module_name,
        entry_model,
        lambda entry: _build_generic_channel(
            channel_class=channel_class,
            entry=entry,
            config_class=config_class,
        ),
    )


def _resolve_entry_model(module_name: str, module: Any) -> type[BaseModel] | None:
    local = _resolve_local_class(module, "ChannelEntry")
    if local is not None and issubclass(local, BaseModel):
        return local

    gateway_config = importlib.import_module("opensquilla.gateway.config")
    for obj in vars(gateway_config).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, BaseModel)
            and obj.__module__ == gateway_config.__name__
            and obj.__name__.endswith("ChannelEntry")
            and obj.model_fields.get("type") is not None
            and obj.model_fields["type"].default == module_name
        ):
            return obj
    return None


def _resolve_local_class(module: Any, suffix: str) -> type[Any] | None:
    for obj in vars(module).values():
        if (
            isinstance(obj, type)
            and obj.__module__ == module.__name__
            and obj.__name__.endswith(suffix)
        ):
            return obj
    return None


def _build_generic_channel(
    *,
    channel_class: type[Any],
    entry: BaseModel,
    config_class: type[Any] | None,
) -> ManagedChannel:
    data = entry.model_dump(exclude=cast(Any, _COMMON_ENTRY_FIELDS))
    signature = inspect.signature(channel_class)
    accepted = {
        name
        for name, param in signature.parameters.items()
        if param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }

    if config_class is not None and "config" in accepted:
        config_fields = getattr(config_class, "model_fields", {})
        config_kwargs = {key: value for key, value in data.items() if key in config_fields}
        if "name" in config_fields and hasattr(entry, "name"):
            config_kwargs["name"] = entry.name
        return cast("ManagedChannel", channel_class(config=config_class(**config_kwargs)))

    kwargs = {key: value for key, value in data.items() if key in accepted}
    return cast("ManagedChannel", channel_class(**kwargs))
