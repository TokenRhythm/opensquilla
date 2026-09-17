from __future__ import annotations

from starlette.testclient import TestClient

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import GatewayConfig


def test_sandbox_v2_policy_get_update_and_conflict(tmp_path) -> None:
    config = GatewayConfig(
        host="127.0.0.1",
        state_dir=str(tmp_path),
    )

    with TestClient(
        create_gateway_app(config),
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        baseline_response = client.get("/api/v2/sandbox/policy")
        assert baseline_response.status_code == 200
        baseline = baseline_response.json()
        baseline["network"]["denyDomains"] = ["telemetry.example"]

        saved_response = client.put(
            "/api/v2/sandbox/policy",
            json={
                "basePolicyVersion": baseline["policyVersion"],
                "policy": baseline,
            },
        )
        conflict_response = client.put(
            "/api/v2/sandbox/policy",
            json={
                "basePolicyVersion": baseline["policyVersion"],
                "policy": baseline,
            },
        )

    assert saved_response.status_code == 200
    assert saved_response.json()["policyVersion"] == 1
    assert conflict_response.status_code == 409
    assert conflict_response.json()["code"] == "POLICY_VERSION_CONFLICT"
    assert conflict_response.json()["details"]["currentPolicy"]["policyVersion"] == 1
