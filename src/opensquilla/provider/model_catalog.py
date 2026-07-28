"""ModelCatalog — in-memory cache of model metadata fetched from provider API."""

from __future__ import annotations

import fnmatch
import tomllib
from collections.abc import Mapping
from functools import cache
from importlib import resources
from typing import Any, Literal

import httpx
import structlog

from opensquilla.env import trust_env as _trust_env
from opensquilla.secrets import clean_header_secret

from .app_attribution import provider_app_headers
from .catalog_types import CatalogSource, ModelCatalogEntry, coerce_entry_field
from .models_dev import lookup_limits as _models_dev_limits
from .models_dev import lookup_model as _models_dev_model
from .ollama import _OLLAMA_DEFAULT_NUM_CTX
from .registry import LOCAL_RUNTIME_PROVIDERS
from .types import ModelCapabilities, ModelInfo

log = structlog.get_logger(__name__)

DEFAULT_MAX_TOKENS = 16384
SAFE_OPENROUTER_DEFAULT_MAX_TOKENS = 8192
DEFAULT_CONTEXT_WINDOW = 200_000

# Layer attribution for the ``*_with_source`` resolver variants. "override"
# is an explicit operator value (caller-supplied for max_tokens, the
# ``[models.*]`` user-override layer for context windows), "catalog" is any
# model-metadata layer (live catalog, models.dev snapshot, packaged static
# fallback), "default" is a hardcoded engine default.
MaxTokensSource = Literal["override", "catalog", "default"]
ContextWindowSource = Literal["override", "catalog", "default"]

# Local runtimes (Ollama, …) have unqualified model ids that miss the catalog
# and the packaged corrections, so the 200k cloud default would make the turn
# budget over-estimate and skip trimming while the runtime silently truncates.
# Report the runtime's own default window so budgeting matches what it
# actually allows. Membership lives in registry.py (LOCAL_RUNTIME_PROVIDERS)
# next to its keyless sibling set so the two cannot drift apart unnoticed.
_LOCAL_CONTEXT_WINDOW = _OLLAMA_DEFAULT_NUM_CTX

# One-release migration gate (recorded decision OQ#5). get_capabilities has
# always early-returned EMPTY capabilities (reasoning off, tools on, vision
# off, streaming on) for the anthropic and ollama providers instead of
# consulting any catalog. Keep that exact behavior for one release while the
# rest of the ladder moves to catalog data. When this flips to True, both
# providers resolve through the layered catalog like every other provider —
# real rows then change engine-level behavior: supports_vision from the
# catalog stops the engine stripping images for vision-capable models, and
# supports_tools starts gating tool wiring per model instead of always-on.
CATALOG_CAPABILITIES_FOR_ANTHROPIC_OLLAMA = False


def _price_per_1k(value: object) -> float:
    """Convert an OpenRouter per-token price string to a per-1k-token float.

    OpenRouter reports prices as per-token USD strings; downstream cost
    accounting expects per-1k-token floats. Missing or non-numeric values
    fall back to 0.0 (free / unknown).
    """
    try:
        return float(value) * 1000.0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Layered resolution (resolve_entry) — user > live > corrections > snapshot >
# synthesized. Each layer adapter returns a dict of only the fields it
# GENUINELY KNOWS for a model; merging is per field, so a lower layer fills
# only fields every higher layer left unset (see catalog_types.py for the
# per-type "unset" sentinels). get_capabilities resolves through this chain
# (host-trust branches excepted). The legacy resolve_max_tokens /
# resolve_context_window paths keep their own chain order (live > snapshot >
# corrections budgets > defaults); they consult the corrections data only in
# the slot the retired static fallback table occupied, via
# ``_corrections_budget_fallback``.
# ---------------------------------------------------------------------------

# Synthesized floor applied after all layers: conservative budgets for
# models nothing knows, so resolution never fails.
_SYNTHESIZED_DEFAULTS: dict[str, Any] = {
    "context_window": 32_768,
    "max_output_tokens": 8_192,
    "supports_tools": True,
    "supports_reasoning": False,
}

# Protocol variants that share one service-side model catalog. User and live
# overrides remain keyed to the exact configured provider; only packaged
# corrections use this alias.
_CORRECTIONS_PROVIDER_ALIASES: dict[str, str] = {
    "qwen_token_plan_anthropic": "qwen_token_plan",
}


def _normalize_corrections(payload: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Normalize a parsed catalog_overrides.toml payload.

    Provider and model keys are lowercased; field values are validated and
    coerced via ``coerce_entry_field``. Bad rows or fields are logged and
    dropped — packaged corrections degrade, they never crash resolution.
    """
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    for provider_key, models in payload.items():
        if not isinstance(models, Mapping):
            log.warning("model_catalog.corrections_bad_provider", provider=str(provider_key))
            continue
        table: dict[str, dict[str, Any]] = {}
        for model_key, fields in models.items():
            if not isinstance(fields, Mapping):
                log.warning(
                    "model_catalog.corrections_bad_entry",
                    provider=str(provider_key),
                    model=str(model_key),
                )
                continue
            entry: dict[str, Any] = {}
            for name, value in fields.items():
                try:
                    entry[str(name)] = coerce_entry_field(str(name), value)
                except ValueError as exc:
                    log.warning(
                        "model_catalog.corrections_bad_field",
                        provider=str(provider_key),
                        model=str(model_key),
                        error=str(exc),
                    )
            if entry:
                table[str(model_key).strip().lower()] = entry
        if table:
            tables[str(provider_key).strip().lower()] = table
    return tables


@cache
def _corrections_tables() -> dict[str, dict[str, dict[str, Any]]]:
    """Lazily load the packaged corrections file (catalog_overrides.toml)."""
    try:
        path = resources.files("opensquilla.provider").joinpath("catalog_overrides.toml")
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a missing/corrupt file degrades, never crashes
        log.warning("model_catalog.corrections_unavailable")
        return {}
    return _normalize_corrections(payload)


def _provider_corrections_budget(provider_id: str, model_id: str) -> tuple[int, int] | None:
    """Exact provider-scoped ``(max_output_tokens, context_window)`` correction.

    An exact (non-glob) corrections row keyed by the resolving provider is
    that provider's hand-authored budget contract, so it outranks the
    models.dev snapshot in the budget chain — matching ``resolve_entry``'s
    corrections-above-snapshot order. This is load-bearing for providers
    with no snapshot table (hosted aggregators like tokenrhythm): without
    it, the snapshot's cross-provider bare-id merge would serve foreign
    windows several times larger than the provider's real ones, and
    over-estimating a window causes silent server-side truncation. A
    dimension the row does not carry is returned as 0 (callers treat 0 as
    unknown); glob rows belong to the capability ladder and are never
    consulted for budgets.
    """
    provider_l = (provider_id or "").strip().lower()
    provider_l = _CORRECTIONS_PROVIDER_ALIASES.get(provider_l, provider_l)
    model_l = (model_id or "").strip().lower()
    if not provider_l or not model_l:
        return None
    entry = _corrections_tables().get(provider_l, {}).get(model_l)
    if not entry:
        return None
    max_output = int(entry.get("max_output_tokens") or 0)
    window = int(entry.get("context_window") or 0)
    if max_output <= 0 and window <= 0:
        return None
    return max_output, window


def _corrections_budget_fallback(model_id: str) -> tuple[int, int] | None:
    """Conservative ``(max_output_tokens, context_window)`` from corrections.

    Fills exactly the resolution slot the retired static fallback table
    occupied in the legacy ``resolve_max_tokens`` / ``resolve_context_window``
    chains: consulted only after the live catalog and the models.dev snapshot
    both miss. Like that table, the lookup is provider-agnostic and keyed by
    basename — the requested id and every exact (non-glob) corrections row
    key are normalized to the basename after the final ``/``, so a model
    resolves identically whether referenced bare (``moonshot-v1-8k``) or
    provider-qualified (``moonshot/moonshot-v1-8k``), and regardless of which
    provider table carries the row. Glob rows belong to the capability
    ladder and are never consulted for budgets.

    When several rows share a basename, the per-dimension minimum wins —
    over-estimating a context window causes silent server-side truncation,
    while under-estimating only triggers compaction earlier. A dimension no
    row knows is returned as 0 (callers treat 0 as unknown).
    """
    basename = (model_id or "").strip().lower().rsplit("/", 1)[-1]
    if not basename:
        return None
    max_outputs: list[int] = []
    windows: list[int] = []
    matched = False
    for table in _corrections_tables().values():
        for key, entry in table.items():
            if any(marker in key for marker in "*?["):
                continue
            if key.rsplit("/", 1)[-1] != basename:
                continue
            max_output = int(entry.get("max_output_tokens") or 0)
            window = int(entry.get("context_window") or 0)
            if max_output <= 0 and window <= 0:
                continue
            matched = True
            if max_output > 0:
                max_outputs.append(max_output)
            if window > 0:
                windows.append(window)
    if not matched:
        return None
    return (
        min(max_outputs) if max_outputs else 0,
        min(windows) if windows else 0,
    )


def _live_layer_fields(info: ModelInfo | None) -> dict[str, Any]:
    """Fields the live provider catalog knows, adapted per-1k → per-Mtok.

    Capability booleans are computed deterministically from the provider
    response at populate time, so they are emitted as known whenever the
    model is in the cache. A 0.0 per-1k price is the live cache's "free or
    unknown" sentinel, so costs are emitted only when positive — this layer
    never claims a known $0 price.
    """
    if info is None:
        return {}
    fields: dict[str, Any] = {
        "supports_reasoning": info.supports_reasoning,
        "supports_tools": info.supports_tools,
        "supports_vision": info.supports_vision,
    }
    if info.display_name:
        fields["display_name"] = info.display_name
    if info.context_window > 0:
        fields["context_window"] = info.context_window
    if info.max_output_tokens > 0:
        fields["max_output_tokens"] = info.max_output_tokens
    if info.supports_reasoning:
        # The live cache is the OpenRouter catalog; its reasoning models
        # stream through the OpenRouter dialect (matches get_capabilities).
        fields["reasoning_format"] = "openrouter"
    if info.input_cost_per_1k > 0:
        fields["input_cost_per_mtok"] = info.input_cost_per_1k * 1000.0
    if info.output_cost_per_1k > 0:
        fields["output_cost_per_mtok"] = info.output_cost_per_1k * 1000.0
    return fields


def _corrections_layer_fields(provider_id: str, model_id: str) -> dict[str, Any]:
    """Fields from the packaged corrections table for ``(provider, model)``.

    The exact (lowercased) model key is consulted first; every other key in
    the provider table is then tried as an fnmatch glob against the
    lowercased model id, in file order, each filling only fields still
    unset within this layer. No provider → no corrections.
    """
    if not provider_id:
        return {}
    provider_l = provider_id.strip().lower()
    provider_l = _CORRECTIONS_PROVIDER_ALIASES.get(provider_l, provider_l)
    table = _corrections_tables().get(provider_l)
    if not table:
        return {}
    model_l = model_id.strip().lower()
    fields: dict[str, Any] = {}
    exact = table.get(model_l)
    if exact:
        fields.update(exact)
    for pattern, entry in table.items():
        if pattern == model_l:
            continue
        if fnmatch.fnmatchcase(model_l, pattern):
            for name, value in entry.items():
                fields.setdefault(name, value)
    return fields


def _snapshot_layer_fields(provider_id: str, model_id: str) -> dict[str, Any]:
    """Fields from the vendored models.dev snapshot.

    The snapshot carries ``supports_reasoning`` as data but never a
    ``reasoning_format`` — the streaming dialect is provider knowledge the
    snapshot does not have. Optional per-Mtok cost keys (``in_mtok``,
    ``out_mtok``, ``cr_mtok``, ``cw_mtok``) are emitted when present:
    snapshot costs are explicit data, so a vendored 0 means a known-free
    price (unlike the live cache's 0.0 "free or unknown" sentinel).
    """
    entry = _models_dev_model(provider_id, model_id)
    if entry is None:
        return {}
    fields: dict[str, Any] = {}
    context_window = int(entry.get("ctx") or 0)
    max_output = int(entry.get("out") or 0)
    if context_window > 0:
        fields["context_window"] = context_window
    if max_output > 0:
        fields["max_output_tokens"] = max_output
    for snapshot_key, field_name in (
        ("reasoning", "supports_reasoning"),
        ("tools", "supports_tools"),
        ("vision", "supports_vision"),
    ):
        if snapshot_key in entry:
            fields[field_name] = bool(entry[snapshot_key])
    for snapshot_key, field_name in (
        ("in_mtok", "input_cost_per_mtok"),
        ("out_mtok", "output_cost_per_mtok"),
        ("cr_mtok", "cache_read_cost_per_mtok"),
        ("cw_mtok", "cache_write_cost_per_mtok"),
    ):
        value = entry.get(snapshot_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            fields[field_name] = float(value)
    return fields


def _capabilities_from_entry(entry: ModelCatalogEntry) -> ModelCapabilities:
    """Adapt one resolved ``ModelCatalogEntry`` to ``ModelCapabilities``.

    Reasoning is enabled only when the entry ALSO names a streaming dialect
    (``reasoning_format`` other than ``"none"``). The snapshot layer may
    know ``supports_reasoning=True`` for a model but it never carries a
    ``reasoning_format`` — the dialect is provider knowledge the snapshot
    does not have — and claiming reasoning without a dialect would send
    requests with no thinking toggle at all. This preserves the legacy
    fallback's deliberate semantics: for models only the snapshot knows,
    tools/vision are filled but reasoning stays OFF; the adaptation never
    invents a reasoning format.
    """
    reasoning_format = entry.reasoning_format
    supports_reasoning = entry.supports_reasoning and reasoning_format not in ("", "none")
    return ModelCapabilities(
        supports_reasoning=supports_reasoning,
        supports_tools=entry.supports_tools,
        supports_vision=entry.supports_vision,
        reasoning_format=reasoning_format if supports_reasoning else "none",
    )


class ModelCatalog:
    """In-memory cache of model metadata fetched from provider API.

    Priority chain for max_tokens:
      1. User config override (>0)
      2. Provider-scoped live ingest (set_live_provider_entries)
      3. API-fetched catalog value (bare-id OpenRouter cache)
      4. models.dev snapshot value
      5. Packaged corrections budgets (catalog_overrides.toml)
      6. DEFAULT_MAX_TOKENS (16384)
      → then clamp to min(value, context_window)
    """

    def __init__(self) -> None:
        self._models: dict[str, ModelInfo] = {}
        # User-override layer for resolve_entry; keys are lowercased
        # "provider/model" or bare model ids (see set_user_overrides).
        self._user_overrides: dict[str, dict[str, Any]] = {}
        # Provider-scoped live layer: boot-time ingest of a provider's own
        # public model listing (see provider/live_catalog.py). Keyed
        # provider -> lowercased model id -> validated entry fields.
        # Deliberately separate from the bare-id ``_models`` cache — that
        # cache is the OpenRouter catalog and its ids are provider-agnostic,
        # so aggregator rows placed there would leak windows into other
        # providers' resolutions of the same bare ids.
        self._live_provider_entries: dict[str, dict[str, dict[str, Any]]] = {}

    def __len__(self) -> int:
        return len(self._models)

    def _populate_from_data(self, models: list[dict]) -> None:
        """Parse a list of OpenRouter model dicts into ModelInfo entries."""
        for m in models:
            model_id = m.get("id", "")
            if not model_id:
                continue
            top_provider = m.get("top_provider") or {}
            max_completion = top_provider.get("max_completion_tokens") or 0
            supported = set(m.get("supported_parameters", []))
            architecture = m.get("architecture") or {}
            input_modalities = {
                str(item).lower() for item in architecture.get("input_modalities", [])
            }
            pricing = m.get("pricing") or {}
            self._models[model_id] = ModelInfo(
                provider="openrouter",
                model_id=model_id,
                display_name=m.get("name", model_id),
                context_window=m.get("context_length", 0),
                max_output_tokens=max_completion,
                supports_reasoning="reasoning" in supported or "reasoning_effort" in supported,
                supports_tools="tools" in supported or "tool_choice" in supported,
                supports_vision="image" in input_modalities,
                input_cost_per_1k=_price_per_1k(pricing.get("prompt")),
                output_cost_per_1k=_price_per_1k(pricing.get("completion")),
            )

    def get_capabilities(
        self,
        model_id: str,
        provider_name: str = "openrouter",
        base_url: str = "",
    ) -> ModelCapabilities:
        """Resolve ModelCapabilities through the layered catalog.

        Per-model capability knowledge (the former per-provider prefix
        ladder) lives in the corrections layer (``catalog_overrides.toml``)
        and resolves via ``resolve_entry``. Only decisions that hinge on
        HOST TRUST remain code below: trust in a base URL cannot be
        expressed in the (provider, model)-keyed corrections schema, so
        the base-url-sniffing branches keep their exact legacy shape
        (mirroring the context-capabilities decision).
        """
        # Anthropic/Ollama keep the historical early-return-empty behavior
        # behind a one-release gate — see the flag's comment at the top of
        # this module for what changes when it flips.
        if (
            provider_name in ("anthropic", "ollama")
            and not CATALOG_CAPABILITIES_FOR_ANTHROPIC_OLLAMA
        ):
            return ModelCapabilities()
        # HOST TRUST (code, not data): an OpenAI-kind config whose base URL
        # points at DeepSeek serves DeepSeek reasoning models regardless of
        # the model id spelling.
        if provider_name == "openai" and "deepseek" in base_url.lower():
            return ModelCapabilities(
                supports_reasoning=True, supports_tools=True, reasoning_format="deepseek"
            )
        # Live OpenRouter catalog hit: its reasoning models stream through
        # the OpenRouter dialect. resolve_entry's live layer would produce
        # the same answer, but the explicit branch keeps the ladder's
        # historical ordering — a live reasoning hit outranks the
        # api.openai.com host guard below.
        #
        info = self._models.get(model_id)
        override_fields = self._user_override_fields(
            model_id.strip(), (provider_name or "").strip().lower()
        )
        override_reasoning = override_fields.get("supports_reasoning")
        override_reasoning_format = override_fields.get("reasoning_format")
        # An override that turns reasoning OFF must fall through to
        # resolve_entry (which ranks user > live) rather than taking the live
        # reasoning early return. When reasoning stays on, still let the
        # override correct the tool/vision flags.
        if (
            info
            and info.supports_reasoning
            and override_reasoning is not False
            and override_reasoning_format not in ("", "none")
        ):
            supports_tools = info.supports_tools
            supports_vision = info.supports_vision
            if isinstance(override_fields.get("supports_tools"), bool):
                supports_tools = override_fields["supports_tools"]
            if isinstance(override_fields.get("supports_vision"), bool):
                supports_vision = override_fields["supports_vision"]
            return ModelCapabilities(
                supports_reasoning=True,
                supports_tools=supports_tools,
                supports_vision=supports_vision,
                reasoning_format=(
                    override_reasoning_format
                    if isinstance(override_reasoning_format, str)
                    else "openrouter"
                ),
            )
        model_l = model_id.strip().lower()
        # HOST TRUST (code, not data): only api.openai.com is trusted to
        # serve the real gpt-5/o1/o3/o4 reasoning stack. The model-prefix
        # set stays code WITH the host check because a corrections row is
        # keyed by (provider, model) only — it cannot express "reasoning
        # with the openai dialect at this host, snapshot capabilities at
        # any other", so transcribing the prefixes to data would grant the
        # openai reasoning dialect to arbitrary proxy base URLs.
        if (
            provider_name == "openai"
            and "api.openai.com" in base_url.lower()
            and model_l.startswith(("gpt-5", "o1", "o3", "o4"))
        ):
            return ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="openai",
            )
        # Everything else is data: user overrides > live > corrections
        # (the transcribed capability ladder) > snapshot > synthesized.
        return _capabilities_from_entry(self.resolve_entry(model_id, provider=provider_name))

    async def fetch_openrouter(self, api_key: str, base_url: str, proxy: str = "") -> None:
        """Fetch model list from OpenRouter /api/v1/models endpoint.

        ``base_url`` MUST NOT end with ``/v1`` — boot.py strips it.
        URL constructed as: ``f"{base_url}/v1/models"``
        """
        url = f"{base_url}/v1/models"
        headers = {
            "Authorization": f"Bearer {clean_header_secret(api_key, label='OpenRouter API key')}"
        }
        headers.update(provider_app_headers(base_url))
        async with httpx.AsyncClient(
            timeout=10.0, trust_env=_trust_env(), proxy=proxy or None
        ) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        self._populate_from_data(data.get("data", []))
        log.debug("model_catalog.fetched", count=len(self._models))

    def get(self, model_id: str) -> ModelInfo | None:
        """Look up model metadata by ID."""
        return self._models.get(model_id)

    def set_user_overrides(self, overrides: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the user-override layer (highest resolution authority).

        Keys are ``"provider/model"`` or a bare model id and are matched
        case-insensitively. At resolve time the provider-qualified key is
        consulted first; the bare-model key then fills only fields the
        qualified key left unset. Values map ``ModelCatalogEntry`` data-field
        names to values. Unknown field names or type-incompatible values are
        REJECTED with ``ValueError`` (fail fast at configuration time); on
        rejection the previously installed overrides remain in effect.
        """
        validated: dict[str, dict[str, Any]] = {}
        for key, fields in overrides.items():
            entry: dict[str, Any] = {}
            for name, value in fields.items():
                try:
                    entry[str(name)] = coerce_entry_field(str(name), value)
                except ValueError as exc:
                    raise ValueError(
                        f"invalid model catalog override for {key!r}: {exc}"
                    ) from exc
            validated[str(key).strip().lower()] = entry
        self._user_overrides = validated

    def _user_override_fields(self, model_id: str, provider_id: str) -> dict[str, Any]:
        """Fields from the user-override layer for ``(provider, model)``."""
        if not self._user_overrides:
            return {}
        model_l = model_id.strip().lower()
        keys = [f"{provider_id}/{model_l}"] if provider_id else []
        keys.append(model_l)
        fields: dict[str, Any] = {}
        for key in keys:
            entry = self._user_overrides.get(key)
            if entry:
                for name, value in entry.items():
                    fields.setdefault(name, value)
        return fields

    def set_live_provider_entries(
        self, provider_id: str, entries: Mapping[str, Mapping[str, Any]]
    ) -> None:
        """Replace one provider's scoped live-layer table.

        ``entries`` maps model ids to ``ModelCatalogEntry`` data-field dicts
        (the output of a ``provider/live_catalog.py`` parser). Fields are
        validated via ``coerce_entry_field``; unknown names or mistyped
        values are logged and DROPPED rather than raised — live data arrives
        from the network mid-boot, so it degrades like the packaged
        corrections, it never crashes resolution. The whole provider table
        is replaced atomically so a re-warm cannot leave stale rows behind.
        """
        provider_l = (provider_id or "").strip().lower()
        if not provider_l:
            return
        table: dict[str, dict[str, Any]] = {}
        for model_key, fields in entries.items():
            entry: dict[str, Any] = {}
            for name, value in fields.items():
                try:
                    entry[str(name)] = coerce_entry_field(str(name), value)
                except ValueError as exc:
                    log.warning(
                        "model_catalog.live_provider_bad_field",
                        provider=provider_l,
                        model=str(model_key),
                        error=str(exc),
                    )
            if entry:
                table[str(model_key).strip().lower()] = entry
        self._live_provider_entries[provider_l] = table

    def _live_provider_fields(self, model_id: str, provider_id: str) -> dict[str, Any]:
        """Scoped live-layer fields for ``(provider, model)``, exact key only.

        Empty when the provider was never ingested — the layer is inert for
        every provider without live-catalog registry metadata, so bare-id
        resolutions and other providers' scoped lookups can never see a
        foreign platform's rows.
        """
        if not provider_id or not self._live_provider_entries:
            return {}
        table = self._live_provider_entries.get(provider_id.strip().lower())
        if not table:
            return {}
        return dict(table.get(model_id.strip().lower()) or {})

    def resolve_entry(self, model: str, *, provider: str = "") -> ModelCatalogEntry:
        """Resolve one typed catalog entry through the layered sources.

        Authority order, merged per FIELD — a lower layer fills only fields
        every higher layer left unset:

        1. user overrides (``set_user_overrides``)
        2. live provider catalog — the provider-scoped ingest
           (``set_live_provider_entries``) first, then the bare-id
           OpenRouter cache (per-1k costs adapted to per-Mtok)
        3. packaged corrections (``catalog_overrides.toml``, exact then glob)
        4. models.dev snapshot
        5. synthesized fallback — never fails: unknown models yield a
           conservative entry (32k context / 8k output, tools on,
           reasoning off) with ``source="synthesized"``.

        ``source`` names the highest-authority layer that contributed at
        least one field. ``get_capabilities`` resolves through this chain
        (its host-trust branches excepted). The legacy ``resolve_max_tokens``
        / ``resolve_context_window`` paths keep their own chain order and
        consult the corrections data only at the slot the retired static
        fallback table occupied (below the snapshot), via
        ``_corrections_budget_fallback``.
        """
        provider_id = (provider or "").strip().lower()
        model_id = (model or "").strip()
        layers: tuple[tuple[CatalogSource, dict[str, Any]], ...] = (
            ("user", self._user_override_fields(model_id, provider_id)),
            ("live", self._live_provider_fields(model_id, provider_id)),
            ("live", _live_layer_fields(self._models.get(model_id))),
            ("corrections", _corrections_layer_fields(provider_id, model_id)),
            ("snapshot", _snapshot_layer_fields(provider_id, model_id)),
        )
        merged: dict[str, Any] = {}
        source: CatalogSource = "synthesized"
        for layer_source, fields in layers:
            for name, value in fields.items():
                if name not in merged:
                    merged[name] = value
                    if source == "synthesized":
                        source = layer_source
        for name, value in _SYNTHESIZED_DEFAULTS.items():
            merged.setdefault(name, value)
        return ModelCatalogEntry(
            provider_id=provider_id, model_id=model_id, source=source, **merged
        )

    def resolve_max_tokens(
        self, model_id: str, user_override: int = 0, provider: str = ""
    ) -> int:
        """Resolve max_tokens: user > scoped live > bare live > provider
        corrections > snapshot > basename corrections > default, then clamp."""
        return self.resolve_max_tokens_with_source(model_id, user_override, provider)[0]

    def resolve_max_tokens_with_source(
        self, model_id: str, user_override: int = 0, provider: str = ""
    ) -> tuple[int, MaxTokensSource]:
        """Resolve max_tokens and name the layer that decided the value.

        ``override`` = the caller-supplied ``user_override`` (an explicit
        config value); ``catalog`` = provider-scoped live ingest, live
        provider catalog, exact provider-scoped corrections row, models.dev
        snapshot, or the provider-agnostic corrections budget fallback (in
        that order); ``default`` = :data:`DEFAULT_MAX_TOKENS`.
        :meth:`resolve_max_tokens` delegates
        here (single implementation), so value and attribution can never
        drift apart. The clamp below may lower the number without changing
        the attribution: the source names the layer that supplied the
        pre-clamp candidate.
        """
        context_window = self.resolve_context_window(model_id, provider)
        info = self._models.get(model_id)
        scoped_live = self._live_provider_fields(model_id, provider)
        scoped_max_output = int(scoped_live.get("max_output_tokens") or 0)

        override_fields = self._user_override_fields(
            model_id.strip(), (provider or "").strip().lower()
        )
        override_max = override_fields.get("max_output_tokens")

        using_user_override = user_override > 0
        provider_budget = _provider_corrections_budget(provider, model_id)
        snapshot_limits = _models_dev_limits(provider, model_id)
        source: MaxTokensSource
        if using_user_override:
            effective = user_override
            source = "override"
        elif isinstance(override_max, int) and override_max > 0:
            # A [models.*] operator override is authoritative for budgeting;
            # treat it like an explicit user override (skip the safe-default
            # reduction, but keep the context-window clamp).
            effective = override_max
            source = "override"
            using_user_override = True
        elif scoped_max_output > 0:
            effective = scoped_max_output
            source = "catalog"
        elif info and info.max_output_tokens > 0:
            effective = info.max_output_tokens
            source = "catalog"
        elif provider_budget is not None and provider_budget[0] > 0:
            effective = provider_budget[0]
            source = "catalog"
        elif snapshot_limits is not None and snapshot_limits[0] > 0:
            effective = snapshot_limits[0]
            source = "catalog"
        elif (budgets := _corrections_budget_fallback(model_id)) is not None and budgets[0] > 0:
            effective = budgets[0]
            source = "catalog"
        else:
            effective = DEFAULT_MAX_TOKENS
            source = "default"

        # Clamp to context window. Some provider catalogs report a model's
        # max_completion_tokens as almost the entire context window; using that
        # value as max_tokens leaves no room for ordinary prompt/tool/image input
        # and causes preventable context-limit failures.
        if context_window > 0:
            effective = min(effective, context_window)
            if (
                not using_user_override
                and context_window > DEFAULT_MAX_TOKENS
                and effective >= context_window - DEFAULT_MAX_TOKENS
            ):
                effective = min(effective, SAFE_OPENROUTER_DEFAULT_MAX_TOKENS)

        return effective, source

    def resolve_context_window(self, model_id: str, provider: str = "") -> int:
        """Resolve context window: user override > scoped live > bare live >
        provider corrections > snapshot > basename corrections > default."""
        return self.resolve_context_window_with_source(model_id, provider)[0]

    def user_context_window_override(self, model_id: str, provider: str = "") -> int | None:
        """Positive ``[models.*]`` context_window override for the model, else None.

        Zero/negative override values are not usable as a budgeting window,
        so they report None (the layered chain resolves as if unset).
        """
        fields = self._user_override_fields(
            (model_id or "").strip(), (provider or "").strip().lower()
        )
        value = fields.get("context_window")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None

    def resolve_context_window_with_source(
        self, model_id: str, provider: str = ""
    ) -> tuple[int, ContextWindowSource]:
        """Resolve the context window and name the layer that decided it.

        ``override`` = a positive ``[models.*]`` user-override value
        (``set_user_overrides``); ``catalog`` = provider-scoped live ingest,
        live provider catalog, exact provider-scoped corrections row,
        models.dev snapshot, or the provider-agnostic corrections budget
        fallback (in that order); ``default`` = the local-runtime or cloud
        default window.
        :meth:`resolve_context_window` delegates here (single
        implementation), so value and attribution can never drift apart.
        The remaining chain deliberately keeps its own layer order rather
        than delegating to :meth:`resolve_entry` (parity tests pin it).
        """
        override = self.user_context_window_override(model_id, provider)
        if override is not None:
            return override, "override"
        scoped_live = self._live_provider_fields(model_id, provider)
        scoped_window = int(scoped_live.get("context_window") or 0)
        if scoped_window > 0:
            return scoped_window, "catalog"
        info = self._models.get(model_id)
        if info and info.context_window > 0:
            return info.context_window, "catalog"
        provider_budget = _provider_corrections_budget(provider, model_id)
        if provider_budget is not None and provider_budget[1] > 0:
            return provider_budget[1], "catalog"
        snapshot_limits = _models_dev_limits(provider, model_id)
        if snapshot_limits is not None and snapshot_limits[1] > 0:
            return snapshot_limits[1], "catalog"
        budgets = _corrections_budget_fallback(model_id)
        if budgets is not None and budgets[1] > 0:
            return budgets[1], "catalog"
        if provider and provider.strip().lower() in LOCAL_RUNTIME_PROVIDERS:
            return _LOCAL_CONTEXT_WINDOW, "default"
        return DEFAULT_CONTEXT_WINDOW, "default"


def resolve_effective_context_window(
    catalog: Any, model_id: str, provider: str = "", global_override: int = 0
) -> tuple[int, str]:
    """Resolve the effective context window with the full layered precedence.

    Single implementation of the rule "per-model ``[models.*]`` override >
    global ``llm.context_window_tokens`` (``global_override``) > catalog >
    default". Sources returned: ``"override"`` | ``"config"`` | ``"catalog"``
    | ``"default"``.

    ``catalog`` is duck-typed: override detection and catalog/default
    attribution use ``resolve_context_window_with_source`` when present
    (never reporting ``"override"`` without it, so the global config value
    still applies), while the catalog-layer value itself comes from the
    plain ``resolve_context_window`` — the canonical value API, which
    catalog-shaped stand-ins may implement alone. Junk/non-positive
    ``global_override`` values count as unset.
    """
    with_source = getattr(catalog, "resolve_context_window_with_source", None)
    source = "catalog"
    if callable(with_source):
        window, raw_source = with_source(model_id, provider=provider)
        source = str(raw_source)
        if source == "override":
            return int(window), "override"
    try:
        global_window = int(global_override)
    except (TypeError, ValueError):
        global_window = 0
    if global_window > 0:
        return global_window, "config"
    return int(catalog.resolve_context_window(model_id, provider=provider)), source


# ---------------------------------------------------------------------------
# Shared process-wide catalog instance.
#
# The gateway boots ONE catalog and warms it (fetch_openrouter); every other
# resolution site should consult that same instance instead of constructing
# cold copies that only ever see snapshot/corrections data. Callers that run
# without a gateway boot (standalone CLI paths) fall back to a lazily-built
# cold instance, which preserves today's snapshot/corrections-only semantics.
# ---------------------------------------------------------------------------

_shared_catalog: ModelCatalog | None = None
_cold_catalog: ModelCatalog | None = None


def set_shared_catalog(catalog: ModelCatalog | None) -> None:
    """Install (or, with ``None``, clear) the process-wide shared catalog."""
    global _shared_catalog
    _shared_catalog = catalog


def shared_catalog() -> ModelCatalog:
    """Return the injected shared catalog, else a lazily-built cold instance.

    The cold fallback is created once and reused, so repeated calls without
    an injected catalog are stable (same object). Construction is idempotent
    and GIL-serialized, so no locking is needed here.
    """
    if _shared_catalog is not None:
        return _shared_catalog
    global _cold_catalog
    if _cold_catalog is None:
        _cold_catalog = ModelCatalog()
    return _cold_catalog
