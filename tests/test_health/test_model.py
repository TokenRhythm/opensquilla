from __future__ import annotations

from opensquilla.health.model import FixStep, HealthFinding, build_report


def test_build_report_ready_when_all_findings_ok() -> None:
    report = build_report(
        [
            HealthFinding(
                id="gateway.ready",
                severity="ok",
                surface="gateway",
                title="Gateway ready",
                detail="Gateway health and readiness endpoints respond.",
            )
        ]
    )

    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["counts"] == {"error": 0, "warn": 0, "info": 0, "ok": 1}
    assert report["summary"] == "Ready"


def test_build_report_summary_explains_optional_items_without_degrading_readiness() -> None:
    report = build_report(
        [
            HealthFinding(
                id="image_generation.disabled",
                severity="info",
                surface="image_generation",
                title="Image generation is disabled",
                detail="Image generation is optional and is currently disabled.",
            ),
            HealthFinding(
                id="sandbox.posture.bypass",
                severity="info",
                surface="sandbox",
                title="Sandbox posture is bypass",
                detail="OpenSquilla is configured for maximum convenience.",
            ),
            HealthFinding(
                id="gateway.ready",
                severity="ok",
                surface="gateway",
                title="Gateway ready",
                detail="Gateway health and readiness endpoints respond.",
            ),
        ]
    )

    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["summary"] == "Ready, 2 optional setup items"


def test_build_report_action_required_for_error() -> None:
    report = build_report(
        [
            HealthFinding(
                id="provider.active.not_configured",
                severity="error",
                surface="provider",
                title="Active provider is not configured",
                detail="openrouter is active but missing an API key.",
                fix_steps=[
                    FixStep(
                        label="Configure provider",
                        command="opensquilla providers configure openrouter --api-key YOUR_API_KEY",
                    )
                ],
                restart_required=True,
            )
        ]
    )

    assert report["ready"] is False
    assert report["status"] == "action_required"
    assert report["summary"] == "1 action required"
    assert report["impactCounts"]["blocks_ready"] == 1
    assert report["findings"][0]["fixSteps"][0]["command"].startswith("opensquilla providers")
    assert report["findings"][0]["restartRequired"] is True


def test_build_report_error_can_be_non_blocking_when_impact_is_degraded() -> None:
    report = build_report(
        [
            HealthFinding(
                id="channel.feishu.dead",
                severity="error",
                readiness_impact="degrades",
                surface="channels",
                title="Channel feishu is dead",
                detail="The configured channel is not able to receive or send messages.",
            )
        ]
    )

    assert report["ready"] is True
    assert report["status"] == "degraded"
    assert report["summary"] == "Ready, 1 degraded check"
    assert report["counts"]["error"] == 1
    assert report["impactCounts"] == {
        "blocks_ready": 0,
        "degrades": 1,
        "optional": 0,
        "none": 0,
    }
    assert report["findings"][0]["readinessImpact"] == "degrades"


def test_build_report_degraded_for_warning_only() -> None:
    report = build_report(
        [
            HealthFinding(
                id="memory.vector.unavailable",
                severity="warn",
                surface="memory",
                title="Vector memory unavailable",
                detail="Memory search falls back to text retrieval.",
            )
        ]
    )

    assert report["ready"] is True
    assert report["status"] == "degraded"
    assert report["summary"] == "Ready, 1 degraded check"


def test_build_report_orders_actionable_findings_before_informational_items() -> None:
    report = build_report(
        [
            HealthFinding(
                id="gateway.rpc.ready",
                severity="ok",
                surface="gateway",
                title="Gateway ready",
                detail="RPC is reachable.",
            ),
            HealthFinding(
                id="sandbox.bypass",
                severity="info",
                surface="sandbox",
                title="Sandbox bypass enabled",
                detail="Sandbox enforcement is disabled by configuration.",
            ),
            HealthFinding(
                id="logs.unavailable",
                severity="warn",
                surface="logs",
                title="Logs unavailable",
                detail="The debug log cannot be read.",
            ),
            HealthFinding(
                id="provider.missing",
                severity="error",
                surface="provider",
                title="Provider missing",
                detail="No model provider is configured.",
            ),
            HealthFinding(
                id="channels.disabled",
                severity="info",
                surface="channels",
                title="Channel disabled",
                detail="A configured channel is disabled.",
            ),
        ]
    )

    assert [finding["id"] for finding in report["findings"]] == [
        "provider.missing",
        "logs.unavailable",
        "sandbox.bypass",
        "channels.disabled",
        "gateway.rpc.ready",
    ]


def test_build_report_labels_warn_optional_findings_as_operator_action_items() -> None:
    # A warn-severity optional finding is work someone is blocked on (pending
    # pairing approvals, unconfirmed sends) — not an "optional setup item".
    report = build_report(
        [
            HealthFinding(
                id="channel.telegram-main.pending_pairings",
                severity="warn",
                readiness_impact="optional",
                surface="channels",
                title="Channel telegram-main has pairing requests awaiting approval",
                detail="2 sender(s) are waiting on operator approval.",
            ),
            HealthFinding(
                id="image_generation.disabled",
                severity="info",
                surface="image_generation",
                title="Image generation is disabled",
                detail="Image generation is optional and is currently disabled.",
            ),
        ]
    )

    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["summary"] == "Ready, 1 item awaiting operator action, 1 optional setup item"
