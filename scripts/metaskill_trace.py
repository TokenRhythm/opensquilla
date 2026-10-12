"""Export an invocation's native MetaSkill ledger to a local JSON file."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import asyncio
import json
from pathlib import Path
import threading
from typing import Any, Iterator, TYPE_CHECKING
from uuid import uuid4

from opensquilla.paths import native_io_path

if TYPE_CHECKING:
    from opensquilla.persistence.meta_run_writer import MetaRunWriter

ROOT = Path(__file__).resolve().parents[1]


class LocalUsageSink:
    """Record native provider-call envelopes inside one invocation's workspace.

    The engine performs receipt normalization and physical-call accounting.
    This adapter only implements its sink contract; it never reads a user
    session or pricing profile. Missing receipts stay unknown, not zero-cost.
    """

    def __init__(self, destination: Path):
        self.destination = destination
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _write(self, call, status: str, *, result=None, reason=None, scope_key=None):
        with self._lock:
            previous = self._records.get(call.event_id)
            if previous and previous["status"] in {"finalized", "unknown"}:
                return  # Native cancellation/retry delivery is idempotent.
            record = {
                "call": asdict(call), "status": status,
                "scope_key": scope_key if scope_key is not None else (previous or {}).get("scope_key"),
            }
            if result is not None:
                record["result"] = asdict(result)
            if reason is not None:
                record["reason"] = reason
            destination = native_io_path(self.destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Do not persist prompts, tool arguments, headers, or credentials.
            with destination.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
            self._records[call.event_id] = record

    async def start(self, call):
        from opensquilla.engine.usage import _current_usage_scope

        await asyncio.to_thread(self._write, call, "started", scope_key=_current_usage_scope.get())

    async def finalize(self, call, result):
        await asyncio.to_thread(self._write, call, "finalized", result=result)

    async def mark_unknown(self, call, reason):
        from opensquilla.usage_reasons import normalize_usage_unknown_reason

        await asyncio.to_thread(self._write, call, "unknown", reason=normalize_usage_unknown_reason(reason))

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records.values())


def summarize_provider_usage(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate envelopes once; per-model items are dimensions, not extra spend."""
    known = []
    unknown = 0
    missing_cost = 0
    for record in calls:
        result = record.get("result", {})
        has_tokens = any(result.get(key, 0) for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        if record["status"] != "finalized" or result.get("missing_usage_entries", 0) or not has_tokens:
            unknown += 1
        else:
            known.append(result)
        missing_item_cost = any(item.get("cost_source") in {None, "unavailable"} for item in result.get("items", []))
        if record["status"] != "finalized" or result.get("cost_source") in {None, "unavailable"} or result.get("missing_usage_entries", 0) or missing_item_cost:
            missing_cost += 1
    sums = {key: sum(item.get(key, 0) for item in known) for key in (
        "input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
    )}
    finalized = [record["result"] for record in calls if record["status"] == "finalized"]
    observed_billed = sum(item.get("billed_cost_nanos", 0) for item in finalized) / 1_000_000_000
    observed_estimated = sum(item.get("estimated_cost_nanos", 0) for item in finalized) / 1_000_000_000
    return {
        "call_count": len(calls), "known_usage_calls": len(known), "unknown_usage_calls": unknown,
        "usage_available": bool(calls) and unknown == 0,
        **{key: value if calls and not unknown else None for key, value in sums.items()},
        "total_tokens": sums["input_tokens"] + sums["output_tokens"] if calls and not unknown else None,
        "known_token_subtotals": sums,
        "cost_available": bool(calls) and missing_cost == 0,
        "cost_usd": observed_billed + observed_estimated if calls and not missing_cost else None,
        "observed_billed_cost_usd": observed_billed,
        "observed_estimated_cost_usd": observed_estimated,
        "missing_cost_calls": missing_cost,
        "coverage": "not_applicable" if not calls else "complete" if not unknown and not missing_cost else "partial",
        "scope": "native provider calls; external Skill programs and task-specific paid APIs are not included",
    }


def _expanded_trace_inputs(value: Any, limit: int) -> tuple[Any, bool]:
    """Retain large rendered bindings within an explicit limit, still redacting.

    The native writer additionally clips each input string at 4 KiB. An opt-in
    benchmark trace therefore carries this projection without changing the
    native database's redaction/replay behavior.
    """
    from opensquilla.persistence.meta_run_writer import (
        _CURRENT_RUN_ONLY_INPUT_KEYS, _SECRET_KEY_RE, _SECRET_PREFIX_RE,
    )

    def redact(item):
        if isinstance(item, dict):
            return {
                key: "[REDACTED]" if _SECRET_KEY_RE.search(str(key)) else redact(content)
                for key, content in item.items() if str(key) not in _CURRENT_RUN_ONLY_INPUT_KEYS
            }
        if isinstance(item, (list, tuple)):
            return [redact(content) for content in item]
        if isinstance(item, str) and _SECRET_PREFIX_RE.match(item):
            return "[REDACTED]"
        return item

    projected = redact(value)
    size = len(json.dumps(projected, ensure_ascii=False).encode("utf-8"))
    if size > limit:
        return {"__trace_field_truncated__": True, "original_utf8_bytes": size}, True
    return projected, False


class _TraceWriterAdapter:
    """Observe rendered inputs while delegating all lifecycle writes natively."""

    def __init__(self, writer, limit: int):
        self.native = writer
        self.limit = limit
        self.rendered: dict[str, tuple[Any, bool]] = {}

    def __getattr__(self, name):
        return getattr(self.native, name)

    def begin_step_sync(self, *, run_id, step, effective_skill, rendered_inputs):
        self.rendered[step.id] = _expanded_trace_inputs(rendered_inputs, self.limit)
        return self.native.begin_step_sync(
            run_id=run_id, step=step, effective_skill=effective_skill, rendered_inputs=rendered_inputs,
        )


def _decode_json_fields(value: Any) -> Any:
    """Decode the native writer's JSON columns without changing their contents."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key.endswith("_json"):
                if not isinstance(item, str):
                    raise RuntimeError(f"Invalid trace column: {key}")
                try:
                    result[key.removesuffix("_json")] = json.loads(item)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"Invalid persisted trace JSON: {key}") from exc
            else:
                result[key] = _decode_json_fields(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_decode_json_fields(item) for item in value]
    return value


@dataclass
class LocalTrace:
    writer: MetaRunWriter
    destination: Path
    database: Path
    max_field_bytes: int | None = None
    usage_sink: LocalUsageSink | None = None

    def export(self, run_id: str | None = None) -> Path:
        """Export a terminal native record; missing persistence is an error.

        Each invocation opens a new database. Omitting ``run_id`` therefore
        requires exactly one recorded run rather than selecting an older run.
        Input redaction and field limits are those of the native writer.
        Plans and output text may still contain information supplied by Skills.
        """
        if run_id is None:
            runs = self.writer.list_runs(limit=2)
            if len(runs) != 1:
                raise RuntimeError("Trace requires exactly one persisted run")
            run_id = runs[0].run_id
        record = self.writer.get_run(run_id)
        if record is None:
            raise RuntimeError(f"Trace record not found: {run_id}")
        if record.status not in {"ok", "failed", "cancelled"} or record.ended_at_ms is None:
            raise RuntimeError("Trace run has not reached a terminal state")
        if any(step.ended_at_ms is None for step in record.steps):
            raise RuntimeError("Trace contains an unfinished step record")
        payload = _decode_json_fields(asdict(record))
        if self.max_field_bytes is not None:
            for step in payload["steps"]:
                if step["step_id"] in self.writer.rendered:
                    step["rendered_inputs"], clipped = self.writer.rendered[step["step_id"]]
                    step["rendered_inputs_truncated"] = clipped
            payload["trace_limits"] = {
                "max_field_bytes": self.max_field_bytes,
                "rendered_inputs": "opt-in expanded, secret-redacted projection; database retains native clipping",
                "inputs": "native redaction and 4 KiB per-string clipping remain in effect",
            }
        if self.usage_sink is not None:
            calls = self.usage_sink.snapshot()
            payload["provider_calls"] = calls
            payload["provider_usage"] = summarize_provider_usage(calls)
            for step in payload["steps"]:
                step_calls = [call for call in calls if str(call.get("scope_key") or "").endswith(f":{step['step_id']}")]
                step["provider_usage"] = summarize_provider_usage(step_calls)
        if record.status == "ok":
            plan = payload["plan_snapshot"].get("plan", payload["plan_snapshot"])
            declared = plan.get("steps", [])
            substitutes = {step.get("on_failure") for step in declared if step.get("on_failure")}
            required = {
                step["id"] for step in declared
                if not step.get("when") and step["id"] not in substitutes
            }
            recorded = {step.step_id for step in record.steps}
            if required - recorded:
                raise RuntimeError("Trace is missing required step records")
        destination = native_io_path(self.destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return self.destination


@contextmanager
def open_trace(state: Path, destination: Path, *, max_field_bytes: int | None = None,
               usage_sink: LocalUsageSink | None = None) -> Iterator[LocalTrace]:
    """Use explicit workspace paths and source migrations, never a user DB."""
    from opensquilla.persistence.meta_run_writer import open_meta_run_writer
    from opensquilla.persistence.migrator import _native_sqlite_path, apply_pending

    if max_field_bytes is not None and (
        isinstance(max_field_bytes, bool) or not isinstance(max_field_bytes, int) or max_field_bytes <= 0
    ):
        raise ValueError("max_field_bytes must be a positive integer")
    state = state.expanduser().resolve()
    destination = destination.expanduser().resolve()
    native_io_path(state).mkdir(parents=True, exist_ok=True)
    migrations = ROOT / "migrations"
    if not migrations.is_dir():
        raise RuntimeError("Trace schema migrations are missing from the checkout")
    database = state / f"trace-{uuid4().hex}.sqlite3"
    # Both the migrator and writer must use the same Windows-native spelling.
    native_database = _native_sqlite_path(database)
    apply_pending(native_database, migrations)
    writer = open_meta_run_writer(native_database)
    if max_field_bytes is not None:
        # The native factory omits its constructor's optional field limit.
        writer._max_field_bytes = max_field_bytes
        writer = _TraceWriterAdapter(writer, max_field_bytes)
    try:
        yield LocalTrace(writer=writer, destination=destination, database=database,
                         max_field_bytes=max_field_bytes, usage_sink=usage_sink)
    finally:
        writer.close()
