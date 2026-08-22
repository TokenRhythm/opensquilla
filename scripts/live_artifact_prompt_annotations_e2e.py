#!/usr/bin/env python3
"""Fail-closed live certification harness for prompt-driven HTML edits.

The worker owns an isolated Gateway and an authenticated, typed Desktop bridge
fixture.  It exercises the real PromptAnnotation RPC ingress and shared turn
runtime; the bridge fixture certifies only the Gateway/provider path and does
not replace the separate real-Electron E2E gate.

The worker receives the rotated credential only as ``TOKENRHYTHM_API_KEY``.
Prompts, responses, annotation bodies, runtime identifiers, paths, bridge
tokens, and raw traces remain inside its 0700 temporary tree and are never part
of the 0600 public report.  Certification covers the three V1 zero-call
preflights plus the Direct, Router, and Ensemble mutation matrix.  Product
feature defaults are recorded as release evidence but do not change the live
provider result.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from opensquilla.artifact_session.html_anchors import (  # noqa: E402
    canonical_selection_proofs,
)
from opensquilla.artifacts import ArtifactStore  # noqa: E402
from opensquilla.gateway_client import GatewayRPCClient, GatewayRPCError  # noqa: E402
from opensquilla.subprocess_encoding import apply_utf8_child_env  # noqa: E402
from scripts.live_harness_security import (  # noqa: E402
    child_environment,
    classify_failure,
    is_temporary_report_path,
    provider_secret_names,
    registry_endpoint,
    report_contains_secret,
    sanitize_report,
    scan_and_remove_temporary_tree,
    write_safe_report,
)
from scripts.smoke_v4_phase3_router import (  # noqa: E402
    _free_port,
    _read_turn_call_records,
    _stop_gateway,
    _wait_for_gateway_health,
)

PROVIDER_ID = "tokenrhythm"
KEY_ENV = "TOKENRHYTHM_API_KEY"
BASE_URL_ENV = "TOKENRHYTHM_BASE_URL"
DESKTOP_BRIDGE_URL_ENV = "OPENSQUILLA_DESKTOP_ARTIFACT_BRIDGE_URL"
DESKTOP_BRIDGE_TOKEN_ENV = "OPENSQUILLA_DESKTOP_ARTIFACT_BRIDGE_TOKEN"
DESKTOP_BRIDGE_VERSION = 3
DESKTOP_BRIDGE_V4_VERSION = 4

DIRECT_MODEL = "glm-5.2"
ROUTER_MODELS = {
    "c0": "deepseek-v4-flash",
    "c1": "deepseek-v4-pro",
    "c2": "kimi-k2.7-code",
    "c3": "glm-5.2",
}

_FIXTURE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
.btn-primary { background: #2563eb; color: white; }
.btn-outline { border: 1px solid #64748b; color: #334155; }
</style></head><body><main>
<h1 id="artifact-title">Annotation live fixture</h1>
<button id="btn-confirm" class="btn-primary">Confirm</button>
<button id="btn-reset" class="btn-outline">Reset</button>
</main></body></html>"""
_TITLE_TEXT = "PromptAnnotation applied"
_SINGLE_ANNOTATION_BODY = (
    "Set only this selected button's inline background-color to #ef4444. "
    "Preserve its id, class list, text, and every other element."
)
_SOURCE_PATCH_INSERTION = '<span id="reset-status" role="status">Ready</span>'
_SOURCE_PATCH_ANNOTATION_BODY = (
    "Exercise the autonomous candidate-repair loop using separate model iterations. First "
    "inspect the bound Document, then read its source. Stage a provisional source patch that "
    "inserts exactly "
    f"{_SOURCE_PATCH_INSERTION!r} immediately after this selected button. Inspect the candidate "
    "preview and capture a screenshot before repairing that provisional span to include "
    "aria-live=\"polite\". Inspect the repaired candidate again, then commit it explicitly. "
    "Preserve the selected button and every other existing byte. Call only one tool per model "
    "response in this certification flow."
)
_TITLE_ANNOTATION_BODY = (
    f"Replace only this selected heading's text with {_TITLE_TEXT!r}. "
    "Preserve its tag and attributes."
)
_TURN_PROMPT = (
    "Apply every attached artifact annotation safely. Verify the bound preview, repair if needed, "
    "then explicitly commit or discard the candidate."
)
_ELEMENT_PATHS = {
    "title": json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "main", 1], ["", "h1", 1]],
        separators=(",", ":"),
    ),
    "reset": json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "main", 1], ["", "button", 2]],
        separators=(",", ":"),
    ),
}

# The six annotation cases use raw provider-request accounting. Preview
# inspection, screenshot/action, repair, explicit finish, and the tools=[]
# finalizer are all physical requests and must never be deducted from the
# matrix budget.
# Each full B5 Ensemble case needs one five-member fusion round,
# then four stateful primary-aggregator continuations that reuse the admitted
# proposer evidence. A single
# controlled scenario retry plus bounded in-place transient retries may consume
# at most sixteen extra requests; one additional full Ensemble tool round is
# five requests. Sixty-four remains an absolute guardrail, not a target.
EXPECTED_PHYSICAL_CALLS = 42
TRANSIENT_RETRY_ALLOWANCE = 16
ENSEMBLE_EXTRA_TOOL_ROUND_ALLOWANCE = 5
WORST_CASE_PHYSICAL_CALLS = 63
HARD_PHYSICAL_CALL_CAP = 64

# A mutation case may legitimately consume its full request/turn deadline, and
# the six provider-backed cases run sequentially.  Keep that deadline separate
# from the parent process guard so a healthy matrix is not terminated merely
# because its cumulative latency exceeds one case timeout.
DEFAULT_MATRIX_TIMEOUT_SECONDS = 600.0
MIN_MATRIX_TIMEOUT_SECONDS = 300.0
MAX_MATRIX_TIMEOUT_SECONDS = 900.0
DEFAULT_CASE_TIMEOUT_SECONDS = 120.0

_ANNOTATION_TOOLS = (
    "document_apply",
    "document_browser_act",
    "document_browser_inspect",
    "document_browser_reload",
    "document_browser_screenshot",
    "document_finish",
    "document_inspect",
    "document_locate",
    "document_patch",
    "document_read",
)
_REPORT_KEYS = frozenset(
    {
        "schemaVersion",
        "certification",
        "provider",
        "featureDefaultEnabled",
        "featureDefaults",
        "physicalCallBudget",
        "securityChecks",
        "cases",
        "reasonCodes",
    }
)
_BUDGET_KEYS = frozenset(
    {"expected", "retryAllowance", "ensembleExtraAllowance", "worstCase", "hardCap", "observed"}
)
_SECURITY_KEYS = frozenset(
    {
        "isolatedChildEnvironment",
        "registryEndpointPinned",
        "rawPayloadPersistenceDisabled",
        "syntheticBridgeAuthenticated",
        "temporaryTreeMode",
        "reportMode",
    }
)
_CASE_KEYS = frozenset(
    {
        "case",
        "mode",
        "tier",
        "modelSlot",
        "expectedTools",
        "expectedPhysicalCalls",
        "observedPhysicalCalls",
        "providerCalls",
        "loopContinuationCalls",
        "providerCalled",
        "beforeHashVerified",
        "afterHashVerified",
        "singleRevisionVerified",
        "singleChangeSetVerified",
        "acceptedAnnotationsVerified",
        "modeVerified",
        "routerTierVerified",
        "observedTools",
        "writerCalls",
        "writerAttempts",
        "proposerToolCalls",
        "aggregatorToolsVerified",
        "revertVerified",
        "passed",
        "status",
        "reasonCode",
    }
)
_ALLOWED_TOOLS = frozenset(_ANNOTATION_TOOLS)
_WINDOWS_PROCESS_ENV_ALLOWLIST = frozenset(
    {
        "ALLUSERSPROFILE",
        "COMPUTERNAME",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "PROCESSOR_LEVEL",
        "PROCESSOR_REVISION",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "PUBLIC",
        "SYSTEMDRIVE",
        "USERDOMAIN",
        "USERDOMAIN_ROAMINGPROFILE",
        "USERNAME",
    }
)
_ALLOWED_STATUSES = frozenset({"not_run", "passed", "failed"})
_ALLOWED_REASON_CODES = frozenset(
    {
        "artifact_invariant_failed",
        "gateway_setup_failed",
        "live_gateway_executor_failed",
        "none",
        "physical_call_accounting_ambiguous",
        "preflight_rejection_mismatch",
        "provider_projection_failed",
        "routing_evidence_failed",
        "tool_boundary_failed",
    }
)
_FEATURE_DEFAULT_FALSE_RE = {
    "artifactPromptAnnotations": re.compile(r"artifactPromptAnnotations\s*:\s*false\b"),
    "documentWorkbenchResources": re.compile(r"documentWorkbenchResources\s*:\s*false\b"),
}


@dataclass(frozen=True)
class Scenario:
    case: str
    mode: Literal["preflight", "direct", "router", "ensemble"]
    tier: str | None
    model_slot: str
    expected_tools: tuple[str, ...]
    expected_physical_calls: int
    zero_call_preflight: bool = False


SCENARIOS = (
    Scenario("discarded_annotation_zero_call", "preflight", None, "none", (), 0, True),
    Scenario("cross_session_zero_call", "preflight", None, "none", (), 0, True),
    Scenario("dom_mismatch_zero_call", "preflight", None, "none", (), 0, True),
    Scenario("direct_single_annotation", "direct", None, "configured_direct", _ANNOTATION_TOOLS, 9),
    Scenario("direct_double_annotation", "direct", None, "configured_direct", _ANNOTATION_TOOLS, 5),
    Scenario("router_single_annotation", "router", "c2", "router_c2", _ANNOTATION_TOOLS, 5),
    Scenario("router_double_annotation", "router", "c3", "router_c3", _ANNOTATION_TOOLS, 5),
    Scenario("ensemble_single_annotation", "ensemble", None, "static_b5", _ANNOTATION_TOOLS, 9),
    Scenario("ensemble_double_annotation", "ensemble", None, "static_b5", _ANNOTATION_TOOLS, 9),
)


def _expected_writer_name(scenario: Scenario) -> str:
    return "document_patch" if scenario.case == "direct_single_annotation" else "document_apply"


def _expected_writer_calls(scenario: Scenario) -> int:
    """The source-fallback fixture deliberately stages one repair candidate."""

    return 2 if scenario.case == "direct_single_annotation" else 1


def _assert_scenario_plan() -> None:
    cases = [scenario.case for scenario in SCENARIOS]
    if len(cases) != len(set(cases)):
        raise RuntimeError("live certification cases must be unique")
    if sum(scenario.expected_physical_calls for scenario in SCENARIOS) != EXPECTED_PHYSICAL_CALLS:
        raise RuntimeError("live certification expected-call plan changed")
    if (
        EXPECTED_PHYSICAL_CALLS
        + TRANSIENT_RETRY_ALLOWANCE
        + ENSEMBLE_EXTRA_TOOL_ROUND_ALLOWANCE
        != WORST_CASE_PHYSICAL_CALLS
    ):
        raise RuntimeError("live certification worst-case budget changed")
    if WORST_CASE_PHYSICAL_CALLS >= HARD_PHYSICAL_CALL_CAP:
        # Leave at least one request of headroom without allowing it to be used.
        if WORST_CASE_PHYSICAL_CALLS != HARD_PHYSICAL_CALL_CAP - 1:
            raise RuntimeError("live certification hard-call guard changed")


@dataclass
class PhysicalCallBudget:
    """Case-level reservation for every physical provider request.

    A live case is admitted only after its configured maximum has been
    reserved.  Gateway limits then make that reservation a real upper bound;
    raw traces reconcile the number actually started after the turn. Browser,
    finish, and finalizer requests are included in this reservation;
    ``loopContinuationCalls`` is diagnostic evidence only.
    """

    hard_cap: int = HARD_PHYSICAL_CALL_CAP
    baseline_used: int = 0
    retry_used: int = 0
    ensemble_extra_used: int = 0
    baseline_reserved: int = 0
    retry_reserved: int = 0
    ensemble_extra_reserved: int = 0

    def __post_init__(self) -> None:
        if not WORST_CASE_PHYSICAL_CALLS <= self.hard_cap <= HARD_PHYSICAL_CALL_CAP:
            raise ValueError(
                f"physical call cap must be between {WORST_CASE_PHYSICAL_CALLS} "
                f"and {HARD_PHYSICAL_CALL_CAP}"
            )

    @property
    def observed(self) -> int:
        return self.baseline_used + self.retry_used + self.ensemble_extra_used

    def ensure_full_matrix_fits(self) -> None:
        if WORST_CASE_PHYSICAL_CALLS > self.hard_cap:
            raise RuntimeError("insufficient physical-call budget for the remaining matrix")

    @property
    def reserved(self) -> int:
        return self.baseline_reserved + self.retry_reserved + self.ensemble_extra_reserved

    def reserve(
        self,
        kind: Literal["baseline", "retry", "ensemble_extra"],
        count: int,
    ) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("physical call reservation must be a positive integer")
        limits = {
            "baseline": EXPECTED_PHYSICAL_CALLS,
            "retry": TRANSIENT_RETRY_ALLOWANCE,
            "ensemble_extra": ENSEMBLE_EXTRA_TOOL_ROUND_ALLOWANCE,
        }
        field_names = {
            "baseline": "baseline_reserved",
            "retry": "retry_reserved",
            "ensemble_extra": "ensemble_extra_reserved",
        }
        field_name = field_names[kind]
        updated = getattr(self, field_name) + count
        if updated > limits[kind] or self.reserved + count > self.hard_cap:
            raise RuntimeError("physical-call hard cap or reservation allowance exceeded")
        setattr(self, field_name, updated)

    def claim(
        self,
        kind: Literal["baseline", "retry", "ensemble_extra"],
        count: int = 1,
    ) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("physical call count must be a positive integer")
        limits = {
            "baseline": EXPECTED_PHYSICAL_CALLS,
            "retry": TRANSIENT_RETRY_ALLOWANCE,
            "ensemble_extra": ENSEMBLE_EXTRA_TOOL_ROUND_ALLOWANCE,
        }
        field_names = {
            "baseline": "baseline_used",
            "retry": "retry_used",
            "ensemble_extra": "ensemble_extra_used",
        }
        field_name = field_names[kind]
        updated = getattr(self, field_name) + count
        reserved = getattr(self, field_name.replace("_used", "_reserved"))
        if (
            updated > limits[kind]
            or updated > reserved
            or self.observed + count > self.hard_cap
        ):
            raise RuntimeError("physical-call hard cap or allowance exceeded")
        setattr(self, field_name, updated)


@dataclass(frozen=True, slots=True)
class CaseEvidence:
    observed_physical_calls: int = 0
    provider_calls: int = 0
    loop_continuation_calls: int = 0
    provider_called: bool = False
    before_hash_verified: bool = False
    after_hash_verified: bool = False
    single_revision_verified: bool = False
    single_change_set_verified: bool = False
    accepted_annotations_verified: bool = False
    mode_verified: bool = False
    router_tier_verified: bool = False
    observed_tools: tuple[str, ...] = ()
    writer_calls: int = 0
    writer_attempts: int = 0
    proposer_tool_calls: int = 0
    aggregator_tools_verified: bool = False
    revert_verified: bool = False
    passed: bool = False
    status: Literal["not_run", "passed", "failed"] = "failed"
    reason_code: str = "live_gateway_executor_failed"


class CertificationDriver(Protocol):
    async def start(self) -> None: ...

    async def run_case(self, scenario: Scenario) -> CaseEvidence: ...

    async def close(self) -> None: ...


def _feature_defaults() -> dict[str, bool]:
    source = (REPO_ROOT / "opensquilla-webui" / "src" / "stores" / "app.ts").read_text(
        encoding="utf-8"
    )
    # Missing or non-literal defaults are reported as enabled. Record each
    # default independently so the release state remains visible without
    # coupling it to live-provider certification.
    return {
        name: pattern.search(source) is None
        for name, pattern in _FEATURE_DEFAULT_FALSE_RE.items()
    }

def _worker_environment(api_key: str) -> dict[str, str]:
    if not api_key.strip():
        raise ValueError("rotated TokenRhythm key is required")
    env: dict[str, str] = dict(
        child_environment(
            PROVIDER_ID,
            {KEY_ENV: api_key},
            base_environment=os.environ,
        )
    )
    for name in provider_secret_names():
        if name != KEY_ENV and name in env:
            raise RuntimeError("isolated worker received an unrelated provider credential")
    if BASE_URL_ENV in env:
        raise RuntimeError("isolated worker received an endpoint override")
    apply_utf8_child_env(env)
    # Startup diagnostics must reach the private log before a health timeout,
    # including when Windows cannot flush a still-running child process.
    env["PYTHONUNBUFFERED"] = "1"
    if os.name == "nt":
        for name, value in os.environ.items():
            if name.upper() in _WINDOWS_PROCESS_ENV_ALLOWLIST and value:
                env[name] = value
    return env


def _apply_isolated_home_environment(env: dict[str, str], home: Path) -> None:
    """Give an isolated child a portable home without exposing the operator's profile."""

    resolved_home = home.resolve()
    isolated_home = str(resolved_home)
    # pathlib.Path.home() consults HOME on POSIX and USERPROFILE on Windows.
    # Set both explicitly because the least-privilege provider environment
    # intentionally inherits neither from the host.
    env["HOME"] = isolated_home
    env["USERPROFILE"] = isolated_home
    if os.name == "nt":
        roaming = resolved_home / "AppData" / "Roaming"
        local = resolved_home / "AppData" / "Local"
        roaming.mkdir(mode=0o700, parents=True, exist_ok=True)
        local.mkdir(mode=0o700, parents=True, exist_ok=True)
        env["APPDATA"] = str(roaming)
        env["LOCALAPPDATA"] = str(local)
        env["HOMEDRIVE"] = resolved_home.drive
        env["HOMEPATH"] = isolated_home[len(resolved_home.drive) :]
        program_files = env.get("PROGRAMFILES") or env.get("ProgramFiles")
        windows_root = env.get("WINDIR") or env.get("windir")
        module_roots = [
            (
                str(Path(program_files) / "WindowsPowerShell" / "Modules")
                if program_files
                else ""
            ),
            (
                str(
                    Path(windows_root)
                    / "System32"
                    / "WindowsPowerShell"
                    / "v1.0"
                    / "Modules"
                )
                if windows_root
                else ""
            ),
        ]
        env["PSModulePath"] = os.pathsep.join(path for path in module_roots if path)


def _wait_for_router_preload(
    process: subprocess.Popen[str],
    stdout_path: Path,
    *,
    timeout_seconds: float,
) -> str | None:
    """Wait for the owned Gateway's process-local router runtime to be ready."""

    deadline = time.monotonic() + timeout_seconds
    failure_markers = (
        "gateway.squilla_router_preload_failed",
        "gateway.squilla_router_preload_unavailable",
    )
    while time.monotonic() < deadline:
        try:
            output = stdout_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            output = ""
        if "gateway.squilla_router_preloaded" in output:
            return None
        if any(marker in output for marker in failure_markers):
            return "owned Gateway could not preload the router runtime"
        return_code = process.poll()
        if return_code is not None:
            return f"owned Gateway exited before router preload (exit={return_code})"
        time.sleep(0.1)
    return "owned Gateway router preload did not finish before timeout"


def _case_payload(scenario: Scenario, evidence: CaseEvidence | None = None) -> dict[str, Any]:
    observed = evidence if evidence is not None else CaseEvidence(status="not_run")
    provider_calls = observed.provider_calls or observed.observed_physical_calls
    return {
        "case": scenario.case,
        "mode": scenario.mode,
        "tier": scenario.tier,
        "modelSlot": scenario.model_slot,
        "expectedTools": list(scenario.expected_tools),
        "expectedPhysicalCalls": scenario.expected_physical_calls,
        "observedPhysicalCalls": observed.observed_physical_calls,
        "providerCalls": provider_calls,
        "loopContinuationCalls": observed.loop_continuation_calls,
        "providerCalled": observed.provider_called,
        "beforeHashVerified": observed.before_hash_verified,
        "afterHashVerified": observed.after_hash_verified,
        "singleRevisionVerified": observed.single_revision_verified,
        "singleChangeSetVerified": observed.single_change_set_verified,
        "acceptedAnnotationsVerified": observed.accepted_annotations_verified,
        "modeVerified": observed.mode_verified,
        "routerTierVerified": observed.router_tier_verified,
        "observedTools": list(observed.observed_tools),
        "writerCalls": observed.writer_calls,
        "writerAttempts": observed.writer_attempts,
        "proposerToolCalls": observed.proposer_tool_calls,
        "aggregatorToolsVerified": observed.aggregator_tools_verified,
        "revertVerified": observed.revert_verified,
        "passed": observed.passed,
        "status": observed.status,
        "reasonCode": observed.reason_code,
    }


def _report(
    *,
    hard_cap: int,
    evidences: Mapping[str, CaseEvidence] | None = None,
) -> dict[str, Any]:
    _assert_scenario_plan()
    evidence_by_case = dict(evidences or {})
    feature_defaults = _feature_defaults()
    rows = [
        _case_payload(scenario, evidence_by_case.get(scenario.case))
        for scenario in SCENARIOS
    ]
    observed_calls = sum(int(row["observedPhysicalCalls"]) for row in rows)
    reason_codes = sorted(
        {
            str(row["reasonCode"])
            for row in rows
            if row["reasonCode"] != "none"
        }
    )
    complete = bool(
        len(evidence_by_case) == len(SCENARIOS)
        and all(row["passed"] is True for row in rows)
    )
    return {
        "schemaVersion": 1,
        "certification": "complete" if complete else "incomplete",
        "provider": PROVIDER_ID,
        "featureDefaultEnabled": any(feature_defaults.values()),
        "featureDefaults": feature_defaults,
        "physicalCallBudget": {
            "expected": EXPECTED_PHYSICAL_CALLS,
            "retryAllowance": TRANSIENT_RETRY_ALLOWANCE,
            "ensembleExtraAllowance": ENSEMBLE_EXTRA_TOOL_ROUND_ALLOWANCE,
            "worstCase": WORST_CASE_PHYSICAL_CALLS,
            "hardCap": hard_cap,
            "observed": observed_calls,
        },
        "securityChecks": {
            "isolatedChildEnvironment": True,
            "registryEndpointPinned": True,
            "rawPayloadPersistenceDisabled": True,
            "syntheticBridgeAuthenticated": True,
            "temporaryTreeMode": "0700",
            "reportMode": "0600",
        },
        "cases": rows,
        "reasonCodes": reason_codes,
    }


def _incomplete_report(*, hard_cap: int) -> dict[str, Any]:
    """Return the fail-closed pre-execution report used by validation tests."""

    return _report(hard_cap=hard_cap)


async def _run_certification(
    driver: CertificationDriver,
    *,
    hard_cap: int,
) -> dict[str, Any]:
    """Run the finite matrix with case-level call reservations.

    No retry is implicit here.  A future controlled retry must reserve from
    the dedicated allowance before invoking the driver again.
    """

    budget = PhysicalCallBudget(hard_cap=hard_cap)
    budget.ensure_full_matrix_fits()
    evidences: dict[str, CaseEvidence] = {}
    provider_calls_observed = 0
    try:
        await driver.start()
        for scenario in SCENARIOS:
            if scenario.expected_physical_calls:
                budget.reserve("baseline", scenario.expected_physical_calls)
            evidence = await driver.run_case(scenario)
            provider_calls_observed += evidence.provider_calls or evidence.observed_physical_calls
            if provider_calls_observed > hard_cap:
                raise RuntimeError("live certification exceeded its raw provider-call hard cap")
            if evidence.observed_physical_calls > scenario.expected_physical_calls:
                raise RuntimeError("live case exceeded its reserved physical-call budget")
            if evidence.observed_physical_calls:
                budget.claim("baseline", evidence.observed_physical_calls)
            evidences[scenario.case] = evidence
    finally:
        await driver.close()
    report = _report(hard_cap=hard_cap, evidences=evidences)
    if report["physicalCallBudget"]["observed"] != budget.observed:
        raise RuntimeError("live certification call evidence did not reconcile")
    reported_provider_calls = sum(
        int(row.get("providerCalls") or 0) for row in report["cases"]
    )
    if reported_provider_calls > hard_cap:
        raise RuntimeError("live certification raw provider-call evidence exceeded its cap")
    return report


def _assert_report_safe(report: Any, secrets: Mapping[str, str]) -> None:
    if not isinstance(report, dict) or set(report) != _REPORT_KEYS:
        raise RuntimeError("live certification report has an invalid top-level schema")
    if report.get("schemaVersion") != 1 or report.get("provider") != PROVIDER_ID:
        raise RuntimeError("live certification report has an invalid identity")
    if report.get("certification") not in {"complete", "incomplete"}:
        raise RuntimeError("live certification report has an invalid status")
    if not isinstance(report.get("featureDefaultEnabled"), bool):
        raise RuntimeError("live certification feature-default evidence is invalid")
    feature_defaults = report.get("featureDefaults")
    if (
        not isinstance(feature_defaults, dict)
        or set(feature_defaults) != set(_FEATURE_DEFAULT_FALSE_RE)
        or any(not isinstance(value, bool) for value in feature_defaults.values())
        or report["featureDefaultEnabled"] != any(feature_defaults.values())
    ):
        raise RuntimeError("live certification feature-default gates are invalid")

    budget = report.get("physicalCallBudget")
    if not isinstance(budget, dict) or set(budget) != _BUDGET_KEYS:
        raise RuntimeError("live certification report has an invalid budget schema")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in budget.values()):
        raise RuntimeError("live certification budget values must be integers")
    if budget["hardCap"] > HARD_PHYSICAL_CALL_CAP or budget["observed"] > budget["hardCap"]:
        raise RuntimeError("live certification report exceeds its physical-call cap")
    if (
        budget["expected"] != EXPECTED_PHYSICAL_CALLS
        or budget["worstCase"] != WORST_CASE_PHYSICAL_CALLS
    ):
        raise RuntimeError("live certification report changed the approved call budget")

    security = report.get("securityChecks")
    if not isinstance(security, dict) or set(security) != _SECURITY_KEYS:
        raise RuntimeError("live certification report has an invalid security schema")
    if security.get("temporaryTreeMode") != "0700" or security.get("reportMode") != "0600":
        raise RuntimeError("live certification report has unsafe filesystem modes")
    if not all(
        security.get(name) is True
        for name in (
            "isolatedChildEnvironment",
            "registryEndpointPinned",
            "rawPayloadPersistenceDisabled",
            "syntheticBridgeAuthenticated",
        )
    ):
        raise RuntimeError("live certification security evidence is incomplete")

    cases = report.get("cases")
    if not isinstance(cases, list) or len(cases) != len(SCENARIOS):
        raise RuntimeError("live certification report has an invalid case matrix")
    expected_cases = {scenario.case: scenario for scenario in SCENARIOS}
    observed_calls = 0
    observed_provider_calls = 0
    for row in cases:
        if not isinstance(row, dict) or set(row) != _CASE_KEYS:
            raise RuntimeError("live certification case has an invalid schema")
        case_name = row.get("case")
        if not isinstance(case_name, str):
            raise RuntimeError("live certification case identity is invalid")
        scenario = expected_cases.get(case_name)
        if scenario is None or row.get("mode") != scenario.mode or row.get("tier") != scenario.tier:
            raise RuntimeError("live certification case identity is invalid")
        if row.get("modelSlot") != scenario.model_slot:
            raise RuntimeError("live certification model slot is invalid")
        if row.get("expectedTools") != list(scenario.expected_tools) or any(
            tool not in _ALLOWED_TOOLS for tool in row.get("expectedTools", [])
        ):
            raise RuntimeError("live certification tool evidence is invalid")
        if row.get("expectedPhysicalCalls") != scenario.expected_physical_calls:
            raise RuntimeError("live certification expected-call evidence is invalid")
        physical_calls = row.get("observedPhysicalCalls")
        provider_calls = row.get("providerCalls")
        loop_calls = row.get("loopContinuationCalls")
        if (
            isinstance(physical_calls, bool)
            or not isinstance(physical_calls, int)
            or physical_calls < 0
            or physical_calls > scenario.expected_physical_calls
        ):
            raise RuntimeError("live certification observed-call evidence is invalid")
        if (
            isinstance(provider_calls, bool)
            or not isinstance(provider_calls, int)
            or provider_calls != physical_calls
            or isinstance(loop_calls, bool)
            or not isinstance(loop_calls, int)
            or not 0 <= loop_calls <= provider_calls
        ):
            raise RuntimeError("live certification provider-call evidence is invalid")
        if row.get("providerCalled") is not (physical_calls > 0):
            raise RuntimeError("live certification provider-call evidence is inconsistent")
        observed_calls += physical_calls
        observed_provider_calls += provider_calls
        for field in (
            "providerCalled",
            "beforeHashVerified",
            "afterHashVerified",
            "singleRevisionVerified",
            "singleChangeSetVerified",
            "acceptedAnnotationsVerified",
            "modeVerified",
            "routerTierVerified",
            "aggregatorToolsVerified",
            "revertVerified",
            "passed",
        ):
            if not isinstance(row.get(field), bool):
                raise RuntimeError("live certification boolean evidence is invalid")
        observed_tools = row.get("observedTools")
        if (
            not isinstance(observed_tools, list)
            or any(
                not isinstance(tool, str) or tool not in _ALLOWED_TOOLS
                for tool in observed_tools
            )
            or observed_tools != sorted(set(observed_tools))
        ):
            raise RuntimeError("live certification observed tools are invalid")
        for field in (
            "writerCalls",
            "writerAttempts",
            "proposerToolCalls",
        ):
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeError("live certification count evidence is invalid")
        if row.get("status") not in _ALLOWED_STATUSES:
            raise RuntimeError("live certification case status is invalid")
        if row.get("reasonCode") not in _ALLOWED_REASON_CODES:
            raise RuntimeError("live certification case reason is invalid")
        if scenario.zero_call_preflight and (
            physical_calls != 0 or row.get("providerCalled") is not False
        ):
            raise RuntimeError("zero-call preflight contacted a provider")
        if row.get("passed") is True and row.get("status") != "passed":
            raise RuntimeError("passed case must have passed status")
        if row.get("status") == "passed" and row.get("passed") is not True:
            raise RuntimeError("passed status requires complete passing evidence")
        if row.get("status") == "failed" and row.get("reasonCode") == "none":
            raise RuntimeError("failed case must include a bounded reason")
        if row.get("passed") is True:
            if scenario.zero_call_preflight:
                if not all(
                    row.get(field) is True
                    for field in (
                        "beforeHashVerified",
                        "modeVerified",
                        "routerTierVerified",
                    )
                ):
                    raise RuntimeError("passed preflight lacks required evidence")
            elif (
                physical_calls != scenario.expected_physical_calls
                or row.get("providerCalled") is not True
                or not all(
                    row.get(field) is True
                    for field in (
                        "beforeHashVerified",
                        "afterHashVerified",
                        "singleRevisionVerified",
                        "singleChangeSetVerified",
                        "acceptedAnnotationsVerified",
                        "modeVerified",
                        "routerTierVerified",
                        "aggregatorToolsVerified",
                        "revertVerified",
                    )
                )
                or row.get("writerCalls") != _expected_writer_calls(scenario)
                or row.get("writerAttempts") != _expected_writer_calls(scenario)
                or row.get("proposerToolCalls") != 0
                or _expected_writer_name(scenario) not in observed_tools
                or row.get("reasonCode") != "none"
            ):
                raise RuntimeError("passed mutation lacks required certification evidence")

    if observed_calls != budget["observed"]:
        raise RuntimeError("live certification physical-call accounting does not reconcile")
    if observed_provider_calls > budget["hardCap"]:
        raise RuntimeError("live certification raw provider-call evidence exceeds its cap")
    evidence_complete = all(row.get("passed") is True for row in cases)
    expected_certification = "complete" if evidence_complete else "incomplete"
    if report["certification"] != expected_certification:
        raise RuntimeError("live certification status is inconsistent with case evidence")
    reasons = report.get("reasonCodes")
    if not isinstance(reasons, list) or any(
        reason not in _ALLOWED_REASON_CODES for reason in reasons
    ):
        raise RuntimeError("live certification reason codes are invalid")
    expected_reasons = sorted(
        {
            str(row["reasonCode"])
            for row in cases
            if row["reasonCode"] != "none"
        }
    )
    if reasons != expected_reasons:
        raise RuntimeError("live certification reason summary is inconsistent")
    if report_contains_secret(report, secrets):
        raise RuntimeError("credential detected in live certification report")


@dataclass(frozen=True, slots=True)
class _BridgeSelection:
    active_preview_artifact_id: str
    selection_id: str
    tag_name: str
    element_path: str
    dom_sha256: str | None
    element_proof_sha256: str
    scope_id: str

    def request(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": DESKTOP_BRIDGE_VERSION,
            "activePreviewArtifactId": self.active_preview_artifact_id,
            "selectionId": self.selection_id,
            "tagName": self.tag_name,
            "elementPath": self.element_path,
            "elementProofSha256": self.element_proof_sha256,
        }
        if self.dom_sha256 is not None:
            payload["domSha256"] = self.dom_sha256
        return payload


class SyntheticDesktopBridge:
    """Minimal authenticated v4 bridge used only inside the isolated worker."""

    def __init__(self) -> None:
        self._token = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._selections: dict[str, _BridgeSelection] = {}
        self._candidate_handle: str | None = None
        self._candidate_bound = False
        self._gateway_endpoint: str | None = None
        self._active_preview_artifact_id: str | None = None
        self._scope_id: str | None = None

    def set_gateway_endpoint(self, endpoint: str) -> None:
        """Set the owned Gateway origin used to resolve opaque preview handles.

        The real Electron bridge receives only the opaque candidate handle and
        asks its owned Gateway to resolve the candidate artifact identity. The
        synthetic bridge follows the same boundary instead of manufacturing an
        artifact id in the fixture.
        """

        if not re.fullmatch(r"http://127\.0\.0\.1:\d+", endpoint):
            raise ValueError("synthetic bridge requires an IPv4 loopback Gateway endpoint")
        with self._lock:
            self._gateway_endpoint = endpoint

    def register(self, selection: _BridgeSelection) -> None:
        with self._lock:
            if selection.selection_id in self._selections:
                raise RuntimeError("synthetic selection id already registered")
            self._selections[selection.selection_id] = selection
            self._active_preview_artifact_id = selection.active_preview_artifact_id
            self._scope_id = selection.scope_id

    def start(self) -> Mapping[str, str]:
        if self._server is not None:
            return self.environment()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "OpenSquillaSyntheticBridge/3"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                owner._handle(self)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name="opensquilla-live-artifact-bridge",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread
        return self.environment()

    def environment(self) -> Mapping[str, str]:
        server = self._server
        if server is None:
            raise RuntimeError("synthetic Desktop bridge is not running")
        host, port = server.server_address[:2]
        if host != "127.0.0.1":
            raise RuntimeError("synthetic Desktop bridge did not bind IPv4 loopback")
        return {
            DESKTOP_BRIDGE_URL_ENV: f"http://127.0.0.1:{port}",
            DESKTOP_BRIDGE_TOKEN_ENV: self._token,
        }

    def close(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        with self._lock:
            self._selections.clear()
            self._candidate_handle = None
            self._candidate_bound = False
            self._gateway_endpoint = None
            self._active_preview_artifact_id = None
            self._scope_id = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)

    def _json_response(
        self,
        handler: http.server.BaseHTTPRequestHandler,
        status: int,
        payload: Mapping[str, Any],
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        handler.send_response(status)
        handler.send_header("cache-control", "no-store")
        handler.send_header("content-type", "application/json; charset=utf-8")
        handler.send_header("content-length", str(len(body)))
        handler.send_header("x-content-type-options", "nosniff")
        handler.end_headers()
        handler.wfile.write(body)

    def _candidate_identity(self, handle: str) -> tuple[str, str]:
        """Resolve candidate identity through the authenticated Gateway API."""

        with self._lock:
            endpoint = self._gateway_endpoint
            token = self._token
        if endpoint is None:
            raise RuntimeError("synthetic bridge has no owned Gateway endpoint")
        request = urllib.request.Request(
            f"{endpoint}/api/v1/desktop-artifact-candidate-preview/resolve",
            data=json.dumps({"version": 1, "candidateHandle": handle}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5.0) as response:  # noqa: S310 - owned loopback Gateway
                if response.status != 200:
                    raise RuntimeError("candidate preview resolution failed")
                payload = json.loads(response.read(1024 * 1024))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError("candidate preview resolution failed") from exc
        if not isinstance(payload, Mapping):
            raise RuntimeError("candidate preview resolution was malformed")
        artifact_id = payload.get("candidate_artifact_id")
        scope_id = payload.get("scope_id")
        if (
            payload.get("candidate_handle") != handle
            or not isinstance(artifact_id, str)
            or not artifact_id
            or not isinstance(scope_id, str)
            or not scope_id
        ):
            raise RuntimeError("candidate preview resolution was malformed")
        return artifact_id, scope_id

    def _handle(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        expected_auth = f"Bearer {self._token}"
        supplied_auth = str(handler.headers.get("authorization") or "")
        if not hmac.compare_digest(supplied_auth, expected_auth):
            self._json_response(
                handler,
                401,
                {"ok": False, "code": "unauthorized", "message": "Bridge authentication failed."},
            )
            return
        content_type = str(handler.headers.get("content-type") or "").split(";", 1)[0]
        if content_type.strip().lower() != "application/json":
            self._json_response(
                handler,
                415,
                {"ok": False, "code": "unsupported-media-type", "message": "Use JSON."},
            )
            return
        try:
            length = int(handler.headers.get("content-length") or "0")
        except ValueError:
            length = -1
        if not 0 < length <= 64 * 1024:
            self._json_response(
                handler,
                400,
                {"ok": False, "code": "invalid-request", "message": "Invalid request."},
            )
            return
        try:
            body = json.loads(handler.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json_response(
                handler,
                400,
                {"ok": False, "code": "invalid-request", "message": "Invalid request."},
            )
            return
        if (
            handler.path == "/v1/capabilities"
            and isinstance(body, dict)
            and body.get("version") in {DESKTOP_BRIDGE_VERSION, DESKTOP_BRIDGE_V4_VERSION}
        ):
            self._json_response(
                handler,
                200,
                {
                    "ok": True,
                    "value": {
                        "version": DESKTOP_BRIDGE_V4_VERSION,
                        "available": True,
                        "captureSelection": False,
                        "resolveAnnotationSelection": True,
                        "focusAnnotation": False,
                        "browserInspect": True,
                        "browserAct": True,
                        "screenshot": True,
                        "officeFlush": False,
                        "reloadSurface": True,
                        "bindCandidatePreview": True,
                        "restoreCanonicalPreview": True,
                    },
                },
            )
            return
        if (
            handler.path != "/v1/invoke"
            or not isinstance(body, dict)
            or body.get("version") not in {DESKTOP_BRIDGE_VERSION, DESKTOP_BRIDGE_V4_VERSION}
            or not isinstance(body.get("request"), dict)
        ):
            self._json_response(
                handler,
                400,
                {"ok": False, "code": "invalid-request", "message": "Invalid request."},
            )
            return
        request = body["request"]
        method = body.get("method")
        if method == "bindCandidatePreview":
            handle = request.get("candidateHandle")
            if (
                request.get("version") != DESKTOP_BRIDGE_V4_VERSION
                or not isinstance(handle, str)
                or not handle.startswith("candidate_")
            ):
                self._json_response(
                    handler,
                    400,
                    {
                        "ok": False,
                        "code": "invalid-request",
                        "message": "Invalid candidate handle.",
                    },
                )
                return
            with self._lock:
                self._candidate_handle = handle
                self._candidate_bound = True
            self._json_response(
                handler,
                200,
                {"ok": True, "method": method, "value": {"bound": True, "candidateHandle": handle}},
            )
            return
        if method == "restoreCanonicalPreview":
            with self._lock:
                self._candidate_handle = None
                self._candidate_bound = False
            self._json_response(
                handler,
                200,
                {"ok": True, "method": method, "value": {"restored": True}},
            )
            return
        if method == "browserInspect":
            if request.get("version") != DESKTOP_BRIDGE_V4_VERSION:
                self._json_response(
                    handler,
                    400,
                    {
                        "ok": False,
                        "code": "unsupported-version",
                        "message": "Browser preview requires v4.",
                    },
                )
                return
            scope = request.get("scope")
            max_nodes = request.get("maxNodes")
            with self._lock:
                bound = self._candidate_bound
                candidate_handle = self._candidate_handle
                canonical_artifact_id = self._active_preview_artifact_id
                canonical_scope_id = self._scope_id
            if scope not in {"document", "selection", "viewport"} or not isinstance(max_nodes, int):
                self._json_response(
                    handler,
                    400,
                    {
                        "ok": False,
                        "code": "invalid-request",
                        "message": "Invalid browser inspection.",
                    },
                )
                return
            active_artifact_id = canonical_artifact_id
            active_scope_id = canonical_scope_id
            if bound:
                if not isinstance(candidate_handle, str):
                    self._json_response(
                        handler,
                        409,
                        {
                            "ok": False,
                            "code": "candidate-preview-unbound",
                            "message": "Candidate preview is not bound.",
                        },
                    )
                    return
                try:
                    active_artifact_id, active_scope_id = self._candidate_identity(candidate_handle)
                except RuntimeError:
                    self._json_response(
                        handler,
                        503,
                        {
                            "ok": False,
                            "code": "candidate-preview-unavailable",
                            "message": "Candidate preview identity is unavailable.",
                        },
                    )
                    return
            self._json_response(
                handler,
                200,
                {
                    "ok": True,
                    "method": method,
                    "value": {
                        "scope": scope,
                        "nodes": [
                            {
                                "anchor": "anchor_main",
                                "role": "main",
                                "name": "main",
                                "text": "",
                                "interactive": False,
                                "disabled": False,
                                "selected": False,
                            }
                        ] if bound else [],
                        "truncated": False,
                        "activePreviewArtifactId": active_artifact_id,
                        "scopeId": active_scope_id,
                        "candidateHandle": candidate_handle if bound else None,
                    },
                },
            )
            return
        if method == "browserAct":
            self._json_response(
                handler,
                200,
                {"ok": True, "method": method, "value": {"performed": True, "changed": False}},
            )
            return
        if method == "screenshot":
            self._json_response(
                handler,
                200,
                {
                    "ok": True,
                    "method": method,
                    "value": {
                        "mime": "image/png",
                        "dataBase64": (
                            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
                            "+A8AAQUBAScY42YAAAAASUVORK5CYII="
                        ),
                        "width": 1,
                        "height": 1,
                    },
                },
            )
            return
        if method == "reloadSurface":
            self._json_response(
                handler,
                200,
                {"ok": True, "method": method, "value": {"reloaded": True}},
            )
            return
        if (
            method != "resolveAnnotationSelection"
            or request.get("version") not in {DESKTOP_BRIDGE_VERSION, DESKTOP_BRIDGE_V4_VERSION}
        ):
            self._json_response(
                handler,
                400,
                {"ok": False, "code": "invalid-request", "message": "Invalid bridge method."},
            )
            return
        selection_id = request.get("selectionId")
        with self._lock:
            selection = self._selections.get(str(selection_id))
        expected_request = selection.request() if selection is not None else None
        if expected_request is not None:
            # v4 keeps the v3 annotation-selection payload for compatibility;
            # only the envelope version changes during capability negotiation.
            expected_request["version"] = request.get("version")
        if selection is None or request != expected_request:
            self._json_response(
                handler,
                409,
                {"ok": False, "code": "selection-changed", "message": "Selection changed."},
            )
            return
        with self._lock:
            self._selections.pop(str(selection_id), None)
        value = {
            **{key: value for key, value in selection.request().items() if key != "version"},
            "scopeId": selection.scope_id,
            "rect": {"x": 10, "y": 10, "width": 100, "height": 32},
        }
        self._json_response(
            handler,
            200,
            {"ok": True, "method": "resolveAnnotationSelection", "value": value},
        )


def _tool_name(raw: object) -> str | None:
    if not isinstance(raw, Mapping):
        return None
    name = raw.get("name")
    if isinstance(name, str) and name:
        return name
    function = raw.get("function")
    if isinstance(function, Mapping) and isinstance(function.get("name"), str):
        return str(function["name"])
    return None


@dataclass(frozen=True, slots=True)
class _TraceEvidence:
    physical_calls: int
    provider_calls: int
    loop_continuation_calls: int
    accounting_ambiguous: bool
    observed_tools: tuple[str, ...]
    surfaced_tools_exact: bool
    writer_calls: int
    writer_attempts: int
    writer_succeeded: bool
    proposer_tool_calls: int
    aggregator_tools_verified: bool
    ensemble_fallback_used: bool
    restricted_prompt_verified: bool
    request_tool_groups: tuple[tuple[str, ...], ...]


def _request_tool_groups(records: Sequence[Mapping[str, Any]]) -> list[tuple[str, ...]]:
    """Associate each provider request with the tool calls it produced."""

    groups: list[list[str]] = []
    current: list[str] | None = None
    for record in records:
        kind = record.get("kind")
        if kind == "llm_request":
            current = []
            groups.append(current)
        elif kind == "tool_request" and current is not None:
            payload = record.get("payload")
            if isinstance(payload, Mapping) and isinstance(payload.get("name"), str):
                current.append(str(payload["name"]))
    return [tuple(group) for group in groups]


def _direct_repair_loop_verified(groups: Sequence[tuple[str, ...]]) -> bool:
    """Require the fixed GLM-5.2 acceptance case to exercise every loop phase.

    Cardinality matters here: combining read+patch in one response would hide
    an agent iteration and would no longer prove that candidate feedback is
    returned to the model before its next decision.
    """

    if len(groups) != 9:
        return False
    return bool(
        groups[0] == ("document_inspect",)
        and groups[1] == ("document_read",)
        and groups[2] == ("document_patch",)
        and groups[3] == ("document_browser_inspect",)
        and groups[4]
        in {
            ("document_browser_screenshot",),
            ("document_browser_act",),
        }
        and groups[5] == ("document_patch",)
        and groups[6] == ("document_browser_inspect",)
        and groups[7] == ("document_finish",)
        and groups[8] == ()
    )


def _trace_evidence(
    records: Sequence[Mapping[str, Any]],
    *,
    mode: str,
) -> _TraceEvidence:
    requests = [record for record in records if record.get("kind") == "llm_request"]
    surfaced_sets: list[frozenset[str]] = []
    for record in requests:
        payload = record.get("payload")
        tools = payload.get("tools") if isinstance(payload, Mapping) else None
        surfaced_sets.append(
            frozenset(
                name
                for name in (_tool_name(item) for item in (tools or []))
                if name is not None
            )
        )
    tool_requests = [record for record in records if record.get("kind") == "tool_request"]
    observed_tools = tuple(
        sorted(
            {
                str((record.get("payload") or {}).get("name"))
                for record in tool_requests
                if isinstance(record.get("payload"), Mapping)
                and (record.get("payload") or {}).get("name") in _ALLOWED_TOOLS
            }
        )
    )
    writer_names = frozenset({"document_apply", "document_patch"})
    writer_requests = [
        record
        for record in tool_requests
        if (record.get("payload") or {}).get("name") in writer_names
    ]
    writer_ids = {
        str((record.get("payload") or {}).get("tool_use_id") or "")
        for record in writer_requests
    }
    writer_responses = [
        record
        for record in records
        if record.get("kind") == "tool_response"
        and (record.get("payload") or {}).get("name") in writer_names
        and str((record.get("payload") or {}).get("tool_use_id") or "") in writer_ids
    ]
    writer_succeeded = bool(
        writer_requests
        and len(writer_responses) == len(writer_requests)
        and all(
            (record.get("payload") or {}).get("is_error") is False
            for record in writer_responses
        )
    )
    attempt_records = [
        record
        for record in records
        if record.get("kind")
        in {
            "artifact_mutation_intent_observed",
            "artifact_mutation_intent_replay",
            "artifact_mutation_intent_rejected",
        }
    ]
    # Candidate-loop writers intentionally do not reserve a durable mutation
    # attempt until document_finish(commit).  The live trace still needs to
    # account for the single writer proposal, so use its tool request as the
    # turn-local attempt evidence when the finish tool is present and no legacy
    # intent journal event exists.
    candidate_loop = any(
        (record.get("payload") or {}).get("name") == "document_finish"
        for record in tool_requests
        if isinstance(record.get("payload"), Mapping)
    )
    if candidate_loop and not attempt_records:
        attempt_records = list(writer_requests)
    prompt_reports: list[Mapping[str, Any]] = []
    for record in records:
        payload = record.get("payload")
        if record.get("kind") == "prompt_report" and isinstance(payload, Mapping):
            prompt_reports.append(payload)
    prompt_report: Mapping[str, Any] = prompt_reports[0] if len(prompt_reports) == 1 else {}
    forbidden_prompt_markers = (
        "## Workspace Files (injected)",
        "<available_skills>",
        "Working directory:",
        "workspace:AGENTS.md",
    )
    request_projection = json.dumps(requests, sort_keys=True, ensure_ascii=False)
    restricted_prompt_verified = bool(
        len(prompt_reports) == 1
        and prompt_report.get("injected_workspace_files_count") == 0
        and prompt_report.get("skill_count") == 0
        and prompt_report.get("skills_prompt_chars") == 0
        and prompt_report.get("bootstrap_files") == []
        and all(marker not in request_projection for marker in forbidden_prompt_markers)
    )

    request_groups = _request_tool_groups(records)
    loop_continuation_calls = sum(
        bool(group)
        and all(
            name == "document_finish" or name.startswith("document_browser_")
            for name in group
        )
        for group in request_groups
    )
    provider_calls = len(requests)
    # Physical accounting is deliberately identical to the raw provider
    # request count. Browser/finish continuations and the tools=[] finalizer
    # are real paid requests and must not disappear from the certification
    # budget.
    physical_calls = provider_calls
    accounting_ambiguous = False
    proposer_tool_calls = 0
    aggregator_tools_verified = mode != "ensemble"
    ensemble_fallback_used = False
    if mode == "ensemble":
        traces: list[Mapping[str, Any]] = []
        for record in records:
            if record.get("kind") not in {"llm_response", "llm_error"}:
                continue
            payload = record.get("payload")
            trace = payload.get("ensemble_trace") if isinstance(payload, Mapping) else None
            if isinstance(trace, Mapping):
                traces.append(trace)
        # Ensemble traces are emitted once per aggregator continuation, while
        # the ordinary llm_request stream records logical aggregator turns and
        # omits proposer fan-out. The cumulative trace is therefore the only
        # complete physical provider-request count for Ensemble.
        cumulative_request_counts = [
            int(trace.get("llm_request_count") or 0) for trace in traces
        ]
        if (
            not cumulative_request_counts
            or cumulative_request_counts[0] <= 0
            or any(
                current <= previous
                for previous, current in zip(
                    cumulative_request_counts,
                    cumulative_request_counts[1:],
                    strict=False,
                )
            )
        ):
            accounting_ambiguous = True
        provider_calls = cumulative_request_counts[-1] if cumulative_request_counts else 0
        physical_calls = provider_calls
        aggregator_tools_verified = bool(traces)
        for trace_index, trace in enumerate(traces):
            ensemble_fallback_used = ensemble_fallback_used or trace.get("fallback_used") is True
            candidates = trace.get("candidates")
            if not isinstance(candidates, list) or len(candidates) != 4:
                aggregator_tools_verified = False
                accounting_ambiguous = True
                continue
            for candidate in candidates:
                execution = candidate.get("execution") if isinstance(candidate, Mapping) else None
                if not isinstance(execution, Mapping):
                    aggregator_tools_verified = False
                    continue
                count = int(execution.get("tool_count") or 0)
                proposer_tool_calls += count
                if execution.get("tools_enabled") is not False or count != 0:
                    aggregator_tools_verified = False
            final_request = trace.get("final_request")
            execution = (
                final_request.get("execution")
                if isinstance(final_request, Mapping)
                else None
            )
            final_names = (
                execution.get("tool_names") if isinstance(execution, Mapping) else None
            )
            tools_enabled = (
                execution.get("tools_enabled") if isinstance(execution, Mapping) else None
            )
            final_name_set = (
                frozenset(str(name) for name in final_names)
                if isinstance(final_names, list)
                else None
            )
            is_outcome_finalization = trace_index == len(traces) - 1
            if is_outcome_finalization:
                if final_name_set != frozenset() or tools_enabled is not False:
                    aggregator_tools_verified = False
            elif final_name_set != _ALLOWED_TOOLS or tools_enabled is not True:
                aggregator_tools_verified = False
    exact_tool_boundary = len(surfaced_sets) >= 2 and surfaced_sets[-1] == frozenset()
    exact_tool_boundary = exact_tool_boundary and all(
        tool_set == _ALLOWED_TOOLS for tool_set in surfaced_sets[:-1]
    )
    return _TraceEvidence(
        physical_calls=physical_calls,
        provider_calls=provider_calls,
        loop_continuation_calls=loop_continuation_calls,
        accounting_ambiguous=accounting_ambiguous,
        observed_tools=observed_tools,
        surfaced_tools_exact=exact_tool_boundary,
        writer_calls=len(writer_requests),
        writer_attempts=len(attempt_records),
        writer_succeeded=writer_succeeded,
        proposer_tool_calls=proposer_tool_calls,
        aggregator_tools_verified=aggregator_tools_verified,
        ensemble_fallback_used=ensemble_fallback_used,
        restricted_prompt_verified=restricted_prompt_verified,
        request_tool_groups=tuple(request_groups),
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_gateway_config(
    path: Path,
    *,
    provider_endpoint: str,
    media_root: Path,
    allow_local_test_model_overrides: bool = False,
    preload_router: bool = True,
) -> None:
    """Write the finite live-gate profile without embedding credentials."""

    lines = [
        'host = "127.0.0.1"',
        "debug = false",
        "llm_request_timeout_seconds = 90",
        "agent_runtime_timeout_seconds = 120",
        # Autonomous PromptAnnotation turns have no feature-specific iteration
        # cap. Global runtime/time/cost budgets remain the safety fuse.
        "agent_max_iterations = 0",
        "agent_max_provider_retries = 0",
        "",
        "[auth]",
        'mode = "none"',
        "",
        "[control_ui]",
        "enabled = false",
        "",
        "[rate_limit]",
        "enabled = false",
        "",
        "[privacy]",
        "disable_network_observability = true",
        "",
        "[attachments]",
        f"media_root = {_toml_string(str(media_root))}",
        "persist_transcripts = true",
        "",
        "[tools]",
        # The live gate deliberately starts from the broad owner profile.  The
        # PromptAnnotation exclusive ceiling must still surface the complete
        # ten-tool document-agent surface in every tool-enabled request and at
        # dispatch.
        'profile = "full"',
        "",
        "[memory]",
        'source = "state"',
        "",
        "[naming]",
        # Auxiliary auto-title requests are outside the approved 42-call turn
        # matrix and must never consume the live credential.
        "enabled = false",
        "",
        "[model_catalog]",
        'refresh = "off"',
        "",
        "[llm]",
        f'provider = "{PROVIDER_ID}"',
        f"model = {_toml_string(DIRECT_MODEL)}",
        f'api_key_env = "{KEY_ENV}"',
        f"base_url = {_toml_string(provider_endpoint)}",
        "max_tokens = 4096",
        'thinking = "off"',
        "",
        "[squilla_router]",
        f"enabled = {'true' if preload_router else 'false'}",
        'rollout_phase = "full"',
        'strategy = "v4_phase3"',
        'default_tier = "c0"',
        "confidence_threshold = 0.5",
        "require_router_runtime = true",
    ]
    for tier, model in ROUTER_MODELS.items():
        lines.extend(
            [
                "",
                f"[squilla_router.tiers.{tier}]",
                f'provider = "{PROVIDER_ID}"',
                f"model = {_toml_string(model)}",
                "supports_image = false",
                "image_only = false",
                'thinking = "off"',
            ]
        )
    lines.extend(
        [
            "",
            "[llm_ensemble]",
            "enabled = false",
            'selection_mode = "static_tokenrhythm_b5"',
            "proposer_tools = false",
            "min_successful_proposers = 1",
            'all_failed_policy = "error"',
            "shuffle_candidates = false",
            "record_candidates = false",
        ]
    )
    if allow_local_test_model_overrides:
        for model in sorted(set(ROUTER_MODELS.values())):
            lines.extend(
                [
                    "",
                    f'[models.{PROVIDER_ID}.{json.dumps(model)}]',
                    "supports_tools = true",
                ]
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _records_for_session(
    records: Sequence[Mapping[str, Any]],
    session_key: str,
) -> list[Mapping[str, Any]]:
    return [record for record in records if record.get("session_key") == session_key]


def _source_proofs(source: str, element_path: str) -> tuple[str, str]:
    return canonical_selection_proofs(source, element_path=element_path)


class GatewayCertificationDriver:
    """Owned-Gateway executor for the real PromptAnnotation live matrix.

    Construction and import are side-effect free.  Network-capable work starts
    only when the private worker explicitly calls :meth:`start`; the public CLI
    requires ``--execute-live-matrix`` in addition to the cost/key attestations.
    Tests may pin ``provider_endpoint`` to a local fake server without weakening
    the worker's official-registry endpoint policy.
    """

    def __init__(
        self,
        *,
        temp_root: Path,
        api_key: str,
        timeout_seconds: float,
        provider_endpoint: str,
        allow_local_test_model_overrides: bool = False,
        preload_router: bool = True,
    ) -> None:
        if allow_local_test_model_overrides and not provider_endpoint.startswith(
            ("http://127.0.0.1:", "http://localhost:")
        ):
            raise ValueError("test model capability overrides require a loopback provider")
        self.temp_root = temp_root
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.provider_endpoint = provider_endpoint
        self.allow_local_test_model_overrides = allow_local_test_model_overrides
        self.preload_router = preload_router
        self.state_dir = temp_root / "state"
        self.user_state_dir = temp_root / "user-state"
        self.media_root = temp_root / "media"
        self.turn_log_dir = temp_root / "turn-calls"
        # OPENSQUILLA_DESKTOP=1 makes the lifecycle guard require the config to
        # live inside the selected profile.  Keeping it under state_dir proves
        # the synthetic bridge cannot bypass Desktop profile fencing.
        self.config_path = self.state_dir / "config.toml"
        self.stdout_path = temp_root / "gateway.stdout.log"
        self.stderr_path = temp_root / "gateway.stderr.log"
        self.bridge = SyntheticDesktopBridge()
        self.client = GatewayRPCClient(
            scopes=["operator.admin"],
            request_timeout_s=min(30.0, timeout_seconds),
        )
        self.port: int | None = None
        self.process: subprocess.Popen[str] | None = None
        self._stdout: Any = None
        self._stderr: Any = None

    async def start(self) -> None:
        for directory in (
            self.state_dir,
            self.user_state_dir,
            self.media_root,
            self.turn_log_dir,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        bridge_environment = self.bridge.start()
        _write_gateway_config(
            self.config_path,
            provider_endpoint=self.provider_endpoint,
            media_root=self.media_root,
            allow_local_test_model_overrides=self.allow_local_test_model_overrides,
            preload_router=self.preload_router,
        )
        env = _worker_environment(self.api_key)
        _apply_isolated_home_environment(env, self.user_state_dir)
        env.update(bridge_environment)
        env["OPENSQUILLA_DESKTOP"] = "1"
        if self.preload_router:
            env["OPENSQUILLA_DESKTOP_PRELOAD_ROUTER"] = "1"
        env["PYTHONPATH"] = str(SRC_DIR)
        env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] = str(self.config_path)
        env["OPENSQUILLA_STATE_DIR"] = str(self.state_dir)
        env["OPENSQUILLA_USER_STATE_DIR"] = str(self.user_state_dir)
        env["OPENSQUILLA_TEST_PROFILE_LOCK_ROOT"] = "1"
        env["OPENSQUILLA_MEMORY_DREAM_DISABLED"] = "1"
        env["OPENSQUILLA_TURN_CALL_LOG"] = "1"
        env["OPENSQUILLA_TURN_CALL_LOG_DIR"] = str(self.turn_log_dir)
        self.port = _free_port()
        self.bridge.set_gateway_endpoint(f"http://127.0.0.1:{self.port}")
        self._stdout = self.stdout_path.open("w", encoding="utf-8")
        self._stderr = self.stderr_path.open("w", encoding="utf-8")
        os.chmod(self.stdout_path, 0o600)
        os.chmod(self.stderr_path, 0o600)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "opensquilla.cli.main",
                "gateway",
                "run",
                "--port",
                str(self.port),
                "--bind",
                "127.0.0.1",
            ],
            cwd=self.temp_root,
            env=env,
            stdout=self._stdout,
            stderr=self._stderr,
            text=True,
            shell=False,
        )
        _health, error = await asyncio.to_thread(
            _wait_for_gateway_health,
            self.process,
            self.port,
        )
        if error is not None:
            for stream in (self._stdout, self._stderr):
                if stream is not None:
                    stream.flush()
            stream_tails: list[str] = []
            for label, path in (
                ("stdout", self.stdout_path),
                ("stderr", self.stderr_path),
            ):
                try:
                    tail = path.read_text(encoding="utf-8", errors="replace")[-2000:]
                except OSError:
                    tail = ""
                if tail:
                    stream_tails.append(f"{label}={tail}")
            diagnostic = "; ".join((error, *stream_tails))
            for secret in (
                self.api_key,
                bridge_environment.get(DESKTOP_BRIDGE_TOKEN_ENV, ""),
            ):
                if secret:
                    diagnostic = diagnostic.replace(secret, "<redacted>")
            diagnostic = diagnostic.replace(str(self.temp_root), "<temp-root>")
            raise RuntimeError(f"owned Gateway did not become ready: {diagnostic}")
        if self.preload_router:
            assert self.process is not None
            preload_error = await asyncio.to_thread(
                _wait_for_router_preload,
                self.process,
                self.stdout_path,
                timeout_seconds=self.timeout_seconds,
            )
            if preload_error is not None:
                raise RuntimeError(preload_error)
        await self.client.connect(f"ws://127.0.0.1:{self.port}/ws")

    async def close(self) -> None:
        try:
            await self.client.close()
        finally:
            process = self.process
            self.process = None
            if process is not None:
                await asyncio.to_thread(_stop_gateway, process)
            self.bridge.close()
            for stream_name in ("_stdout", "_stderr"):
                stream = getattr(self, stream_name)
                if stream is not None:
                    stream.close()
                    setattr(self, stream_name, None)

    async def _set_mode(self, mode: str) -> None:
        if mode == "direct":
            patches = {
                "llm_ensemble.enabled": False,
                "squilla_router.enabled": False,
                "squilla_router.rollout_phase": "full",
            }
        elif mode == "router":
            patches = {
                "llm_ensemble.enabled": False,
                "squilla_router.enabled": True,
                "squilla_router.rollout_phase": "full",
            }
        elif mode == "ensemble":
            patches = {
                "llm_ensemble.selection_mode": "static_tokenrhythm_b5",
                "llm_ensemble.enabled": True,
            }
        else:
            return
        await self.client.call("config.patch.safe", {"patches": patches})

    async def _new_document(self) -> tuple[str, str, str, dict[str, Any]]:
        created = await self.client.call(
            "sessions.create",
            {"agentId": "main", "kind": "webchat"},
        )
        session_key = str(created["key"])
        session_id = str(created["sessionId"])
        ref = await asyncio.to_thread(
            ArtifactStore(self.media_root).publish_bytes,
            _FIXTURE_HTML.encode("utf-8"),
            session_id=session_id,
            session_key=session_key,
            name="annotation-live-fixture.html",
            mime="text/html",
            source="live_prompt_annotation_fixture",
        )
        opened = await self.client.call(
            "artifacts.documents.open",
            {"sessionKey": session_key, "artifactId": ref.id},
        )
        document = dict(opened["document"])
        head = document.get("head")
        active_preview_artifact_id = (
            head.get("artifactId") if isinstance(head, Mapping) else None
        )
        if not isinstance(active_preview_artifact_id, str):
            raise RuntimeError("opened document has no canonical head artifact identity")
        return session_key, session_id, active_preview_artifact_id, document

    async def _create_annotation(
        self,
        *,
        session_key: str,
        artifact_id: str,
        document: Mapping[str, Any],
        target: Literal["reset", "title"],
        body: str,
        invalid_proof: bool = False,
    ) -> str:
        element_path = _ELEMENT_PATHS[target]
        dom_sha256, element_proof_sha256 = _source_proofs(_FIXTURE_HTML, element_path)
        if invalid_proof:
            element_proof_sha256 = "0" * 64
        annotation_id = f"ann_{uuid.uuid4().hex}"
        selection_id = f"sel_{uuid.uuid4().hex}"
        tag_name = "button" if target == "reset" else "h1"
        self.bridge.register(
            _BridgeSelection(
                active_preview_artifact_id=artifact_id,
                selection_id=selection_id,
                tag_name=tag_name,
                element_path=element_path,
                dom_sha256=dom_sha256,
                element_proof_sha256=element_proof_sha256,
                scope_id=session_key,
            )
        )
        created = await self.client.call(
            "artifacts.prompt_annotations.create",
            {
                "annotationId": annotation_id,
                "sessionKey": session_key,
                "documentId": document["id"],
                "revisionId": document["headRevisionId"],
                "selection": {
                    "selectionId": selection_id,
                    "tagName": tag_name,
                    "elementPath": element_path,
                    "domSha256": dom_sha256,
                    "elementProofSha256": element_proof_sha256,
                },
                "body": "",
            },
        )
        annotation = dict(created["annotation"])
        updated = await self.client.call(
            "artifacts.prompt_annotations.update",
            {
                "sessionKey": session_key,
                "annotationId": annotation_id,
                "expectedStateRevision": annotation["stateRevision"],
                "body": body,
            },
        )
        if updated.get("annotation", {}).get("body") != body:
            raise RuntimeError("PromptAnnotation update was not durable")
        return annotation_id

    async def _wait_for_task(self, session_key: str, task_id: str) -> Mapping[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            bootstrap = await self.client.call(
                "sessions.bootstrap",
                {"key": session_key, "limit": 100},
            )
            tasks = bootstrap.get("tasks")
            if isinstance(tasks, list):
                match = next(
                    (
                        row
                        for row in tasks
                        if isinstance(row, Mapping) and row.get("task_id") == task_id
                    ),
                    None,
                )
                if match is not None and match.get("status") not in {"queued", "running"}:
                    return match
            await asyncio.sleep(0.25)
        raise TimeoutError("owned Gateway task did not finish before the live-case deadline")

    def _session_trace(self, session_key: str) -> list[Mapping[str, Any]]:
        return _records_for_session(_read_turn_call_records(self.turn_log_dir), session_key)

    async def _run_zero_call_case(self, scenario: Scenario) -> CaseEvidence:
        session_key, _session_id, artifact_id, document = await self._new_document()
        rejected = False
        try:
            if scenario.case == "dom_mismatch_zero_call":
                try:
                    await self._create_annotation(
                        session_key=session_key,
                        artifact_id=artifact_id,
                        document=document,
                        target="reset",
                        body=_SINGLE_ANNOTATION_BODY,
                        invalid_proof=True,
                    )
                except GatewayRPCError as exc:
                    rejected = exc.code in {
                        "ANNOTATION_UNAVAILABLE",
                        "ARTIFACT_ELEMENT_CHANGED",
                        "ARTIFACT_SELECTION_MISMATCH",
                        "ARTIFACT_PREVIEW_CHANGED",
                        "DOCUMENT_CHANGED",
                    }
            else:
                annotation_id = await self._create_annotation(
                    session_key=session_key,
                    artifact_id=artifact_id,
                    document=document,
                    target="reset",
                    body=_SINGLE_ANNOTATION_BODY,
                )
                send_session_key = session_key
                if scenario.case == "discarded_annotation_zero_call":
                    listed = await self.client.call(
                        "artifacts.prompt_annotations.list",
                        {"sessionKey": session_key, "documentId": document["id"]},
                    )
                    annotation = next(
                        row
                        for row in listed["annotations"]
                        if row["id"] == annotation_id
                    )
                    await self.client.call(
                        "artifacts.prompt_annotations.discard",
                        {
                            "sessionKey": session_key,
                            "annotationId": annotation_id,
                            "expectedStateRevision": annotation["stateRevision"],
                        },
                    )
                elif scenario.case == "cross_session_zero_call":
                    other = await self.client.call(
                        "sessions.create",
                        {"agentId": "main", "kind": "webchat"},
                    )
                    send_session_key = str(other["key"])
                try:
                    await self.client.call(
                        "chat.send",
                        {
                            "sessionKey": send_session_key,
                            "message": _TURN_PROMPT,
                            "clientRequestId": f"req_{uuid.uuid4().hex}",
                            "promptAnnotationIds": [annotation_id],
                        },
                    )
                except GatewayRPCError as exc:
                    rejected = exc.code in {
                        "ANNOTATION_BUSY",
                        "ANNOTATION_UNAVAILABLE",
                        "ARTIFACT_ANNOTATION_NOT_FOUND",
                        "ARTIFACT_ANNOTATION_STALE",
                        "ARTIFACT_REVISION_CHANGED",
                        "DOCUMENT_CHANGED",
                        "DOCUMENT_UNAVAILABLE",
                        "NOT_FOUND",
                        "PROMPT_ANNOTATION_STALE",
                    }
                session_key = send_session_key
        except Exception as exc:
            raise RuntimeError("zero-call preflight executor failed") from exc
        trace = self._session_trace(session_key)
        provider_calls = len([row for row in trace if row.get("kind") == "llm_request"])
        passed = rejected and provider_calls == 0
        return CaseEvidence(
            observed_physical_calls=provider_calls,
            provider_called=provider_calls > 0,
            before_hash_verified=True,
            mode_verified=True,
            router_tier_verified=True,
            passed=passed,
            status="passed" if passed else "failed",
            reason_code="none" if passed else "preflight_rejection_mismatch",
        )

    async def _run_mutation_case(self, scenario: Scenario) -> CaseEvidence:
        await self._set_mode(scenario.mode)
        session_key, _session_id, artifact_id, document = await self._new_document()
        document_id = str(document["id"])
        before = await self.client.call(
            "artifacts.source.read",
            {"sessionKey": session_key, "documentId": document_id},
        )
        before_source = before["source"]
        primary_body = (
            _SOURCE_PATCH_ANNOTATION_BODY
            if scenario.case == "direct_single_annotation"
            else _SINGLE_ANNOTATION_BODY
        )
        annotations = [
            await self._create_annotation(
                session_key=session_key,
                artifact_id=artifact_id,
                document=document,
                target="reset",
                body=primary_body,
            )
        ]
        if "double" in scenario.case:
            annotations.append(
                await self._create_annotation(
                    session_key=session_key,
                    artifact_id=artifact_id,
                    document=document,
                    target="title",
                    body=_TITLE_ANNOTATION_BODY,
                )
            )
        accepted = await self.client.call(
            "chat.send",
            {
                "sessionKey": session_key,
                "message": _TURN_PROMPT,
                "clientRequestId": f"req_{uuid.uuid4().hex}",
                "promptAnnotationIds": annotations,
            },
        )
        task_id = str(accepted["task_id"])
        task = await self._wait_for_task(session_key, task_id)
        after = await self.client.call(
            "artifacts.source.read",
            {"sessionKey": session_key, "documentId": document_id},
        )
        after_source = after["source"]
        revisions = await self.client.call(
            "artifacts.revisions.list",
            {"sessionKey": session_key, "documentId": document_id, "limit": 20},
        )
        changes = await self.client.call(
            "artifacts.changes.list",
            {"sessionKey": session_key, "documentId": document_id, "limit": 20},
        )
        trace = _trace_evidence(
            self._session_trace(session_key),
            mode=scenario.mode,
        )
        source_text = str(after_source.get("text") or "")
        if scenario.case == "direct_single_annotation":
            repaired_status = (
                '<span id="reset-status" role="status" aria-live="polite">Ready</span>'
            )
            reset_local = bool(
                '<button id="btn-reset" class="btn-outline">Reset</button>' in source_text
                and repaired_status in source_text
                and "#ef4444" not in source_text.lower()
                and '<button id="btn-confirm" class="btn-primary">Confirm</button>' in source_text
            )
        else:
            reset_local = bool(
                'id="btn-reset"' in source_text
                and "btn-outline" in source_text
                and "#ef4444" in source_text.lower()
                and '<button id="btn-confirm" class="btn-primary">Confirm</button>' in source_text
            )
        title_correct = _TITLE_TEXT in source_text if len(annotations) == 2 else (
            "Annotation live fixture" in source_text
        )
        revision_rows = revisions.get("revisions") or []
        change_rows = changes.get("changeSets") or []
        single_revision = len(revision_rows) == 2
        single_change = bool(
            len(change_rows) == 1
            and change_rows[0].get("turnId") == task_id
            and change_rows[0].get("resultRevisionId") == after_source.get("revisionId")
        )
        accepted_exact = accepted.get("acceptedPromptAnnotationIds") == annotations
        expected_model = (
            ROUTER_MODELS[str(scenario.tier)]
            if scenario.mode == "router" and scenario.tier is not None
            else DIRECT_MODEL
        )
        records = self._session_trace(session_key)
        request_models = {
            str(
                ((record.get("payload") or {}).get("config") or {}).get("model")
                or record.get("model")
                or ""
            )
            for record in records
            if record.get("kind") == "llm_request"
        }
        mode_verified = (
            trace.aggregator_tools_verified and not trace.ensemble_fallback_used
            if scenario.mode == "ensemble"
            else request_models == {expected_model}
        )
        router_tier_verified = scenario.mode != "router"
        if scenario.mode == "router":
            try:
                decisions = await self.client.call(
                    "router.decisions.list",
                    {"sessionKey": session_key, "limit": 1},
                )
                row = (decisions.get("decisions") or [{}])[0]
                router_tier_verified = row.get("finalTier") == scenario.tier
            except (GatewayRPCError, IndexError, TypeError):
                router_tier_verified = False
        after_ok = bool(
            task.get("status") == "succeeded"
            and after_source.get("sha256") != before_source.get("sha256")
            and reset_local
            and title_correct
        )
        revert_verified = False
        if single_change:
            current_document = await self.client.call(
                "artifacts.documents.get",
                {"sessionKey": session_key, "documentId": document_id},
            )
            current = current_document["document"]
            reverted = await self.client.call(
                "artifacts.changes.revert",
                {
                    "sessionKey": session_key,
                    "documentId": document_id,
                    "changeSetId": change_rows[0]["id"],
                    "expectedHeadRevisionId": current["headRevisionId"],
                    "expectedStateRevision": current["stateRevision"],
                },
            )
            restored = await self.client.call(
                "artifacts.source.read",
                {"sessionKey": session_key, "documentId": document_id},
            )
            revert_verified = bool(
                reverted.get("revision", {}).get("source") == "revert"
                and restored.get("source", {}).get("sha256") == before_source.get("sha256")
            )
        physical_exact = trace.physical_calls == scenario.expected_physical_calls
        tool_boundary = (
            trace.surfaced_tools_exact
            and trace.writer_calls == _expected_writer_calls(scenario)
            and (
                scenario.case != "direct_single_annotation"
                or _direct_repair_loop_verified(trace.request_tool_groups)
            )
        )
        mutation_invariants = bool(
            after_ok
            and single_revision
            and single_change
            and accepted_exact
            and trace.writer_attempts == _expected_writer_calls(scenario)
            and trace.writer_succeeded
            and revert_verified
        )
        passed = bool(
            physical_exact
            and not trace.accounting_ambiguous
            and tool_boundary
            and trace.restricted_prompt_verified
            and mutation_invariants
            and mode_verified
            and router_tier_verified
            and trace.proposer_tool_calls == 0
        )
        if not trace.restricted_prompt_verified:
            reason = "provider_projection_failed"
        elif not tool_boundary:
            reason = "tool_boundary_failed"
        elif not physical_exact or trace.accounting_ambiguous:
            reason = "physical_call_accounting_ambiguous"
        elif not mode_verified or not router_tier_verified:
            reason = "routing_evidence_failed"
        elif not mutation_invariants:
            reason = "artifact_invariant_failed"
        else:
            reason = "none"
        return CaseEvidence(
            observed_physical_calls=trace.physical_calls,
            provider_calls=trace.provider_calls,
            loop_continuation_calls=trace.loop_continuation_calls,
            provider_called=trace.physical_calls > 0,
            before_hash_verified=before_source.get("sha256")
            == hashlib.sha256(_FIXTURE_HTML.encode()).hexdigest(),
            after_hash_verified=after_ok,
            single_revision_verified=single_revision,
            single_change_set_verified=single_change,
            accepted_annotations_verified=accepted_exact,
            mode_verified=mode_verified,
            router_tier_verified=router_tier_verified,
            observed_tools=trace.observed_tools,
            writer_calls=trace.writer_calls,
            writer_attempts=trace.writer_attempts,
            proposer_tool_calls=trace.proposer_tool_calls,
            aggregator_tools_verified=trace.aggregator_tools_verified,
            revert_verified=revert_verified,
            passed=passed,
            status="passed" if passed else "failed",
            reason_code=reason,
        )

    async def run_case(self, scenario: Scenario) -> CaseEvidence:
        if scenario.zero_call_preflight:
            return await self._run_zero_call_case(scenario)
        return await self._run_mutation_case(scenario)


def _worker_main(
    *,
    hard_cap: int,
    timeout_seconds: float = 120.0,
    execute_live_matrix: bool = False,
) -> int:
    api_key = os.environ.get(KEY_ENV, "").strip()
    if not api_key:
        return 2
    if any(
        os.environ.get(name)
        for name in provider_secret_names()
        if name != KEY_ENV
    ):
        return 2
    if os.environ.get(BASE_URL_ENV):
        return 2
    endpoint = registry_endpoint(PROVIDER_ID)
    if execute_live_matrix:
        driver = GatewayCertificationDriver(
            temp_root=Path.cwd(),
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            provider_endpoint=endpoint,
        )
        report = asyncio.run(_run_certification(driver, hard_cap=hard_cap))
    else:
        report = _incomplete_report(hard_cap=hard_cap)
    _assert_report_safe(report, {KEY_ENV: api_key})
    # stdout is a private 0600 file owned by the parent. No raw model or
    # artifact data is ever emitted by this scaffold.
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


def _launch_worker(
    *,
    api_key: str,
    hard_cap: int,
    timeout_seconds: float,
    matrix_timeout_seconds: float = DEFAULT_MATRIX_TIMEOUT_SECONDS,
    execute_live_matrix: bool = False,
) -> dict[str, Any]:
    if not MIN_MATRIX_TIMEOUT_SECONDS <= matrix_timeout_seconds <= MAX_MATRIX_TIMEOUT_SECONDS:
        raise ValueError(
            "matrix timeout must remain within the bounded certification window"
        )
    temp_root = Path(
        tempfile.mkdtemp(prefix="opensquilla-artifact-prompt-annotations-e2e-")
    )
    os.chmod(temp_root, 0o700)
    stdout_path = temp_root / "worker.stdout.json"
    stderr_path = temp_root / "worker.stderr.log"
    stdout_path.touch(mode=0o600)
    stderr_path.touch(mode=0o600)
    os.chmod(stdout_path, 0o600)
    os.chmod(stderr_path, 0o600)
    worker_home = temp_root / "user-state"
    worker_home.mkdir(mode=0o700)
    worker_environment = _worker_environment(api_key)
    _apply_isolated_home_environment(worker_environment, worker_home)
    secrets = {KEY_ENV: api_key}
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--_worker",
                "--physical-call-cap",
                str(hard_cap),
                "--timeout-seconds",
                str(timeout_seconds),
            ]
            if execute_live_matrix:
                command.append("--_execute-live-matrix")
            completed = subprocess.run(
                command,
                cwd=temp_root,
                env=worker_environment,
                stdout=stdout,
                stderr=stderr,
                text=True,
                timeout=matrix_timeout_seconds,
                check=False,
            )
        if completed.returncode != 0:
            raise RuntimeError("isolated live certification worker failed")
        report = json.loads(stdout_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise RuntimeError("isolated live certification worker returned invalid JSON")
        _assert_report_safe(report, secrets)
        return report
    finally:
        scan_and_remove_temporary_tree(temp_root, secrets)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the isolated PromptAnnotation live-certification boundary. "
            "It is dry-run by default; --execute-live-matrix starts the owned Gateway "
            "and can incur TokenRhythm charges."
        )
    )
    parser.add_argument("--output")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_CASE_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--matrix-timeout-seconds",
        type=float,
        default=DEFAULT_MATRIX_TIMEOUT_SECONDS,
        help=(
            "whole-matrix worker deadline; independent from the per-case "
            "--timeout-seconds deadline"
        ),
    )
    parser.add_argument(
        "--physical-call-cap",
        type=int,
        default=HARD_PHYSICAL_CALL_CAP,
    )
    parser.add_argument("--confirm-live-cost", action="store_true")
    parser.add_argument("--confirm-rotated-key", action="store_true")
    parser.add_argument(
        "--execute-live-matrix",
        action="store_true",
        help="run the owned-Gateway live matrix after both attestations",
    )
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_execute-live-matrix", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args._worker:
        try:
            return _worker_main(
                hard_cap=args.physical_call_cap,
                timeout_seconds=args.timeout_seconds,
                execute_live_matrix=args._execute_live_matrix,
            )
        except (OSError, RuntimeError, ValueError):
            return 2

    if not args.output:
        parser.error("--output is required")
    output = Path(args.output)
    if not is_temporary_report_path(output):
        parser.error("--output must be inside the system temporary directory")
    output.unlink(missing_ok=True)
    if not args.confirm_live_cost:
        print("live certification requires --confirm-live-cost", file=sys.stderr)
        return 2
    if not args.confirm_rotated_key:
        print("live certification requires --confirm-rotated-key", file=sys.stderr)
        return 2
    if not WORST_CASE_PHYSICAL_CALLS <= args.physical_call_cap <= HARD_PHYSICAL_CALL_CAP:
        print(
            f"--physical-call-cap must be between {WORST_CASE_PHYSICAL_CALLS} "
            f"and {HARD_PHYSICAL_CALL_CAP}",
            file=sys.stderr,
        )
        return 2
    if not 5.0 <= args.timeout_seconds <= 120.0:
        print("--timeout-seconds must be between 5 and 120", file=sys.stderr)
        return 2
    if not MIN_MATRIX_TIMEOUT_SECONDS <= args.matrix_timeout_seconds <= MAX_MATRIX_TIMEOUT_SECONDS:
        print(
            f"--matrix-timeout-seconds must be between "
            f"{MIN_MATRIX_TIMEOUT_SECONDS:g} and {MAX_MATRIX_TIMEOUT_SECONDS:g}",
            file=sys.stderr,
        )
        return 2
    if os.environ.get(BASE_URL_ENV):
        print(f"{BASE_URL_ENV} overrides are forbidden for certification", file=sys.stderr)
        return 2
    api_key = os.environ.get(KEY_ENV, "").strip()
    if not api_key:
        print(f"{KEY_ENV} must contain a rotated live certification key", file=sys.stderr)
        return 2

    secrets = {KEY_ENV: api_key}
    try:
        report = _launch_worker(
            api_key=api_key,
            hard_cap=args.physical_call_cap,
            timeout_seconds=args.timeout_seconds,
            matrix_timeout_seconds=args.matrix_timeout_seconds,
            execute_live_matrix=args.execute_live_matrix,
        )
        report = sanitize_report(report, secrets)
        _assert_report_safe(report, secrets)
        report = write_safe_report(output, report, secrets)
        _assert_report_safe(report, secrets)
    except Exception as exc:  # noqa: BLE001 - never emit provider or artifact bodies
        output.unlink(missing_ok=True)
        print(
            json.dumps(
                {
                    "status": "failed",
                    "failureClass": classify_failure(type(exc).__name__),
                    "exceptionClass": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(
            {
                "status": report["certification"],
                "observedPhysicalCalls": report["physicalCallBudget"]["observed"],
            },
            sort_keys=True,
        )
    )
    return 0 if report["certification"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
