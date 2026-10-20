from scripts.live_usage_report import build_summary


def candidate(case_id, *, provider="p", kind="text", model="fixture", bound="1"):
    return {
        "id": case_id,
        "provider": provider,
        "model": model,
        "kind": kind,
        "reservationUsd": bound,
    }


def confirmed(case_id, amount, *, functional=True, **extra):
    return {
        "id": case_id,
        "status": "passed" if functional else "failed",
        "functional": functional,
        "replayIdempotent": True,
        "parentRollupConserved": True,
        "eventCount": 1,
        "totals": {
            "billedCostUsdExact": amount,
            "billedCostNanos": 999,
            "costSource": "provider_billed",
            "missingCostEntries": 0,
            "pendingBillingReceiptCount": 0,
        },
        **extra,
    }


def test_latest_coverage_preserves_old_charges_and_exact_native_amounts():
    first = confirmed("a", "0.0000000001", functional=False)
    second = confirmed("a-a2", "0.2", caseId="a", attempt=2)
    second["totals"]["nativeBilledByCurrency"] = {
        "CNY": {
            "amount": "1.4000000001",
            "amountNanos": "1400000000",
            "usdEquivalentExact": "0.2",
            "receiptCount": 1,
        }
    }
    result = build_summary(
        {"results": [first, second], "budgetUsedUsd": "0.2000000001"},
        [candidate("a"), candidate("b")],
    )
    assert result["coverage"]["candidates"] == 2
    assert result["coverage"]["completePass"] == 1
    assert result["coverage"]["statuses"] == {"not_attempted": 1, "passed": 1}
    assert result["cases"][0]["latestAttemptId"] == "a-a2"
    assert result["money"]["confirmedUsdEquivalent"] == "0.2000000001"
    assert result["money"]["nativeConfirmedByCurrency"]["CNY"]["amount"] == "1.4000000001"
    assert result["money"]["reportBudgetDifferenceUsd"] == "0.0000000000"


def test_all_candidates_retained_and_unexecuted_lists_do_not_reserve_money():
    matrix = [candidate(str(index)) for index in range(1233)]
    matrix[0]["reservationUsd"] = None
    matrix[1]["model"] = "fixture:batch"
    result = build_summary(
        {"results": [{"id": "2", "status": "skipped_budget", "reservationUsd": "5"}]}, matrix
    )
    assert len(result["cases"]) == 1233
    assert result["coverage"]["statuses"] == {"not_attempted": 1232, "skipped_budget": 1}
    assert result["incompleteCaseIds"]["unpriced"] == ["0"]
    assert result["incompleteCaseIds"]["unsupportedTransport"] == ["1"]
    assert result["incompleteCaseIds"]["budget"] == ["2"]
    assert result["money"]["retainedUnknownReservationUsd"] == "0"


def test_functional_and_accounting_pass_are_independent_and_reserve_not_spend():
    paid_failure = confirmed("a", "0.25", functional=False)
    unknown = {
        "id": "b",
        "status": "functional_pass_cost_unavailable",
        "functional": True,
        "eventCount": 1,
        "replayIdempotent": True,
        "parentRollupConserved": True,
        "reservationUsd": "2.1",
        "totals": {"missingCostEntries": 1, "costSource": "unavailable"},
    }
    result = build_summary(
        {"results": [paid_failure, unknown], "budgetUsedUsd": "2.35"},
        [candidate("a"), candidate("b")],
    )
    assert result["coverage"]["functionalPass"] == result["coverage"]["accountingPass"] == 1
    assert result["coverage"]["completePass"] == 0
    assert result["money"]["confirmedUsdEquivalent"] == "0.25"
    assert result["money"]["retainedUnknownReservationUsd"] == "2.1"


def test_duplicate_snapshot_not_double_charged_and_403_not_called_entitlement():
    row = confirmed(
        "a",
        "0.1",
        functional=False,
        httpStatus=403,
        failureKind="not-entitled",
        diagnostic="private secret",
        errorMetadata={"code": 403, "message": "private response"},
    )
    result = build_summary({"results": [row, row]}, [candidate("a")])
    assert result["attempts"]["duplicateSnapshotsDiscarded"] == 1
    assert result["money"]["confirmedUsdEquivalent"] == "0.1"
    assert result["cases"][0]["errorCodes"] == ["http_403", "code:403"]
    assert "private" not in str(result) and "not-entitled" not in str(result)
    assert result["attempts"]["fieldPresenceCounts"]["diagnostic"] == 1


def test_legacy_nanos_fallback_and_orphan_charge_remain_visible():
    orphan = {
        "id": "orphan",
        "status": "failed",
        "reservationUsd": "2",
        "totals": {
            "billedCostNanos": 123456789,
            "billedCostUsd": 99,
            "costSource": "mixed",
            "missingCostEntries": 1,
        },
    }
    result = build_summary({"results": [orphan]}, [candidate("a")])
    assert result["money"]["confirmedUsdEquivalent"] == "0.123456789"
    assert result["attempts"]["orphanAttemptIds"] == ["orphan"]
    assert result["money"]["retainedUnknownReservationUsd"] == "2"
    assert result["money"]["reservedBeyondConfirmedUsd"] == "1.876543211"
    assert result["money"]["confirmedAmountSources"] == {"billedCostNanos": 1}


def test_reported_pass_is_not_accounting_evidence():
    row = {"id": "a", "status": "passed", "functional": True, "totals": {"missingCostEntries": 1}}
    result = build_summary({"results": [row]}, [candidate("a", provider="tr", kind="video")])
    assert result["reportedPassWithoutCompleteEvidenceCaseIds"] == ["a"]
    assert result["byProviderKind"]["tr/video"]["functionalPass"] == 1
    assert result["byProviderKind"]["tr/video"]["accountingPass"] == 0


def test_reconciled_old_snapshot_does_not_replace_latest_attempt_coverage():
    first = confirmed("a", "0.1", functional=False)
    retry = confirmed("a-a2", "0.2", caseId="a", attempt=2)
    first_reconciled = confirmed("a", "0.15", functional=False)
    result = build_summary({"results": [first, retry, first_reconciled]}, [candidate("a")])
    assert result["cases"][0]["latestAttemptId"] == "a-a2"
    assert result["money"]["confirmedUsdEquivalent"] == "0.35"


def test_catalog_exclusions_keep_public_authenticated_and_video_feed_evidence():
    catalogs = {
        "tokenrhythm_public": {
            "models": [
                {"id": "NeoHorse-Jev-4B", "type": "decision", "openai": False},
                {"id": "covered", "type": "image"},
            ]
        },
        "tokenrhythm": {"models": [{"id": "auth-only", "type": "embedding"}]},
        "openrouter_video": {"models": [{"id": "covered", "type": "video"}]},
    }
    result = build_summary(
        {"results": []}, [candidate("a", provider="tokenrhythm", model="covered")], catalogs
    )
    assert result["catalogsProvided"]
    exclusions = result["catalogExclusions"]
    assert {(r["catalog"], r["model"]) for r in exclusions} == {
        ("tokenrhythm_public", "NeoHorse-Jev-4B"),
        ("tokenrhythm", "auth-only"),
        ("openrouter_video", "covered"),
    }
    assert all(r["reason"] == "not_covered_by_runner_protocol" for r in exclusions)
    assert next(r for r in exclusions if r["model"] == "NeoHorse-Jev-4B")["type"] == "decision"
    assert "entitled" not in str(exclusions)


def test_tr_media_attempt_evidence_distinguishes_delivery_job_and_exact_bill():
    first = confirmed("video", "0.17", provider="tokenrhythm", kind="video", model="wan")
    retry = confirmed(
        "video-a2",
        "0.172043010752688172",
        functional=False,
        caseId="video",
        attempt=2,
        provider="tokenrhythm",
        kind="video",
        model="wan",
        videoRecovery={"jobOutcome": "completed"},
        restartReplayIdempotent=True,
    )
    retry["totals"]["nativeBilledByCurrency"] = {
        "CNY": {
            "amount": "1.2000000001",
            "usdEquivalentExact": "0.172043010752688172",
            "receiptCount": 1,
        }
    }
    retry["totals"]["nativeBillingExpectedReceiptCount"] = 1
    retry["totals"]["nativeBillingMissingConfirmedReceiptCount"] = 0
    result = build_summary(
        {"results": [first, retry, retry]},
        [candidate("video", provider="tokenrhythm", kind="video", model="wan")],
    )
    original, repeated = result["tokenrhythmMediaEvidence"]
    assert original["functional"] is True and original["jobOutcome"] is None
    assert repeated["functional"] is False and repeated["jobOutcome"] == "completed"
    assert repeated["confirmedNativeByCurrency"]["CNY"]["amount"] == "1.2000000001"
    assert repeated["confirmedNativeByCurrency"]["CNY"]["receiptCount"] == 1
    assert (
        repeated["oneEvent"]
        and repeated["parentRollupConserved"]
        and repeated["restartReplayIdempotent"]
    )
    assert repeated["expectedReceiptCount"] == 1 and repeated["missingConfirmedReceiptCount"] == 0


def test_optional_catalogs_and_missing_media_evidence_remain_unknown():
    result = build_summary(
        {
            "results": [
                {"id": "image", "provider": "tokenrhythm", "kind": "image", "status": "interrupted"}
            ]
        },
        [candidate("image", provider="tokenrhythm", kind="image")],
    )
    assert result["catalogsProvided"] is False and result["catalogExclusions"] == []
    [media] = result["tokenrhythmMediaEvidence"]
    assert media["functional"] is None and media["oneEvent"] is None
    assert media["confirmedUsdEquivalent"] is None and media["confirmedNativeByCurrency"] == {}


def test_reservation_excess_uses_exact_unique_attempt_charges_including_superseded_failures():
    video = confirmed("video", "0.50", functional=False, reservationUsd="0.40")
    later_video = confirmed("video-a2", "0.20", caseId="video", attempt=2, reservationUsd="0.40")
    text = confirmed("text-a2", "0.0197352", caseId="text", attempt=2, reservationUsd="0.0180224")
    summary = build_summary(
        {"results": [video, later_video, text, video], "budgetLimitUsd": "200"},
        [
            candidate("video", provider="openrouter", model="alibaba/wan-2.7", kind="video"),
            candidate("text", provider="openrouter", model="morph/morph-v3-fast"),
        ],
    )
    assert summary["money"]["reservationExceededAttemptCount"] == 2
    assert {row["id"]: row for row in summary["money"]["reservationExceededAttempts"]} == {
        "video": {
            "id": "video",
            "provider": "openrouter",
            "model": "alibaba/wan-2.7",
            "kind": "video",
            "reservationUsd": "0.40",
            "confirmedUsdEquivalent": "0.50",
            "excessUsd": "0.10",
        },
        "text-a2": {
            "id": "text-a2",
            "provider": "openrouter",
            "model": "morph/morph-v3-fast",
            "kind": "text",
            "reservationUsd": "0.0180224",
            "confirmedUsdEquivalent": "0.0197352",
            "excessUsd": "0.0017128",
        },
    }
    assert summary["money"]["confirmedUsdEquivalent"] == "0.7197352"
    assert summary["cases"][0]["latestAttemptId"] == "video-a2"
    assert "not a provider-enforced spend cap" in summary["semantics"]["budgetReservation"]


def test_reservation_excess_does_not_count_estimates_or_infer_missing_reservations():
    unknown = {
        "id": "unknown",
        "status": "functional_pass_cost_unavailable",
        "reservationUsd": "0.1",
        "totals": {"costUsdExact": "3", "estimatedCostUsdExact": "3", "costSource": "unavailable"},
    }
    mixed = confirmed("mixed", "0.1", reservationUsd="0.2")
    mixed["totals"].update(costSource="mixed", estimatedCostUsdExact="3", costUsdExact="3.1")
    rows = [
        unknown,
        mixed,
        confirmed("equal", "0.2", reservationUsd="0.2"),
        confirmed("missing", "3"),
        confirmed("invalid", "3", reservationUsd="NaN"),
        confirmed("skipped", "3", reservationUsd="0.1", status="skipped_budget"),
    ]
    summary = build_summary({"results": rows}, [candidate(row["id"], bound="0.01") for row in rows])
    assert summary["money"]["reservationExceededAttemptCount"] == 0
    assert summary["money"]["reservationExceededAttempts"] == []


def test_reservation_excess_retains_subnano_precision_and_confirmed_partial_cost():
    partial = confirmed(
        "partial", "0.10000000001", reservationUsd="0.1", provider="p", model="m", kind="image"
    )
    partial["totals"].update(costSource="mixed", estimatedCostUsdExact="9", missingCostEntries=1)
    summary = build_summary({"results": [partial]}, [candidate("partial")])
    [excess] = summary["money"]["reservationExceededAttempts"]
    assert excess["confirmedUsdEquivalent"] == "0.10000000001"
    assert excess["excessUsd"] == "0.00000000001"
    assert summary["coverage"]["accountingPass"] == 0
