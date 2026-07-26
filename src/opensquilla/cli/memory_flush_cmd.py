"""Local memory flush-session command for automation workflows."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from opensquilla.observability.network_policy import (
    provider_request_correlation_disabled,
)
from opensquilla.provider.types import ProviderRequestCorrelation
from opensquilla.session.compaction_lifecycle import flush_receipt_is_successful_flush


@dataclass(frozen=True)
class MemoryFlushSessionResult:
    ok: bool
    key: str
    agent_id: str
    message_window: int | str
    flush_max_chars: int | str
    segment_mode: str
    segment_max_chars: int | str
    segment_overlap_messages: int
    flush_receipt: dict[str, Any]
    usage: dict[str, Any]
    usage_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "key": self.key,
            "agent_id": self.agent_id,
            "message_window": self.message_window,
            "flush_max_chars": self.flush_max_chars,
            "segment_mode": self.segment_mode,
            "segment_max_chars": self.segment_max_chars,
            "segment_overlap_messages": self.segment_overlap_messages,
            "flush_receipt": self.flush_receipt,
            "usage": self.usage,
            "usage_path": self.usage_path,
        }


def parse_message_window(value: str | int | None) -> int | None:
    """Parse CLI message-window values.

    ``None`` means use ``SessionFlushService`` defaults; ``0`` means the whole
    transcript. The CLI exposes this as ``--message-window all``.
    """

    if value is None:
        return None
    if isinstance(value, int):
        if value < 0:
            raise ValueError("message-window must be a positive integer or 'all'")
        return value
    raw = str(value).strip().lower()
    if not raw:
        return None
    if raw == "all":
        return 0
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError("message-window must be a positive integer or 'all'") from exc
    if parsed < 0:
        raise ValueError("message-window must be a positive integer or 'all'")
    return parsed


def format_message_window(value: int | None) -> int | str:
    if value is None:
        return "default"
    if value == 0:
        return "all"
    return value


def parse_flush_max_chars(value: int | None) -> int | None:
    if value is None:
        return None
    if value <= 0:
        raise ValueError("flush-max-chars must be a positive integer")
    return value


def parse_segment_mode(value: str | None) -> str:
    raw = (value or "auto").strip().lower()
    if raw not in {"auto", "off", "always"}:
        raise ValueError("segment-mode must be one of: auto, off, always")
    return raw


def parse_positive_optional(value: int | None, *, name: str) -> int | None:
    if value is None:
        return None
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def parse_non_negative(value: int, *, name: str) -> int:
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def _receipt_is_complete_flush(receipt: dict[str, Any]) -> bool:
    return flush_receipt_is_successful_flush(receipt)


def _emit_text_result(result: MemoryFlushSessionResult, *, success: bool) -> None:
    receipt = result.flush_receipt
    usage = result.usage
    mode = str(receipt.get("mode") or "?")
    if success:
        typer.secho(f"Session flushed: {result.key}", fg=typer.colors.GREEN)
    elif mode == "raw":
        typer.secho(f"Flush degraded to raw backup: {result.key}", fg=typer.colors.YELLOW)
    else:
        typer.secho(f"Flush failed: {result.key}", fg=typer.colors.RED)
    typer.echo(f"  Agent: {result.agent_id}")
    typer.echo(f"  Message window: {result.message_window}")
    typer.echo(f"  Flush max chars: {result.flush_max_chars}")
    typer.echo(f"  Segment mode: {result.segment_mode}")
    typer.echo(f"  Segment max chars: {result.segment_max_chars}")
    typer.echo(f"  Flush mode: {mode}")
    typer.echo(f"  Usage cost: ${float(usage.get('cost_usd') or 0.0):.8f}")
    typer.echo(f"  Usage source: {usage.get('cost_source', 'none')}")
    label = "Saved to" if success else "Backup path"
    for path in receipt.get("flushed_paths") or []:
        typer.echo(f"  {label}: {path}")
    if mode == "raw":
        typer.echo("  Warning: raw fallback is not searchable durable memory.", err=True)
    if not success and mode == "llm" and not receipt.get("error"):
        typer.echo("  Warning: LLM flush receipt is incomplete or degraded.", err=True)
    if receipt.get("error"):
        typer.echo(f"  Error: {receipt['error']}", err=True)


def _zero_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0.0,
        "billed_cost": 0.0,
        "estimated_cost_usd": 0.0,
        "model": "",
        "request_count": 0,
        "cost_source": "none",
    }


def _zero_embedding_usage() -> dict[str, Any]:
    return {
        "request_count": 0,
        "input_count": 0,
        "input_tokens": 0,
        "cache_hit_count": 0,
        "cache_write_count": 0,
        "cost_usd": 0.0,
        "billed_cost": 0.0,
        "model": "",
        "provider": "",
        "cost_source": "none",
    }


def _consume_embedding_usage(service_container: Any, agent_id: str) -> dict[str, Any]:
    stores = getattr(service_container, "memory_stores", {}) or {}
    store = stores.get(agent_id) or stores.get("main")
    consume = getattr(store, "consume_embedding_usage", None)
    if not callable(consume):
        return _zero_embedding_usage()
    usage = consume()
    return usage if isinstance(usage, dict) else _zero_embedding_usage()


def _write_json(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


async def run_memory_flush_session(
    *,
    key: str,
    session_db_path: str,
    workspace: str | None = None,
    config_path: str | None = None,
    agent_id: str | None = None,
    message_window: str | int | None = None,
    flush_max_chars: int | None = None,
    segment_mode: str = "auto",
    segment_max_chars: int | None = None,
    segment_overlap_messages: int = 0,
    timeout: float | None = None,
    usage_path: str | None = None,
    config: Any | None = None,
) -> MemoryFlushSessionResult:
    from opensquilla.cli.agent_cmd import _with_agent_workspace_config
    from opensquilla.gateway import build_services
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.session.keys import (
        canonicalize_session_key,
        normalize_agent_id,
        parse_agent_id,
    )

    session_key = canonicalize_session_key(key)
    resolved_agent_id = normalize_agent_id(agent_id or parse_agent_id(session_key))
    parsed_window = parse_message_window(message_window)
    parsed_flush_max_chars = parse_flush_max_chars(flush_max_chars)
    parsed_segment_mode = parse_segment_mode(segment_mode)
    parsed_segment_max_chars = parse_positive_optional(
        segment_max_chars,
        name="segment-max-chars",
    )
    parsed_segment_overlap_messages = parse_non_negative(
        segment_overlap_messages,
        name="segment-overlap-messages",
    )

    cfg = config or GatewayConfig.load(
        config_path or os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH")
    )
    active_workspace = workspace or getattr(cfg, "workspace_dir", None)
    service_cfg = _with_agent_workspace_config(cfg, active_workspace) if active_workspace else cfg
    extra_agents = [resolved_agent_id] if resolved_agent_id != "main" else None

    svc = await build_services(
        config=service_cfg,
        session_db_path=session_db_path,
        extra_agent_ids=extra_agents,
    )
    try:
        flush_service = getattr(svc, "flush_service", None)
        if flush_service is None:
            raise RuntimeError(
                "session flush service is disabled; set memory.flush_enabled=true "
                "and leave OPENSQUILLA_SESSION_FLUSH unset before running memory flush-session"
            )
        session_manager = svc.session_manager
        if session_manager is None:
            raise RuntimeError("session manager is disabled; cannot read transcript")
        transcript = await session_manager.get_transcript(session_key)
        maintenance_turn_id = f"maintenance_{uuid.uuid4().hex}"
        provider_request_correlation = None
        if not provider_request_correlation_disabled(config=service_cfg):
            try:
                session = await session_manager.get_session(session_key)
            except Exception:  # noqa: BLE001 - correlation must never block the flush
                session = None
            durable_session_id = getattr(session, "session_id", None)
            if isinstance(durable_session_id, str) and durable_session_id:
                provider_request_correlation = ProviderRequestCorrelation(
                    session_id=durable_session_id,
                    turn_id=maintenance_turn_id,
                    execution_id=uuid.uuid4().hex,
                    call_kind="auxiliary.session_flush",
                )
        receipt = await flush_service.execute(
            transcript,
            session_key,
            agent_id=resolved_agent_id,
            timeout=timeout,
            message_window=parsed_window,
            flush_max_chars=parsed_flush_max_chars,
            segment_mode=parsed_segment_mode,
            segment_max_chars=parsed_segment_max_chars,
            segment_overlap_messages=parsed_segment_overlap_messages,
            turn_id=maintenance_turn_id,
            provider_request_correlation=provider_request_correlation,
        )
        receipt_dict = receipt.to_dict() if hasattr(receipt, "to_dict") else dict(receipt)
        usage = receipt_dict.get("usage")
        if not isinstance(usage, dict):
            usage = _zero_usage()
        usage = dict(usage)
        usage["embedding"] = _consume_embedding_usage(svc, resolved_agent_id)
        result = MemoryFlushSessionResult(
            ok=_receipt_is_complete_flush(receipt_dict),
            key=session_key,
            agent_id=resolved_agent_id,
            message_window=format_message_window(parsed_window),
            flush_max_chars=parsed_flush_max_chars or "default",
            segment_mode=parsed_segment_mode,
            segment_max_chars=parsed_segment_max_chars or "default",
            segment_overlap_messages=parsed_segment_overlap_messages,
            flush_receipt=receipt_dict,
            usage=usage,
            usage_path=usage_path,
        )
        if usage_path:
            _write_json(usage_path, usage)
        return result
    finally:
        close = getattr(svc, "close", None)
        if close is not None:
            await close()


def memory_flush_session_cmd(
    key: str = typer.Option(..., "--key", help="Session key to flush."),
    session_db_path: str = typer.Option(
        ...,
        "--session-db-path",
        help="Persistent session SQLite path used by the local agent runs.",
    ),
    workspace: str = typer.Option(
        "",
        "--workspace",
        help="Workspace root for memory/*.md output; forces memory.source=workspace.",
    ),
    config_path: str = typer.Option(
        "",
        "--config",
        help=(
            "Gateway config path. Defaults to OPENSQUILLA_GATEWAY_CONFIG_PATH "
            "or normal config lookup."
        ),
    ),
    agent_id: str = typer.Option(
        "",
        "--agent",
        help="Agent id override. Defaults to the agent parsed from --key.",
    ),
    message_window: str = typer.Option(
        "",
        "--message-window",
        help="Number of most recent messages to flush, or 'all' for the full transcript.",
    ),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        "-T",
        help="Flush timeout in seconds. Defaults to SessionFlushService settings.",
    ),
    flush_max_chars: int | None = typer.Option(
        None,
        "--flush-max-chars",
        help=(
            "Maximum transcript excerpt characters for the LLM flush prompt. "
            "Defaults to a product-safe value; use with --message-window all "
            "to make truncation explicit."
        ),
    ),
    segment_mode: str = typer.Option(
        "auto",
        "--segment-mode",
        help="Segment long full-session flushes: auto, off, or always.",
    ),
    segment_max_chars: int | None = typer.Option(
        None,
        "--segment-max-chars",
        help="Maximum transcript characters per flush segment.",
    ),
    segment_overlap_messages: int = typer.Option(
        0,
        "--segment-overlap-messages",
        help="Number of trailing messages to repeat into the next segment.",
    ),
    usage_path: str = typer.Option(
        "",
        "--usage-path",
        help="Write flush usage/cost JSON to this path.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Flush a local session transcript into searchable durable memory."""

    try:
        result = asyncio.run(
            run_memory_flush_session(
                key=key,
                session_db_path=session_db_path,
                workspace=workspace or None,
                config_path=config_path or None,
                agent_id=agent_id or None,
                message_window=message_window or None,
                flush_max_chars=flush_max_chars,
                segment_mode=segment_mode,
                segment_max_chars=segment_max_chars,
                segment_overlap_messages=segment_overlap_messages,
                timeout=timeout,
                usage_path=usage_path or None,
            )
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        if json_output:
            typer.echo(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            typer.secho(f"Flush failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    payload = result.to_dict()
    if usage_path:
        _write_json(usage_path, result.usage)
    if not result.ok:
        if json_output:
            typer.echo(json.dumps(payload, sort_keys=True))
        else:
            _emit_text_result(result, success=False)
        raise typer.Exit(1)

    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True))
    else:
        _emit_text_result(result, success=True)
