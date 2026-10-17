"""Bind three independent installed NSIS checks to one candidate and full execution."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

PROBES = ("dependency-audit", "gateway-smoke", "first-interaction")
STAGES = ("installer-identity", "install", "installed-identity", "check", "uninstall")
IDENTITY_KEYS = ("GITHUB_SHA", "GITHUB_WORKFLOW_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")
HASH_FIELDS = (
    "installerSha256", "executableSha256", "asarSha256", "gatewaySha256",
    "dependencyInventorySha256",
)


def current_identity() -> dict[str, str]:
    return {key: os.environ.get(key, "") for key in IDENTITY_KEYS}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _first_send_report(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    decoder = json.JSONDecoder()
    reports = []
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("reportType") == "packaged-first-send":
            reports.append(value)
    if len(reports) != 1:
        raise ValueError("expected one complete packaged first-send report")
    report = reports[0]
    if (
        report.get("schemaVersion") != 2 or report.get("ok") is not True
        or report.get("iterations") != 20
        or report.get("viewports") != {"wide": 10, "tight": 10}
        or report.get("rpc") != {"chatSend": 40, "uniqueSessions": 20}
        or report.get("provider", {}).get("chatRequestCount") != 40
        or report.get("externalRendererRequests") != 0
        or report.get("acceptance", {}).get("cleanupSucceeded") is not True
        or report.get("acceptance", {}).get("failures") != []
    ):
        raise ValueError("packaged first-send did not complete all 20 iterations and cleanup")
    return report


def _check_mode_evidence(directory: Path, proof: dict[str, Any]) -> None:
    probe, candidate = proof["probe"], proof["candidate"]
    if probe == "dependency-audit":
        audit = read_json(directory / "dependency-audit" / "summary.json")
        if (
            audit.get("schema_version") != 1 or audit.get("mode") != "artifact"
            or audit.get("exit_code") != 0 or audit.get("errors") != []
            or audit.get("commit") != candidate["sourceSha"]
            or audit.get("inputs", {}).get("python_inventory")
            != candidate["dependencyInventorySha256"]
            or set(audit.get("results", {})) != {"python"}
            or audit["results"]["python"].get("vulnerability_count") != 0
        ):
            raise ValueError(
                "frozen dependency audit is incomplete or differs from installed bytes"
            )
    elif probe == "gateway-smoke":
        lines = (directory / "gateway-smoke.log").read_text(encoding="utf-8-sig").splitlines()
        for marker in (
            "OpenSquilla packaged gateway smoke passed.",
            "Packaged Safe execution passed with windows_default; "
            "write and network denials verified.",
        ):
            if lines.count(marker) != 1:
                raise ValueError("packaged Gateway/Safe smoke did not complete exactly once")
    else:
        _first_send_report(directory / "first-send.log")


def verify_reports(
    reports_dir: Path, *, expected_identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    paths = sorted(reports_dir.rglob("packaged-proof.json"))
    if len(paths) != len(PROBES):
        raise ValueError("expected exactly three installed packaged proofs; rerun all three cells")
    reports = [read_json(path) for path in paths]
    if sorted(report["probe"] for report in reports) != sorted(PROBES):
        raise ValueError("missing, unexpected, or duplicate packaged probe")
    identity, candidate = reports[0]["identity"], reports[0]["candidate"]
    if (
        not isinstance(identity, dict) or set(identity) != set(IDENTITY_KEYS)
        or any(not re.fullmatch(r"[0-9a-f]{40}", identity.get(key, ""))
               for key in IDENTITY_KEYS[:2])
        or any(not re.fullmatch(r"[1-9][0-9]*", identity.get(key, ""))
               for key in IDENTITY_KEYS[2:])
        or (expected_identity is not None and identity != expected_identity)
    ):
        raise ValueError("packaged proof source/run/attempt identity mismatch")
    if (
        candidate.get("sourceSha") != identity["GITHUB_SHA"]
        or candidate.get("workflowSha") != identity["GITHUB_WORKFLOW_SHA"]
        or str(candidate.get("auditRunId")) != identity["GITHUB_RUN_ID"]
        or not re.fullmatch(r"[1-9][0-9]*", str(candidate.get("runAttempt", "")))
        or not 1 <= int(candidate["runAttempt"]) <= int(identity["GITHUB_RUN_ATTEMPT"])
        or candidate.get("signing") != "unsigned regression only"
        or any(not re.fullmatch(r"[0-9a-f]{64}", candidate.get(key, ""))
               for key in HASH_FIELDS)
    ):
        raise ValueError("packaged proof does not identify this run's immutable candidate")
    for path, report in zip(paths, reports, strict=True):
        if (
            report.get("schema_version") != 1 or report.get("success") is not True
            or report.get("identity") != identity or report.get("candidate") != candidate
            or report.get("installedIdentity") != candidate
            or report.get("runner") != {"os": "Windows", "environment": "github-hosted"}
        ):
            raise ValueError("installed packaged proof is incomplete or uses a different candidate")
        stages = report.get("stages", [])
        if [stage["stage"] for stage in stages] != list(STAGES):
            raise ValueError("installed packaged stages are missing, duplicated, or reordered")
        previous = None
        for stage in stages:
            start, end = (datetime.fromisoformat(stage[key])
                          for key in ("startedAt", "finishedAt"))
            if (
                stage.get("status") != "passed" or start.tzinfo is None or end.tzinfo is None
                or end < start or (previous is not None and start < previous)
            ):
                raise ValueError("installed packaged stage failed or has invalid execution times")
            previous = end
        _check_mode_evidence(path.parent, report)
    return {"coverage_complete": True, "probes": list(PROBES), "identity": identity,
            "candidate": candidate, "checks": len(PROBES)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    wait = commands.add_parser("wait")
    wait.add_argument("--destination", required=True, type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("--reports-dir", required=True, type=Path)
    verify.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "wait":
            from wait_ci_candidate import wait_for_candidate

            wait_for_candidate(
                "nsis-regression-installer", args.destination, os.environ["GITHUB_SHA"]
            )
        else:
            result = verify_reports(args.reports_dir, expected_identity=current_identity())
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(result, sort_keys=True))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"Installed packaged proof failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
