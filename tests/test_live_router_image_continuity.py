from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from scripts import live_router_image_continuity as harness


def arguments(tmp_path, *extra):
    return ["--text-model", "synthetic-text", "--vision-model", "synthetic-vision",
            "--report", str(tmp_path / "report.json"), *extra]


def test_live_opt_in_is_required_before_credentials_or_network(tmp_path, monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        pytest.fail("live acceptance ran without explicit opt-in")

    monkeypatch.setattr(harness, "run_acceptance", forbidden)
    assert harness.main(arguments(tmp_path)) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "live_opt_in_required"
    assert not (tmp_path / "report.json").exists()


def test_missing_key_fails_before_network(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    assert harness.main(arguments(tmp_path, "--live")) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "missing_credential"


@pytest.mark.parametrize("flag,value", [("--max-calls", "33"), ("--max-output", "2049")])
def test_cost_bounds_cannot_be_bypassed(tmp_path, flag, value):
    with pytest.raises(SystemExit) as error:
        harness.main(arguments(tmp_path, "--live", flag, value))
    assert error.value.code == 2


def test_payload_projection_exposes_counts_without_prompt_or_image_bytes():
    payload = {"model": "synthetic-text", "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "private synthetic prompt"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,private"}},
        ]},
        {"role": "tool", "content": "[图片未分析：当前模型不支持图片输入]"},
    ]}
    result = harness.payload_facts(payload)
    assert result == {"model": "synthetic-text", "native_images": 1,
                      "image_markers": 1, "tool_results": 1}
    assert "private" not in json.dumps(result)


def test_semantic_evidence_reads_admitted_state_instead_of_execution_tier():
    record = {
        "status": "admitted", "state_committed": True,
        "state": {"tier": "c2"}, "decision": {"intent": "continue", "tier": "c2"},
        "execution_update": {"tier": "c3", "model": "synthetic-vision"},
    }
    assert harness.admitted_semantic_facts(record) == {
        "semantic_tier": "c2", "semantic_intent": "continue", "state_admitted": True,
    }
    record.update(status="staged", state_committed=False)
    with pytest.raises(harness.AcceptanceError, match="not_admitted"):
        harness.admitted_semantic_facts(record)


def test_synthetic_fixture_is_a_decodable_image_with_fresh_oracle(monkeypatch):
    codes = iter(("112233", "AABBCC"))
    monkeypatch.setattr(harness.secrets, "token_hex", lambda _length: next(codes))
    first, first_code = harness.synthetic_image()
    second, second_code = harness.synthetic_image()
    assert first_code != second_code and first != second
    with Image.open(io.BytesIO(first)) as image:
        assert image.format == "PNG" and image.size == (1000, 240)


def test_external_failure_is_classified_without_serializing_credentials(
    tmp_path, monkeypatch, capsys,
):
    credential = "synthetic-private-credential"
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", credential)

    async def fail(_args, api_key):
        assert api_key == credential
        raise RuntimeError(f"401 Authorization: Bearer {api_key}; private response body")

    monkeypatch.setattr(harness, "run_acceptance", fail)
    assert harness.main(arguments(tmp_path, "--live")) == 1
    report_text = (tmp_path / "report.json").read_text()
    report = json.loads(report_text)
    assert report["failure"] == "auth"
    assert not report["ok"]
    printed = capsys.readouterr()
    assert credential not in report_text + printed.out + printed.err
    assert "private response body" not in report_text
