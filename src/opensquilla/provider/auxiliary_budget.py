"""Budget helpers for one-shot generative provider calls outside the turn loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opensquilla.context_budget import ContextBudgetGovernor
from opensquilla.provider.model_catalog import (
    resolve_effective_context_window,
    shared_catalog,
)
from opensquilla.provider.protocol import provider_metadata
from opensquilla.token_estimation import estimate_tokens

_UNKNOWN_DEPLOYMENT_CONTEXT_TOKENS = 32_000


@dataclass(frozen=True)
class AuxiliaryRequestBudget:
    """Resolved, non-secret budget for one physical auxiliary deployment."""

    provider_id: str
    model: str
    context_window_tokens: int
    max_output_tokens: int
    max_input_tokens: int
    provider_request_max_chars: int
    context_window_source: str


class AuxiliaryRequestTooLargeError(ValueError):
    """Raised before an auxiliary provider call whose text cannot fit."""

    def __init__(
        self,
        *,
        actual_chars: int,
        max_chars: int,
        actual_tokens: int = 0,
        max_tokens: int = 0,
    ) -> None:
        self.actual_chars = max(0, int(actual_chars))
        self.max_chars = max(1, int(max_chars))
        self.actual_tokens = max(0, int(actual_tokens))
        self.max_tokens = max(0, int(max_tokens))
        if self.max_tokens and self.actual_tokens > self.max_tokens:
            detail = f"{self.actual_tokens} > {self.max_tokens} tokens"
        else:
            detail = f"{self.actual_chars} > {self.max_chars} chars"
        super().__init__(
            "auxiliary provider input exceeds the resolved deployment budget "
            f"({detail})"
        )


def resolve_auxiliary_request_budget(
    provider: object | None,
    *,
    max_output_tokens: int,
    provider_id: str = "",
    model: str = "",
    context_window_tokens: int = 0,
    provider_request_max_chars: int = 0,
    context_overflow_threshold: float = 0.85,
) -> AuxiliaryRequestBudget:
    """Bind an auxiliary request to the provider/model that will execute it."""

    try:
        metadata = provider_metadata(provider)
        metadata_provider_id = metadata.provider_id
        metadata_provider_name = metadata.provider_name
        metadata_provider_kind = metadata.provider_kind
        metadata_model = metadata.model
    except Exception:  # noqa: BLE001 - derive a nonzero cap without optional metadata
        metadata_provider_id = str(getattr(provider, "provider_id", "") or "")
        metadata_provider_name = str(getattr(provider, "provider_name", "") or "")
        metadata_provider_kind = str(getattr(provider, "provider_kind", "") or "")
        metadata_model = str(getattr(provider, "model", "") or "")
    # A concrete adapter is the authority for the physical deployment. Hints
    # only fill gaps for legacy stubs and call sites that intentionally resolve
    # a budget before constructing the adapter.
    resolved_provider = (
        metadata_provider_id
        or metadata_provider_name
        or metadata_provider_kind
        or str(provider_id or "").strip()
    )
    resolved_model = metadata_model or str(model or "").strip()
    catalog = shared_catalog()
    configured_window = max(0, int(context_window_tokens or 0))
    hinted_provider = str(provider_id or "").strip()
    hinted_model = str(model or "").strip()
    metadata_provider_aliases = {
        value
        for value in (
            metadata_provider_id,
            metadata_provider_name,
            metadata_provider_kind,
        )
        if value
    }
    hint_matches_physical = (
        (
            not hinted_provider
            or not metadata_provider_aliases
            or hinted_provider in metadata_provider_aliases
        )
        and (not hinted_model or not metadata_model or hinted_model == metadata_model)
    )
    effective_configured_window = configured_window if hint_matches_physical else 0
    conservative_unknown_window = (
        effective_configured_window <= 0 and not resolved_model
    )
    window, window_source = resolve_effective_context_window(
        catalog,
        resolved_model,
        provider=resolved_provider,
        global_override=(
            _UNKNOWN_DEPLOYMENT_CONTEXT_TOKENS
            if conservative_unknown_window
            else effective_configured_window
        ),
    )
    if conservative_unknown_window:
        window_source = "conservative_default"
    resolved_output = int(
        catalog.resolve_max_tokens(
            resolved_model,
            user_override=max(0, int(max_output_tokens or 0)),
            provider=resolved_provider,
        )
    )
    snapshot = ContextBudgetGovernor.from_values(
        context_window_tokens=window,
        max_output_tokens=resolved_output,
        thinking_budget_tokens=0,
        context_overflow_threshold=context_overflow_threshold,
    ).snapshot()
    derived_cap = snapshot.provider_request_max_chars
    explicit_cap = max(0, int(provider_request_max_chars or 0))
    effective_cap = min(explicit_cap, derived_cap) if explicit_cap else derived_cap
    return AuxiliaryRequestBudget(
        provider_id=resolved_provider,
        model=resolved_model,
        context_window_tokens=max(1, int(window)),
        max_output_tokens=max(1, resolved_output),
        max_input_tokens=max(
            1,
            int(snapshot.usable_tokens * snapshot.threshold),
        ),
        provider_request_max_chars=max(1, int(effective_cap)),
        context_window_source=str(window_source),
    )


def _auxiliary_text_parts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return [part for item in value for part in _auxiliary_text_parts(item)]
    if isinstance(value, dict):
        block_type = str(value.get("type", "") or "")
        return [
            part
            for key, item in value.items()
            if not (
                key == "data"
                and block_type in {"document", "image", "image_url"}
            )
            for part in _auxiliary_text_parts(item)
        ]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _auxiliary_text_parts(model_dump(mode="python"))
    return []


def auxiliary_text_chars(messages: list[Any], *, system: str = "") -> int:
    """Conservatively count text sent by non-streaming completion shims.

    Binary media payloads are excluded because provider adapters budget those
    separately. The helper is used only for legacy ``complete`` capabilities
    that cannot carry ``ChatConfig`` and therefore need a local preflight.
    """

    return len(system or "") + sum(
        len(part)
        for message in messages
        for part in _auxiliary_text_parts(message)
    )


def auxiliary_text_tokens(messages: list[Any], *, system: str = "") -> int:
    """Conservatively estimate text tokens including message framing."""

    parts = [system] if system else []
    parts.extend(
        part
        for message in messages
        for part in _auxiliary_text_parts(message)
    )
    framing_tokens = 8 + (8 * len(messages))
    return framing_tokens + estimate_tokens("\n".join(parts))


def ensure_auxiliary_text_fits(
    messages: list[Any],
    *,
    max_chars: int,
    max_tokens: int = 0,
    system: str = "",
) -> int:
    """Reject an oversized auxiliary request before starting a physical call."""

    actual = auxiliary_text_chars(messages, system=system)
    if actual > max(1, int(max_chars)):
        raise AuxiliaryRequestTooLargeError(actual_chars=actual, max_chars=max_chars)
    resolved_max_tokens = max(0, int(max_tokens or 0))
    if resolved_max_tokens:
        actual_tokens = auxiliary_text_tokens(messages, system=system)
        if actual_tokens > resolved_max_tokens:
            raise AuxiliaryRequestTooLargeError(
                actual_chars=actual,
                max_chars=max_chars,
                actual_tokens=actual_tokens,
                max_tokens=resolved_max_tokens,
            )
    return actual
