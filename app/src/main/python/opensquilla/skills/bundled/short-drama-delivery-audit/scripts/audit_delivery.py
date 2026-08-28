#!/usr/bin/env python3
"""Deterministically audit a meta-short-drama delivery.

The final delivery model is intentionally not trusted to infer whether paid
media APIs ran.  This helper reads the canonical script and sanitized receipt
files, incorporates the orchestrator's fallback-step evidence, and probes the
actual MP4 files before emitting one bounded JSON verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

MAX_SHOTS = 10
BOOKEND_DURATION_S = 4.0
SHOT_DURATION_TOLERANCE_S = 0.75
FINAL_DURATION_TOLERANCE_S = 0.75
_SHOT_HEADER_RE = re.compile(r"(?m)^=== SHOT_(\d+) ===\s*$")
_INTEGER_FIELD_RE = re.compile(r"(?m)^([A-Z_]+):\s*(\d+)\s*$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,159}$")
_SAFE_POLICY_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_MACHINE_STEP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_MACHINE_RECEIPT_PROOF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_MACHINE_DISPOSITION_BYTES = 8_000
_MAX_MACHINE_DISPOSITION_STEPS = 64
_MAX_RECEIPT_BYTES = 64_000
_PAID_SUBMISSION_DISPOSITIONS = frozenset(
    {"safe_no_submit", "maybe_accepted", "receipt"}
)

Probe = Callable[[Path, str], dict[str, Any]]


def _issue(code: str, asset: str, detail: str) -> dict[str, str]:
    return {"code": code, "asset": asset, "detail": detail}


def _normalize_paid_submission_dispositions(
    raw: object,
) -> dict[str, str]:
    """Accept only the bounded scheduler-owned disposition vocabulary."""

    value = raw
    if isinstance(value, str):
        if len(value.encode("utf-8", errors="ignore")) > _MAX_MACHINE_DISPOSITION_BYTES:
            return {}
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, Mapping):
        return {}

    sanitized: dict[str, str] = {}
    for step_id in sorted(value, key=str):
        if len(sanitized) >= _MAX_MACHINE_DISPOSITION_STEPS:
            break
        disposition = value[step_id]
        if (
            isinstance(step_id, str)
            and _MACHINE_STEP_ID_RE.fullmatch(step_id)
            and isinstance(disposition, str)
            and disposition in _PAID_SUBMISSION_DISPOSITIONS
        ):
            sanitized[step_id] = disposition
    return sanitized


def _normalize_paid_submission_receipt_proofs(raw: object) -> dict[str, str]:
    """Accept only bounded canonical digests from the parent executor."""

    value = raw
    if isinstance(value, str):
        if len(value.encode("utf-8", errors="ignore")) > _MAX_MACHINE_DISPOSITION_BYTES:
            return {}
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, Mapping):
        return {}

    sanitized: dict[str, str] = {}
    for step_id in sorted(value, key=str):
        if len(sanitized) >= _MAX_MACHINE_DISPOSITION_STEPS:
            break
        proof = value[step_id]
        if (
            isinstance(step_id, str)
            and _MACHINE_STEP_ID_RE.fullmatch(step_id)
            and isinstance(proof, str)
            and _MACHINE_RECEIPT_PROOF_RE.fullmatch(proof)
        ):
            sanitized[step_id] = proof
    return sanitized


def _canonical_receipt_proof(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    if len(encoded) > _MAX_RECEIPT_BYTES:
        return None
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _parse_script(script: str) -> tuple[list[dict[str, int]], int | None, list[dict[str, str]]]:
    """Return active shots, the overview duration, and structural issues."""

    issues: list[dict[str, str]] = []
    headers = list(_SHOT_HEADER_RE.finditer(script))
    shots: list[dict[str, int]] = []
    seen: set[int] = set()
    for index, header in enumerate(headers):
        number = int(header.group(1))
        end = headers[index + 1].start() if index + 1 < len(headers) else len(script)
        block = script[header.end() : end]
        duration_match = re.search(r"(?m)^DURATION_S:\s*(\d+)\s*$", block)
        if number in seen or number < 1 or number > MAX_SHOTS:
            issues.append(
                _issue(
                    "INVALID_SHOT_NUMBER",
                    f"shot{number}",
                    f"shot number must be unique and between 1 and {MAX_SHOTS}",
                )
            )
            continue
        seen.add(number)
        if duration_match is None:
            issues.append(
                _issue(
                    "MISSING_SHOT_DURATION",
                    f"shot{number}",
                    "script shot has no integer DURATION_S",
                )
            )
            continue
        shots.append({"number": number, "duration_s": int(duration_match.group(1))})

    shots.sort(key=lambda item: item["number"])
    actual_numbers = [item["number"] for item in shots]
    expected_numbers = list(range(1, len(shots) + 1))
    if not shots:
        issues.append(_issue("NO_ACTIVE_SHOTS", "script.txt", "no SHOT_N blocks found"))
    elif actual_numbers != expected_numbers:
        issues.append(
            _issue(
                "NONCONTIGUOUS_SHOTS",
                "script.txt",
                f"active shot numbers are {actual_numbers}, expected {expected_numbers}",
            )
        )

    overview = script.split("=== SHOT_", 1)[0]
    fields = {match.group(1): int(match.group(2)) for match in _INTEGER_FIELD_RE.finditer(overview)}
    overview_duration = fields.get("DURATION_S")
    declared_shots = fields.get("N_SHOTS")
    if declared_shots is None:
        issues.append(_issue("MISSING_N_SHOTS", "script.txt", "OVERVIEW.N_SHOTS is missing"))
    elif declared_shots != len(shots):
        issues.append(
            _issue(
                "SHOT_COUNT_MISMATCH",
                "script.txt",
                f"OVERVIEW.N_SHOTS={declared_shots}, parsed active shots={len(shots)}",
            )
        )

    content_duration = sum(item["duration_s"] for item in shots)
    if overview_duration is None:
        issues.append(
            _issue("MISSING_CONTENT_DURATION", "script.txt", "OVERVIEW.DURATION_S is missing")
        )
    elif abs(overview_duration - content_duration) > 2:
        issues.append(
            _issue(
                "CONTENT_DURATION_MISMATCH",
                "script.txt",
                f"OVERVIEW.DURATION_S={overview_duration}, shot sum={content_duration}",
            )
        )
    return shots, overview_duration, issues


def _load_receipt(
    path: Path,
    *,
    asset: str,
) -> tuple[dict[str, Any] | None, str | None, dict[str, str] | None]:
    if not path.is_file():
        return None, None, _issue("MISSING_RECEIPT", asset, f"missing {path.name}")
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_RECEIPT_BYTES:
            raise ValueError("receipt exceeds size limit")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None, None, _issue("INVALID_RECEIPT", asset, f"invalid JSON in {path.name}")
    if not isinstance(value, dict):
        return None, None, _issue("INVALID_RECEIPT", asset, f"{path.name} is not a JSON object")
    proof = _canonical_receipt_proof(value)
    if proof is None:
        return None, None, _issue("INVALID_RECEIPT", asset, f"invalid data in {path.name}")
    return value, proof, None


def _safe_token(value: object) -> str:
    """Allow only provider identifier syntax into the delivery prompt."""

    text = str(value or "").strip()
    if "://" in text or text.lower().startswith(("sk-", "sk_", "bearer")):
        return ""
    return text if _SAFE_TOKEN_RE.fullmatch(text) else ""


def _safe_policy_code(value: object) -> str:
    text = str(value or "").strip()
    if _SAFE_POLICY_CODE_RE.fullmatch(text) is None:
        return ""
    folded = text.casefold()
    if folded.startswith(
        (
            "sk-",
            "sk_",
            "bearer",
            "req-",
            "req_",
            "request-id-",
            "request_id_",
            "job-",
            "job_",
            "trace-",
            "trace_",
        )
    ):
        return ""
    if not any(
        marker in folded
        for marker in ("policy", "privacy", "sensitive", "moderation", "safety", "filter")
    ):
        return ""
    return text


def _audit_image_receipt(
    path: Path,
    *,
    asset: str,
) -> tuple[dict[str, Any], str | None, list[dict[str, str]]]:
    receipt, receipt_proof, load_issue = _load_receipt(path, asset=asset)
    if load_issue is not None or receipt is None:
        return {}, None, [load_issue] if load_issue is not None else []

    status = _safe_token(receipt.get("status"))
    provider = _safe_token(receipt.get("provider"))
    model = _safe_token(receipt.get("model"))
    reason = _safe_token(receipt.get("reason"))
    policy_code = _safe_policy_code(receipt.get("policy_code"))
    request_id = _safe_token(receipt.get("request_id"))
    placeholder = receipt.get("placeholder")
    sanitized: dict[str, Any] = {
        "status": status,
        "provider": provider,
        "model": model,
        "placeholder": placeholder if isinstance(placeholder, bool) else None,
    }
    if status == "generated":
        sanitized["request_id"] = request_id
    elif status == "policy_rejected":
        sanitized.update({"reason": reason, "policy_code": policy_code})
    issues: list[dict[str, str]] = []
    if status != "generated":
        issues.append(_issue("IMAGE_NOT_GENERATED", asset, "receipt status is not generated"))
    if status == "policy_rejected":
        if reason == "provider_policy_rejected" and policy_code:
            issues.append(
                _issue(
                    "IMAGE_POLICY_REJECTED",
                    asset,
                    f"upstream image provider rejected generation; policy_code={policy_code}",
                )
            )
        else:
            issues.append(
                _issue(
                    "IMAGE_POLICY_RECEIPT_INVALID",
                    asset,
                    "policy rejection receipt is missing its sanitized reason/code",
                )
            )
    if placeholder is not False:
        issues.append(_issue("IMAGE_PLACEHOLDER", asset, "placeholder is not explicitly false"))
    if not provider or provider.lower() in {"local", "placeholder", "fallback"}:
        issues.append(_issue("IMAGE_PROVIDER_NOT_REAL", asset, "receipt has no real provider"))
    if not model:
        issues.append(_issue("IMAGE_MODEL_MISSING", asset, "receipt model is missing"))
    if status == "generated" and not request_id:
        issues.append(_issue("IMAGE_REQUEST_ID_MISSING", asset, "provider request_id is missing"))
    return sanitized, receipt_proof, issues


def _audit_video_receipt(
    path: Path,
    *,
    asset: str,
) -> tuple[dict[str, str], str | None, list[dict[str, str]]]:
    receipt, receipt_proof, load_issue = _load_receipt(path, asset=asset)
    if load_issue is not None or receipt is None:
        return {}, None, [load_issue] if load_issue is not None else []

    provider = _safe_token(receipt.get("provider"))
    model = _safe_token(receipt.get("model"))
    status = _safe_token(receipt.get("status"))
    reason = _safe_token(receipt.get("reason"))
    policy_code = _safe_policy_code(receipt.get("policy_code"))
    request_id = _safe_token(receipt.get("request_id"))
    job_id = _safe_token(receipt.get("job_id"))
    sanitized = {
        "status": status,
        "provider": provider,
        "model": model,
    }
    if status == "generated":
        sanitized.update({"request_id": request_id, "job_id": job_id})
    elif status == "policy_rejected":
        sanitized.update({"reason": reason, "policy_code": policy_code})
    issues: list[dict[str, str]] = []
    if status != "generated":
        issues.append(_issue("VIDEO_NOT_GENERATED", asset, "receipt status is not generated"))
    if status == "policy_rejected":
        if reason == "provider_policy_rejected" and policy_code:
            issues.append(
                _issue(
                    "VIDEO_POLICY_REJECTED",
                    asset,
                    f"upstream media provider rejected generation; policy_code={policy_code}",
                )
            )
        else:
            issues.append(
                _issue(
                    "VIDEO_POLICY_RECEIPT_INVALID",
                    asset,
                    "policy rejection receipt is missing its sanitized reason/code",
                )
            )
    if receipt.get("fallback") is not False:
        issues.append(_issue("VIDEO_RECEIPT_FALLBACK", asset, "fallback is not explicitly false"))
    if not provider or provider.lower() in {"local", "placeholder", "fallback"}:
        issues.append(_issue("VIDEO_PROVIDER_NOT_REAL", asset, "receipt has no real provider"))
    if not model:
        issues.append(_issue("VIDEO_MODEL_MISSING", asset, "receipt model is missing"))
    if status == "generated" and not request_id and not job_id:
        issues.append(
            _issue("VIDEO_PROVIDER_ID_MISSING", asset, "provider request_id/job_id is missing")
        )
    return sanitized, receipt_proof, issues


def _has_issue(issues: list[dict[str, str]], code: str) -> bool:
    return any(issue.get("code") == code for issue in issues)


def _real_provider(receipt: Mapping[str, object]) -> bool:
    provider = str(receipt.get("provider") or "").strip().lower()
    return bool(provider) and provider not in {"local", "placeholder", "fallback"}


def _image_submission_status_is_conclusive(
    receipt: Mapping[str, object],
) -> bool:
    """Return true only for a complete generated or policy-refused image receipt."""

    status = receipt.get("status")
    if not _real_provider(receipt) or not receipt.get("model"):
        return False
    if status == "generated":
        return receipt.get("placeholder") is False and bool(receipt.get("request_id"))
    if status == "policy_rejected":
        return (
            receipt.get("reason") == "provider_policy_rejected"
            and bool(receipt.get("policy_code"))
        )
    return False


def _video_submission_status_is_conclusive(
    receipt: Mapping[str, object],
    issues: list[dict[str, str]],
) -> bool:
    """Return true only for a complete generated or policy-refused video receipt."""

    status = receipt.get("status")
    if (
        not _real_provider(receipt)
        or not receipt.get("model")
        or _has_issue(issues, "VIDEO_RECEIPT_FALLBACK")
    ):
        return False
    if status == "generated":
        return bool(receipt.get("request_id") or receipt.get("job_id"))
    if status == "policy_rejected":
        return (
            receipt.get("reason") == "provider_policy_rejected"
            and bool(receipt.get("policy_code"))
        )
    return False


def _probe_video(path: Path, ffprobe: str) -> dict[str, Any]:
    completed = subprocess.run(  # noqa: S603 - argv contains only manifest/file values.
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("ffprobe rejected the video")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid JSON") from exc
    streams = payload.get("streams")
    if not isinstance(streams, list) or not any(
        isinstance(stream, dict) and stream.get("codec_type") == "video" for stream in streams
    ):
        raise RuntimeError("video stream is missing")
    duration_values: list[float] = []
    raw_format = payload.get("format")
    if isinstance(raw_format, dict):
        try:
            duration_values.append(float(raw_format.get("duration")))
        except (TypeError, ValueError):
            pass
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        try:
            duration_values.append(float(stream.get("duration")))
        except (TypeError, ValueError):
            pass
    finite = [value for value in duration_values if math.isfinite(value) and value > 0]
    if not finite:
        raise RuntimeError("video duration is missing")
    return {"decodable": True, "duration_s": round(max(finite), 3)}


def _probe_or_issue(
    path: Path,
    *,
    asset: str,
    ffprobe: str,
    probe: Probe,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not path.is_file() or path.stat().st_size <= 0:
        return {}, [_issue("VIDEO_FILE_MISSING", asset, f"missing or empty {path.name}")]
    try:
        result = probe(path, ffprobe)
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
        return {}, [_issue("VIDEO_NOT_DECODABLE", asset, f"ffprobe failed for {path.name}")]
    duration = result.get("duration_s")
    if result.get("decodable") is not True or not isinstance(duration, int | float):
        return {}, [_issue("VIDEO_NOT_DECODABLE", asset, f"ffprobe rejected {path.name}")]
    return {"decodable": True, "duration_s": round(float(duration), 3)}, []


def audit_delivery(
    run_dir: Path,
    fallback_outputs: Mapping[str, object],
    *,
    paid_submission_dispositions: Mapping[str, object] | str | None = None,
    paid_submission_receipt_proofs: Mapping[str, object] | str | None = None,
    ffprobe: str = "ffprobe",
    probe: Probe = _probe_video,
) -> dict[str, Any]:
    """Return a machine-owned delivery verdict with no secret-bearing fields."""

    run_dir = run_dir.resolve()
    issues: list[dict[str, str]] = []
    paid_submission_status_unknown_assets: list[str] = []
    paid_asset_dispositions: dict[str, str] = {}
    unexpected_paid_assets: list[str] = []
    machine_dispositions = _normalize_paid_submission_dispositions(
        paid_submission_dispositions
    )
    machine_receipt_proofs = _normalize_paid_submission_receipt_proofs(
        paid_submission_receipt_proofs
    )

    def record_unknown_paid_submission(asset: str) -> None:
        if asset in paid_submission_status_unknown_assets:
            return
        paid_submission_status_unknown_assets.append(asset)
        issues.append(
            _issue(
                "PAID_SUBMISSION_STATUS_UNKNOWN",
                asset,
                (
                    "provider acceptance and billing cannot be proven; check provider "
                    "history before starting a replacement generation"
                ),
            )
        )

    def record_paid_submission_disposition(
        *,
        asset: str,
        step_id: str,
        conclusive_receipt: bool,
        receipt_proof: str | None,
    ) -> None:
        machine_disposition = machine_dispositions.get(step_id, "unknown")
        proof_matches_current_run = (
            conclusive_receipt
            and receipt_proof is not None
            and machine_receipt_proofs.get(step_id) == receipt_proof
            and machine_disposition in {"receipt", "maybe_accepted"}
        )
        # A sidecar is workspace evidence and can outlive a failed attempt.
        # Upgrade only when its digest is carried by this run's exact bundled
        # paid subprocess. Otherwise preserve the scheduler's billing state.
        disposition = "confirmed" if proof_matches_current_run else machine_disposition
        paid_asset_dispositions[asset] = disposition
        if conclusive_receipt and not proof_matches_current_run:
            issues.append(
                _issue(
                    "RECEIPT_NOT_PROVEN_CURRENT_RUN",
                    asset,
                    "receipt sidecar is not bound to this paid subprocess execution",
                )
            )
        if not proof_matches_current_run and disposition != "safe_no_submit":
            record_unknown_paid_submission(asset)

    def record_unexpected_paid_asset(asset: str) -> None:
        if asset in unexpected_paid_assets:
            return
        unexpected_paid_assets.append(asset)
        issues.append(
            _issue(
                "UNEXPECTED_PAID_ASSET",
                asset,
                "paid-media evidence exists for a shot absent from the canonical script",
            )
        )
    try:
        script = (run_dir / "script.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        script = ""
        issues.append(_issue("SCRIPT_UNREADABLE", "script.txt", "script.txt is unreadable"))

    shots, overview_duration, script_issues = _parse_script(script)
    issues.extend(script_issues)
    content_duration = sum(item["duration_s"] for item in shots)
    expected_final_duration = content_duration + BOOKEND_DURATION_S

    reference_receipt, reference_proof, reference_issues = _audit_image_receipt(
        run_dir / "reference.png.receipt.json",
        asset="reference_image",
    )
    issues.extend(reference_issues)
    record_paid_submission_disposition(
        asset="reference_image",
        step_id="reference_image",
        conclusive_receipt=_image_submission_status_is_conclusive(reference_receipt),
        receipt_proof=reference_proof,
    )

    audited_shots: list[dict[str, Any]] = []
    for shot in shots:
        number = shot["number"]
        asset = f"shot{number}"
        image_receipt, image_proof, image_issues = _audit_image_receipt(
            run_dir / f"{number}_shot.png.receipt.json",
            asset=f"{asset}_image",
        )
        video_receipt, video_proof, video_issues = _audit_video_receipt(
            run_dir / f"{number}_shot.mp4.receipt.json",
            asset=f"{asset}_video",
        )
        probe_result, probe_issues = _probe_or_issue(
            run_dir / f"{number}_shot.mp4",
            asset=f"{asset}_video",
            ffprobe=ffprobe,
            probe=probe,
        )
        issues.extend(image_issues)
        issues.extend(video_issues)
        issues.extend(probe_issues)

        record_paid_submission_disposition(
            asset=f"{asset}_image",
            step_id=f"shot{number}_image",
            conclusive_receipt=_image_submission_status_is_conclusive(image_receipt),
            receipt_proof=image_proof,
        )

        record_paid_submission_disposition(
            asset=f"{asset}_video",
            step_id=f"shot{number}_video",
            conclusive_receipt=_video_submission_status_is_conclusive(
                video_receipt,
                video_issues,
            ),
            receipt_proof=video_proof,
        )

        fallback_executed = bool(str(fallback_outputs.get(str(number)) or "").strip())
        if fallback_executed:
            issues.append(
                _issue(
                    "VIDEO_FALLBACK_EXECUTED",
                    f"{asset}_video",
                    "runtime fallback step produced output and replaced the provider clip",
                )
            )
        if probe_result:
            actual_duration = float(probe_result["duration_s"])
            expected_duration = float(shot["duration_s"])
            if abs(actual_duration - expected_duration) > SHOT_DURATION_TOLERANCE_S:
                issues.append(
                    _issue(
                        "SHOT_DURATION_MISMATCH",
                        f"{asset}_video",
                        f"actual={actual_duration:.3f}s, expected={expected_duration:.3f}s",
                    )
                )

        audited_shots.append(
            {
                "number": number,
                "expected_duration_s": shot["duration_s"],
                "actual_duration_s": probe_result.get("duration_s"),
                "decodable": probe_result.get("decodable", False),
                "fallback_executed": fallback_executed,
                "image_submission_disposition": paid_asset_dispositions.get(
                    f"{asset}_image",
                    "unknown",
                ),
                "video_submission_disposition": paid_asset_dispositions.get(
                    f"{asset}_video",
                    "unknown",
                ),
                "image_receipt": image_receipt,
                "video_receipt": video_receipt,
            }
        )

    active_numbers = {item["number"] for item in shots}
    for number in range(1, MAX_SHOTS + 1):
        if number in active_numbers:
            continue
        asset = f"shot{number}"
        image_path = run_dir / f"{number}_shot.png"
        image_receipt_path = run_dir / f"{number}_shot.png.receipt.json"
        if image_path.is_file() or image_receipt_path.is_file():
            image_receipt, image_proof, image_issues = _audit_image_receipt(
                image_receipt_path,
                asset=f"{asset}_image",
            )
            issues.extend(image_issues)
            record_unexpected_paid_asset(f"{asset}_image")
            record_paid_submission_disposition(
                asset=f"{asset}_image",
                step_id=f"shot{number}_image",
                conclusive_receipt=_image_submission_status_is_conclusive(image_receipt),
                receipt_proof=image_proof,
            )

        video_path = run_dir / f"{number}_shot.mp4"
        video_receipt_path = run_dir / f"{number}_shot.mp4.receipt.json"
        fallback_executed = bool(str(fallback_outputs.get(str(number)) or "").strip())
        if video_path.is_file() or video_receipt_path.is_file() or fallback_executed:
            video_receipt, video_proof, video_issues = _audit_video_receipt(
                video_receipt_path,
                asset=f"{asset}_video",
            )
            issues.extend(video_issues)
            record_unexpected_paid_asset(f"{asset}_video")
            if fallback_executed:
                issues.append(
                    _issue(
                        "VIDEO_FALLBACK_EXECUTED",
                        f"{asset}_video",
                        "runtime fallback step produced output and replaced the provider clip",
                    )
                )
            record_paid_submission_disposition(
                asset=f"{asset}_video",
                step_id=f"shot{number}_video",
                conclusive_receipt=_video_submission_status_is_conclusive(
                    video_receipt,
                    video_issues,
                ),
                receipt_proof=video_proof,
            )

    final_probe, final_probe_issues = _probe_or_issue(
        run_dir / "final_subtitled.mp4",
        asset="final_video",
        ffprobe=ffprobe,
        probe=probe,
    )
    issues.extend(final_probe_issues)
    final_duration = final_probe.get("duration_s")
    if isinstance(final_duration, int | float) and shots:
        if abs(float(final_duration) - expected_final_duration) > FINAL_DURATION_TOLERANCE_S:
            issues.append(
                _issue(
                    "FINAL_DURATION_MISMATCH",
                    "final_video",
                    (
                        f"actual={float(final_duration):.3f}s, expected approximately "
                        f"content {content_duration:.3f}s + bookends {BOOKEND_DURATION_S:.3f}s"
                    ),
                )
            )

    blocking_codes = {
        "SCRIPT_UNREADABLE",
        "NO_ACTIVE_SHOTS",
        "INVALID_SHOT_NUMBER",
        "MISSING_SHOT_DURATION",
        "VIDEO_FILE_MISSING",
        "VIDEO_NOT_DECODABLE",
    }
    status = "verified" if not issues else "blocked" if any(
        item["code"] in blocking_codes for item in issues
    ) else "degraded"
    verified = status == "verified"
    provenance = (
        "verified real API E2E"
        if verified
        else f"{status} — not a verified real API E2E"
    )
    return {
        "verdict_version": 1,
        "status": status,
        "verified": verified,
        "media_provenance": provenance,
        "active_shots": [item["number"] for item in shots],
        "overview_content_duration_s": overview_duration,
        "content_duration_s": content_duration,
        "bookend_duration_s": BOOKEND_DURATION_S,
        "expected_final_duration_s": expected_final_duration,
        "final_duration_s": final_duration,
        "duration_tolerance_s": FINAL_DURATION_TOLERANCE_S,
        "final_video_decodable": final_probe.get("decodable", False),
        "reference_image_receipt": reference_receipt,
        "shots": audited_shots,
        "unexpected_paid_assets": unexpected_paid_assets,
        "paid_submission_dispositions": paid_asset_dispositions,
        "safe_no_submit_assets": [
            asset
            for asset, disposition in paid_asset_dispositions.items()
            if disposition == "safe_no_submit"
        ],
        "paid_submission_status_unknown_assets": paid_submission_status_unknown_assets,
        "may_have_been_billed": bool(paid_submission_status_unknown_assets),
        "billing_guidance": (
            "Check provider history before starting a replacement generation."
            if paid_submission_status_unknown_assets
            else ""
        ),
        "issues": issues,
    }


def _parse_runtime_stdin() -> tuple[
    dict[str, object],
    dict[str, str],
    dict[str, str],
]:
    try:
        payload = json.load(sys.stdin)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, {}, {}
    if not isinstance(payload, dict):
        return {}, {}, {}
    raw_fallbacks = payload.get("fallback_outputs")
    fallbacks = raw_fallbacks if isinstance(raw_fallbacks, dict) else {}
    dispositions = _normalize_paid_submission_dispositions(
        payload.get("paid_submission_dispositions")
    )
    receipt_proofs = _normalize_paid_submission_receipt_proofs(
        payload.get("paid_submission_receipt_proofs")
    )
    return fallbacks, dispositions, receipt_proofs


def _unavailable_probe(_path: Path, _ffprobe: str) -> dict[str, Any]:
    raise RuntimeError("ffprobe executable is unavailable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit short-drama media provenance")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args(argv)

    ffprobe_missing = shutil.which(args.ffprobe) is None and not Path(args.ffprobe).is_file()
    (
        fallback_outputs,
        paid_submission_dispositions,
        paid_submission_receipt_proofs,
    ) = _parse_runtime_stdin()
    verdict = audit_delivery(
        Path(args.run_dir),
        fallback_outputs,
        paid_submission_dispositions=paid_submission_dispositions,
        paid_submission_receipt_proofs=paid_submission_receipt_proofs,
        ffprobe=args.ffprobe,
        probe=_unavailable_probe if ffprobe_missing else _probe_video,
    )
    if ffprobe_missing:
        verdict["status"] = "blocked"
        verdict["verified"] = False
        verdict["media_provenance"] = "blocked — not a verified real API E2E"
        verdict["issues"].insert(
            0,
            _issue("FFPROBE_MISSING", "runtime", "ffprobe executable is unavailable"),
        )
    print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
