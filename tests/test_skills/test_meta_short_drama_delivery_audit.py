"""Deterministic delivery-gate regressions for meta-short-drama."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from opensquilla.skills.loader import SkillLoader

BUNDLED = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "opensquilla"
    / "skills"
    / "bundled"
)


def _load_audit_module() -> ModuleType:
    script = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "opensquilla"
        / "skills"
        / "bundled"
        / "short-drama-delivery-audit"
        / "scripts"
        / "audit_delivery.py"
    )
    spec = importlib.util.spec_from_file_location("meta_short_drama_delivery_audit", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _load_audit_module()


def _load_video_module() -> ModuleType:
    script = BUNDLED / "seedance-2-prompt" / "scripts" / "generate_video.py"
    name = "meta_short_drama_policy_workflow_video"
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _script(*durations: int, overview_duration: int | None = None) -> str:
    content_duration = sum(durations) if overview_duration is None else overview_duration
    blocks = [
        "=== OVERVIEW ===",
        "TITLE: Synthetic test drama",
        f"DURATION_S: {content_duration}",
        "ASPECT_RATIO: 9:16",
        f"N_SHOTS: {len(durations)}",
    ]
    for number, duration in enumerate(durations, start=1):
        blocks.extend(
            [
                "",
                f"=== SHOT_{number} ===",
                f"DURATION_S: {duration}",
                "IMAGE_PROMPT: synthetic",
                "VIDEO_PROMPT: synthetic",
            ]
        )
    return "\n".join(blocks) + "\n"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _image_receipt(request_id: str = "gen-image-123") -> dict[str, object]:
    return {
        "status": "generated",
        "placeholder": False,
        "provider": "openrouter",
        "model": "google/gemini-image",
        "request_id": request_id,
    }


def _video_receipt(job_id: str = "job-video-123") -> dict[str, object]:
    return {
        "status": "generated",
        "fallback": False,
        "provider": "openrouter",
        "model": "bytedance/seedance-2.0",
        "job_id": job_id,
    }


def _current_run_evidence(
    run_dir: Path,
    *,
    shot_numbers: tuple[int, ...] = (1,),
) -> tuple[dict[str, str], dict[str, str]]:
    """Build the parent-owned evidence a current executor run would supply."""

    paths = {"reference_image": run_dir / "reference.png.receipt.json"}
    for number in shot_numbers:
        paths[f"shot{number}_image"] = run_dir / f"{number}_shot.png.receipt.json"
        paths[f"shot{number}_video"] = run_dir / f"{number}_shot.mp4.receipt.json"

    dispositions: dict[str, str] = {}
    proofs: dict[str, str] = {}
    for step_id, path in paths.items():
        if not path.is_file():
            continue
        receipt = json.loads(path.read_text(encoding="utf-8"))
        proof = audit._canonical_receipt_proof(receipt)
        assert proof is not None
        dispositions[step_id] = "receipt"
        proofs[step_id] = proof
    return dispositions, proofs


def _run_fixture(
    tmp_path: Path,
    *,
    durations: tuple[int, ...] = (3,),
    final_duration: float | None = None,
    fallback_outputs: dict[str, object] | None = None,
    paid_submission_dispositions: dict[str, object] | str | None = None,
    paid_submission_receipt_proofs: dict[str, object] | str | None = None,
    image_overrides: dict[str, object] | None = None,
    video_overrides: dict[str, object] | None = None,
) -> dict[str, Any]:
    tmp_path.joinpath("script.txt").write_text(_script(*durations), encoding="utf-8")
    _write_json(tmp_path / "reference.png.receipt.json", _image_receipt("gen-reference"))
    media_durations: dict[str, float] = {}
    for number, duration in enumerate(durations, start=1):
        tmp_path.joinpath(f"{number}_shot.mp4").write_bytes(b"synthetic-video")
        _write_json(
            tmp_path / f"{number}_shot.png.receipt.json",
            image_overrides if number == 1 and image_overrides is not None else _image_receipt(),
        )
        _write_json(
            tmp_path / f"{number}_shot.mp4.receipt.json",
            video_overrides if number == 1 and video_overrides is not None else _video_receipt(),
        )
        media_durations[f"{number}_shot.mp4"] = float(duration)
    tmp_path.joinpath("final_subtitled.mp4").write_bytes(b"synthetic-final-video")
    media_durations["final_subtitled.mp4"] = (
        float(sum(durations) + 4) if final_duration is None else final_duration
    )

    def fake_probe(path: Path, _ffprobe: str) -> dict[str, object]:
        return {"decodable": True, "duration_s": media_durations[path.name]}

    default_dispositions, default_proofs = _current_run_evidence(
        tmp_path,
        shot_numbers=tuple(range(1, len(durations) + 1)),
    )
    if isinstance(paid_submission_dispositions, dict):
        default_dispositions.update(paid_submission_dispositions)
        paid_submission_dispositions = default_dispositions
    elif paid_submission_dispositions is None:
        paid_submission_dispositions = default_dispositions
    if isinstance(paid_submission_receipt_proofs, dict):
        paid_submission_receipt_proofs = dict(paid_submission_receipt_proofs)
    elif paid_submission_receipt_proofs is None:
        paid_submission_receipt_proofs = default_proofs

    return audit.audit_delivery(
        tmp_path,
        fallback_outputs or {str(number): "" for number in range(1, len(durations) + 1)},
        paid_submission_dispositions=paid_submission_dispositions,
        paid_submission_receipt_proofs=paid_submission_receipt_proofs,
        probe=fake_probe,
    )


def _codes(verdict: dict[str, Any]) -> set[str]:
    return {str(issue["code"]) for issue in verdict["issues"]}


def test_ffprobe_parser_requires_video_stream_and_reads_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"synthetic")
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "streams": [{"codec_type": "video", "duration": "6.96"}],
                    "format": {"duration": "7.00"},
                }
            ),
        )

    monkeypatch.setattr(audit.subprocess, "run", fake_run)

    result = audit._probe_video(video, "ffprobe-test")

    assert result == {"decodable": True, "duration_s": 7.0}
    assert seen["argv"][0] == "ffprobe-test"
    assert seen["argv"][-1] == str(video)
    assert seen["kwargs"] == {
        "capture_output": True,
        "text": True,
        "timeout": 20,
        "check": False,
    }


def test_real_receipts_and_probed_content_plus_bookends_are_verified(tmp_path: Path) -> None:
    verdict = _run_fixture(tmp_path, durations=(3, 4))

    assert verdict["status"] == "verified"
    assert verdict["verified"] is True
    assert verdict["media_provenance"] == "verified real API E2E"
    assert verdict["active_shots"] == [1, 2]
    assert verdict["content_duration_s"] == 7
    assert verdict["bookend_duration_s"] == 4.0
    assert verdict["expected_final_duration_s"] == 11.0
    assert verdict["final_duration_s"] == 11.0
    assert verdict["final_video_decodable"] is True
    assert verdict["paid_submission_dispositions"] == {
        "reference_image": "confirmed",
        "shot1_image": "confirmed",
        "shot1_video": "confirmed",
        "shot2_image": "confirmed",
        "shot2_video": "confirmed",
    }
    assert verdict["safe_no_submit_assets"] == []
    assert verdict["issues"] == []


def test_stale_sidecars_without_current_run_proof_cannot_confirm(tmp_path: Path) -> None:
    verdict = _run_fixture(
        tmp_path,
        paid_submission_receipt_proofs={},
    )

    assert verdict["verified"] is False
    assert verdict["status"] == "degraded"
    assert verdict["paid_submission_dispositions"] == {
        "reference_image": "receipt",
        "shot1_image": "receipt",
        "shot1_video": "receipt",
    }
    assert verdict["paid_submission_status_unknown_assets"] == [
        "reference_image",
        "shot1_image",
        "shot1_video",
    ]
    assert {
        issue["asset"]
        for issue in verdict["issues"]
        if issue["code"] == "RECEIPT_NOT_PROVEN_CURRENT_RUN"
    } == {"reference_image", "shot1_image", "shot1_video"}


def test_sidecar_changed_after_current_run_proof_cannot_confirm(tmp_path: Path) -> None:
    _run_fixture(tmp_path)
    dispositions, proofs = _current_run_evidence(tmp_path)
    _write_json(
        tmp_path / "1_shot.mp4.receipt.json",
        _video_receipt("job-forged-after-proof"),
    )

    def fake_probe(path: Path, _ffprobe: str) -> dict[str, object]:
        return {
            "decodable": True,
            "duration_s": 7.0 if path.name.startswith("final") else 3.0,
        }

    verdict = audit.audit_delivery(
        tmp_path,
        {"1": ""},
        paid_submission_dispositions=dispositions,
        paid_submission_receipt_proofs=proofs,
        probe=fake_probe,
    )

    assert verdict["paid_submission_dispositions"]["shot1_video"] == "receipt"
    assert verdict["shots"][0]["video_submission_disposition"] == "receipt"
    assert verdict["paid_submission_status_unknown_assets"] == ["shot1_video"]
    assert any(
        issue["code"] == "RECEIPT_NOT_PROVEN_CURRENT_RUN"
        and issue["asset"] == "shot1_video"
        for issue in verdict["issues"]
    )


@pytest.mark.parametrize(
    ("receipt", "expected_code"),
    [
        ({"status": "placeholder", "placeholder": True, "provider": "local"}, "IMAGE_PLACEHOLDER"),
        (
            {
                "status": "generated",
                "placeholder": False,
                "provider": "openrouter",
                "model": "google/gemini-image",
            },
            "IMAGE_REQUEST_ID_MISSING",
        ),
        (
            {
                "status": "generated",
                "placeholder": False,
                "provider": "openrouter",
                "model": "google/gemini-image",
                "request_id": "sk-secret-must-not-flow",
            },
            "IMAGE_REQUEST_ID_MISSING",
        ),
    ],
)
def test_placeholder_or_spoofed_image_receipt_is_degraded(
    tmp_path: Path,
    receipt: dict[str, object],
    expected_code: str,
) -> None:
    verdict = _run_fixture(tmp_path, image_overrides=receipt)

    assert verdict["status"] == "degraded"
    assert verdict["verified"] is False
    assert verdict["media_provenance"] == "degraded — not a verified real API E2E"
    assert expected_code in _codes(verdict)
    assert "sk-secret" not in json.dumps(verdict)


def test_placeholder_image_preserves_sanitized_unknown_billing_signal(
    tmp_path: Path,
) -> None:
    verdict = _run_fixture(
        tmp_path,
        image_overrides={
            "status": "placeholder",
            "placeholder": True,
            "provider": "local",
            "model": "google/gemini-image",
            "reason": "all_model_attempts_failed",
            "raw_message": "https://signed.example/image?token=secret#fragment",
        },
    )

    assert verdict["may_have_been_billed"] is True
    assert verdict["paid_submission_status_unknown_assets"] == ["shot1_image"]
    assert verdict["billing_guidance"] == (
        "Check provider history before starting a replacement generation."
    )
    assert "PAID_SUBMISSION_STATUS_UNKNOWN" in _codes(verdict)
    serialized = json.dumps(verdict)
    assert "signed.example" not in serialized
    assert "token=secret" not in serialized


def test_image_policy_rejection_placeholder_is_honest_sanitized_degradation(
    tmp_path: Path,
) -> None:
    policy_code = "InputImageSensitiveContentDetected.PrivacyInformation"
    verdict = _run_fixture(
        tmp_path,
        image_overrides={
            "status": "policy_rejected",
            "placeholder": True,
            "provider": "openrouter",
            "model": "google/gemini-3.1-flash-image-preview",
            "reason": "provider_policy_rejected",
            "policy_code": policy_code,
            "request_id": "req-must-not-survive",
            "raw_message": "https://signed.example/image?token=secret#fragment",
        },
    )

    assert verdict["status"] == "degraded"
    assert verdict["verified"] is False
    assert {
        "IMAGE_NOT_GENERATED",
        "IMAGE_POLICY_REJECTED",
        "IMAGE_PLACEHOLDER",
    } <= _codes(verdict)
    image_receipt = verdict["shots"][0]["image_receipt"]
    assert image_receipt == {
        "status": "policy_rejected",
        "provider": "openrouter",
        "model": "google/gemini-3.1-flash-image-preview",
        "placeholder": True,
        "reason": "provider_policy_rejected",
        "policy_code": policy_code,
    }
    serialized = json.dumps(verdict)
    assert "req-must-not-survive" not in serialized
    assert "signed.example" not in serialized
    assert "token=secret" not in serialized


@pytest.mark.parametrize(
    "unsafe_policy_code",
    [
        "https://signed.example/image",
        "https://signed.example/image?token=secret#fragment",
        "sk-or-secret",
        "sk-policy-secret",
        "req-private",
        "req-privacy-private",
        "provider policy prose with spaces",
    ],
)
def test_image_policy_receipt_rejects_provider_prose_as_policy_code(
    tmp_path: Path,
    unsafe_policy_code: str,
) -> None:
    verdict = _run_fixture(
        tmp_path,
        image_overrides={
            "status": "policy_rejected",
            "placeholder": True,
            "provider": "openrouter",
            "model": "google/gemini-3.1-flash-image-preview",
            "reason": "provider_policy_rejected",
            "policy_code": unsafe_policy_code,
        },
    )

    assert "IMAGE_POLICY_RECEIPT_INVALID" in _codes(verdict)
    assert "IMAGE_POLICY_REJECTED" not in _codes(verdict)
    assert verdict["may_have_been_billed"] is True
    assert verdict["paid_submission_status_unknown_assets"] == ["shot1_image"]
    assert "PAID_SUBMISSION_STATUS_UNKNOWN" in _codes(verdict)
    serialized = json.dumps(verdict)
    assert "signed.example" not in serialized
    assert "token=secret" not in serialized


@pytest.mark.parametrize(
    "unsafe_policy_code",
    [
        "https://signed.example/video",
        "https://signed.example/video?token=secret#fragment",
        "sk-or-secret",
        "sk-policy-secret",
        "req-private",
        "req-privacy-private",
        "provider policy prose with spaces",
    ],
)
def test_video_policy_receipt_rejects_unsafe_policy_code_before_delivery(
    tmp_path: Path,
    unsafe_policy_code: str,
) -> None:
    verdict = _run_fixture(
        tmp_path,
        video_overrides={
            "status": "policy_rejected",
            "fallback": False,
            "provider": "openrouter",
            "model": "bytedance/seedance-2.0",
            "reason": "provider_policy_rejected",
            "policy_code": unsafe_policy_code,
        },
    )

    assert "VIDEO_POLICY_RECEIPT_INVALID" in _codes(verdict)
    assert "VIDEO_POLICY_REJECTED" not in _codes(verdict)
    assert verdict["may_have_been_billed"] is True
    assert verdict["paid_submission_status_unknown_assets"] == ["shot1_video"]
    assert "PAID_SUBMISSION_STATUS_UNKNOWN" in _codes(verdict)
    video_receipt = verdict["shots"][0]["video_receipt"]
    assert video_receipt["policy_code"] == ""
    serialized = json.dumps(verdict)
    for forbidden in (
        "signed.example",
        "token=secret",
        "sk-or-secret",
        "sk-policy-secret",
        "req-private",
        "req-privacy-private",
        "provider policy prose",
    ):
        assert forbidden not in serialized


def test_missing_video_receipt_and_executed_fallback_cannot_pass(tmp_path: Path) -> None:
    _run_fixture(tmp_path, fallback_outputs={"1": "wrote 1_shot.mp4"})
    (tmp_path / "1_shot.mp4.receipt.json").unlink()

    def fake_probe(path: Path, _ffprobe: str) -> dict[str, object]:
        return {"decodable": True, "duration_s": 7.0 if path.name.startswith("final") else 3.0}

    dispositions, proofs = _current_run_evidence(tmp_path)
    verdict = audit.audit_delivery(
        tmp_path,
        {"1": "wrote 1_shot.mp4 sk-secret https://signed.example/video?token=private"},
        paid_submission_dispositions=dispositions,
        paid_submission_receipt_proofs=proofs,
        probe=fake_probe,
    )

    assert verdict["status"] == "degraded"
    assert verdict["verified"] is False
    assert {
        "MISSING_RECEIPT",
        "VIDEO_FALLBACK_EXECUTED",
        "PAID_SUBMISSION_STATUS_UNKNOWN",
    } <= _codes(verdict)
    assert verdict["shots"][0]["fallback_executed"] is True
    assert verdict["may_have_been_billed"] is True
    assert verdict["paid_submission_status_unknown_assets"] == ["shot1_video"]
    assert verdict["billing_guidance"] == (
        "Check provider history before starting a replacement generation."
    )
    serialized = json.dumps(verdict)
    assert "sk-secret" not in serialized
    assert "signed.example" not in serialized
    assert "token=private" not in serialized


def test_missing_video_receipt_is_unknown_even_without_fallback_output(tmp_path: Path) -> None:
    _run_fixture(tmp_path)
    (tmp_path / "1_shot.mp4.receipt.json").unlink()

    def fake_probe(path: Path, _ffprobe: str) -> dict[str, object]:
        return {"decodable": True, "duration_s": 7.0 if path.name.startswith("final") else 3.0}

    dispositions, proofs = _current_run_evidence(tmp_path)
    verdict = audit.audit_delivery(
        tmp_path,
        {"1": ""},
        paid_submission_dispositions=dispositions,
        paid_submission_receipt_proofs=proofs,
        probe=fake_probe,
    )

    assert verdict["status"] == "degraded"
    assert verdict["may_have_been_billed"] is True
    assert verdict["paid_submission_status_unknown_assets"] == ["shot1_video"]
    assert {"MISSING_RECEIPT", "PAID_SUBMISSION_STATUS_UNKNOWN"} <= _codes(verdict)


@pytest.mark.parametrize(
    ("disposition", "expected_billed"),
    [
        ("safe_no_submit", False),
        ("maybe_accepted", True),
        ("receipt", True),
    ],
)
def test_parent_paid_disposition_controls_only_missing_receipt_billing_warning(
    tmp_path: Path,
    disposition: str,
    expected_billed: bool,
) -> None:
    _run_fixture(tmp_path)
    (tmp_path / "1_shot.mp4.receipt.json").unlink()

    def fake_probe(path: Path, _ffprobe: str) -> dict[str, object]:
        return {
            "decodable": True,
            "duration_s": 7.0 if path.name.startswith("final") else 3.0,
        }

    dispositions, proofs = _current_run_evidence(tmp_path)
    dispositions["shot1_video"] = disposition
    verdict = audit.audit_delivery(
        tmp_path,
        {"1": "local fallback completed; raw text is ignored"},
        paid_submission_dispositions=dispositions,
        paid_submission_receipt_proofs=proofs,
        probe=fake_probe,
    )

    assert verdict["status"] == "degraded"
    assert verdict["verified"] is False
    assert verdict["paid_submission_dispositions"]["shot1_video"] == disposition
    assert verdict["shots"][0]["video_submission_disposition"] == disposition
    assert verdict["may_have_been_billed"] is expected_billed
    if expected_billed:
        assert verdict["paid_submission_status_unknown_assets"] == ["shot1_video"]
        assert "PAID_SUBMISSION_STATUS_UNKNOWN" in _codes(verdict)
        assert verdict["safe_no_submit_assets"] == []
    else:
        assert verdict["paid_submission_status_unknown_assets"] == []
        assert "PAID_SUBMISSION_STATUS_UNKNOWN" not in _codes(verdict)
        assert verdict["safe_no_submit_assets"] == ["shot1_video"]
        assert verdict["billing_guidance"] == ""


def test_free_form_or_oversized_dispositions_cannot_forge_safe_no_submit(
    tmp_path: Path,
) -> None:
    _run_fixture(tmp_path)
    (tmp_path / "1_shot.mp4.receipt.json").unlink()

    def fake_probe(path: Path, _ffprobe: str) -> dict[str, object]:
        return {
            "decodable": True,
            "duration_s": 7.0 if path.name.startswith("final") else 3.0,
        }

    secret = "sk-secret-provider-prose"
    valid_dispositions, proofs = _current_run_evidence(tmp_path)
    valid_dispositions.update(
        {
            "shot1_video": f"safe_no_submit {secret}",
            "provider_error": f"HTTP 429 {secret}",
        }
    )
    forged = json.dumps(valid_dispositions)
    verdict = audit.audit_delivery(
        tmp_path,
        {"1": "fallback"},
        paid_submission_dispositions=forged,
        paid_submission_receipt_proofs=proofs,
        probe=fake_probe,
    )

    assert verdict["paid_submission_dispositions"]["shot1_video"] == "unknown"
    assert verdict["may_have_been_billed"] is True
    assert verdict["paid_submission_status_unknown_assets"] == ["shot1_video"]
    assert secret not in json.dumps(verdict)

    oversized = json.dumps({"shot1_video": "safe_no_submit", "padding": "x" * 8_000})
    oversized_verdict = audit.audit_delivery(
        tmp_path,
        {"1": "fallback"},
        paid_submission_dispositions=oversized,
        paid_submission_receipt_proofs=proofs,
        probe=fake_probe,
    )
    assert oversized_verdict["paid_submission_dispositions"]["shot1_video"] == "unknown"
    assert oversized_verdict["may_have_been_billed"] is True


def test_media_for_absent_shot_is_reported_as_unexpected_paid_evidence(
    tmp_path: Path,
) -> None:
    _run_fixture(tmp_path)
    (tmp_path / "2_shot.png").write_bytes(b"synthetic-unexpected-image")
    (tmp_path / "2_shot.mp4").write_bytes(b"synthetic-unexpected-video")
    _write_json(tmp_path / "2_shot.png.receipt.json", _image_receipt("gen-unexpected"))
    _write_json(tmp_path / "2_shot.mp4.receipt.json", _video_receipt("job-unexpected"))

    def fake_probe(path: Path, _ffprobe: str) -> dict[str, object]:
        return {"decodable": True, "duration_s": 7.0 if path.name.startswith("final") else 3.0}

    dispositions, proofs = _current_run_evidence(tmp_path)
    verdict = audit.audit_delivery(
        tmp_path,
        {"1": "", "2": ""},
        paid_submission_dispositions=dispositions,
        paid_submission_receipt_proofs=proofs,
        probe=fake_probe,
    )

    assert verdict["status"] == "degraded"
    assert verdict["unexpected_paid_assets"] == ["shot2_image", "shot2_video"]
    assert [
        issue["asset"]
        for issue in verdict["issues"]
        if issue["code"] == "UNEXPECTED_PAID_ASSET"
    ] == ["shot2_image", "shot2_video"]
    assert verdict["may_have_been_billed"] is True
    assert verdict["paid_submission_status_unknown_assets"] == [
        "shot2_image",
        "shot2_video",
    ]


def test_policy_rejection_reason_survives_fallback_as_sanitized_degradation(
    tmp_path: Path,
) -> None:
    verdict = _run_fixture(
        tmp_path,
        fallback_outputs={"1": "wrote 1_shot.mp4"},
        video_overrides={
            "status": "policy_rejected",
            "fallback": False,
            "provider": "openrouter",
            "model": "bytedance/seedance-2.0",
            "reason": "provider_policy_rejected",
            "policy_code": "InputImageSensitiveContentDetected.PrivacyInformation",
            "request_id": "req-must-not-survive",
            "raw_message": "https://signed.example/video?token=secret#fragment",
        },
    )

    assert verdict["status"] == "degraded"
    assert verdict["verified"] is False
    assert {
        "VIDEO_NOT_GENERATED",
        "VIDEO_POLICY_REJECTED",
        "VIDEO_FALLBACK_EXECUTED",
    } <= _codes(verdict)
    video_receipt = verdict["shots"][0]["video_receipt"]
    assert video_receipt == {
        "status": "policy_rejected",
        "provider": "openrouter",
        "model": "bytedance/seedance-2.0",
        "reason": "provider_policy_rejected",
        "policy_code": "InputImageSensitiveContentDetected.PrivacyInformation",
    }
    serialized = json.dumps(verdict)
    assert "req-must-not-survive" not in serialized
    assert "signed.example" not in serialized
    assert "token=secret" not in serialized
    assert verdict["may_have_been_billed"] is False
    assert verdict["paid_submission_status_unknown_assets"] == []
    assert verdict["billing_guidance"] == ""
    assert "PAID_SUBMISSION_STATUS_UNKNOWN" not in _codes(verdict)


def test_missing_ffprobe_still_returns_complete_billing_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_fixture(tmp_path)
    (tmp_path / "1_shot.mp4.receipt.json").unlink()
    monkeypatch.setattr(audit.shutil, "which", lambda _name: None)
    dispositions, proofs = _current_run_evidence(tmp_path)
    monkeypatch.setattr(
        audit.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "fallback_outputs": {"1": "wrote fallback"},
                    "paid_submission_dispositions": dispositions,
                    "paid_submission_receipt_proofs": proofs,
                }
            )
        ),
    )

    assert audit.main(["--run-dir", str(tmp_path), "--ffprobe", "missing-ffprobe-test"]) == 0

    verdict = json.loads(capsys.readouterr().out)
    assert verdict["status"] == "blocked"
    assert verdict["may_have_been_billed"] is True
    assert verdict["paid_submission_status_unknown_assets"] == ["shot1_video"]
    assert {"FFPROBE_MISSING", "PAID_SUBMISSION_STATUS_UNKNOWN"} <= _codes(verdict)


def test_policy_rejection_flows_once_through_declared_fallback_into_degraded_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Offline workflow boundary: Seedance reject -> fallback -> delivery gate."""

    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    loader.invalidate_cache()
    meta = loader.get_by_name("meta-short-drama")
    assert meta is not None and meta.composition_raw is not None
    steps = {step["id"]: step for step in meta.composition_raw["steps"]}
    assert steps["shot1_video"]["on_failure"] == "shot1_video_fallback"
    assert steps["shot1_video_fallback"]["with"]["output_path"].endswith(
        "/1_shot.mp4"
    )

    run_dir = tmp_path / "run-policy"
    run_dir.mkdir()
    output = run_dir / "1_shot.mp4"
    video = _load_video_module()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-offline-test-only")
    calls: list[str] = []

    def reject_once(method: str, *args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        calls.append(method)
        return {
            "error": {
                "code": 400,
                "message": (
                    'HTTP 400: {"error":{"code":'
                    '"InputImageSensitiveContentDetected.PrivacyInformation",'
                    '"request_id":"req-private",'
                    '"url":"https://signed.example/video?token=private#fragment"}}'
                ),
            }
        }

    monkeypatch.setattr(video, "_http_request", reject_once)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_video.py",
            "--prompt",
            "clearly fictional synthetic character",
            "--filename",
            str(output),
            "--duration",
            "3",
            "--max-retries",
            "2",
        ],
    )

    assert video.main() == 1
    assert calls == ["POST"]
    provider_stderr = capsys.readouterr().err
    assert "req-private" not in provider_stderr
    assert "signed.example" not in provider_stderr
    assert "token=private" not in provider_stderr

    # This is the declared on_failure output: a playable local substitute at
    # the exact path the failed provider step would have produced.
    output.write_bytes(b"synthetic-fallback-video")
    run_dir.joinpath("final_subtitled.mp4").write_bytes(b"synthetic-final-video")
    run_dir.joinpath("script.txt").write_text(_script(3), encoding="utf-8")
    _write_json(run_dir / "reference.png.receipt.json", _image_receipt("gen-reference"))
    _write_json(run_dir / "1_shot.png.receipt.json", _image_receipt("gen-shot"))

    def fake_probe(path: Path, _ffprobe: str) -> dict[str, object]:
        duration = 7.0 if path.name == "final_subtitled.mp4" else 3.0
        return {"decodable": True, "duration_s": duration}

    dispositions, proofs = _current_run_evidence(run_dir)
    dispositions["shot1_video"] = "maybe_accepted"
    verdict = audit.audit_delivery(
        run_dir,
        {"1": "wrote fallback 1_shot.mp4"},
        paid_submission_dispositions=dispositions,
        paid_submission_receipt_proofs=proofs,
        probe=fake_probe,
    )

    assert verdict["status"] == "degraded"
    assert verdict["verified"] is False
    assert {
        "VIDEO_NOT_GENERATED",
        "VIDEO_POLICY_REJECTED",
        "VIDEO_FALLBACK_EXECUTED",
    } <= _codes(verdict)
    receipt = verdict["shots"][0]["video_receipt"]
    assert receipt["reason"] == "provider_policy_rejected"
    assert receipt["policy_code"] == (
        "InputImageSensitiveContentDetected.PrivacyInformation"
    )
    serialized = json.dumps(verdict)
    assert "req-private" not in serialized
    assert "signed.example" not in serialized
    assert "token=private" not in serialized


def test_video_receipt_without_real_provider_job_id_is_degraded(tmp_path: Path) -> None:
    verdict = _run_fixture(
        tmp_path,
        video_overrides={
            "status": "generated",
            "fallback": False,
            "provider": "local",
            "model": "bytedance/seedance-2.0",
            "job_id": "",
        },
    )

    assert verdict["verified"] is False
    assert {"VIDEO_PROVIDER_NOT_REAL", "VIDEO_PROVIDER_ID_MISSING"} <= _codes(verdict)


def test_three_second_content_is_not_reported_as_three_second_final(tmp_path: Path) -> None:
    verdict = _run_fixture(tmp_path, durations=(3,), final_duration=3.0)

    assert verdict["content_duration_s"] == 3
    assert verdict["expected_final_duration_s"] == 7.0
    assert verdict["final_duration_s"] == 3.0
    assert verdict["status"] == "degraded"
    assert "FINAL_DURATION_MISMATCH" in _codes(verdict)


def test_unprobeable_final_video_blocks_delivery_verification(tmp_path: Path) -> None:
    tmp_path.joinpath("script.txt").write_text(_script(3), encoding="utf-8")
    _write_json(tmp_path / "reference.png.receipt.json", _image_receipt())
    _write_json(tmp_path / "1_shot.png.receipt.json", _image_receipt())
    _write_json(tmp_path / "1_shot.mp4.receipt.json", _video_receipt())
    tmp_path.joinpath("1_shot.mp4").write_bytes(b"video")
    tmp_path.joinpath("final_subtitled.mp4").write_bytes(b"broken")

    def selective_probe(path: Path, _ffprobe: str) -> dict[str, object]:
        if path.name == "final_subtitled.mp4":
            raise RuntimeError("corrupt")
        return {"decodable": True, "duration_s": 3.0}

    dispositions, proofs = _current_run_evidence(tmp_path)
    verdict = audit.audit_delivery(
        tmp_path,
        {"1": ""},
        paid_submission_dispositions=dispositions,
        paid_submission_receipt_proofs=proofs,
        probe=selective_probe,
    )

    assert verdict["status"] == "blocked"
    assert verdict["verified"] is False
    assert verdict["final_video_decodable"] is False
    assert "VIDEO_NOT_DECODABLE" in _codes(verdict)
