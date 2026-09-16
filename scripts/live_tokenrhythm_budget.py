"""Optional real-HTTP budget relay for offline-by-default client acceptance runs.

Only this relay receives the real provider credential. A test transport sends
the client's official-URL requests here with a synthetic key. Budget mode
reserves reviewed cost before dispatch; explicit functional mode records HTTP
metadata in a separate log without consulting prices or billing receipts.
Bodies are never persisted. Budget bounds assume the provider respects reviewed
token limits and prices; an account-side limit is needed for an unconditional cap.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import http.server
import json
import math
import os
import re
import secrets
import signal
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

BUCKET_LIMITS_CNY = {"probe": 50, "regular": 200, "extended": 500, "contingency": 250}
_NANOS = Decimal(1_000_000_000)
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:/+-]{1,128}\Z")
_MAX_BODY_BYTES = 16 * 1024 * 1024


class BudgetRejectedError(RuntimeError):
    """A content-free reason for rejecting a request before provider dispatch."""


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise BudgetRejectedError("invalid_identifier")
    return value


def _amount(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise BudgetRejectedError("unknown_cost")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise BudgetRejectedError("unknown_cost") from exc
    if not amount.is_finite() or amount < 0:
        raise BudgetRejectedError("unknown_cost")
    return amount


def _nanos(value: Decimal) -> int:
    return int((value * _NANOS).to_integral_value(rounding=ROUND_CEILING))


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BudgetRejectedError("unknown_token_limit")
    return int(value)


@dataclass(frozen=True)
class ModelCostBound:
    model: str
    input_limit: int
    output_limit: int
    input_cny_per_token: Decimal
    output_cny_per_token: Decimal
    valid_until: float
    catalog_sha256: str
    image_tokens_bounded: bool = False

    def reserve_nanos(self, request: Mapping[str, Any], now: float) -> int:
        _identifier(self.model)
        if request.get("model") != self.model:
            raise BudgetRejectedError("model_bound_mismatch")
        if not math.isfinite(self.valid_until) or now >= self.valid_until:
            raise BudgetRejectedError("price_snapshot_expired")
        if not re.fullmatch(r"[a-f0-9]{64}", self.catalog_sha256):
            raise BudgetRejectedError("unverified_price_snapshot")
        input_limit = _positive_int(self.input_limit)
        output_limit = _positive_int(self.output_limit)
        input_rate = _amount(self.input_cny_per_token)
        output_rate = _amount(self.output_cny_per_token)
        if request.get("n", 1) != 1 or request.get("best_of", 1) != 1:
            raise BudgetRejectedError("unbounded_multiple_completions")
        output_fields = [
            request[key] for key in ("max_tokens", "max_completion_tokens") if key in request
        ]
        if not output_fields or any(_positive_int(value) > output_limit for value in output_fields):
            raise BudgetRejectedError("unbounded_output_tokens")
        if request.get("background") or request.get("batch"):
            raise BudgetRejectedError("unsupported_deferred_request")
        tools = request.get("tools", [])
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools
        ):
            raise BudgetRejectedError("unbounded_hosted_tool_cost")
        stack: list[Any] = [request]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                kind = node.get("type")
                if kind in {"input_audio", "audio", "video", "input_video"}:
                    raise BudgetRejectedError("unbounded_media_cost")
                if kind in {"image", "image_url", "input_image"} and not self.image_tokens_bounded:
                    raise BudgetRejectedError("unbounded_image_cost")
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        # Full model limits, not a local prompt-token estimate. This includes
        # cache/reasoning at the highest reviewed applicable unit rate.
        return _nanos(input_rate * input_limit + output_rate * output_limit)


def reviewed_catalog_bounds(
    payload: Mapping[str, Any],
    *,
    valid_until: float,
    reviewed_models: set[str],
    image_tokens_bounded_models: frozenset[str] = frozenset(),
) -> dict[str, ModelCostBound]:
    """Build conservative bounds from an explicitly reviewed public catalog.

    No network request or approval is implied. Missing/ambiguous billing facts
    reject the catalog; callers must review all models a normal preset may use.
    """
    from opensquilla.provider.tokenrhythm_catalog import parse_tokenrhythm_published

    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    catalog = parse_tokenrhythm_published(payload)
    result: dict[str, ModelCostBound] = {}
    for model in reviewed_models:
        entry = catalog.get(_identifier(model))
        if entry is None or entry.pricing is None:
            raise BudgetRejectedError("unknown_model_price")
        pricing = entry.pricing
        if pricing.currency != "CNY" or pricing.billing_mode != "per_1m_tokens":
            raise BudgetRejectedError("unknown_billing_mode")
        if pricing.price_per_image is not None:
            raise BudgetRejectedError("unbounded_image_price")
        if model in image_tokens_bounded_models and (
            "image" not in (entry.modalities or ()) or not entry.capabilities.vision
        ):
            raise BudgetRejectedError("unverified_image_token_billing")
        unit = _positive_int(pricing.billing_unit)
        buckets = (pricing.standard, pricing.discount, pricing.effective)
        # Standard prices must be present; temporary discounts cannot become
        # the only evidence for a hard reservation ceiling.
        _amount(pricing.standard.input)
        _amount(pricing.standard.output)
        input_rates = [
            _amount(value)
            for bucket in buckets
            for value in (bucket.input, bucket.cache_read)
            if value is not None
        ]
        output_rates = [_amount(bucket.output) for bucket in buckets if bucket.output is not None]
        result[model] = ModelCostBound(
            model=model,
            input_limit=_positive_int(entry.context_window),
            output_limit=_positive_int(entry.max_output_tokens),
            input_cny_per_token=max(input_rates) / unit,
            output_cny_per_token=max(output_rates) / unit,
            valid_until=valid_until,
            catalog_sha256=digest,
            image_tokens_bounded=model in image_tokens_bounded_models,
        )
    return result


class BudgetLedger:
    """One durable balance shared by baseline/new workers and relay processes."""

    def __init__(self, path: Path, *, enabled: bool = False) -> None:
        self.path = path
        self.enabled = enabled
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, bucket TEXT, "
                "variant TEXT, case_id TEXT, model TEXT, started REAL, ended REAL, "
                "reserved_nanos INTEGER, charged_nanos INTEGER, status TEXT, "
                "reason TEXT, catalog_sha256 TEXT, input_tokens INTEGER, output_tokens INTEGER)"
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(requests)")}
            for name, sql_type in (
                ("response_headers_at", "REAL"),
                ("first_event_at", "REAL"),
                ("http_status", "INTEGER"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE requests ADD COLUMN {name} {sql_type}")
            expected = json.dumps(BUCKET_LIMITS_CNY, sort_keys=True)
            found = db.execute("SELECT value FROM state WHERE key='limits'").fetchone()
            if found and found[0] != expected:
                raise BudgetRejectedError("ledger_limits_changed")
            db.execute("INSERT OR IGNORE INTO state VALUES ('limits', ?)", (expected,))
        path.chmod(0o600)

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def select_phase(self, *, bucket: str, variant: str, case_id: str) -> None:
        if bucket not in BUCKET_LIMITS_CNY or variant not in {"baseline", "new"}:
            raise BudgetRejectedError("invalid_phase")
        _identifier(case_id)
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM requests WHERE status='reserved' LIMIT 1").fetchone():
                raise BudgetRejectedError("requests_still_in_flight")
            phase = json.dumps({"bucket": bucket, "variant": variant, "case_id": case_id})
            db.execute("INSERT OR REPLACE INTO state VALUES ('phase', ?)", (phase,))

    def reserve(self, bound: ModelCostBound, request: Mapping[str, Any]) -> str:
        if not self.enabled:
            raise BudgetRejectedError("live_requests_disabled")
        now = time.time()
        amount = bound.reserve_nanos(request, now)
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM state WHERE key='halted'").fetchone():
                raise BudgetRejectedError("ledger_halted")
            row = db.execute("SELECT value FROM state WHERE key='phase'").fetchone()
            if row is None:
                raise BudgetRejectedError("phase_not_selected")
            phase = json.loads(row[0])
            used = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN status='confirmed' THEN charged_nanos "
                "ELSE reserved_nanos END), 0) FROM requests WHERE bucket=?",
                (phase["bucket"],),
            ).fetchone()[0]
            if used + amount > _nanos(Decimal(BUCKET_LIMITS_CNY[phase["bucket"]])):
                raise BudgetRejectedError("bucket_budget_exhausted")
            request_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO requests (id, bucket, variant, case_id, model, started, ended, "
                "reserved_nanos, charged_nanos, status, reason, catalog_sha256, "
                "input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, "
                "'reserved', NULL, ?, NULL, NULL)",
                (
                    request_id,
                    phase["bucket"],
                    phase["variant"],
                    phase["case_id"],
                    bound.model,
                    now,
                    amount,
                    bound.catalog_sha256,
                ),
            )
        return request_id

    def observe_response(
        self,
        request_id: str,
        *,
        http_status: int | None = None,
        first_event_at: float | None = None,
    ) -> None:
        """Record response metadata only; first event does not mean first visible token."""
        if http_status is not None and (
            type(http_status) is not int or not 100 <= http_status <= 599
        ):
            raise BudgetRejectedError("invalid_http_status")
        if first_event_at is not None and (
            not math.isfinite(first_event_at) or first_event_at <= 0
        ):
            raise BudgetRejectedError("invalid_event_time")
        with self._transaction() as db:
            db.execute(
                "UPDATE requests SET http_status=COALESCE(http_status, ?), "
                "response_headers_at=COALESCE(response_headers_at, ?), "
                "first_event_at=COALESCE(first_event_at, ?) WHERE id=?",
                (
                    http_status,
                    time.time() if http_status is not None else None,
                    first_event_at,
                    request_id,
                ),
            )

    def settle(
        self,
        request_id: str,
        *,
        cost_cny: Decimal | None,
        reason: str = "unknown_cost",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        _identifier(reason)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is not None and row["reason"] == "relay_shutdown":
                return
            if row is None or row["status"] != "reserved":
                raise BudgetRejectedError("invalid_settlement")
            charged = _nanos(_amount(cost_cny)) if cost_cny is not None else None
            status = "confirmed" if charged is not None else "unknown"
            if charged is not None and charged > row["reserved_nanos"]:
                status, reason = "over_bound", "provider_cost_exceeded_reviewed_bound"
            if status != "confirmed":
                db.execute("INSERT OR REPLACE INTO state VALUES ('halted', ?)", (reason,))
            db.execute(
                "UPDATE requests SET ended=?, charged_nanos=?, status=?, reason=?, "
                "input_tokens=?, output_tokens=? WHERE id=?",
                (
                    time.time(),
                    charged,
                    status,
                    None if status == "confirmed" else reason,
                    input_tokens,
                    output_tokens,
                    request_id,
                ),
            )

    def interrupt_requests(self, request_ids: set[str]) -> None:
        with self._transaction() as db:
            interrupted = False
            for request_id in request_ids:
                result = db.execute(
                    "UPDATE requests SET status='unknown', reason='relay_shutdown', ended=? "
                    "WHERE id=? AND status='reserved'",
                    (time.time(), request_id),
                )
                interrupted = interrupted or result.rowcount > 0
            if interrupted:
                db.execute("INSERT OR REPLACE INTO state VALUES ('halted', 'relay_shutdown')")

    def snapshot(self) -> dict[str, Any]:
        with self._transaction() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY started, id")]
            halted = db.execute("SELECT value FROM state WHERE key='halted'").fetchone()
        return {
            "limits_cny": dict(BUCKET_LIMITS_CNY),
            "total_limit_cny": sum(BUCKET_LIMITS_CNY.values()),
            "halted": halted[0] if halted else None,
            "requests": rows,
        }


class FunctionalRequestLog:
    """Physical HTTP metadata for an explicitly selected functional test run."""

    def __init__(self, path: Path, *, enabled: bool = False) -> None:
        self.path = path.resolve()
        self.enabled = enabled
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            # Inspect before any writable connection: a budget ledger is never migrated.
            try:
                with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as db:
                    mode = db.execute("SELECT value FROM state WHERE key='mode'").fetchone()
                    version = db.execute(
                        "SELECT value FROM state WHERE key='schema_version'"
                    ).fetchone()
                if mode != ("functional",) or version != ("1",):
                    raise BudgetRejectedError("not_a_functional_request_log")
            except sqlite3.DatabaseError as exc:
                raise BudgetRejectedError("not_a_functional_request_log") from exc
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, variant TEXT, "
                "case_id TEXT, model TEXT, started REAL, ended REAL, status TEXT, reason TEXT, "
                "response_headers_at REAL, http_status INTEGER, first_chunk_at REAL, "
                "first_event_at REAL, request_bytes INTEGER, response_bytes INTEGER)"
            )
            db.execute("INSERT OR IGNORE INTO state VALUES ('mode', 'functional')")
            db.execute("INSERT OR IGNORE INTO state VALUES ('schema_version', '1')")
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def select_phase(self, *, variant: str, case_id: str) -> None:
        if variant not in {"baseline", "new"}:
            raise BudgetRejectedError("invalid_phase")
        _identifier(case_id)
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM requests WHERE status='in_flight' LIMIT 1").fetchone():
                raise BudgetRejectedError("requests_still_in_flight")
            db.execute(
                "INSERT OR REPLACE INTO state VALUES ('phase', ?)",
                (json.dumps({"variant": variant, "case_id": case_id}),),
            )

    def start_request(self, *, model: str, request_bytes: int) -> str:
        if not self.enabled:
            raise BudgetRejectedError("live_requests_disabled")
        _identifier(model)
        with self._transaction() as db:
            row = db.execute("SELECT value FROM state WHERE key='phase'").fetchone()
            if row is None:
                raise BudgetRejectedError("phase_not_selected")
            phase = json.loads(row[0])
            request_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO requests (id, variant, case_id, model, started, status, "
                "request_bytes, response_bytes) VALUES (?, ?, ?, ?, ?, 'in_flight', ?, 0)",
                (request_id, phase["variant"], phase["case_id"], model, time.time(), request_bytes),
            )
        return request_id

    def observe_response(
        self,
        request_id: str,
        *,
        http_status: int | None = None,
        first_chunk_at: float | None = None,
        first_event_at: float | None = None,
    ) -> None:
        with self._transaction() as db:
            db.execute(
                "UPDATE requests SET http_status=COALESCE(http_status, ?), "
                "response_headers_at=COALESCE(response_headers_at, ?), "
                "first_chunk_at=COALESCE(first_chunk_at, ?), "
                "first_event_at=COALESCE(first_event_at, ?) WHERE id=?",
                (http_status, time.time() if http_status is not None else None,
                 first_chunk_at, first_event_at, request_id),
            )

    def finish_request(
        self, request_id: str, *, completed: bool, reason: str, response_bytes: int
    ) -> None:
        if reason not in {
            "http_eof", "upstream_timeout", "transport_error", "stream_not_exhausted",
            "relay_shutdown",
        }:
            raise BudgetRejectedError("invalid_completion_reason")
        with self._transaction() as db:
            db.execute(
                "UPDATE requests SET ended=?, status=?, reason=?, response_bytes=? "
                "WHERE id=? AND status='in_flight'",
                (time.time(), "completed" if completed else "interrupted", reason,
                 response_bytes, request_id),
            )
            # A concurrent relay shutdown owns the outcome; retain bytes observed
            # by the response iterator when it subsequently unwinds.
            db.execute(
                "UPDATE requests SET response_bytes=MAX(response_bytes, ?) "
                "WHERE id=? AND reason='relay_shutdown'",
                (response_bytes, request_id),
            )

    def interrupt_requests(self, request_ids: set[str]) -> None:
        for request_id in request_ids:
            self.finish_request(
                request_id, completed=False, reason="relay_shutdown", response_bytes=0
            )

    def snapshot(self) -> dict[str, Any]:
        with self._transaction() as db:
            phase = db.execute("SELECT value FROM state WHERE key='phase'").fetchone()
            rows = [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY started, id")]
        return {
            "mode": "functional", "schemaVersion": 1,
            "phase": json.loads(phase[0]) if phase else {},
            "pendingRequests": sum(row["status"] == "in_flight" for row in rows),
            "firstEventDefinition": "first complete upstream SSE data line or JSON object; "
            "not first visible-token TTFT",
            "requests": rows,
        }


class _FirstEventObservation:
    """Detect a transport event boundary without examining billing or storing content."""

    def __init__(self, *, event_stream: bool) -> None:
        self.event_stream = event_stream
        self.buffer = b""
        self.first_event_at: float | None = None
        self.exhausted = False

    def feed(self, chunk: bytes) -> None:
        if self.first_event_at is not None or self.exhausted:
            return
        self.buffer += chunk
        if len(self.buffer) > _MAX_BODY_BYTES:
            self.buffer = b""
            self.exhausted = True
            return
        if self.event_stream:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if line.startswith(b"data:") and line[5:].strip():
                    self.first_event_at = time.time()
                    self.buffer = b""
                    return
        else:
            try:
                value = json.loads(self.buffer)
            except (ValueError, UnicodeDecodeError):
                return
            if isinstance(value, dict):
                self.first_event_at = time.time()
                self.buffer = b""


class BillingObservation:
    """Inspect only native billing and usage fields while streaming bytes unchanged."""

    def __init__(self, *, event_stream: bool) -> None:
        self.event_stream = event_stream
        self.buffer = b""
        self.cost: Decimal | None = None
        self.pending: bool | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.invalid = False
        self.first_event_at: float | None = None

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        if len(self.buffer) > _MAX_BODY_BYTES:
            self.invalid = True
            self.buffer = b""
            return
        if self.event_stream:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if line.startswith(b"data:"):
                    self._observe(line[5:].strip())

    def finish(self) -> Decimal | None:
        if self.buffer:
            self._observe(self.buffer[5:].strip() if self.event_stream else self.buffer)
        self.buffer = b""
        return self.cost if self.pending is False and not self.invalid else None

    def _observe(self, data: bytes) -> None:
        if not data:
            return
        if data == b"[DONE]":
            if self.first_event_at is None:
                self.first_event_at = time.time()
            return
        try:
            value = json.loads(data, parse_float=Decimal)
        except (ValueError, UnicodeDecodeError):
            self.invalid = True
            return
        if not isinstance(value, dict):
            return
        if self.first_event_at is None:
            self.first_event_at = time.time()
        if "billing_pending" in value:
            pending = value["billing_pending"]
            if type(pending) is not bool:
                self.invalid = True
            else:
                self.pending = pending
        if "cost_cny" in value:
            try:
                self.cost = _amount(value["cost_cny"])
            except BudgetRejectedError:
                self.invalid = True
        usage = value.get("usage")
        if isinstance(usage, dict):
            for field, name in (
                ("prompt_tokens", "input_tokens"),
                ("completion_tokens", "output_tokens"),
            ):
                tokens = usage.get(field)
                if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
                    setattr(self, name, tokens)


@dataclass(frozen=True)
class ForwardedResponse:
    status_code: int
    headers: Mapping[str, str]
    chunks: Iterator[bytes]


class BudgetRelay:
    """TokenRhythm chat-completions transport only; never a general HTTP proxy."""

    def __init__(
        self,
        ledger: BudgetLedger | None,
        bounds: Mapping[str, ModelCostBound],
        *,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
        request_log: FunctionalRequestLog | None = None,
    ) -> None:
        from opensquilla.provider.registry import get_provider_spec

        if (ledger is None) == (request_log is None) or (request_log is not None and bounds):
            raise BudgetRejectedError("select_exactly_one_relay_mode")
        self.ledger = ledger
        self.request_log = request_log
        self.bounds = dict(bounds)
        self._api_key = api_key
        self.client_key = "live-budget-placeholder-" + secrets.token_hex(16)
        self.upstream = get_provider_spec("tokenrhythm").default_base_url.rstrip("/")
        if self.upstream != "https://tokenrhythm.studio/v1":
            raise BudgetRejectedError("registry_endpoint_changed")
        self._client = httpx.Client(
            transport=transport or httpx.HTTPTransport(retries=0),
            follow_redirects=False,
            trust_env=False,
            timeout=None,
        )
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._active_requests: set[str] = set()

    @contextlib.contextmanager
    def forward(
        self, body: bytes, headers: Mapping[str, str] | None = None
    ) -> Iterator[ForwardedResponse]:
        if len(body) > _MAX_BODY_BYTES:
            raise BudgetRejectedError("request_body_too_large")
        try:
            request = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise BudgetRejectedError("invalid_request_json") from exc
        if not isinstance(request, dict):
            raise BudgetRejectedError("invalid_request_json")
        model = _identifier(request.get("model"))
        if self.request_log is not None:
            with self._forward_functional(body, model=model, headers=headers) as forwarded:
                yield forwarded
            return
        ledger = self.ledger
        assert ledger is not None
        bound = self.bounds.get(model)
        if bound is None:
            raise BudgetRejectedError("unknown_model_price")
        if not self._api_key:
            raise BudgetRejectedError("credential_missing")
        with self._lock:
            if self._closed:
                raise BudgetRejectedError("relay_closed")
            request_id = ledger.reserve(bound, request)
            self._active_requests.add(request_id)
        observation: BillingObservation | None = None
        completed = False
        try:
            with self._client.stream(
                "POST",
                self.upstream + "/chat/completions",
                content=body,
                headers=self._upstream_headers(headers),
            ) as response:
                ledger.observe_response(request_id, http_status=response.status_code)
                observation = BillingObservation(
                    event_stream="text/event-stream" in response.headers.get("content-type", "")
                )

                def chunks() -> Iterator[bytes]:
                    nonlocal completed
                    first_event_recorded = False
                    for chunk in response.iter_bytes():
                        observation.feed(chunk)
                        if not first_event_recorded and observation.first_event_at is not None:
                            ledger.observe_response(
                                request_id, first_event_at=observation.first_event_at
                            )
                            first_event_recorded = True
                        yield chunk
                    completed = True

                yield ForwardedResponse(response.status_code, response.headers, chunks())
        finally:
            cost = observation.finish() if observation is not None and completed else None
            if observation is not None and observation.first_event_at is not None:
                ledger.observe_response(request_id, first_event_at=observation.first_event_at)
            ledger.settle(
                request_id,
                cost_cny=cost,
                reason="unknown_cost" if completed else "incomplete_http_response",
                input_tokens=observation.input_tokens if observation else None,
                output_tokens=observation.output_tokens if observation else None,
            )
            with self._lock:
                self._active_requests.discard(request_id)

    @contextlib.contextmanager
    def _forward_functional(
        self, body: bytes, *, model: str, headers: Mapping[str, str] | None
    ) -> Iterator[ForwardedResponse]:
        request_log = self.request_log
        assert request_log is not None
        if not self._api_key:
            raise BudgetRejectedError("credential_missing")
        with self._lock:
            if self._closed:
                raise BudgetRejectedError("relay_closed")
            request_id = request_log.start_request(model=model, request_bytes=len(body))
            self._active_requests.add(request_id)
        completed = False
        reason = "stream_not_exhausted"
        response_bytes = 0
        try:
            with self._client.stream(
                "POST", self.upstream + "/chat/completions", content=body,
                headers=self._upstream_headers(headers),
            ) as response:
                request_log.observe_response(request_id, http_status=response.status_code)
                observation = _FirstEventObservation(
                    event_stream="text/event-stream" in response.headers.get("content-type", "")
                )

                def chunks() -> Iterator[bytes]:
                    nonlocal completed, reason, response_bytes
                    event_recorded = False
                    for chunk in response.iter_bytes():
                        first_chunk = time.time() if response_bytes == 0 and chunk else None
                        response_bytes += len(chunk)
                        observation.feed(chunk)
                        if first_chunk is not None or (
                            not event_recorded and observation.first_event_at is not None
                        ):
                            request_log.observe_response(
                                request_id, first_chunk_at=first_chunk,
                                first_event_at=observation.first_event_at,
                            )
                            event_recorded = observation.first_event_at is not None
                        yield chunk
                    completed, reason = True, "http_eof"

                yield ForwardedResponse(response.status_code, response.headers, chunks())
        except httpx.TimeoutException:
            reason = "upstream_timeout"
            raise
        except (OSError, httpx.HTTPError):
            reason = "transport_error"
            raise
        finally:
            try:
                request_log.finish_request(
                    request_id, completed=completed, reason=reason, response_bytes=response_bytes
                )
            finally:
                with self._lock:
                    self._active_requests.discard(request_id)

    @contextlib.contextmanager
    def forward_models(
        self, headers: Mapping[str, str] | None = None
    ) -> Iterator[ForwardedResponse]:
        recorder = self.request_log if self.request_log is not None else self.ledger
        assert recorder is not None
        if not recorder.enabled:
            raise BudgetRejectedError("live_requests_disabled")
        if self._closed or not self._api_key:
            raise BudgetRejectedError("relay_unavailable")
        with self._client.stream(
            "GET",
            self.upstream + "/models",
            headers=self._upstream_headers(headers),
        ) as response:
            yield ForwardedResponse(response.status_code, response.headers, response.iter_bytes())

    def _upstream_headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        excluded = {
            "host", "authorization", "x-api-key", "proxy-authorization", "cookie",
            "connection", "content-length", "transfer-encoding", "keep-alive",
            "te", "trailer", "upgrade",
        }
        result = {
            key: value for key, value in (headers or {}).items() if key.lower() not in excluded
        }
        result["Authorization"] = "Bearer " + self._api_key
        result.setdefault("Content-Type", "application/json")
        return result

    def start(self) -> str:
        if self._server is not None:
            raise BudgetRejectedError("relay_already_started")
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, _format: str, *_args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
                if self.path != "/v1/models":
                    self.send_error(404, "unsupported_provider_endpoint")
                    return
                if not hmac.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + relay.client_key
                ):
                    self.send_error(403, "invalid_client_credential")
                    return
                try:
                    with relay.forward_models(dict(self.headers)) as response:
                        self._write_response(response)
                except BudgetRejectedError as exc:
                    self.send_error(402, str(exc))
                except (OSError, httpx.HTTPError):
                    self.close_connection = True

            def _write_response(self, response: ForwardedResponse) -> None:
                self.send_response(response.status_code)
                excluded = {
                    "connection",
                    "content-encoding",
                    "content-length",
                    "transfer-encoding",
                    "keep-alive",
                    "proxy-authenticate",
                    "proxy-authorization",
                    "te",
                    "trailer",
                    "upgrade",
                    "authorization",
                    "x-api-key",
                    "set-cookie",
                }
                for name, value in response.headers.items():
                    if name.lower() not in excluded:
                        self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in response.chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()

            def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
                if self.path != "/v1/chat/completions":
                    self.send_error(404, "unsupported_provider_endpoint")
                    return
                if not hmac.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + relay.client_key
                ):
                    self.send_error(403, "invalid_client_credential")
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= _MAX_BODY_BYTES or self.headers.get("Transfer-Encoding"):
                        raise BudgetRejectedError("invalid_request_size")
                    body = self.rfile.read(size)
                    if len(body) != size:
                        raise BudgetRejectedError("incomplete_request_body")
                    with relay.forward(body, dict(self.headers)) as response:
                        self._write_response(response)
                except BudgetRejectedError as exc:
                    self.send_error(402, str(exc))
                except (OSError, httpx.HTTPError):
                    self.close_connection = True

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = set(self._active_requests)
        recorder = self.request_log if self.request_log is not None else self.ledger
        assert recorder is not None
        recorder.interrupt_requests(pending)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._client.close()


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, delete=False, encoding="utf-8"
        ) as out:
            temporary = Path(out.name)
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-live", action="store_true")
    parser.add_argument("--mode", choices=("budget", "functional"), default="budget")
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--request-log", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--reviewed-model", action="append", default=[])
    parser.add_argument("--catalog-valid-until", type=float)
    parser.add_argument("--image-tokens-bounded-model", action="append", default=[])
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--receipt-file", type=Path)
    parser.add_argument("--bucket", choices=BUCKET_LIMITS_CNY, default="probe")
    parser.add_argument("--variant", choices=("baseline", "new"), default="new")
    parser.add_argument("--case-id", default="synthetic-task")
    args = parser.parse_args(argv)
    if not args.enable_live:
        summary: dict[str, Any] = {"enabled": False, "mode": args.mode}
        if args.mode == "budget":
            summary["limits_cny"] = BUCKET_LIMITS_CNY
        if args.ready_file:
            _write_private_json(args.ready_file, summary)
        print(json.dumps(summary))
        return 0
    if args.mode == "functional":
        if any((args.ledger, args.catalog, args.reviewed_model, args.catalog_valid_until,
                args.image_tokens_bounded_model)):
            parser.error("functional mode does not accept budget or price-catalog settings")
        if not all((args.request_log, args.ready_file, args.receipt_file)):
            parser.error("functional mode requires request-log, ready and receipt files")
    elif args.request_log or not all((args.ledger, args.catalog, args.reviewed_model,
                                     args.catalog_valid_until, args.ready_file, args.receipt_file)):
        parser.error("budget mode requires ledger, reviewed catalog/models/expiry, ready and "
                     "receipt files, without request-log")
    api_key = os.environ.get("TOKENRHYTHM_API_KEY", "")
    if not api_key:
        parser.error("TOKENRHYTHM_API_KEY must be injected into the isolated relay process")
    recorder: BudgetLedger | FunctionalRequestLog
    if args.mode == "functional":
        recorder = FunctionalRequestLog(args.request_log, enabled=True)
        recorder.select_phase(variant=args.variant, case_id=args.case_id)
        relay = BudgetRelay(None, {}, api_key=api_key, request_log=recorder)
    else:
        payload = json.loads(args.catalog.read_text(encoding="utf-8"))
        bounds = reviewed_catalog_bounds(
            payload,
            valid_until=args.catalog_valid_until,
            reviewed_models=set(args.reviewed_model),
            image_tokens_bounded_models=frozenset(args.image_tokens_bounded_model),
        )
        recorder = BudgetLedger(args.ledger, enabled=True)
        recorder.select_phase(bucket=args.bucket, variant=args.variant, case_id=args.case_id)
        relay = BudgetRelay(recorder, bounds, api_key=api_key)
    stopping = threading.Event()

    def stop(_signal: int, _frame: Any) -> None:
        stopping.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    try:
        base_url = relay.start()
        _write_private_json(
            args.ready_file,
            {
                "enabled": True,
                "mode": args.mode,
                "base_url": base_url,
                "client_key": relay.client_key,
                **({"request_log": str(recorder.path)} if args.mode == "functional"
                   else {"bucket": args.bucket}),
                "variant": args.variant,
                "case_id": args.case_id,
            },
        )
        while not stopping.wait(0.25):
            pass
    finally:
        relay.close()
        _write_private_json(args.receipt_file, recorder.snapshot())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
