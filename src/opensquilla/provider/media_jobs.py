"""Durable, secret-free media job identity beside the active usage database.

Accepted jobs retain their original accounting owner. Polling only revises that
event; neither a new turn nor a process restart creates a second paid request.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import secrets
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from opensquilla.engine.usage_accounting import UsageCallStart, current_usage_accounting_scope
from opensquilla.engine.usage_http import DirectUsageReservation, reserve_direct_usage_call
from opensquilla.provider.media_pricing import media_estimate, media_settlement
from opensquilla.provider.metered_http import response_measurement
from opensquilla.usage_metering import UsageMeter, UsageSettlementRevision

_volatile: dict[tuple[int, str, str], dict[str, Any]] = {}
_observation_locks: dict[tuple[int, str, str], asyncio.Lock] = {}


def _database() -> Path | None:
    scope = current_usage_accounting_scope()
    storage = getattr(getattr(scope, "sink", None), "_storage", None)
    path = getattr(storage, "_db_path", None)
    if not isinstance(path, (str, Path)) or str(path) == ":memory:":
        return None
    ledger = Path(path).resolve()
    return ledger.with_name(ledger.name + ".media-jobs.sqlite3")


def job_record(
    namespace: str,
    key: str,
    value: dict[str, Any] | None = None,
    *,
    create_only: bool = False,
) -> dict[str, Any] | None:
    """Small synchronous commit used inside the accepted-job callback.

    It must finish before control can yield or a caller receives the job handle.
    Tests/CLI without a durable sink use only isolated in-memory state.
    """
    path = _database()
    scope = current_usage_accounting_scope()
    if path is None:
        cache_key = (id(getattr(scope, "sink", None)), namespace, key)
        if value is not None:
            stored = json.loads(json.dumps(value))
            if create_only:
                # Concurrent first derivations must return the same winning
                # salt, just like the durable ON CONFLICT DO NOTHING path.
                return _volatile.setdefault(cache_key, stored)
            _volatile[cache_key] = stored
        return _volatile.get(cache_key)
    with sqlite3.connect(path, timeout=5) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS media_jobs (namespace TEXT, job_key TEXT, "
            "payload TEXT NOT NULL, PRIMARY KEY(namespace,job_key))"
        )
        if value is not None:
            conflict = "DO NOTHING" if create_only else "DO UPDATE SET payload=excluded.payload"
            conn.execute(
                "INSERT INTO media_jobs VALUES (?,?,?) ON CONFLICT(namespace,job_key) " + conflict,
                (namespace, key, json.dumps(value, separators=(",", ":"))),
            )
        row = conn.execute(
            "SELECT payload FROM media_jobs WHERE namespace=? AND job_key=?", (namespace, key)
        ).fetchone()
        return json.loads(row[0]) if row else None


def credential_fingerprint(api_key: str) -> str:
    record = job_record("settings", "credential_salt")
    if record is None:
        record = job_record(
            "settings", "credential_salt", {"salt": secrets.token_hex(32)}, create_only=True
        )
        assert record is not None
    return hashlib.pbkdf2_hmac(
        "sha256", api_key.encode(), bytes.fromhex(record["salt"]), 600_000
    ).hex()


def job_records(namespace: str) -> dict[str, dict[str, Any]]:
    path = _database()
    scope = current_usage_accounting_scope()
    if path is None:
        return {
            key: value
            for (sink, category, key), value in _volatile.items()
            if sink == id(getattr(scope, "sink", None)) and category == namespace
        }
    job_record(namespace, "__initialize__")
    with sqlite3.connect(path, timeout=5) as conn:
        return {
            row[0]: json.loads(row[1])
            for row in conn.execute(
                "SELECT job_key,payload FROM media_jobs WHERE namespace=?", (namespace,)
            )
        }


def _key(provider: str, base_url: str, job_id: str, api_key: str = "") -> str:
    return _key_from_fingerprint(provider, base_url, job_id, credential_fingerprint(api_key))


def _key_from_fingerprint(provider: str, base_url: str, job_id: str, fingerprint: str) -> str:
    return hashlib.sha256(
        json.dumps(
            [provider, base_url.rstrip("/"), fingerprint, job_id]
        ).encode()
    ).hexdigest()


def accounted_video_submission(provider: str):
    """Wrap only the paid submit boundary, after request/catalog validation."""

    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            if current_usage_accounting_scope() is None:
                return await function(*args, **kwargs)
            base_url = kwargs["api_root"]
            selected_provider = kwargs.get("provider", provider)
            # Derive before creating a paid-call reservation: cancellation here
            # must not leave a ledger start or dispatch a provider request.
            fingerprint = await asyncio.to_thread(
                credential_fingerprint, kwargs.get("api_key", ""),
            )
            reservation = await reserve_direct_usage_call(
                provider=selected_provider,
                model=kwargs["model"],
                base_url=base_url,
                operation_kind="video_generation",
            )
            if reservation.call is None:
                return await function(*args, **kwargs)
            callback = kwargs.get("on_job_accepted")
            accepted: dict[str, Any] | None = None
            accepted_key: str | None = None
            duration = kwargs.get("duration")
            dimensions = {"operation": "video_generation", "mode": "text_to_video"}
            if kwargs.get("resolution"):
                dimensions["resolution"] = str(kwargs["resolution"]).lower()
            meters = (
                UsageMeter(
                    "requested_video_seconds",
                    "second",
                    str(duration) if duration is not None else None,
                    known=duration is not None,
                    source="request",
                    dimensions=dimensions,
                ),
                UsageMeter(
                    "input_video_seconds", "second", "0", source="request", dimensions=dimensions
                ),
                UsageMeter("input_images", "image", "0", source="request", dimensions=dimensions),
                UsageMeter(
                    "input_audio_seconds", "second", "0", source="request", dimensions=dimensions
                ),
            )

            def remember(job_id: str):
                nonlocal accepted, accepted_key
                accepted = {
                    "call": asdict(reservation.call),
                    "provider": selected_provider,
                    "base_url": base_url,
                    "job_id": job_id,
                    "dimensions": dimensions,
                    "meters": [asdict(m) for m in meters],
                    "revision_id": "initial",
                    "finalized": False,
                }
                accepted_key = _key_from_fingerprint(
                    selected_provider, base_url, job_id, fingerprint,
                )
                job_record("usage", accepted_key, accepted)
                if callback is not None:
                    callback(job_id)

            kwargs["on_job_accepted"] = remember
            try:
                return await function(*args, **kwargs)
            finally:
                if accepted is None:
                    await reservation.mark_unknown("video_submission_unconfirmed")
                else:
                    await reservation.finalize_measurement(
                        meters=meters,
                        settlement=UsageSettlementRevision(
                            "initial", "unknown", "pending", reason_code="video_job_accepted"
                        ),
                    )
                    accepted["finalized"] = True
                    assert accepted_key is not None
                    job_record("usage", accepted_key, accepted)

        return wrapped

    return decorate


async def observe_video_job(
    provider: str,
    base_url: str,
    job_id: str,
    payload: dict[str, Any],
    *,
    outcome: str = "completed",
    api_key: str = "",
) -> None:
    """Commit the receipt before downloading; media delivery is not billing."""
    scope = current_usage_accounting_scope()
    if scope is None:
        return
    key = await asyncio.to_thread(_key, provider, base_url, job_id, api_key)
    lock_key = (id(asyncio.get_running_loop()), str(_database()), key)
    lock = _observation_locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        await _observe_video_job(scope, key, provider, base_url, payload, outcome=outcome)


async def video_job_needs_settlement(
    provider: str, base_url: str, job_id: str, *, api_key: str
) -> bool:
    """Only refresh retained jobs whose original ledger item has no final bill."""
    scope = current_usage_accounting_scope()
    storage = getattr(getattr(scope, "sink", None), "_storage", None)
    if storage is None or not hasattr(storage, "query_usage_settlements"):
        return False
    key = await asyncio.to_thread(_key, provider, base_url, job_id, api_key)
    record = job_record("usage", key)
    if record is None:
        return False
    rows = await storage.query_usage_settlements([record["call"]["event_id"]])
    return not rows or rows[0]["authority"] != "provider_confirmed"


async def _observe_video_job(scope, key, provider, base_url, payload, *, outcome):
    record = job_record("usage", key)
    if record is None:
        # A job from before accounting was enabled cannot be assigned to the
        # current caller safely. Never invent an additional provider call.
        return
    call = UsageCallStart(**record["call"])
    # A submitted finalization can still be in the sink's retry queue. The
    # durable sidecar records acceptance, not proof of a ledger transaction.
    storage = getattr(scope.sink, "_storage", None)
    current = None
    can_query = storage is not None and hasattr(storage, "query_usage_settlements")
    if can_query:
        rows = await storage.query_usage_settlements([call.event_id])
        current = rows[0] if rows else None
    if (can_query and current is None) or (not can_query and not record["finalized"]):
        initial = DirectUsageReservation(scope, call, base_url, operation_kind="video_generation")
        await initial.finalize_measurement(
            meters=tuple(UsageMeter(**m) for m in record["meters"]),
            settlement=UsageSettlementRevision(
                "initial", "unknown", "pending", reason_code="video_job_accepted"
            ),
        )
    dimensions = dict(record.get("dimensions", {}))
    if isinstance(payload.get("resolution"), str):
        dimensions["resolution"] = payload["resolution"].lower()
    try:
        meters, settlement = response_measurement(
            provider=provider,
            operation="video_generation",
            payload=payload,
            base_url=base_url,
            dimensions=dimensions,
        )
    except (TypeError, ValueError, OverflowError):
        meters, settlement = (), None
    # Provider observations take precedence over the request's known absent
    # input modalities. Keep the requested duration out of actual output meters.
    actual_metrics = {m.metric for m in meters}
    pricing_meters = meters + tuple(
        UsageMeter(**m) for m in record["meters"] if m["metric"] not in actual_metrics
    )
    snapshot = None
    if outcome == "completed" and (
        settlement is None or (settlement.authority == "unknown" and settlement.status == "pending")
    ):
        snapshot, estimate = media_estimate(
            provider=provider,
            model=call.model,
            operation="video_generation",
            base_url=base_url,
            meters=pricing_meters,
            dimensions=dimensions,
        )
        settlement = media_settlement(settlement, estimate)
    settlement = settlement or UsageSettlementRevision(
        "observation", "unknown", "unknown", reason_code=f"video_{outcome}_amount_unavailable"
    )
    # The ledger is authoritative after a crash between its commit and the
    # sidecar update. Missing catalog state or a stale poll cannot erase a
    # stronger valuation already committed for this same physical call.
    if can_query:
        rows = await storage.query_usage_settlements([call.event_id])
        current = rows[0] if rows else None
        if current:
            authority_order = {
                "unknown": 0,
                "local_estimate": 1,
                "provider_estimate": 2,
                "provider_confirmed": 3,
            }
            if authority_order[current["authority"]] > authority_order[settlement.authority]:
                return
            record["revision_id"] = current["revision_id"]
    identity = hashlib.sha256(
        json.dumps(
            {
                "settlement": {
                    **asdict(settlement),
                    "revision_id": "",
                    "previous_revision_id": None,
                },
                "meters": [asdict(m) for m in meters],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if record.get("observation") == identity or record["revision_id"] == identity:
        return
    revision = replace(settlement, revision_id=identity, previous_revision_id=record["revision_id"])
    revise = getattr(scope.sink, "revise", None)
    if revise is None:
        return
    await revise(
        call,
        revision,
        meters=meters,
        price_snapshot=snapshot if revision.price_snapshot_id else None,
    )
    record.update(finalized=True, revision_id=identity, observation=identity)
    job_record("usage", key, record)
