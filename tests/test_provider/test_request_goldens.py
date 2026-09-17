"""Golden request-proof tests: freeze the exact outbound provider request JSON.

Each parametrized case drives one provider adapter offline (MockTransport)
and byte-compares the captured request against a checked-in golden under
``tests/test_provider/golden/requests/``. Any refactor that changes a request
payload byte must fail here; deliberate changes are regenerated with
``OPENSQUILLA_REGEN_GOLDENS=1`` and reviewed as behavior changes.
"""

from __future__ import annotations

import pytest

from opensquilla.provider.compat_policy import known_policy_kinds
from tests.test_provider.golden import _harness as harness

_CASES = harness.build_cases()


@pytest.mark.parametrize("case", _CASES, ids=[case.case_id for case in _CASES])
async def test_provider_request_matches_golden(
    case: harness.GoldenCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = await harness.capture_case_record(case, monkeypatch)
    projection = harness.project_case_request(case)
    assert projection.payload == record["body"], (
        f"{case.case_id}: final request projection drifted from transport payload"
    )
    assert projection.wire_message_count == len(
        record["body"].get("messages", record["body"].get("input", []))
    )
    assert projection.fits_message_count is None
    harness.assert_or_regen(case.golden_path, record)


def test_matrix_covers_every_compat_policy_kind() -> None:
    """Adding a new compat policy kind must fail until its goldens land."""
    covered = {case.kind for case in _CASES if case.backend == "openai_compat"}
    assert covered == set(known_policy_kinds()), (
        "Every OpenAI-compat policy kind needs request goldens: add the kind "
        "to COMPAT_THINKING_MODELS in tests/test_provider/golden/_harness.py "
        "and regenerate."
    )
    assert set(harness.COMPAT_THINKING_MODELS) == set(known_policy_kinds())


def test_golden_tree_matches_case_matrix() -> None:
    """No missing goldens, and no stale files left from renamed cases."""
    expected = {case.golden_path for case in _CASES}
    actual = set(harness.GOLDEN_ROOT.rglob("*.json"))
    missing = sorted(str(path) for path in expected - actual)
    stale = sorted(str(path) for path in actual - expected)
    assert not missing, f"missing goldens (regen with {harness.REGEN_ENV}=1): {missing}"
    assert not stale, f"stale goldens (delete them by hand): {stale}"


def test_goldens_contain_no_credential_material() -> None:
    files = sorted(harness.GOLDEN_ROOT.rglob("*.json"))
    assert files, "no golden files found"
    for path in files:
        raw = path.read_bytes()
        assert b"\r\n" not in raw, f"golden file must be checked out with LF endings: {path}"
        text = raw.decode("utf-8")
        assert harness.FAKE_API_KEY not in text, f"credential material leaked into {path}"
        assert "Bearer " not in text, f"auth header value leaked into {path}"
