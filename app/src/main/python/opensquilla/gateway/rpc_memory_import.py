"""Gateway RPC boundary for model-assisted profile imports.

The gateway owns provider selection and transport accounting.  The profile
import service owns parsing, previews, transactions, receipts, and undo.  This
module deliberately never enters the agent turn loop and never advances the
selector's fallback chain.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.provider.auxiliary_budget import (
    ensure_auxiliary_text_fits,
    resolve_auxiliary_request_budget,
)

_d = get_dispatcher()

_SCHEMA_VERSION = 1
_DERIVED_REFRESH_TIMEOUT_SECONDS = 30.0
_STABLE_ERROR_CODES = frozenset(
    {
        "MEMORY_IMPORT_UNAVAILABLE",
        "MEMORY_IMPORT_INPUT_TOO_LARGE",
        "MEMORY_IMPORT_MODEL_FAILED",
        "MEMORY_IMPORT_INVALID_OUTPUT",
        "MEMORY_IMPORT_BUSY",
        "MEMORY_IMPORT_JOB_NOT_FOUND",
        "MEMORY_IMPORT_PREVIEW_EXPIRED",
        "MEMORY_IMPORT_STALE_PREVIEW",
        "MEMORY_IMPORT_WRITE_FAILED",
    }
)


def _profile_import_output_tokens(resolved_max_tokens: int) -> int:
    """Use the same effective output budget as the configured default model."""

    return max(1, int(resolved_max_tokens))


def _camel_key(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _to_wire(value: Any) -> Any:
    """Recursively convert core dataclasses/Pydantic values to JSON wire data."""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    else:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            value = model_dump(mode="json")
    if isinstance(value, dict):
        return {_camel_key(str(key)): _to_wire(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_wire(item) for item in value]
    if isinstance(value, Enum):
        return _to_wire(value.value)
    if isinstance(value, Path):
        return value.as_posix()
    return value


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _bounded_await(value: Any, *, timeout_seconds: float) -> Any:
    if not inspect.isawaitable(value):
        return value
    async with asyncio.timeout(timeout_seconds):
        return await value


def _consume_background_task(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


async def _close_provider_stream(stream: Any, *, timeout_seconds: float) -> None:
    """Bound best-effort provider cleanup without replacing the call result."""

    close = getattr(stream, "aclose", None)
    if not callable(close):
        return
    result = close()
    if not inspect.isawaitable(result):
        return
    close_task = asyncio.ensure_future(result)
    try:
        done, _pending = await asyncio.wait(
            {close_task},
            timeout=min(1.0, timeout_seconds),
        )
    except BaseException:
        close_task.cancel()
        close_task.add_done_callback(_consume_background_task)
        raise
    if close_task in done:
        _consume_background_task(close_task)
        return

    close_task.cancel()
    done, _pending = await asyncio.wait({close_task}, timeout=0.05)
    if close_task in done:
        _consume_background_task(close_task)
    else:
        close_task.add_done_callback(_consume_background_task)


def _params(value: dict | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("params must be an object")
    schema_version = value.get("schemaVersion")
    if schema_version is not None and schema_version != _SCHEMA_VERSION:
        raise ValueError(f"params.schemaVersion must be {_SCHEMA_VERSION}")
    return value


def _required_text(params: dict[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"params.{name} is required")
    return value


def _required_bool(params: dict[str, Any], name: str) -> bool:
    value = params.get(name)
    if not isinstance(value, bool):
        raise ValueError(f"params.{name} is required")
    return value


def _optional_text(
    params: dict[str, Any],
    name: str,
    *,
    default: str | None = None,
) -> str | None:
    value = params.get(name, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"params.{name} must be a string")
    return value


def _injected_service(ctx: RpcContext, agent_id: str) -> Any | None:
    injected = getattr(ctx, "profile_import_service", None)
    if injected is None:
        return None
    if callable(injected) and not all(
        hasattr(injected, name) for name in ("info", "preview", "apply", "undo", "discard")
    ):
        try:
            return injected(ctx=ctx, agent_id=agent_id)
        except TypeError:
            return injected(ctx, agent_id)
    return injected


def _shared_state_dir(ctx: RpcContext) -> Path:
    from opensquilla.agents.scope import default_state_dir

    configured = getattr(getattr(ctx, "config", None), "state_dir", None)
    return Path(configured).expanduser() if configured else default_state_dir()


def _is_loopback_deployment(provider: str, base_url: str) -> bool:
    from opensquilla.gateway.scopes import is_loopback_address

    if base_url:
        return is_loopback_address(urlsplit(base_url).hostname)
    provider_id = provider.strip().lower().replace("-", "_")
    return provider_id in {"ollama", "lmstudio", "lm_studio"}


def _profile_import_paths(ctx: RpcContext, agent_id: str) -> Any:
    from opensquilla.agents.scope import resolve_agent_workspace_dir
    from opensquilla.memory.profile_import import ProfileImportPaths

    managers = getattr(ctx, "memory_managers", None) or {}
    manager = managers.get(agent_id)
    if manager is None:
        raise RpcHandlerError(
            "MEMORY_IMPORT_UNAVAILABLE",
            f"Memory is not configured for agent {agent_id!r}.",
        )
    memory_root = getattr(manager, "workspace_dir", None) or getattr(
        manager, "memory_dir", None
    )
    if memory_root is None:
        raise RpcHandlerError(
            "MEMORY_IMPORT_UNAVAILABLE",
            f"Memory source is not configured for agent {agent_id!r}.",
        )
    config = getattr(ctx, "config", None)
    state_dir = _shared_state_dir(ctx).expanduser().resolve(strict=False)
    return ProfileImportPaths(
        agent_id=agent_id,
        agent_workspace_dir=Path(resolve_agent_workspace_dir(agent_id, config)),
        memory_workspace_dir=Path(memory_root),
        state_dir=state_dir,
        profile_home_dir=state_dir.parent,
    )


class _GatewayFusionCompletion:
    """Lazily resolve and call exactly one configured primary model."""

    def __init__(
        self,
        *,
        ctx: RpcContext,
        selector: Any,
        provider_id: str,
        model: str,
        agent_id: str,
        max_tokens: int | None = None,
        provider_request_max_chars: int = 0,
        request_timeout: float | None = None,
    ) -> None:
        self._ctx = ctx
        self._selector = selector
        self._provider_id = provider_id
        self._model = model
        self._agent_id = agent_id
        request_budget = resolve_auxiliary_request_budget(
            None,
            provider_id=provider_id,
            model=model,
            max_output_tokens=max_tokens or 16_384,
            provider_request_max_chars=provider_request_max_chars,
        )
        self._max_tokens = request_budget.max_output_tokens
        self._provider_request_max_chars = request_budget.provider_request_max_chars
        self._provider_request_max_tokens = request_budget.max_input_tokens
        configured_timeout = request_timeout
        if configured_timeout is None:
            configured_timeout = getattr(
                getattr(ctx, "config", None),
                "llm_request_timeout_seconds",
                120.0,
            )
        try:
            parsed_timeout = float(configured_timeout)
        except (TypeError, ValueError):
            parsed_timeout = 120.0
        self._request_timeout = parsed_timeout if parsed_timeout > 0 else 120.0
        self._provider: Any = None

    def _resolve_once(self) -> Any:
        if self._provider is None:
            # Deliberately no next_fallback()/router/ensemble path.
            self._provider = self._selector.resolve()
        return self._provider

    async def __call__(self, request: Any) -> str:
        from opensquilla.engine.usage_accounting import (
            UsageAccountingScope,
            UsageExecutionContext,
            account_provider_stream,
            bind_usage_accounting_scope,
            provider_accounts_physical_usage,
        )
        from opensquilla.observability.network_policy import (
            provider_request_correlation_disabled,
        )
        from opensquilla.provider.correlation_context import (
            bind_provider_request_correlation,
        )
        from opensquilla.provider.types import (
            ChatConfig,
            DoneEvent,
            ErrorEvent,
            Message,
            ProviderRequestCorrelation,
            TextDeltaEvent,
        )

        provider = self._resolve_once()
        call_id = uuid.uuid4().hex
        session_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"opensquilla:system:profile-import:{self._agent_id}",
        ).hex
        correlation = None
        if not provider_request_correlation_disabled(config=getattr(self._ctx, "config", None)):
            correlation = ProviderRequestCorrelation(
                session_id=session_id,
                turn_id=call_id,
                execution_id=call_id,
                call_kind="auxiliary.profile_import",
            )
        llm_config = getattr(getattr(self._ctx, "config", None), "llm", None)
        configured_max_tokens = int(getattr(llm_config, "max_tokens", 0) or 0)
        chat_config = ChatConfig(
            max_tokens=self._max_tokens or configured_max_tokens or 16_384,
            temperature=0.0,
            timeout=self._request_timeout,
            system=str(getattr(request, "system_prompt", "") or ""),
            output_json_schema=getattr(request, "response_schema", None),
            output_json_schema_strict=True,
            candidate_output_mode="inert_artifact",
            provider_request_max_chars=self._provider_request_max_chars,
            provider_request_correlation=correlation,
        )
        messages = [
            Message(role="user", content=str(getattr(request, "user_prompt", "") or ""))
        ]
        ensure_auxiliary_text_fits(
            messages,
            system=chat_config.system or "",
            max_chars=self._provider_request_max_chars,
            max_tokens=self._provider_request_max_tokens,
        )
        usage_scope = None
        if getattr(self._ctx, "usage_event_sink", None) is not None:
            usage_scope = UsageAccountingScope(
                sink=self._ctx.usage_event_sink,
                context=UsageExecutionContext(
                    execution_id=call_id,
                    agent_run_id=call_id,
                    turn_id=call_id,
                    session_id=session_id,
                    agent_id=self._agent_id,
                    run_kind="profile_import",
                ),
            )

        stream: Any = None
        started = time.monotonic()
        first_text_at: float | None = None
        ok = False
        output: list[str] = []
        done_model = ""
        done_provider = ""
        try:
            async with asyncio.timeout(self._request_timeout):
                with (
                    bind_usage_accounting_scope(usage_scope),
                    bind_provider_request_correlation(correlation),
                ):
                    if provider_accounts_physical_usage(provider):
                        stream = provider.chat(messages, tools=None, config=chat_config)
                    else:
                        stream = account_provider_stream(
                            lambda: provider.chat(messages, tools=None, config=chat_config),
                            provider=self._provider_id,
                            model=self._model,
                        )
                    async for event in stream:
                        if isinstance(event, TextDeltaEvent):
                            if first_text_at is None:
                                first_text_at = time.monotonic()
                            output.append(event.text)
                        elif isinstance(event, ErrorEvent):
                            raise RuntimeError(
                                event.message or "profile import provider failed"
                            )
                        elif isinstance(event, DoneEvent):
                            done_model = str(event.model or "")
                            done_provider = str(event.provider or "")
            if done_model and done_model != self._model:
                raise RuntimeError(
                    "profile import response model did not match the configured model"
                )
            if done_provider and done_provider != self._provider_id:
                raise RuntimeError(
                    "profile import response provider did not match the configured provider"
                )
            ok = True
            return "".join(output)
        finally:
            cleanup_interrupt: BaseException | None = None
            try:
                await _close_provider_stream(
                    stream,
                    timeout_seconds=self._request_timeout,
                )
            except BaseException as exc:
                cleanup_interrupt = exc
            stats = getattr(self._ctx, "provider_stats", None)
            record = getattr(stats, "record", None)
            if callable(record):
                ended = time.monotonic()
                record(
                    provider_id=self._provider_id,
                    model=self._model,
                    ttft_ms=(
                        round((first_text_at - started) * 1000)
                        if first_text_at is not None
                        else None
                    ),
                    duration_ms=round((ended - started) * 1000),
                    ok=ok,
                    failure_kind="" if ok else "profile_import_failed",
                )
            if cleanup_interrupt is not None:
                raise cleanup_interrupt


def _build_profile_import_service(
    ctx: RpcContext,
    agent_id: str,
    *,
    require_model: bool = True,
) -> Any:
    """Build the disk-backed service with a direct, single-model completion.

    Imports stay local so older installations that do not contain the additive
    profile-import package can still boot and expose a clean unavailable state.
    """
    try:
        from opensquilla.memory.profile_import import (
            ModelIdentity,
            ProfileImportQuotas,
            ProfileImportService,
        )
    except ImportError as exc:
        raise RpcHandlerError(
            "MEMORY_IMPORT_UNAVAILABLE",
            "Profile import is not available in this gateway build.",
        ) from exc

    managers = getattr(ctx, "memory_managers", None) or {}
    manager = managers.get(agent_id)
    paths = _profile_import_paths(ctx, agent_id)
    if manager is None:
        raise RpcHandlerError(
            "MEMORY_IMPORT_UNAVAILABLE",
            f"Memory is not configured for agent {agent_id!r}.",
        )
    config = getattr(ctx, "config", None)
    memory_config = getattr(config, "memory", None) or getattr(
        manager, "memory_config", None
    )
    if not require_model:
        llm_config = getattr(config, "llm", None)
        provider_id = str(getattr(llm_config, "provider", "") or "unconfigured")
        model = str(getattr(llm_config, "model", "") or "unconfigured")
        base_url = str(getattr(llm_config, "base_url", "") or "")
        return ProfileImportService(
            paths,
            ModelIdentity(
                provider=provider_id,
                model=model,
                is_loopback=_is_loopback_deployment(provider_id, base_url),
            ),
            None,
            quotas=ProfileImportQuotas(
                max_file_size_kb=int(
                    getattr(memory_config, "max_file_size_kb", 1024) or 0
                ),
                max_total_size_kb=int(
                    getattr(memory_config, "max_total_size_kb", 102400) or 0
                ),
                max_files=int(getattr(memory_config, "max_files", 500) or 0),
                max_raw_bytes=262_144,
            ),
        )

    selector = getattr(ctx, "provider_selector", None)
    if selector is None or not getattr(selector, "is_configured", True):
        raise RpcHandlerError(
            "MEMORY_IMPORT_UNAVAILABLE",
            "Configure the default language model before importing a profile.",
        )
    clone = getattr(selector, "clone", None)
    if not callable(clone):
        raise RpcHandlerError(
            "MEMORY_IMPORT_UNAVAILABLE",
            "The configured provider does not support isolated profile import calls.",
        )
    isolated_selector = clone()
    disable_replay = getattr(isolated_selector, "disable_provider_state_replay", None)
    if callable(disable_replay):
        disable_replay()
    remaining_chain = getattr(isolated_selector, "remaining_chain", None)
    chain = remaining_chain() if callable(remaining_chain) else []
    primary = chain[0] if chain else None
    provider_id = str(
        getattr(primary, "provider", "")
        or getattr(isolated_selector, "active_provider_id", "")
    )
    model = str(getattr(primary, "model", ""))
    if not provider_id or not model:
        raise RpcHandlerError(
            "MEMORY_IMPORT_UNAVAILABLE",
            "The default provider and model must both be configured.",
        )

    from opensquilla.provider.model_catalog import (
        resolve_effective_context_window,
        shared_catalog,
    )

    catalog = shared_catalog()
    llm_config = getattr(config, "llm", None)
    configured_max_tokens = int(getattr(llm_config, "max_tokens", 0) or 0)
    configured_context_window = int(
        getattr(llm_config, "context_window_tokens", 0) or 0
    )
    context_window, _context_source = resolve_effective_context_window(
        catalog,
        model,
        provider=provider_id,
        global_override=configured_context_window,
    )
    max_output_tokens = _profile_import_output_tokens(
        int(
            catalog.resolve_max_tokens(
                model,
                user_override=configured_max_tokens,
                provider=provider_id,
            )
        )
    )
    request_budget = resolve_auxiliary_request_budget(
        None,
        provider_id=provider_id,
        model=model,
        context_window_tokens=context_window,
        max_output_tokens=max_output_tokens,
    )
    input_budget_tokens = min(
        max(1, int(context_window) - request_budget.max_output_tokens),
        request_budget.max_input_tokens,
    )
    quotas = ProfileImportQuotas(
        max_file_size_kb=int(getattr(memory_config, "max_file_size_kb", 1024) or 0),
        max_total_size_kb=int(getattr(memory_config, "max_total_size_kb", 102400) or 0),
        max_files=int(getattr(memory_config, "max_files", 500) or 0),
        max_raw_bytes=262_144,
        max_request_tokens=input_budget_tokens,
    )
    base_url = str(getattr(primary, "base_url", "") or "")
    identity = ModelIdentity(
        provider=provider_id,
        model=model,
        is_loopback=_is_loopback_deployment(provider_id, base_url),
    )
    completion = _GatewayFusionCompletion(
        ctx=ctx,
        selector=isolated_selector,
        provider_id=provider_id,
        model=model,
        agent_id=agent_id,
        max_tokens=request_budget.max_output_tokens,
        provider_request_max_chars=request_budget.provider_request_max_chars,
    )
    return ProfileImportService(
        paths,
        identity,
        completion,
        quotas=quotas,
    )


def _service(
    ctx: RpcContext,
    agent_id: str,
    *,
    require_model: bool = True,
) -> Any:
    service = _injected_service(ctx, agent_id)
    return (
        service
        if service is not None
        else _build_profile_import_service(
            ctx,
            agent_id,
            require_model=require_model,
        )
    )


def _job_runner() -> Any:
    from opensquilla.memory.profile_import.jobs import (
        current_profile_import_job_runner,
    )

    return current_profile_import_job_runner()


def _assert_expected_model_identity(
    service: Any,
    *,
    expected_provider: str,
    expected_model: str,
    expected_is_local: bool,
) -> None:
    """Reject a hot-config identity change before imported text reaches a provider."""

    identity = getattr(service, "model", None)
    actual_provider = str(getattr(identity, "provider", "") or "")
    actual_model = str(getattr(identity, "model", "") or "")
    actual_is_local = bool(getattr(identity, "is_loopback", False))
    if not actual_provider or not actual_model:
        raise RpcHandlerError(
            "MEMORY_IMPORT_UNAVAILABLE",
            "The profile import service did not expose its provider and model.",
        )
    if (actual_provider, actual_model, actual_is_local) != (
        expected_provider,
        expected_model,
        expected_is_local,
    ):
        raise RpcHandlerError(
            "MEMORY_IMPORT_STALE_PREVIEW",
            "The default provider or model changed. Refresh the import page before retrying.",
            details={
                "expectedProvider": expected_provider,
                "expectedModel": expected_model,
                "expectedIsLocal": expected_is_local,
                "actualProvider": actual_provider,
                "actualModel": actual_model,
                "actualIsLocal": actual_is_local,
            },
        )


def _raise_stable_error(exc: Exception, *, fallback_code: str) -> None:
    if isinstance(exc, RpcHandlerError):
        raise exc
    raw_code = getattr(exc, "code", None)
    code = str(raw_code) if raw_code in _STABLE_ERROR_CODES else fallback_code
    details = _to_wire(getattr(exc, "details", None))
    retryable = bool(getattr(exc, "retryable", False))
    raise RpcHandlerError(
        code,
        str(exc) or "Profile import failed.",
        details=details,
        retryable=retryable,
    ) from exc


async def _call(
    service: Any,
    method: str,
    *args: Any,
    fallback_code: str,
    **kwargs: Any,
) -> Any:
    fn = getattr(service, method, None)
    if not callable(fn):
        raise RpcHandlerError(
            "MEMORY_IMPORT_UNAVAILABLE",
            "Profile import service is not configured.",
        )
    try:
        return await _maybe_await(fn(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001 - translated into the stable RPC surface
        _raise_stable_error(exc, fallback_code=fallback_code)


async def _recover_before_mutation(
    ctx: RpcContext,
    *,
    service: Any,
    agent_id: str,
) -> list[str]:
    recover = getattr(service, "recover", None)
    if not callable(recover):
        return []
    try:
        result = await _maybe_await(recover())
        if isinstance(result, list):
            return [str(item) for item in result if isinstance(item, str)]
        return []
    except Exception as exc:  # noqa: BLE001 - stable recovery failure contract
        # Recovery may have durably finalized an earlier journal before a
        # later corrupt/conflicting journal failed. Its partial batch list is
        # then unavailable, so conservatively invalidate snapshots and force a
        # complete derived-index sync before surfacing the stable error.
        await _refresh_recovered_runtime(
            ctx,
            service=service,
            agent_id=agent_id,
            batch_ids=[],
            force_full_sync=True,
        )
        _raise_stable_error(exc, fallback_code="MEMORY_IMPORT_WRITE_FAILED")
    return []


def _with_schema(result: Any) -> dict[str, Any]:
    wire = _to_wire(result)
    if wire is None:
        wire = {}
    if not isinstance(wire, dict):
        raise RpcHandlerError(
            "MEMORY_IMPORT_INVALID_OUTPUT",
            "Profile import service returned an invalid response.",
        )
    return {"schemaVersion": _SCHEMA_VERSION, **wire}


def _info_wire(result: Any) -> dict[str, Any]:
    wire = _with_schema(result)
    if "isLocal" not in wire and "isLoopback" in wire:
        wire["isLocal"] = bool(wire.pop("isLoopback"))
    if "maxInputBytes" not in wire and "maxRawBytes" in wire:
        wire["maxInputBytes"] = int(wire.pop("maxRawBytes"))
    return wire


def _unavailable_info(ctx: RpcContext) -> dict[str, Any]:
    llm = getattr(getattr(ctx, "config", None), "llm", None)
    provider = str(getattr(llm, "provider", "") or "")
    model = str(getattr(llm, "model", "") or "")
    base_url = str(getattr(llm, "base_url", "") or "")
    return {
        "schemaVersion": _SCHEMA_VERSION,
        "available": False,
        "provider": provider,
        "model": model,
        "isLocal": _is_loopback_deployment(provider, base_url),
        "maxInputBytes": 262_144,
        "promptVersion": "profile-fusion-v3",
        "recentImport": None,
    }


def _preview_request(params: dict[str, Any]) -> Any:
    raw_text = _required_text(params, "rawText")
    ui_locale = _optional_text(params, "uiLocale", default="en") or "en"
    export_prompt_version = (
        _optional_text(params, "exportPromptVersion", default="profile-export-v1")
        or "profile-export-v1"
    )
    client_request_id = _required_text(params, "clientRequestId")
    declared_source = _optional_text(params, "declaredSource")
    try:
        from opensquilla.memory.profile_import import ProfileImportPreviewRequest
    except ImportError:
        # Injection seam for gateway-only tests and downstream implementations.
        return {
            "raw_text": raw_text,
            "ui_locale": ui_locale,
            "export_prompt_version": export_prompt_version,
            "client_request_id": client_request_id,
            "declared_source": declared_source,
        }
    return ProfileImportPreviewRequest(
        raw_text=raw_text,
        ui_locale=ui_locale,
        export_prompt_version=export_prompt_version,
        client_request_id=client_request_id,
        declared_source=declared_source,
    )


async def _index_receipt_sources(
    service: Any,
    manager: Any,
    receipt_id: str,
    *,
    timeout_seconds: float = _DERIVED_REFRESH_TIMEOUT_SECONDS,
) -> bool | None:
    """Index committed MEMORY/IMPORT sources; return None for injected old seams."""

    domain_store = getattr(service, "store", None)
    load_receipt = getattr(domain_store, "load_receipt", None)
    index_store = getattr(manager, "store", None)
    index_file = getattr(index_store, "index_file", None)
    remove_file = getattr(index_store, "remove_file", None)
    if not callable(load_receipt) or not callable(index_file) or not callable(remove_file):
        return None

    from opensquilla.memory.profile_import.files import read_text_image, target_path
    from opensquilla.memory.types import MemorySource

    try:
        async with asyncio.timeout(timeout_seconds):
            receipt = load_receipt(receipt_id)
            for plan in receipt.files:
                target = str(
                    getattr(getattr(plan, "target", None), "value", plan.target)
                )
                if target not in {"MEMORY", "IMPORT"}:
                    continue
                root, path = target_path(service.paths, plan)
                exists, content, _mode = read_text_image(root, path)
                if exists:
                    await _maybe_await(
                        index_file(
                            path=plan.relative_path,
                            content=content,
                            source=MemorySource.memory,
                        )
                    )
                else:
                    await _maybe_await(remove_file(plan.relative_path))
        return True
    except Exception:  # noqa: BLE001 - index is derived; source files remain authoritative
        return False


async def _persist_index_status(
    service: Any,
    receipt_id: str,
    status: str,
    *,
    timeout_seconds: float = _DERIVED_REFRESH_TIMEOUT_SECONDS,
) -> None:
    update = getattr(service, "set_index_status", None)
    if not callable(update):
        return
    try:
        await _bounded_await(
            update(receipt_id, status),
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        # Diagnostic metadata may lag; source files and the dirty retry marker
        # remain authoritative.
        return


async def _refresh_recovered_runtime(
    ctx: RpcContext,
    *,
    service: Any,
    agent_id: str,
    batch_ids: list[str],
    force_full_sync: bool = False,
    timeout_seconds: float = _DERIVED_REFRESH_TIMEOUT_SECONDS,
) -> None:
    """Refresh derived state after journal recovery changed canonical files."""

    if not batch_ids and not force_full_sync:
        return
    derived_ok = True
    runner = getattr(ctx, "turn_runner", None)
    for name in ("invalidate_profile_snapshot", "refresh_memory_snapshot"):
        callback = getattr(runner, name, None)
        if not callable(callback):
            continue
        try:
            await _bounded_await(
                callback(agent_id),
                timeout_seconds=timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - canonical files remain authoritative
            derived_ok = False

    managers = getattr(ctx, "memory_managers", None) or {}
    manager = managers.get(agent_id)
    receipts: list[Any] = []
    domain_store = getattr(service, "store", None)
    load_by_batch = getattr(domain_store, "load_receipt_by_batch", None)
    for batch_id in batch_ids:
        if not callable(load_by_batch):
            break
        try:
            receipt = load_by_batch(batch_id)
        except Exception:  # noqa: BLE001 - recovery itself already completed safely
            derived_ok = False
            continue
        if receipt is not None:
            receipts.append(receipt)

    if manager is None:
        for receipt in receipts:
            await _persist_index_status(service, str(receipt.receipt_id), "pending")
        return

    mark_dirty = getattr(getattr(manager, "sync_manager", None), "mark_dirty", None)
    if callable(mark_dirty):
        try:
            mark_dirty()
        except Exception:  # noqa: BLE001 - derived index remains retryable
            derived_ok = False

    requires_full_sync = force_full_sync or len(receipts) != len(batch_ids)
    receipt_indexed: dict[str, bool] = {}
    for receipt in receipts:
        receipt_id = str(receipt.receipt_id)
        indexed = await _index_receipt_sources(
            service,
            manager,
            receipt_id,
            timeout_seconds=timeout_seconds,
        )
        if indexed is None:
            requires_full_sync = True
        else:
            receipt_indexed[receipt_id] = indexed

    full_sync_ok = True
    if requires_full_sync:
        sync = getattr(manager, "sync", None)
        if callable(sync):
            try:
                await _bounded_await(
                    sync(reason="profile_import_recovery", force=True),
                    timeout_seconds=timeout_seconds,
                )
            except Exception:  # noqa: BLE001 - source files stay authoritative
                full_sync_ok = False
        else:
            full_sync_ok = False

    for receipt in receipts:
        receipt_id = str(receipt.receipt_id)
        indexed = receipt_indexed.get(receipt_id, full_sync_ok)
        status = "ready" if indexed and derived_ok else "pending"
        await _persist_index_status(
            service,
            receipt_id,
            status,
            timeout_seconds=timeout_seconds,
        )


def _startup_profile_import_service(ctx: RpcContext, agent_id: str) -> Any:
    from opensquilla.memory.profile_import import ModelIdentity, ProfileImportService

    return ProfileImportService(
        _profile_import_paths(ctx, agent_id),
        ModelIdentity(
            provider="startup-maintenance",
            model="startup-maintenance",
        ),
        None,
    )


async def run_profile_import_startup_recovery(
    *,
    config: Any,
    memory_managers: dict[str, Any],
) -> dict[str, list[str]]:
    """Recover canonical profile files serially before Gateway readiness."""

    ctx = RpcContext(
        conn_id="profile-import-recovery",
        config=config,
        memory_managers=memory_managers,
    )
    recovered: dict[str, list[str]] = {}
    for agent_id in sorted(memory_managers):
        service = _startup_profile_import_service(ctx, agent_id)
        try:
            recovered[agent_id] = await service.recover()
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeError(
                f"profile import startup recovery failed for agent {agent_id!r}: {exc}"
            ) from exc
    return recovered


async def run_profile_import_startup_maintenance(
    *,
    config: Any,
    memory_managers: dict[str, Any],
    turn_runner: Any = None,
    recovered_batches: dict[str, list[str]] | None = None,
    timeout_seconds: float = _DERIVED_REFRESH_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Run non-canonical cleanup and derived refresh after Gateway readiness."""

    from datetime import UTC, datetime

    from opensquilla.memory.profile_import.store import (
        cleanup_expired_profile_import_raw,
        harden_profile_import_private_state,
    )

    failures: dict[str, str] = {}
    state_dir = _shared_state_dir(
        RpcContext(
            conn_id="profile-import-maintenance",
            config=config,
        )
    )
    try:
        await asyncio.to_thread(
            lambda: (
                harden_profile_import_private_state(state_dir),
                cleanup_expired_profile_import_raw(state_dir, datetime.now(UTC)),
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort after readiness
        failures["_raw"] = str(exc)

    ctx = RpcContext(
        conn_id="profile-import-maintenance",
        config=config,
        memory_managers=memory_managers,
        turn_runner=turn_runner,
    )

    async def maintain_agent(agent_id: str) -> tuple[str, str | None]:
        service = _startup_profile_import_service(ctx, agent_id)
        try:
            await _refresh_recovered_runtime(
                ctx,
                service=service,
                agent_id=agent_id,
                batch_ids=(recovered_batches or {}).get(agent_id, []),
                timeout_seconds=timeout_seconds,
            )
            # Purging expired preview metadata is private-state maintenance, not
            # canonical recovery. It remains serial with the shared profile
            # operation lock and is cancellable with this lifecycle task.
            await service.info()
            return agent_id, None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one agent must not block boot
            return agent_id, str(exc)

    # ProfileOperationLock is shared by every agent under one profile home.
    # Serial maintenance avoids self-contention and preserves deterministic
    # shutdown/cancellation behavior.
    results = []
    for agent_id in sorted(memory_managers):
        results.append(await maintain_agent(agent_id))
    failures.update(
        {
            agent_id: error
            for agent_id, error in results
            if error is not None
        }
    )
    return failures


async def _refresh_runtime(
    ctx: RpcContext,
    result: dict[str, Any],
    *,
    service: Any,
) -> None:
    """Refresh derived snapshots/index without rolling back canonical files."""

    status = str(result.get("status") or "")
    if status not in {"applied", "undone", "alreadyApplied", "alreadyUndone"}:
        return
    agent_id = str(result.get("agentId") or "main")
    receipt_id = str(result.get("receiptId") or "")
    runner = getattr(ctx, "turn_runner", None)
    invalidate = getattr(runner, "invalidate_profile_snapshot", None)
    derived_ok = True
    if callable(invalidate):
        try:
            await _bounded_await(
                invalidate(agent_id),
                timeout_seconds=_DERIVED_REFRESH_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 - committed source files stay authoritative
            derived_ok = False
    refresh = getattr(runner, "refresh_memory_snapshot", None)
    if callable(refresh):
        try:
            await _bounded_await(
                refresh(agent_id),
                timeout_seconds=_DERIVED_REFRESH_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 - committed source files stay authoritative
            derived_ok = False
    managers = getattr(ctx, "memory_managers", None) or {}
    manager = managers.get(agent_id)
    if manager is None:
        result["indexStatus"] = "pending"
        if receipt_id:
            await _persist_index_status(service, receipt_id, "pending")
        return
    mark_dirty = getattr(getattr(manager, "sync_manager", None), "mark_dirty", None)
    if callable(mark_dirty):
        try:
            mark_dirty()
        except Exception:  # noqa: BLE001 - source files stay authoritative
            derived_ok = False

    indexed = (
        await _index_receipt_sources(
            service,
            manager,
            receipt_id,
            timeout_seconds=_DERIVED_REFRESH_TIMEOUT_SECONDS,
        )
        if receipt_id
        else None
    )
    if indexed is None:
        sync = getattr(manager, "sync", None)
        if callable(sync):
            try:
                await _bounded_await(
                    sync(reason="profile_import", force=True),
                    timeout_seconds=_DERIVED_REFRESH_TIMEOUT_SECONDS,
                )
                indexed = True
            except Exception:  # noqa: BLE001 - source files remain authoritative
                indexed = False
        else:
            indexed = False
    result["indexStatus"] = "ready" if indexed and derived_ok else "pending"
    if receipt_id:
        await _persist_index_status(service, receipt_id, result["indexStatus"])


async def _artifact_agent(
    ctx: RpcContext,
    *,
    kind: str,
    artifact_id: str,
    explicit_agent_id: str | None,
) -> str:
    from opensquilla.session.keys import normalize_agent_id

    if explicit_agent_id:
        return normalize_agent_id(explicit_agent_id)
    if getattr(ctx, "profile_import_service", None) is not None:
        return "main"
    try:
        from opensquilla.memory.profile_import import (
            lookup_preview_agent,
            lookup_receipt_agent,
        )
    except ImportError:
        return "main"
    lookup: Callable[[Path, str], str | None]
    if kind == "preview":
        lookup = lookup_preview_agent
    elif kind == "job":
        from opensquilla.memory.profile_import import lookup_job_agent

        lookup = lookup_job_agent
    else:
        lookup = lookup_receipt_agent
    try:
        agent_id = await _maybe_await(lookup(_shared_state_dir(ctx), artifact_id))
    except Exception as exc:  # noqa: BLE001 - normalize opaque-id and locator failures
        _raise_stable_error(exc, fallback_code="MEMORY_IMPORT_PREVIEW_EXPIRED")
    return normalize_agent_id(str(agent_id or "main"))


@_d.method("memory.import.info", scope="operator.read")
async def _handle_memory_import_info(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    values = _params(params)
    agent_id = _optional_text(values, "agentId", default="main") or "main"
    try:
        service = _service(ctx, agent_id)
    except RpcHandlerError as exc:
        if exc.code == "MEMORY_IMPORT_UNAVAILABLE":
            try:
                service = _service(ctx, agent_id, require_model=False)
            except RpcHandlerError:
                return _unavailable_info(ctx)
        else:
            raise
    result = await _call(
        service,
        "info",
        fallback_code="MEMORY_IMPORT_UNAVAILABLE",
    )
    return _info_wire(result)


@_d.method("memory.import.preview", scope="operator.admin")
async def _handle_memory_import_preview(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    values = _params(params)
    agent_id = _optional_text(values, "agentId", default="main") or "main"
    expected_provider = _required_text(values, "expectedProvider")
    expected_model = _required_text(values, "expectedModel")
    expected_is_local = _required_bool(values, "expectedIsLocal")
    request = _preview_request(values)
    service = _service(ctx, agent_id)
    _assert_expected_model_identity(
        service,
        expected_provider=expected_provider,
        expected_model=expected_model,
        expected_is_local=expected_is_local,
    )
    recovered = await _recover_before_mutation(
        ctx,
        service=service,
        agent_id=agent_id,
    )
    await _refresh_recovered_runtime(
        ctx,
        service=service,
        agent_id=agent_id,
        batch_ids=recovered,
    )
    result = await _call(
        service,
        "preview",
        request,
        fallback_code="MEMORY_IMPORT_MODEL_FAILED",
    )
    wire = _with_schema(result)
    wire.setdefault("agentId", agent_id)
    return wire


@_d.method("memory.import.start", scope="operator.admin")
async def _handle_memory_import_start(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    values = _params(params)
    agent_id = _optional_text(values, "agentId", default="main") or "main"
    service = _service(ctx, agent_id)
    _assert_expected_model_identity(
        service,
        expected_provider=_required_text(values, "expectedProvider"),
        expected_model=_required_text(values, "expectedModel"),
        expected_is_local=_required_bool(values, "expectedIsLocal"),
    )
    recovered = await _recover_before_mutation(
        ctx,
        service=service,
        agent_id=agent_id,
    )
    await _refresh_recovered_runtime(
        ctx,
        service=service,
        agent_id=agent_id,
        batch_ids=recovered,
    )
    result = await _call(
        _job_runner(),
        "start",
        service,
        _preview_request(values),
        fallback_code="MEMORY_IMPORT_MODEL_FAILED",
    )
    wire = _with_schema(result)
    wire.setdefault("agentId", agent_id)
    return wire


@_d.method("memory.import.status", scope="operator.admin")
async def _handle_memory_import_status(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    values = _params(params)
    job_id = _required_text(values, "jobId")
    agent_id = await _artifact_agent(
        ctx,
        kind="job",
        artifact_id=job_id,
        explicit_agent_id=_optional_text(values, "agentId"),
    )
    service = _service(ctx, agent_id, require_model=False)
    result = await _call(
        service,
        "job_status",
        job_id,
        fallback_code="MEMORY_IMPORT_JOB_NOT_FOUND",
    )
    wire = _with_schema(result)
    wire.setdefault("agentId", agent_id)
    return wire


@_d.method("memory.import.retry", scope="operator.admin")
async def _handle_memory_import_retry(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    values = _params(params)
    job_id = _required_text(values, "jobId")
    agent_id = await _artifact_agent(
        ctx,
        kind="job",
        artifact_id=job_id,
        explicit_agent_id=_optional_text(values, "agentId"),
    )
    service = _service(ctx, agent_id)
    _assert_expected_model_identity(
        service,
        expected_provider=_required_text(values, "expectedProvider"),
        expected_model=_required_text(values, "expectedModel"),
        expected_is_local=_required_bool(values, "expectedIsLocal"),
    )
    result = await _call(
        _job_runner(),
        "retry",
        service,
        job_id,
        _required_text(values, "clientRequestId"),
        fallback_code="MEMORY_IMPORT_MODEL_FAILED",
    )
    wire = _with_schema(result)
    wire.setdefault("agentId", agent_id)
    return wire


@_d.method("memory.import.cancel", scope="operator.admin")
async def _handle_memory_import_cancel(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    values = _params(params)
    job_id = _required_text(values, "jobId")
    agent_id = await _artifact_agent(
        ctx,
        kind="job",
        artifact_id=job_id,
        explicit_agent_id=_optional_text(values, "agentId"),
    )
    service = _service(ctx, agent_id, require_model=False)
    result = await _call(
        _job_runner(),
        "cancel",
        service,
        job_id,
        fallback_code="MEMORY_IMPORT_WRITE_FAILED",
    )
    wire = _with_schema(result)
    wire.setdefault("agentId", agent_id)
    return wire


@_d.method("memory.import.apply", scope="operator.admin")
async def _handle_memory_import_apply(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    values = _params(params)
    preview_id = _required_text(values, "previewId")
    agent_id = await _artifact_agent(
        ctx,
        kind="preview",
        artifact_id=preview_id,
        explicit_agent_id=_optional_text(values, "agentId"),
    )
    service = _service(ctx, agent_id, require_model=False)
    recovered = await _recover_before_mutation(
        ctx,
        service=service,
        agent_id=agent_id,
    )
    await _refresh_recovered_runtime(
        ctx,
        service=service,
        agent_id=agent_id,
        batch_ids=recovered,
    )
    result = await _call(
        service,
        "apply",
        preview_id,
        _required_text(values, "candidateHash"),
        _required_text(values, "idempotencyKey"),
        fallback_code="MEMORY_IMPORT_WRITE_FAILED",
    )
    wire = _with_schema(result)
    wire.setdefault("agentId", agent_id)
    await _refresh_runtime(ctx, wire, service=service)
    return wire


@_d.method("memory.import.undo", scope="operator.admin")
async def _handle_memory_import_undo(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    values = _params(params)
    receipt_id = _required_text(values, "receiptId")
    expected_provider = _required_text(values, "expectedProvider")
    expected_model = _required_text(values, "expectedModel")
    expected_is_local = _required_bool(values, "expectedIsLocal")
    agent_id = await _artifact_agent(
        ctx,
        kind="receipt",
        artifact_id=receipt_id,
        explicit_agent_id=_optional_text(values, "agentId"),
    )
    service = _service(ctx, agent_id, require_model=False)
    recovered = await _recover_before_mutation(
        ctx,
        service=service,
        agent_id=agent_id,
    )
    await _refresh_recovered_runtime(
        ctx,
        service=service,
        agent_id=agent_id,
        batch_ids=recovered,
    )
    result = await _call(
        service,
        "undo",
        receipt_id,
        _required_text(values, "clientRequestId"),
        fallback_code="MEMORY_IMPORT_WRITE_FAILED",
    )
    first_wire = _to_wire(result)
    if (
        isinstance(first_wire, dict)
        and first_wire.get("status") == "reviewRequired"
        and first_wire.get("preview") is None
    ):
        service = _service(ctx, agent_id)
        _assert_expected_model_identity(
            service,
            expected_provider=expected_provider,
            expected_model=expected_model,
            expected_is_local=expected_is_local,
        )
        result = await _call(
            service,
            "undo",
            receipt_id,
            _required_text(values, "clientRequestId"),
            fallback_code="MEMORY_IMPORT_MODEL_FAILED",
        )
    wire = _with_schema(result)
    wire.setdefault("agentId", agent_id)
    await _refresh_runtime(ctx, wire, service=service)
    return wire


@_d.method("memory.import.discard", scope="operator.admin")
async def _handle_memory_import_discard(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    values = _params(params)
    preview_id = _optional_text(values, "previewId")
    job_id = _optional_text(values, "jobId")
    if bool(preview_id) == bool(job_id):
        raise ValueError("exactly one of params.previewId or params.jobId is required")
    artifact_id = str(job_id or preview_id)
    agent_id = await _artifact_agent(
        ctx,
        kind="job" if job_id else "preview",
        artifact_id=artifact_id,
        explicit_agent_id=_optional_text(values, "agentId"),
    )
    service = _service(ctx, agent_id, require_model=False)
    if job_id:
        result = await _call(
            service,
            "discard_job",
            job_id,
            fallback_code="MEMORY_IMPORT_WRITE_FAILED",
        )
    else:
        recovered = await _recover_before_mutation(
            ctx,
            service=service,
            agent_id=agent_id,
        )
        await _refresh_recovered_runtime(
            ctx,
            service=service,
            agent_id=agent_id,
            batch_ids=recovered,
        )
        result = await _call(
            service,
            "discard",
            preview_id,
            fallback_code="MEMORY_IMPORT_WRITE_FAILED",
        )
    wire = _with_schema(result)
    if len(wire) == 1:
        wire.update(
            {
                "status": "discarded",
                **({"jobId": job_id} if job_id else {"previewId": preview_id}),
            }
        )
    return wire
