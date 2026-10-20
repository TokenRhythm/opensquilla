from __future__ import annotations

import pytest

from opensquilla.provider.metered_http import response_measurement


def test_iqs_receipt_keeps_billing_components_separate_without_inventing_dollars():
    meters, settlement = response_measurement(
        provider="iqs",
        operation="web_search",
        payload={
            "requestId": "synthetic-request",
            "pageItems": [{"mainText": "private search result"}],
            "costCredits": {
                "search": {
                    "genericTextSearch": 0,
                    "liteTextSearch": 1,
                    "liteAdvancedTextSearch": "2.5",
                    "autoTextSearch": 3,
                    "futureSku": 99,
                },
                "valueAdded": {"summary": 2, "advanced": 0, "privateField": "secret"},
                "total": 100,
            },
        },
    )
    assert settlement is None
    assert {meter.dimensions["billing_component"]: meter.quantity for meter in meters} == {
        "search.genericTextSearch": "0",
        "search.liteTextSearch": "1",
        "search.liteAdvancedTextSearch": "2.5",
        "search.autoTextSearch": "3",
        "valueAdded.summary": "2",
        "valueAdded.advanced": "0",
    }
    for meter in meters:
        assert meter.metric == "search_credits" and meter.unit == "credit" and meter.known
        assert meter.dimensions == {
            "operation": "web_search",
            "provider_request_id": "synthetic-request",
            "billing_component": meter.dimensions["billing_component"],
        }


@pytest.mark.parametrize("quantity", [None, True, -1, "NaN", "Infinity", {}])
def test_iqs_invalid_credit_component_remains_unknown_and_keeps_other_components(quantity):
    meters, settlement = response_measurement(
        provider="iqs",
        operation="web_search",
        payload={
            "costCredits": {
                "search": {"genericTextSearch": quantity},
                "valueAdded": {"summary": 0},
            },
        },
    )
    [invalid, zero] = meters
    assert invalid.quantity is None and not invalid.known
    assert invalid.dimensions["billing_component"] == "search.genericTextSearch"
    assert zero.quantity == "0" and zero.known
    assert settlement is None


@pytest.mark.parametrize(
    "credits", [None, [], {}, {"search": None}, {"search": {"unknownSku": 5}},
                {"valueAdded": "invalid"}],
)
def test_iqs_missing_known_credit_fields_keeps_request_without_assuming_zero(credits):
    meters, settlement = response_measurement(
        provider="iqs", operation="web_search", payload={"costCredits": credits},
    )
    [meter] = meters
    assert (meter.metric, meter.quantity, meter.source) == ("requests", "1", "observed")
    assert settlement is None
