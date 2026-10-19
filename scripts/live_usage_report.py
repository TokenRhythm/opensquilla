"""Summarize local live-acceptance evidence without network or ledger writes.

Latest attempts determine candidate coverage. Every distinct attempt contributes
its confirmed charges, including failed and superseded attempts. Reservations
and estimates are reported separately from confirmed USD equivalents.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

_ZERO = Decimal(0)
_NANO = Decimal(1_000_000_000)
_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_SKIPPED = {"not_attempted", "unsupported_in_runner"}


def _amount(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite() or result < 0 or result.adjusted() > 24:
        return None
    return result


def _money(source: dict[str, Any], *fields: str) -> tuple[Decimal | None, str | None]:
    for field in fields:
        value = _amount(source.get(field))
        if value is not None:
            return (value / _NANO if field.endswith("Nanos") else value), field
    return None, None


def _text(value: Decimal) -> str:
    return format(value, "f")


def _not_executed(status: str) -> bool:
    return status in _SKIPPED or status.startswith("skipped_")


def _error_codes(row: dict[str, Any]) -> list[str]:
    codes = []
    status = row.get("httpStatus")
    if isinstance(status, int) and 400 <= status <= 599:
        codes.append(f"http_{status}")
    metadata = row.get("errorMetadata")
    if isinstance(metadata, dict):
        for key in ("code", "error_type", "provider_code", "limit_source"):
            value = str(metadata.get(key) or "")
            if _CODE.fullmatch(value):
                codes.append(f"{key}:{value}")
    failure = str(row.get("failureKind") or "")
    if failure in {"not-entitled", "not_entitled"}:
        # Older runners inferred this from every 403. Do not repeat that claim.
        failure = "provider_rejection_unclassified"
    if failure and _CODE.fullmatch(failure) and not codes:
        codes.append(failure)
    error_type = str(row.get("errorType") or "")
    if not codes and _CODE.fullmatch(error_type):
        codes.append(error_type)
    return codes


def _attempt_money(row: dict[str, Any]) -> dict[str, Any]:
    totals = row.get("totals") or {}
    confirmed, source = _money(totals, "billedCostUsdExact", "billedCostNanos", "billedCostUsd")
    if confirmed is None and totals.get("costSource") == "provider_billed":
        confirmed, source = _money(totals, "costUsdExact", "costNanos", "costUsd")
    estimated, _ = _money(totals, "estimatedCostUsdExact", "estimatedCostNanos", "estimatedCostUsd")
    complete = (
        confirmed is not None
        and totals.get("costSource") == "provider_billed"
        and totals.get("missingCostEntries") == 0
        and totals.get("pendingBillingReceiptCount", 0) == 0
        and totals.get("nativeBillingMissingConfirmedReceiptCount", 0) == 0
        and (row.get("eventCount", totals.get("eventCount", 0)) or 0) > 0
    )
    not_executed = _not_executed(str(row.get("status", "failed")))
    accounted = _amount(row.get("budgetAccountedUsd"))
    if accounted is None and not not_executed:
        accounted = confirmed if complete else _amount(row.get("reservationUsd"))
    reserve = _ZERO if complete or not_executed else accounted
    return {
        "confirmed": confirmed,
        "source": source,
        "estimated": estimated,
        "complete": complete,
        "accounted": accounted,
        "reserve": reserve,
    }


def _coverage(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "candidates": len(cases),
        "statuses": dict(sorted(Counter(row["status"] for row in cases).items())),
        **{
            key: sum(row[key] for row in cases)
            for key in (
                "functionalPass",
                "accountingIntegrityPass",
                "accountingPass",
                "completePass",
            )
        },
    }


def _attempt_rank(case_id: str, row: dict[str, Any]) -> tuple[int, int]:
    ordinal = row.get("attempt")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        suffix = re.fullmatch(re.escape(case_id) + r"-a(\d+)", str(row["id"]))
        ordinal = int(suffix[1]) if suffix else 1
    started = row.get("startedAtMs", 0)
    return ordinal, started if isinstance(started, int) else 0


def catalog_exclusions(
    matrix: list[dict[str, Any]], catalogs: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Compare provider-scoped identities, without inferring account access."""
    represented = {(row["provider"], row["model"]) for row in matrix}
    providers = {
        "tokenrhythm_public": "tokenrhythm",
        "tokenrhythm": "tokenrhythm",
        "openrouter": "openrouter",
        "openrouter_video": "openrouter",
    }
    missing = {}
    for catalog, feed in (catalogs or {}).items():
        if not isinstance(feed, dict):
            continue
        provider = providers.get(catalog, feed.get("provider", catalog))
        for model in feed.get("models", []):
            if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                continue
            if (provider, model["id"]) in represented:
                continue
            model_type = model.get("type")
            missing[(catalog, model["id"])] = {
                "catalog": catalog,
                "provider": provider,
                "model": model["id"],
                "type": model_type if isinstance(model_type, str) else None,
                "reason": "not_covered_by_runner_protocol",
            }
    return [missing[key] for key in sorted(missing)]


def _media_evidence(row: dict[str, Any], money: dict[str, Any]) -> dict[str, Any]:
    totals = row.get("totals") or {}
    native = {}
    for currency, entry in (totals.get("nativeBilledByCurrency") or {}).items():
        if not re.fullmatch(r"[A-Z]{3}", currency) or not isinstance(entry, dict):
            continue
        amount, source = _money(entry, "amountExact", "amount", "amountNanos")
        usd, _ = _money(entry, "usdEquivalentExact", "usdEquivalentNanos", "usdEquivalent")
        native[currency] = {
            "amount": None if amount is None else _text(amount),
            "amountSource": source,
            "usdEquivalent": None if usd is None else _text(usd),
            "receiptCount": entry.get("receiptCount"),
        }
    event_count = row.get("eventCount", totals.get("eventCount"))
    recovery = row.get("videoRecovery") or {}
    outcome = recovery.get("jobOutcome", row.get("jobOutcome"))
    result = {
        "attemptId": row["id"],
        "caseId": row.get("caseId") or row["id"],
        "model": row.get("model"),
        "kind": row.get("kind"),
        "status": row.get("status"),
        "attempted": not _not_executed(str(row.get("status", "failed"))),
        "jobOutcome": outcome if outcome in {"completed", "failed", "pending"} else None,
        "confirmedNativeByCurrency": native,
        "confirmedUsdEquivalent": None if money["confirmed"] is None else _text(money["confirmed"]),
        "confirmedAmountSource": money["source"],
        "receiptComplete": money["complete"],
        "expectedReceiptCount": totals.get("nativeBillingExpectedReceiptCount"),
        "pendingReceiptCount": totals.get("pendingBillingReceiptCount"),
        "missingConfirmedReceiptCount": totals.get("nativeBillingMissingConfirmedReceiptCount"),
        "eventCount": event_count,
        "oneEvent": event_count == 1 if isinstance(event_count, int) else None,
    }
    for field in (
        "functional",
        "parentRollupConserved",
        "replayIdempotent",
        "restartReplayIdempotent",
    ):
        value = row.get(field)
        result[field] = value if isinstance(value, bool) else None
    return result


def build_summary(
    report: dict[str, Any], matrix: list[dict[str, Any]], catalogs: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return a content-free, decimal-safe summary of an immutable snapshot."""
    with localcontext() as context:
        context.prec = 80
        return _build_summary(report, matrix, catalogs)


def _build_summary(
    report: dict[str, Any], matrix: list[dict[str, Any]], catalogs: dict[str, Any] | None
) -> dict[str, Any]:
    candidates = {str(row["id"]): row for row in matrix}
    if len(candidates) != len(matrix):
        raise ValueError("matrix candidate IDs must be unique")
    rows = report.get("results", [])
    # Reconciliation can replace a row snapshot. An attempt ID is one dispatch,
    # so a duplicated snapshot must never double its charge.
    attempts = {str(row["id"]): (index, row) for index, row in enumerate(rows)}
    ordered = [row for _, row in sorted(attempts.values(), key=lambda item: item[0])]
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    money = {}
    confirmed = estimated = retained = accounted = reserved_beyond_confirmed = _ZERO
    amount_sources: Counter[str] = Counter()
    error_codes: Counter[str] = Counter()
    fields: Counter[str] = Counter()
    native: dict[str, dict[str, Any]] = {}
    unknown_attempts = []
    reservation_exceeded_attempts = []
    for row in ordered:
        attempt_id = str(row["id"])
        by_case[str(row.get("caseId") or attempt_id)].append(row)
        amount = money[attempt_id] = _attempt_money(row)
        reservation = _amount(row.get("reservationUsd"))
        if (
            not _not_executed(str(row.get("status", "failed")))
            and reservation is not None
            and amount["confirmed"] is not None
            and amount["confirmed"] > reservation
        ):
            candidate = candidates.get(str(row.get("caseId") or attempt_id), {})
            reservation_exceeded_attempts.append(
                {
                    "id": attempt_id,
                    **{
                        field: row.get(field, candidate.get(field))
                        for field in ("provider", "model", "kind")
                    },
                    "reservationUsd": _text(reservation),
                    "confirmedUsdEquivalent": _text(amount["confirmed"]),
                    "excessUsd": _text(amount["confirmed"] - reservation),
                }
            )
        confirmed += amount["confirmed"] or _ZERO
        estimated += amount["estimated"] or _ZERO
        accounted += amount["accounted"] or _ZERO
        amount_sources[amount["source"] or "unavailable"] += 1
        error_codes.update(_error_codes(row))
        fields.update(
            name
            for name in (
                "diagnostic",
                "httpStatus",
                "errorType",
                "errorMetadata",
                "totals",
                "eventCount",
                "replayIdempotent",
                "parentRollupConserved",
                "budgetAccountedUsd",
            )
            if name in row
        )
        if not amount["complete"] and not _not_executed(str(row.get("status", "failed"))):
            reserve = amount["reserve"]
            retained += reserve or _ZERO
            reserved_beyond_confirmed += max(
                _ZERO, (reserve or _ZERO) - (amount["confirmed"] or _ZERO)
            )
            unknown_attempts.append(
                {
                    "attemptId": attempt_id,
                    "reservedUsd": None if reserve is None else _text(reserve),
                }
            )
        for currency, entry in (row.get("totals", {}).get("nativeBilledByCurrency") or {}).items():
            if not re.fullmatch(r"[A-Z]{3}", currency) or not isinstance(entry, dict):
                continue
            value, _ = _money(entry, "amountExact", "amount", "amountNanos")
            usd, _ = _money(entry, "usdEquivalentExact", "usdEquivalentNanos", "usdEquivalent")
            target = native.setdefault(
                currency,
                {
                    "amount": _ZERO,
                    "usdEquivalent": _ZERO,
                    "receiptCount": 0,
                    "missingAmountEntries": 0,
                    "missingUsdEquivalentEntries": 0,
                },
            )
            target["amount"] += value or _ZERO
            target["usdEquivalent"] += usd or _ZERO
            target["receiptCount"] += int(entry.get("receiptCount", 0))
            target["missingAmountEntries"] += value is None
            target["missingUsdEquivalentEntries"] += usd is None

    cases = []
    lists: dict[str, list[str]] = {
        key: []
        for key in (
            "notAttempted",
            "unpriced",
            "budget",
            "unsupportedTransport",
            "failedTransport",
            "interrupted",
        )
    }
    for case_id, candidate in candidates.items():
        history = by_case.get(case_id, [])
        history.sort(key=lambda row: _attempt_rank(case_id, row))
        latest = history[-1] if history else {}
        status = str(latest.get("status", "not_attempted"))
        totals = latest.get("totals") or {}
        integrity = bool(
            latest.get("replayIdempotent") is True
            and latest.get("parentRollupConserved") is True
            and latest.get("eventCount", totals.get("eventCount")) == 1
        )
        latest_amount = money.get(str(latest.get("id")), {})
        functional = latest.get("functional") is True
        accounting = integrity and latest_amount.get("complete", False)
        row = {
            "caseId": case_id,
            "provider": candidate["provider"],
            "model": candidate["model"],
            "kind": candidate["kind"],
            "status": status,
            "attempts": len(history),
            "latestAttemptId": latest.get("id"),
            "functionalPass": functional,
            "accountingIntegrityPass": integrity,
            "accountingPass": accounting,
            "completePass": functional and accounting,
            "confirmedUsdAllAttempts": _text(
                sum((money[str(item["id"])]["confirmed"] or _ZERO for item in history), _ZERO)
            ),
        }
        codes = _error_codes(latest)
        if codes:
            row["errorCodes"] = codes
        cases.append(row)
        if not history:
            lists["notAttempted"].append(case_id)
        if status == "skipped_budget":
            lists["budget"].append(case_id)
        if _not_executed(status) and candidate.get("reservationUsd") is None:
            lists["unpriced"].append(case_id)
        if status in {"skipped_transport_unsupported", "unsupported_in_runner"} or (
            str(candidate["model"]).endswith(":batch") and not functional
        ):
            lists["unsupportedTransport"].append(case_id)
        if latest.get("failureKind") == "transport":
            lists["failedTransport"].append(case_id)
        if status in {"in_flight", "interrupted"}:
            lists["interrupted"].append(case_id)

    def grouped(*fields: str) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in cases:
            groups["/".join(str(row[field]) for field in fields)].append(row)
        return {key: _coverage(value) for key, value in sorted(groups.items())}

    budget_used = _amount(report.get("budgetUsedUsd"))
    return {
        "schemaVersion": 1,
        "semantics": {
            "coverage": "latest attempt per matrix candidate; absent candidates are not_attempted",
            "money": (
                "all distinct attempt IDs, including failed and superseded attempts; "
                "no FX conversion inferred"
            ),
            "accountingPass": (
                "one event, replay and parent conservation, complete confirmed cost receipt"
            ),
            "unknownReserve": (
                "retained full reservations; may contain already-confirmed partial charges, "
                "not extra spend"
            ),
            "budgetReservation": (
                "local reservations are based on catalog estimates, not a provider-enforced "
                "spend cap; confirmed charges can exceed an individual reservation"
            ),
            "precision": "exact decimal fields preferred; source counts disclose legacy fallbacks",
        },
        "coverage": _coverage(cases),
        "catalogsProvided": catalogs is not None,
        "catalogExclusions": catalog_exclusions(matrix, catalogs),
        "tokenrhythmMediaEvidence": [
            _media_evidence(row, money[str(row["id"])])
            for row in ordered
            if row.get("provider") == "tokenrhythm" and row.get("kind") in {"image", "video"}
        ],
        "byProvider": grouped("provider"),
        "byKind": grouped("kind"),
        "byProviderKind": grouped("provider", "kind"),
        "reportedPassWithoutCompleteEvidenceCaseIds": [
            row["caseId"] for row in cases if row["status"] == "passed" and not row["completePass"]
        ],
        "attempts": {
            "reportRows": len(rows),
            "distinctAttempts": len(ordered),
            "duplicateSnapshotsDiscarded": len(rows) - len(ordered),
            "statuses": dict(
                sorted(Counter(str(row.get("status", "failed")) for row in ordered).items())
            ),
            "errorCodes": dict(sorted(error_codes.items())),
            "fieldPresenceCounts": dict(sorted(fields.items())),
            "orphanAttemptIds": [
                str(row["id"])
                for row in ordered
                if str(row.get("caseId") or row["id"]) not in candidates
            ],
        },
        "money": {
            "budgetLimitUsd": report.get("budgetLimitUsd"),
            "confirmedUsdEquivalent": _text(confirmed),
            "reservationExceededAttemptCount": len(reservation_exceeded_attempts),
            "reservationExceededAttempts": reservation_exceeded_attempts,
            "estimatedUsd": _text(estimated),
            "nativeConfirmedByCurrency": {
                currency: {
                    key: _text(value) if isinstance(value, Decimal) else value
                    for key, value in entry.items()
                }
                for currency, entry in sorted(native.items())
            },
            "confirmedAmountSources": dict(sorted(amount_sources.items())),
            "retainedUnknownReservationUsd": _text(retained),
            "reservedBeyondConfirmedUsd": _text(reserved_beyond_confirmed),
            "unknownReservationAttempts": unknown_attempts,
            "accountedAttemptBudgetUsd": _text(accounted),
            "reportBudgetReservedOrSpentUsd": None if budget_used is None else _text(budget_used),
            "reportBudgetDifferenceUsd": None
            if budget_used is None
            else _text(budget_used - accounted),
        },
        "incompleteCaseIds": lists,
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(".tmp/usage-accounting-live")
    parser.add_argument("--report", type=Path, default=base / "report.json")
    parser.add_argument("--matrix", type=Path, default=base / "matrix.json")
    parser.add_argument("--catalogs", type=Path, help="optional; defaults beside the report")
    parser.add_argument("--output", type=Path, default=base / "summary.json")
    args = parser.parse_args()
    catalogs_path = args.catalogs or args.report.with_name("catalogs.json")
    if args.output.resolve() in {
        args.report.resolve(),
        args.matrix.resolve(),
        catalogs_path.resolve(),
    }:
        parser.error("output must not overwrite the report, matrix or catalogs")
    # Close each input immediately; the live worker replaces report.json.
    report = json.loads(args.report.read_text(encoding="utf-8"), parse_float=Decimal)
    matrix = json.loads(args.matrix.read_text(encoding="utf-8"), parse_float=Decimal)
    catalogs = (
        json.loads(catalogs_path.read_text(encoding="utf-8"), parse_float=Decimal)
        if catalogs_path.exists()
        else None
    )
    summary = build_summary(report, matrix, catalogs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "coverage": summary["coverage"],
                "confirmedUsdEquivalent": summary["money"]["confirmedUsdEquivalent"],
                "retainedUnknownReservationUsd": summary["money"]["retainedUnknownReservationUsd"],
            }
        )
    )


if __name__ == "__main__":
    main()
