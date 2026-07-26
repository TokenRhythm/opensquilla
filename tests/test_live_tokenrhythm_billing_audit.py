from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from opensquilla.provider.types import DoneEvent, ProviderBillingReceipt


def _load_audit_module():
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "live_tokenrhythm_billing_audit.py"
    )
    spec = importlib.util.spec_from_file_location(
        "live_tokenrhythm_billing_audit", script_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = _load_audit_module()


def test_audit_uses_inline_tokenrhythm_tiers_without_persisted_profile() -> None:
    tiers = audit._inline_tokenrhythm_tiers()

    assert list(tiers) == ["c0", "c1", "c2", "c3"]
    assert all(tier["provider"] == "tokenrhythm" for tier in tiers.values())


def test_audit_builds_default_and_strict_static_b5_quorums() -> None:
    common = {
        "api_key": "synthetic-rotated-key",
        "base_url": "https://tokenrhythm.studio/v1",
        "request_timeout_seconds": 30.0,
    }

    default = audit._build_tokenrhythm_ensemble(**common, strict=False)
    strict = audit._build_tokenrhythm_ensemble(**common, strict=True)

    assert default.profile_name == "static_tokenrhythm_b5"
    assert default.min_successful_proposers == 3
    assert strict.min_successful_proposers == 4
    assert len(default.proposers) == 4
    assert default.aggregator.provider_config.provider == "tokenrhythm"


def test_physical_item_validates_confirmed_zero_and_four_bucket_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from opensquilla.engine.pricing import PriceEntry

    monkeypatch.setattr(
        audit,
        "resolve_model_price",
        lambda model, provider: SimpleNamespace(
            entry=PriceEntry(
                input_per_m=10,
                output_per_m=20,
                cache_read_per_m=1,
                cache_write_per_m=2,
            ),
            source=f"catalog:{provider}:{model}",
        ),
    )
    item = audit._physical_item(
        {
            "provider": "tokenrhythm",
            "model": "deepseek-v4-flash",
            "input_tokens": 100,
            "output_tokens": 10,
            "cached_tokens": 40,
            "cache_write_tokens": 10,
            "billed_cost": 0.0,
            "cost_source": "provider_billed",
            "billing_receipt": ProviderBillingReceipt(
                currency="CNY",
                status="confirmed",
                amount_nanos=0,
                usd_equivalent_nanos=0,
                fx_native_per_usd_nanos=6_975_000_000,
            ),
        },
        0,
    )

    assert item["billingValid"] is True
    assert item["receipt"]["amountNanos"] == "0"
    assert item["cost"]["source"] == "provider_billed"
    assert item["cost"]["estimatedUsd"] == pytest.approx(760 / 1_000_000)
    assert item["cost"]["estimateBasis"] == "cache_aware"


def test_physical_item_keeps_pending_receipt_out_of_billed_cost() -> None:
    item = audit._physical_item(
        {
            "provider": "tokenrhythm",
            "model": "deepseek-v4-flash",
            "input_tokens": 1,
            "output_tokens": 1,
            "billed_cost": 0.0,
            "cost_source": "none",
            "billing_receipt": ProviderBillingReceipt(
                currency="CNY",
                status="pending",
                amount_nanos=None,
                usd_equivalent_nanos=None,
                fx_native_per_usd_nanos=6_975_000_000,
            ),
        },
        0,
    )

    assert item["billingValid"] is True
    assert item["receipt"]["status"] == "pending"
    assert item["cost"]["providerBilledUsdEquivalentNanos"] == "0"
    assert item["cost"]["estimatedUsd"] > 0


def test_scenario_report_reconciles_all_five_token_buckets() -> None:
    receipt = ProviderBillingReceipt(
        currency="CNY",
        status="confirmed",
        amount_nanos=0,
        usd_equivalent_nanos=0,
        fx_native_per_usd_nanos=6_975_000_000,
    )
    done = DoneEvent(
        input_tokens=1,
        output_tokens=2,
        reasoning_tokens=30,
        cached_tokens=40,
        cache_write_tokens=50,
        billed_cost=0.0,
        model="deepseek-v4-flash",
        model_usage_breakdown=[
            {
                "provider": "tokenrhythm",
                "model": "deepseek-v4-flash",
                "input_tokens": 1,
                "output_tokens": 2,
                "reasoning_tokens": 3,
                "cached_tokens": 4,
                "cache_write_tokens": 5,
                "billed_cost": 0.0,
                "cost_source": "provider_billed",
                "billing_receipt": receipt,
            }
        ],
    )

    report = audit._scenario_report(
        scenario_id="synthetic_b5",
        kind="b5_ensemble",
        done=done,
        error=None,
        exception=None,
        latency_ms=1,
        expected_physical_requests=1,
    )

    assert report["reasonCodes"] == [
        "envelope_cache_read_tokens_mismatch",
        "envelope_cache_write_tokens_mismatch",
        "envelope_reasoning_tokens_mismatch",
    ]


def test_report_guard_rejects_raw_prompt_response_and_secret() -> None:
    with pytest.raises(RuntimeError, match="unsafe field"):
        audit._assert_report_safe({"prompt": "must not persist"}, {})

    with pytest.raises(RuntimeError, match="credential detected"):
        audit._assert_report_safe(
            {"provider": "tokenrhythm", "model": "contains-secret-value"},
            {"TOKENRHYTHM_API_KEY": "secret-value"},
        )


def test_main_requires_explicit_cost_confirmation_before_live_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "audit.json"
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "synthetic-rotated-key")
    monkeypatch.setattr(
        audit,
        "_run_all_scenarios",
        lambda **_kwargs: pytest.fail("live audit must not start without confirmation"),
    )
    monkeypatch.setattr(sys, "argv", ["audit", "--output", str(output)])

    assert audit.main() == 2
    assert not output.exists()


def test_main_requires_rotated_key_attestation_before_live_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "audit.json"
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "synthetic-rotated-key")
    monkeypatch.setattr(
        audit,
        "_run_all_scenarios",
        lambda **_kwargs: pytest.fail("live audit must not start without rotation attestation"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit", "--output", str(output), "--confirm-live-cost"],
    )

    assert audit.main() == 2
    assert not output.exists()


def test_main_requires_env_key_and_never_creates_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "audit.json"
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit",
            "--output",
            str(output),
            "--confirm-live-cost",
            "--confirm-rotated-key",
        ],
    )

    assert audit.main() == 2
    assert not output.exists()


def test_main_writes_only_sanitized_accounting_report_in_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "audit.json"
    key = "synthetic-rotated-key-never-persist"

    async def fake_run(**kwargs):
        assert kwargs["api_key"] == key
        return {
            "schemaVersion": 1,
            "provider": "tokenrhythm",
            "scenarios": [],
            "summary": {
                "passedScenarioCount": 7,
                "pendingScenarioCount": 0,
                "failedScenarioCount": 0,
                "nativeCnyAmountNanos": "123",
                "usdEquivalentNanos": "18",
            },
        }

    monkeypatch.setenv("TOKENRHYTHM_API_KEY", key)
    monkeypatch.setattr(audit, "_run_all_scenarios", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit",
            "--output",
            str(output),
            "--confirm-live-cost",
            "--confirm-rotated-key",
        ],
    )

    assert audit.main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["summary"]["nativeCnyAmountNanos"] == "123"
    assert key not in output.read_text(encoding="utf-8")
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600
    printed = capsys.readouterr()
    assert key not in printed.out
    assert key not in printed.err
    assert "scenarios" not in printed.out
