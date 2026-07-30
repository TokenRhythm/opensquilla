"""Opt-in real-key baseline for the fixed multi-provider validation matrix.

The credential file path is supplied through the environment and parsed as
inert data by the shared harness.  Default CI never opts in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.live_harness_security import parse_provider_keys_file, report_contains_secret
from scripts.live_multi_provider_matrix import DEFAULT_PROVIDERS, run_matrix

pytestmark = [pytest.mark.llm, pytest.mark.llm_costly, pytest.mark.llm_gateway]


def _require_live_matrix() -> Path:
    if os.environ.get("OPENSQUILLA_LIVE_PROVIDER_MATRIX") != "1":
        pytest.skip("set OPENSQUILLA_LIVE_PROVIDER_MATRIX=1 to run the provider matrix")
    raw_path = os.environ.get("OPENSQUILLA_PROVIDER_KEYS_FILE", "").strip()
    if not raw_path:
        pytest.skip("set OPENSQUILLA_PROVIDER_KEYS_FILE to the provider.keys path")
    path = Path(raw_path)
    if not path.is_file():
        pytest.skip("OPENSQUILLA_PROVIDER_KEYS_FILE is not a readable file")
    return path


def test_real_provider_baseline_matrix() -> None:
    inventory = parse_provider_keys_file(_require_live_matrix())

    report = run_matrix(
        providers=list(DEFAULT_PROVIDERS),
        secrets=inventory.secrets,
        models=inventory.models,
        smoke_max_tokens=64,
        gateway_max_tokens=64,
        gateway_timeout_seconds=120.0,
        stage_timeout_seconds=300.0,
        special_max_tokens=256,
    )

    assert not report_contains_secret(report, inventory.secrets)
    assert [row["provider"] for row in report["providers"]] == list(DEFAULT_PROVIDERS)
    for row in report["providers"]:
        assert row["stages"][0]["status"] != "skipped", (
            f"expected non-empty credential for {row['provider']}"
        )
    assert report["deep_coverage_complete"] is True
    assert report["ok"] is True, json.dumps(report, ensure_ascii=False, sort_keys=True)
