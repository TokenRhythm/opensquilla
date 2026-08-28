"""Model catalog CLI commands."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import typer
from rich.table import Table

from opensquilla.cli.gateway_rpc import run_gateway_sync
from opensquilla.cli.output import emit_error, print_json
from opensquilla.cli.ui import ACCENT_HEADER, console, error_console, markup_escape
from opensquilla.onboarding.config_store import load_config
from opensquilla.onboarding.probe import discover_provider_models, probe_llm_provider
from opensquilla.redaction import redact_error_text
from opensquilla.router_tiers import TierConfig

app = typer.Typer(help="Inspect available models.")


@app.command("list")
def models_list(
    provider: str | None = typer.Option(None, "--provider", help="Provider filter"),
    capability: list[str] | None = typer.Option(
        None, "--capability", "-c", help="Required capability"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List available models from the running gateway."""

    async def _with_client(client) -> Any:
        return await client.call(
            "models.list", {"provider": provider, "capabilities": capability}
        )

    payload = run_gateway_sync(_with_client, json_output=json_output)
    if isinstance(payload, list):
        # Pre-envelope gateways returned the bare row list.
        rows = cast(list[dict[str, Any]], payload)
        errors: list[dict[str, Any]] = []
    else:
        rows = cast(list[dict[str, Any]], list(payload.get("models") or []))
        errors = cast(list[dict[str, Any]], list(payload.get("errors") or []))

    if json_output:
        print_json(rows)
        return

    table = Table(title="Models", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("ID")
    table.add_column("Provider")
    table.add_column("Context", justify="right")
    table.add_column("Capabilities")
    table.add_column("Input/1k", justify="right")
    table.add_column("Output/1k", justify="right")
    for row in rows:
        pricing = row.get("pricing") or {}
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("provider") or ""),
            str(row.get("contextWindow") or ""),
            ", ".join(str(v) for v in row.get("capabilities") or []),
            str(pricing.get("inputPer1k") or ""),
            str(pricing.get("outputPer1k") or ""),
        )
    console.print(table)

    if errors:
        error_console.print("[yellow]Some providers failed to list models:[/yellow]")
        for err in errors:
            provider_id = markup_escape(str(err.get("provider") or "unknown"))
            kind = markup_escape(str(err.get("kind") or "unknown"))
            detail = markup_escape(str(err.get("detail") or ""))
            line = f"  - {provider_id}: {kind}"
            if detail:
                line += f" — {detail}"
            error_console.print(f"[yellow]{line}[/yellow]")


# ---------------------------------------------------------------------------
# opensquilla models probe
# ---------------------------------------------------------------------------

_PROBE_METHOD_CHAT = "chat"
_PROBE_METHOD_MODELS_LIST = "models_list"
# CLI-owned status for config-level validation failures (unknown provider id,
# no runtime support) — deliberately distinct from the provider failure
# taxonomy, which describes what a *reachable* provider said.
_PROBE_KIND_INVALID_CONFIG = "invalid_config"


@dataclass(frozen=True)
class _ProbeTarget:
    """One configured provider credential set to probe (never persisted)."""

    provider_id: str
    model: str
    api_key: str
    api_key_env: str
    base_url: str
    proxy: str
    source: str  # "llm" (primary) | "llm_profiles" | "draft"


def _tier_model_for_provider(cfg: Any, provider_id: str) -> str:
    """First router-tier model bound to ``provider_id``, or '' if none.

    Credential profiles carry no model of their own; router tiers reference
    them through the tier ``provider`` field, so a tier model is the natural
    probe subject for a profile.
    """
    tiers = getattr(getattr(cfg, "squilla_router", None), "tiers", None) or {}
    if not isinstance(tiers, dict):
        return ""
    for tier_name in sorted(tiers):
        tier = TierConfig.from_value(tiers[tier_name])
        if tier.provider.strip().lower() == provider_id and tier.model:
            return tier.model
    return ""


def _probe_targets(cfg: Any) -> list[_ProbeTarget]:
    """Enumerate configured providers: primary ``[llm]`` + ``[llm_profiles.*]``."""
    targets: list[_ProbeTarget] = []
    llm = getattr(cfg, "llm", None)
    primary_id = str(getattr(llm, "provider", "") or "").strip().lower()
    if primary_id:
        targets.append(
            _ProbeTarget(
                provider_id=primary_id,
                model=str(getattr(llm, "model", "") or "").strip(),
                api_key=str(getattr(llm, "api_key", "") or ""),
                api_key_env=str(getattr(llm, "api_key_env", "") or ""),
                base_url=str(getattr(llm, "base_url", "") or ""),
                proxy=str(getattr(llm, "proxy", "") or ""),
                source="llm",
            )
        )
    profiles = getattr(cfg, "llm_profiles", None) or {}
    for profile_id in sorted(profiles):
        normalized = str(profile_id or "").strip().lower()
        if not normalized or normalized == primary_id:
            continue
        profile = profiles[profile_id]
        targets.append(
            _ProbeTarget(
                provider_id=normalized,
                model=_tier_model_for_provider(cfg, normalized),
                api_key=str(getattr(profile, "api_key", "") or ""),
                api_key_env=str(getattr(profile, "api_key_env", "") or ""),
                base_url=str(getattr(profile, "base_url", "") or ""),
                proxy=str(getattr(profile, "proxy", "") or ""),
                source="llm_profiles",
            )
        )
    return targets


def _probe_row(
    target: _ProbeTarget,
    *,
    ok: bool,
    method: str,
    kind: str = "",
    detail: str = "",
    code: str = "",
    latency_ms: int = 0,
) -> dict[str, Any]:
    """One probe result row. ``detail`` is always redacted before it lands
    here so neither the table nor ``--json`` can echo credential material."""
    return {
        "provider": target.provider_id,
        "model": target.model,
        "ok": ok,
        "kind": "" if ok else kind,
        "detail": redact_error_text(detail),
        "code": code,
        "method": method,
        "source": target.source,
        "latency_ms": latency_ms,
    }


async def _probe_one(target: _ProbeTarget, timeout: float) -> dict[str, Any]:
    method = _PROBE_METHOD_CHAT if target.model else _PROBE_METHOD_MODELS_LIST
    try:
        if target.model:
            result = await probe_llm_provider(
                provider_id=target.provider_id,
                model=target.model,
                api_key=target.api_key,
                api_key_env=target.api_key_env,
                base_url=target.base_url,
                proxy=target.proxy,
                timeout=timeout,
            )
            return _probe_row(
                target,
                ok=result.ok,
                method=method,
                kind=result.failure_kind,
                detail=result.message,
                code=result.code,
                latency_ms=result.latency_ms,
            )
        listing = await discover_provider_models(
            provider_id=target.provider_id,
            api_key=target.api_key,
            api_key_env=target.api_key_env,
            base_url=target.base_url,
            proxy=target.proxy,
        )
        return _probe_row(
            target,
            ok=listing.ok,
            method=method,
            kind=listing.failure_kind,
            detail=listing.detail,
        )
    except ValueError as exc:
        # Validation-level problem (unknown provider id, no runtime support):
        # the provider was never contacted.
        return _probe_row(
            target,
            ok=False,
            method=method,
            kind=_PROBE_KIND_INVALID_CONFIG,
            detail=str(exc),
        )


async def _run_probes(targets: list[_ProbeTarget], timeout: float) -> list[dict[str, Any]]:
    return [await _probe_one(target, timeout) for target in targets]


def _prewarm_draft_probe_install_id() -> None:
    """Register the active privacy context before an unsaved live probe."""

    from opensquilla.provider.tokenrhythm_correlation import (
        prewarm_tokenrhythm_install_id,
    )

    config: Any
    try:
        config = load_config(None)
    except Exception:
        # A malformed local config must not break the desktop bridge, but it
        # also must not fall back to an implicitly enabled privacy context.
        config = SimpleNamespace(
            privacy=SimpleNamespace(disable_network_observability=True)
        )
    prewarm_tokenrhythm_install_id(config=config)


@app.command("probe-draft", hidden=True)
def models_probe_draft() -> None:
    """Probe one unsaved provider draft supplied as JSON on stdin.

    Internal desktop bridge: credential material never appears in argv and the
    command never persists the candidate configuration. Output is always one
    redacted JSON result object.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError) as exc:
        emit_error(
            f"Invalid provider draft JSON: {exc}",
            json_output=True,
            code="invalid_draft",
        )
        raise typer.Exit(code=2) from exc
    if not isinstance(payload, dict):
        emit_error(
            "Provider draft must be a JSON object.",
            json_output=True,
            code="invalid_draft",
        )
        raise typer.Exit(code=2)

    provider_id = str(payload.get("providerId") or "").strip().lower()
    model = str(payload.get("model") or "").strip()
    if not provider_id or not model:
        emit_error(
            "Provider draft requires providerId and model.",
            json_output=True,
            code="invalid_draft",
        )
        raise typer.Exit(code=2)

    _prewarm_draft_probe_install_id()
    target = _ProbeTarget(
        provider_id=provider_id,
        model=model,
        api_key=str(payload.get("apiKey") or ""),
        api_key_env=str(payload.get("apiKeyEnv") or ""),
        base_url=str(payload.get("baseUrl") or ""),
        proxy=str(payload.get("proxy") or ""),
        source="draft",
    )
    row = asyncio.run(_probe_one(target, float(payload.get("timeout") or 30.0)))
    print_json(row)
    if not row["ok"]:
        raise typer.Exit(code=1)


@app.command("probe")
def models_probe(
    provider: str | None = typer.Option(
        None, "--provider", help="Probe only this configured provider id"
    ),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Override the model used for the selected probes"
    ),
    timeout: float = typer.Option(30.0, "--timeout", help="Per-probe timeout in seconds"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Probe configured providers for reachability and credential validity.

    Live command: every configured provider (the primary llm entry plus each
    llm_profiles credential profile) gets a small, provider-bounded chat probe
    against its model — or a model-list probe when no model is bound to a credential
    profile. Failures are classified through the shared provider failure
    taxonomy and error details are redacted before display. Exits 1 when any
    probe fails, 2 on invalid selection.
    """
    cfg = load_config(config_path)
    from opensquilla.provider.tokenrhythm_correlation import (
        prewarm_tokenrhythm_install_id,
    )

    prewarm_tokenrhythm_install_id(config=cfg)
    targets = _probe_targets(cfg)
    if provider:
        wanted = provider.strip().lower()
        targets = [target for target in targets if target.provider_id == wanted]
        if not targets:
            emit_error(
                f"Provider '{provider}' is not configured "
                "(checked [llm] and [llm_profiles]).",
                json_output=json_output,
                code="unknown_provider",
            )
            raise typer.Exit(code=2)
    if not targets:
        emit_error(
            "No LLM providers configured.",
            json_output=json_output,
            code="no_providers",
        )
        raise typer.Exit(code=2)
    if model and model.strip():
        targets = [replace(target, model=model.strip()) for target in targets]

    rows = asyncio.run(_run_probes(targets, timeout))
    failed = [row for row in rows if not row["ok"]]

    if json_output:
        print_json(rows)
    else:
        table = Table(title="Provider probes", show_header=True, header_style=ACCENT_HEADER)
        table.add_column("Provider")
        table.add_column("Model")
        table.add_column("Status")
        table.add_column("Detail")
        for row in rows:
            model_label = str(row["model"] or "")
            if not model_label and row["method"] == _PROBE_METHOD_MODELS_LIST:
                model_label = "(models list)"
            table.add_row(
                markup_escape(str(row["provider"])),
                markup_escape(model_label),
                "ok" if row["ok"] else markup_escape(str(row["kind"] or "error")),
                markup_escape(str(row["detail"] or "")),
            )
        console.print(table)
        if failed:
            error_console.print("[yellow]Some provider probes failed:[/yellow]")
            for row in failed:
                provider_id = markup_escape(str(row["provider"]))
                kind = markup_escape(str(row["kind"] or "error"))
                detail = markup_escape(str(row["detail"] or ""))
                line = f"  - {provider_id}: {kind}"
                if detail:
                    line += f" — {detail}"
                error_console.print(f"[yellow]{line}[/yellow]")

    if failed:
        raise typer.Exit(code=1)
