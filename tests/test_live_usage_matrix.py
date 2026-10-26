from decimal import Decimal

import pytest

from scripts.live_usage_matrix import Budget, build_matrix, safe_write, select_cases


def test_budget_reserves_unknown_requests_across_calls_and_releases_only_settlement():
    budget = Budget("50", "49")
    reserved = budget.reserve("0.8")
    assert reserved == Decimal("0.8")
    budget.settle(reserved, None)
    assert budget.reserve("0.3") is None
    budget.settle(reserved, "0.1")
    assert budget.used == Decimal("49.1")
    assert budget.reserve("0.9") == Decimal("0.9")
    assert budget.reserve("0.000001") is None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "invalid"])
def test_invalid_amount_cannot_bypass_budget(value):
    with pytest.raises(ValueError):
        Budget("50").reserve(value)


def test_tokenrhythm_media_matrix_uses_cny_units_and_two_second_video():
    rows = build_matrix(
        {
            "tokenrhythm_public": {
                "models": [
                    {"id": "image", "type": "image", "pricePerImage": "0.2"},
                    {
                        "id": "video",
                        "type": "video",
                        "videoPricing": {"outputPerSecondCny": {"720P": "0.6"}},
                    },
                ]
            }
        }
    )
    assert len(rows) == 2
    assert Decimal(rows[0]["reservationUsd"]) == Decimal("0.2") / Decimal("6.975") * 2
    assert rows[1]["options"] == {"duration": 2, "resolution": "720p"}
    assert Decimal(rows[1]["reservationUsd"]) == Decimal("0.6") * 2 / Decimal("6.975") * 2


def test_dynamic_router_price_is_unpriced_not_free():
    rows = build_matrix(
        {
            "openrouter": {
                "models": [{"id": "router", "pricing": {"prompt": "-1", "completion": "-1"}}]
            }
        }
    )
    assert rows[0]["reservationUsd"] is None


def _catalog_model(**changes):
    return {
        "id": "fixture/chat",
        "context_length": 32000,
        "supported_parameters": ["max_tokens", "tools"],
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        **changes,
    }


@pytest.mark.parametrize("rate", ["0.000003", "-1", None, "NaN"])
def test_independently_priced_reasoning_has_no_assumed_quantity_bound(rate):
    model = _catalog_model(
        pricing={
            "prompt": "0.000002",
            "completion": "0.000008",
            "internal_reasoning": rate,
            "web_search": "0.005",
        },
        architecture={
            "input_modalities": ["text", "audio", "image"],
            "output_modalities": ["text", "audio", "image"],
        },
    )
    rows = build_matrix({"openrouter": {"models": [model]}})
    assert len(rows) == 6
    assert all(r["reservationUsd"] is None for r in rows)
    assert {r["reservationReason"] for r in rows} == {"internal_reasoning_bound_unverified"}


@pytest.mark.parametrize(
    "model",
    [
        _catalog_model(supported_parameters=[]),
        _catalog_model(id="morph/morph-v3-fast"),
        _catalog_model(id="perplexity/sonar-pro"),
        _catalog_model(id="openai/gpt-4o-search-preview"),
        _catalog_model(id="fixture/chat:online"),
    ],
)
def test_unverified_output_caps_and_native_search_do_not_receive_finite_reservations(model):
    rows = build_matrix({"openrouter": {"models": [model]}})
    assert all(r["reservationUsd"] is None for r in rows)


def test_explicit_free_model_does_not_require_an_output_cap():
    model = _catalog_model(supported_parameters=[], pricing={"prompt": "0", "completion": "0"})
    [row] = build_matrix({"openrouter": {"models": [model]}})
    assert row["reservationUsd"] == "0"
    assert "reservationReason" not in row


def test_fixed_request_fee_is_added_once_and_invalid_fee_is_not_free():
    base = _catalog_model()
    without = build_matrix({"openrouter": {"models": [base]}})
    priced = _catalog_model(pricing={**base["pricing"], "request": "0.02"})
    with_fee = build_matrix({"openrouter": {"models": [priced]}})
    assert all(
        Decimal(b["reservationUsd"]) - Decimal(a["reservationUsd"]) == Decimal("0.02")
        for a, b in zip(without, with_fee, strict=True)
    )
    invalid = _catalog_model(pricing={**base["pricing"], "request": "-1"})
    assert all(
        r["reservationUsd"] is None for r in build_matrix({"openrouter": {"models": [invalid]}})
    )


def test_external_search_bounds_full_augmented_context_and_highest_price_tier():
    model = _catalog_model(
        pricing={
            "prompt": "0.000001",
            "completion": "0.000002",
            "web_search": "0.01",
            "request": "0.02",
            "overrides": [
                {"min_prompt_tokens": 16000, "prompt": "0.000003", "completion": "0.000004"}
            ],
        }
    )
    rows = build_matrix({"openrouter": {"models": [model]}})
    search = next(r for r in rows if r["kind"] == "web_search")
    assert Decimal(search["reservationUsd"]) == (
        Decimal("0.000003") * 32000 + Decimal("0.000004") * 512 + Decimal("0.027")
    )
    del model["context_length"]
    search = next(
        r for r in build_matrix({"openrouter": {"models": [model]}}) if r["kind"] == "web_search"
    )
    assert search["reservationUsd"] is None


def test_video_per_second_skus_do_not_prove_minimum_bill_or_output_duration():
    rows = build_matrix(
        {
            "openrouter_video": {
                "models": [
                    {
                        "id": "alibaba/wan-2.7",
                        "supported_durations": [2, 5],
                        "supported_resolutions": ["720p"],
                        "pricing_skus": {"duration_seconds": "0.1"},
                    }
                ]
            }
        }
    )
    assert rows[0]["options"] == {"duration": 2, "resolution": "720p"}
    assert rows[0]["reservationUsd"] is None
    assert rows[0]["reservationReason"] == "video_billed_quantity_bound_unverified"


def test_report_refuses_credentials_before_creating_file(tmp_path):
    path = tmp_path / "report.json"
    with pytest.raises(RuntimeError):
        safe_write(path, {"unexpected": "synthetic-secret"}, {"provider": "synthetic-secret"})
    assert not path.exists()


def test_retry_keeps_old_attempt_identity_and_does_not_repeat_successes():
    cases = [{"id": "a", "provider": "p", "model": "m", "kind": "text"}]
    failed = {**cases[0], "status": "failed", "failureKind": "rate-limit"}
    retry = select_cases(cases, [failed], {"retry_failure_kinds": ["rate-limit"]})
    assert retry[0]["id"] == "a-a2"
    assert retry[0]["caseId"] == "a"
    assert failed["id"] == "a"
    assert not select_cases(
        cases, [failed, {**retry[0], "status": "passed"}], {"retry_failure_kinds": ["rate-limit"]}
    )


def test_atomic_report_retries_windows_reader_lock(tmp_path, monkeypatch):
    from pathlib import Path

    original = Path.replace
    calls = []

    def replace(path, target):
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError("transient reader lock")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", replace)
    target = tmp_path / "report.json"
    safe_write(target, {"budgetUsedUsd": "3"}, {})
    assert len(calls) == 2
    assert '"3"' in target.read_text()


@pytest.mark.parametrize("cap", [0, -1, 8193, True, 1.5, "2048"])
def test_output_cap_requires_bounded_positive_integer(cap):
    with pytest.raises(ValueError, match="max_output_tokens"):
        select_cases([], [], {"max_output_tokens": cap})


@pytest.mark.parametrize("cap,expected", [(128, "0.1"), (2048, "0.4"), (8192, "1.6")])
@pytest.mark.parametrize("kind", ["tools", "audio_input", "web_search"])
def test_output_cap_scales_reservation_before_dispatch_without_reducing_bound(cap, expected, kind):
    case = {
        "id": "a",
        "provider": "p",
        "model": "m",
        "kind": kind,
        "reservationUsd": "0.1",
        "options": {},
    }
    [selected] = select_cases([case], [], {"max_output_tokens": cap})
    assert selected["options"]["max_output_tokens"] == cap
    assert Decimal(selected["reservationUsd"]) == Decimal(expected)
    assert case["options"] == {} and case["reservationUsd"] == "0.1"


def test_output_cap_only_affects_text_related_cases_and_keeps_unpriced_unknown():
    image = {
        "id": "image",
        "provider": "p",
        "model": "m",
        "kind": "image",
        "reservationUsd": "0.1",
        "options": {"max_output_tokens": 8192},
    }
    text = {"id": "text", "provider": "p", "model": "m", "kind": "text", "reservationUsd": None}
    selected = select_cases([image, text], [], {"max_output_tokens": 2048})
    assert selected[0] == image
    assert selected[1]["reservationUsd"] is None


@pytest.mark.parametrize("kind", ["image", "audio_output", "video"])
def test_command_output_cap_does_not_change_output_media_cases(kind):
    case = {
        "id": "a",
        "provider": "p",
        "model": "m",
        "kind": kind,
        "reservationUsd": "0.1",
        "options": {"max_output_tokens": 8192},
    }
    assert select_cases([case], [], {"max_output_tokens": 2048}) == [case]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["audio_input", "web_search"])
async def test_protocol_request_uses_reserved_output_cap(tmp_path, monkeypatch, kind):
    import json

    import httpx

    from scripts.live_usage_matrix import execute_case

    requests = []

    def handle(request):
        requests.append(request)
        assert json.loads(request.content)["max_tokens"] == 2048
        return httpx.Response(
            200,
            json={
                "model": "fixture",
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "cost": 0.1},
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    case = {
        "id": "fixture-" + kind,
        "provider": "openrouter",
        "model": "fixture",
        "kind": kind,
        "reservationUsd": "0.1",
        "options": {},
    }
    [selected] = select_cases([case], [], {"max_output_tokens": 2048})
    result = await execute_case(selected, "synthetic-key", tmp_path)
    assert Decimal(selected["reservationUsd"]) == Decimal("0.4")
    assert len(requests) == 1 and result["functional"] is True
