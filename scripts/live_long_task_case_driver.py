#!/usr/bin/env python3
"""Execute one bounded long-task release-gate case against a local Gateway.

This is the concrete driver for :mod:`scripts.live_long_task_release_gate`.
It deliberately accepts no provider key flags: the coordinator supplies only
the selected provider environment variable (and, for a fallback row, the one
fallback variable) to this process.  A case runs in an isolated temporary
state/workspace, raw Gateway logs are removed, and the only durable output is
the coordinator's bounded numeric result schema.

The browser scenarios delegate page interaction to the checked-in Playwright
helper in ``opensquilla-webui/scripts/live-long-task-browser.mjs``.  The Python
process remains the lifecycle owner so a browser can request a graceful or
forced Gateway restart without receiving a process id or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
WEBUI_ROOT = REPO_ROOT / "opensquilla-webui"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from opensquilla.gateway_client import GatewayRPCClient  # noqa: E402
from opensquilla.provider.registry import get_provider_spec  # noqa: E402
from scripts.live_harness_security import (  # noqa: E402
    classify_failure,
    is_temporary_report_path,
    minimal_child_environment,
    registry_endpoint,
    scan_and_remove_temporary_tree,
    write_safe_report,
)
from scripts.live_long_task_release_gate import (  # noqa: E402
    _MINIMUM_PHYSICAL_REQUESTS,
    ALL_SCENARIOS,
    CREDENTIAL_ENV_BY_PROVIDER,
    EXIT_BUDGET,
    EXIT_CONFIGURATION,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_PASSED,
    MODEL_MATRIX,
    PERFORMANCE_REPORT_ENV,
)
from scripts.long_task_fault_proxy import (  # noqa: E402
    DeterministicFaultProxy,
    FaultScenario,
)

_CASE_FIELDS: Final = frozenset(
    {
        "schema_version",
        "case_id",
        "provider",
        "model",
        "scenario",
        "repeat_index",
        "fallback_provider",
        "remaining_budget",
    }
)
_BUDGET_FIELDS: Final = frozenset(
    {"wall_ms", "billed_cost_usd", "physical_requests", "billed_tokens"}
)
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,239}$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
_BROWSER_HELPER: Final = WEBUI_ROOT / "scripts" / "live-long-task-browser.mjs"
_TERMINAL_EVENTS: Final = frozenset({"session.event.done", "session.event.error"})
_MAX_CASE_FILE_BYTES: Final = 64 * 1024
_MAX_BROWSER_RESULT_BYTES: Final = 64 * 1024
_HISTORY_SETTLE_TIMEOUT_SECONDS: Final = 5.0
_HISTORY_SETTLE_EVENT_WAIT_SECONDS: Final = 0.05
_PERFORMANCE_FIXTURE: Final = {
    "historyMessages": 200,
    "reasoningDeltas": 20_000,
    "toolFragments": 10_000,
    "textDeltas": 4_000,
}


class DriverConfigurationError(RuntimeError):
    """The case cannot start without changing its configuration."""


class DriverAssertionError(RuntimeError):
    """The live case ran, but its required observable outcome was absent."""


class DriverBudgetError(RuntimeError):
    """The remaining coordinator budget cannot safely cover this case."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_turn_call_records(log_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("turn-calls-*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def _usage_from_llm_responses(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    totals: dict[str, int | float] = {
        "totalInputTokens": 0,
        "totalOutputTokens": 0,
        "reasoningTokens": 0,
        "cachedTokens": 0,
        "totalCostUsd": 0.0,
    }
    for record in records:
        payload = record.get("payload")
        usage = payload.get("usage") if isinstance(payload, Mapping) else None
        if not isinstance(usage, Mapping):
            continue
        totals["totalInputTokens"] += int(usage.get("input_tokens") or 0)
        totals["totalOutputTokens"] += int(usage.get("output_tokens") or 0)
        totals["reasoningTokens"] += int(usage.get("reasoning_tokens") or 0)
        totals["cachedTokens"] += int(usage.get("cached_tokens") or 0)
        totals["totalCostUsd"] += float(usage.get("billed_cost") or 0.0)
    totals["totalTokens"] = (
        int(totals["totalInputTokens"])
        + int(totals["totalOutputTokens"])
        + int(totals["reasoningTokens"])
    )
    return totals


@dataclass(frozen=True)
class CaseBudget:
    wall_ms: int
    billed_cost_usd: float
    physical_requests: int
    billed_tokens: int


@dataclass(frozen=True)
class LiveCase:
    case_id: str
    provider: str
    model: str
    scenario: str
    repeat_index: int
    fallback_provider: str | None
    remaining_budget: CaseBudget


@dataclass
class TurnObservation:
    session_key: str
    marker: str
    started_monotonic: float
    terminal_event: str = ""
    terminal_reason: str = ""
    text_bytes: int = 0
    text_chunks: int = 0
    marker_seen_in_stream: bool = False
    thinking_chunks: int = 0
    tool_ids: set[str] = field(default_factory=set)
    activity_events: int = 0
    activity_phases: list[tuple[str, float, int, bool]] = field(default_factory=list)
    reasoning_pulse_times: list[float] = field(default_factory=list)
    first_reasoning_ms: float | None = None
    first_token_ms: float | None = None
    activity_latency_ms: float | None = None
    compactions: int = 0
    _marker_tail: str = ""

    @property
    def completed(self) -> bool:
        return self.terminal_event == "session.event.done"

    def consume(self, frame: Mapping[str, Any]) -> None:
        event_name = str(frame.get("event") or "")
        payload = frame.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        event_session = str(payload.get("session_key", payload.get("key", "")) or "")
        if event_session and event_session != self.session_key:
            return
        now = time.monotonic()
        elapsed_ms = max(0.0, (now - self.started_monotonic) * 1000)

        if event_name == "session.event.text_delta":
            text = str(payload.get("text") or "")
            if text:
                encoded = text.encode("utf-8")
                self.text_bytes += len(encoded)
                self.text_chunks += 1
                if self.first_token_ms is None:
                    self.first_token_ms = elapsed_ms
                # Retain only enough synthetic text to prove the marker; raw
                # provider output never enters the public result.
                self._marker_tail = (self._marker_tail + text)[-1024:]
                self.marker_seen_in_stream = self.marker in self._marker_tail
        elif event_name == "session.event.thinking":
            text = str(payload.get("text") or "")
            if text:
                self.thinking_chunks += 1
                if self.first_reasoning_ms is None:
                    self.first_reasoning_ms = elapsed_ms
        elif event_name in {"session.event.tool_use", "session.event.tool_use_delta"}:
            raw_tool_id = payload.get(
                "tool_use_id",
                payload.get("toolUseId", payload.get("id")),
            )
            if isinstance(raw_tool_id, str) and raw_tool_id:
                self.tool_ids.add(raw_tool_id)
        elif event_name == "session.event.provider_activity":
            phase = str(payload.get("phase") or "")
            heartbeat = payload.get("heartbeat") is True
            emitted_at = _nonnegative_int(payload.get("emitted_at"), default=0)
            started_at = _nonnegative_int(payload.get("started_at"), default=0)
            self.activity_events += 1
            self.activity_phases.append((phase, now, emitted_at, heartbeat))
            if phase == "reasoning":
                self.reasoning_pulse_times.append(now)
                if self.first_reasoning_ms is None:
                    self.first_reasoning_ms = elapsed_ms
                if emitted_at and started_at:
                    candidate = max(0.0, float(emitted_at - started_at))
                    if self.activity_latency_ms is None:
                        self.activity_latency_ms = candidate
                    else:
                        self.activity_latency_ms = min(self.activity_latency_ms, candidate)
        elif event_name == "session.event.compaction":
            status = str(payload.get("status") or "").lower()
            if (
                payload.get("applied") is True
                or payload.get("compacted") is True
                or status
                in {
                    "completed",
                    "applied",
                }
            ):
                self.compactions += 1

        if event_name in _TERMINAL_EVENTS:
            self.terminal_event = event_name
            self.terminal_reason = str(payload.get("reason") or payload.get("status") or "")


@dataclass(frozen=True)
class BrowserEvidence:
    status: Literal["passed", "failed"]
    counts: dict[str, int]
    metrics: dict[str, float]


@dataclass(frozen=True)
class DurableAccountingEvidence:
    event_count: int = 0
    missing_cost_entries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    billed_cost_usd: float = 0.0


@dataclass(frozen=True)
class PerformanceGateEvidence:
    counts: dict[str, int]
    metrics: dict[str, float]


def _nonnegative_int(value: Any, *, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DriverConfigurationError("case contains an invalid non-negative integer")
    return int(value)


def _nonnegative_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DriverConfigurationError("case contains an invalid non-negative number")
    rendered = float(value)
    if not math.isfinite(rendered) or rendered < 0:
        raise DriverConfigurationError("case contains an invalid non-negative number")
    return rendered


def _safe_case_path(path: Path) -> Path:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise DriverConfigurationError("case file is missing") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise DriverConfigurationError("case file must be a regular file")
    resolved = path.resolve(strict=True)
    if not is_temporary_report_path(resolved):
        raise DriverConfigurationError("case file must be inside system temporary storage")
    if not resolved.parent.name.startswith("opensquilla-long-task-case-"):
        raise DriverConfigurationError("case file is not owned by the release coordinator")
    if resolved.stat().st_size > _MAX_CASE_FILE_BYTES:
        raise DriverConfigurationError("case file is too large")
    return resolved


def _safe_output_path(path: Path, *, case_path: Path) -> Path:
    resolved = path.resolve(strict=False)
    if not is_temporary_report_path(resolved) or resolved.parent != case_path.parent:
        raise DriverConfigurationError("result must share the coordinator temporary directory")
    if resolved.exists():
        mode = resolved.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise DriverConfigurationError("result path must not be a link or special file")
    return resolved


def load_case(path: Path) -> LiveCase:
    case_path = _safe_case_path(path)
    try:
        payload = json.loads(case_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DriverConfigurationError("case file is not valid JSON") from exc
    if not isinstance(payload, Mapping) or set(payload) != _CASE_FIELDS:
        raise DriverConfigurationError("case file has an unsupported schema")
    if payload.get("schema_version") != 1:
        raise DriverConfigurationError("case schema version is unsupported")

    case_id = str(payload.get("case_id") or "")
    provider = str(payload.get("provider") or "").lower()
    model = str(payload.get("model") or "")
    scenario = str(payload.get("scenario") or "")
    fallback_raw = payload.get("fallback_provider")
    fallback_provider = str(fallback_raw).lower() if fallback_raw is not None else None
    if not _SAFE_ID.fullmatch(case_id):
        raise DriverConfigurationError("case id is invalid")
    if provider not in MODEL_MATRIX or model not in MODEL_MATRIX[provider]:
        raise DriverConfigurationError("provider/model is outside the release matrix")
    if scenario not in ALL_SCENARIOS:
        raise DriverConfigurationError("scenario is outside the release matrix")
    if fallback_provider is not None and fallback_provider not in MODEL_MATRIX:
        raise DriverConfigurationError("fallback provider is outside the release matrix")
    if scenario == "fallback" and not fallback_provider:
        raise DriverConfigurationError("fallback scenario requires a fallback provider")
    if scenario != "fallback" and fallback_provider is not None:
        raise DriverConfigurationError("only fallback scenarios may name a fallback provider")
    if not _SAFE_MODEL.fullmatch(model):
        raise DriverConfigurationError("model id is invalid")

    budget_raw = payload.get("remaining_budget")
    if not isinstance(budget_raw, Mapping) or set(budget_raw) != _BUDGET_FIELDS:
        raise DriverConfigurationError("remaining budget is invalid")
    budget = CaseBudget(
        wall_ms=_nonnegative_int(budget_raw.get("wall_ms")),
        billed_cost_usd=_nonnegative_float(budget_raw.get("billed_cost_usd")),
        physical_requests=_nonnegative_int(budget_raw.get("physical_requests")),
        billed_tokens=_nonnegative_int(budget_raw.get("billed_tokens")),
    )
    repeat_index = _nonnegative_int(payload.get("repeat_index"))
    if repeat_index < 1:
        raise DriverConfigurationError("repeat index must be positive")
    if budget.physical_requests < _MINIMUM_PHYSICAL_REQUESTS[scenario]:
        raise DriverBudgetError("remaining physical-request budget cannot cover this case")
    if budget.wall_ms < 1 or budget.billed_tokens < 1 or budget.billed_cost_usd <= 0:
        raise DriverBudgetError("remaining release budget is exhausted")

    required_envs = {CREDENTIAL_ENV_BY_PROVIDER[provider]}
    if fallback_provider:
        required_envs.add(CREDENTIAL_ENV_BY_PROVIDER[fallback_provider])
    if any(not os.environ.get(name, "").strip() for name in required_envs):
        raise DriverConfigurationError("required provider credential environment is missing")
    return LiveCase(
        case_id=case_id,
        provider=provider,
        model=model,
        scenario=scenario,
        repeat_index=repeat_index,
        fallback_provider=fallback_provider,
        remaining_budget=budget,
    )


def _toml_string(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _provider_model(provider: str, *, pro: bool = False) -> str:
    models = MODEL_MATRIX[provider]
    return models[1 if pro else 0]


def _max_tokens_for_scenario(scenario: str) -> int:
    if scenario == "long_answer":
        return 32_768
    if scenario in {"long_reasoning", "browser_hidden_11_minutes"}:
        return 16_384
    if scenario.startswith("browser_") or scenario.startswith("queue_"):
        return 8_192
    return 4_096


def render_gateway_config(
    case: LiveCase,
    *,
    workspace_dir: Path,
    primary_base_url: str | None = None,
    routed_base_url: str | None = None,
    force_router: bool | None = None,
) -> str:
    """Render a credential-name-only config for one isolated Gateway."""

    is_fallback = case.scenario == "fallback"
    primary_provider = cast(str, case.fallback_provider) if is_fallback else case.provider
    primary_model = _provider_model(primary_provider, pro=True) if is_fallback else case.model
    primary_endpoint = primary_base_url or registry_endpoint(primary_provider)
    router_enabled = (
        force_router if force_router is not None else case.scenario in {"router", "fallback"}
    )
    retries = 0 if is_fallback else (1 if case.scenario.startswith("fault_") else 1)
    tool_allow = (
        'also_allow = ["read_file"]'
        if case.scenario in {"tool_compaction", "browser_stop_each_phase"}
        else "also_allow = []"
    )
    lines = [
        'host = "127.0.0.1"',
        "debug = false",
        "log_file_enabled = false",
        f"workspace_dir = {_toml_string(workspace_dir)}",
        "llm_request_timeout_seconds = 900",
        "agent_runtime_timeout_seconds = 1800",
        "agent_max_iterations = 32",
        f"agent_max_provider_retries = {retries}",
        "",
        "[auth]",
        'mode = "none"',
        "",
        "[control_ui]",
        "enabled = true",
        'frontend = "vue"',
        "",
        "[rate_limit]",
        "enabled = false",
        "",
        "[privacy]",
        "disable_network_observability = true",
        "",
        "[tools]",
        'profile = "minimal"',
        tool_allow,
        "",
        "[task_runtime]",
        "turn_hard_deadline_s = 1800",
        "",
        "[memory]",
        'source = "state"',
        "",
        "[naming]",
        "enabled = false",
        "",
        "[llm]",
        f"provider = {_toml_string(primary_provider)}",
        f"model = {_toml_string(primary_model)}",
        f"api_key_env = {_toml_string(get_provider_spec(primary_provider).env_key)}",
        f"base_url = {_toml_string(primary_endpoint)}",
        f"max_tokens = {_max_tokens_for_scenario(case.scenario)}",
        "",
        "[squilla_router]",
        f"enabled = {'true' if router_enabled else 'false'}",
        f"cross_provider_tiers = {'true' if is_fallback else 'false'}",
        'tier_provider_mismatch = "veto"',
        'rollout_phase = "full"',
        'strategy = "v4_phase3"',
        'default_tier = "c0"',
        "require_router_runtime = false",
    ]
    if router_enabled:
        for tier in ("c0", "c1", "c2", "c3"):
            lines.extend(
                [
                    "",
                    f"[squilla_router.tiers.{tier}]",
                    f"provider = {_toml_string(case.provider)}",
                    f"model = {_toml_string(case.model)}",
                    "supports_image = false",
                    "image_only = false",
                ]
            )
    if is_fallback:
        lines.extend(
            [
                "",
                f"[llm_profiles.{case.provider}]",
                f"model = {_toml_string(case.model)}",
                f"api_key_env = {_toml_string(get_provider_spec(case.provider).env_key)}",
                f"base_url = {_toml_string(routed_base_url or registry_endpoint(case.provider))}",
            ]
        )
    return "\n".join(lines) + "\n"


class GatewayProcess:
    """Own one isolated Gateway process and its non-persisted raw artifacts."""

    def __init__(self, case: LiveCase, *, secret_values: tuple[str, ...]) -> None:
        self.case = case
        self.secret_values = secret_values
        self.root = Path(tempfile.mkdtemp(prefix="opensquilla-live-case-"))
        os.chmod(self.root, 0o700)
        self.config_path = self.root / "gateway.toml"
        self.state_dir = self.root / "state"
        self.user_state_dir = self.root / "user-state"
        self.workspace_dir = self.root / "workspace"
        self.turn_log_dir = self.root / "turn-calls"
        for directory in (
            self.state_dir,
            self.user_state_dir,
            self.workspace_dir,
            self.turn_log_dir,
        ):
            directory.mkdir(mode=0o700)
        self.port = _free_port()
        self.proc: subprocess.Popen[bytes] | None = None
        self._stdout: Any = None
        self._stderr: Any = None

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def write_config(
        self,
        *,
        primary_base_url: str | None = None,
        routed_base_url: str | None = None,
        force_router: bool | None = None,
    ) -> None:
        self.config_path.write_text(
            render_gateway_config(
                self.case,
                workspace_dir=self.workspace_dir,
                primary_base_url=primary_base_url,
                routed_base_url=routed_base_url,
                force_router=force_router,
            ),
            encoding="utf-8",
        )
        os.chmod(self.config_path, 0o600)

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": os.pathsep.join((str(REPO_ROOT), str(SRC_DIR))),
                "OPENSQUILLA_GATEWAY_CONFIG_PATH": str(self.config_path),
                "OPENSQUILLA_STATE_DIR": str(self.state_dir),
                "OPENSQUILLA_USER_STATE_DIR": str(self.user_state_dir),
                "OPENSQUILLA_TEST_PROFILE_LOCK_ROOT": "1",
                "OPENSQUILLA_MEMORY_DREAM_DISABLED": "1",
                "OPENSQUILLA_TURN_CALL_LOG": "1",
                "OPENSQUILLA_TURN_CALL_LOG_DIR": str(self.turn_log_dir),
                "OPENSQUILLA_LIVE_DISABLE_DOTENV": "1",
            }
        )
        return env

    def start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError("Gateway is already running")
        self._stdout = (self.root / "gateway.stdout.log").open("ab")
        self._stderr = (self.root / "gateway.stderr.log").open("ab")
        self.proc = subprocess.Popen(
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
            cwd=self.workspace_dir,
            env=self._child_env(),
            stdin=subprocess.DEVNULL,
            stdout=self._stdout,
            stderr=self._stderr,
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise DriverConfigurationError("Gateway exited during startup")
            try:
                with urllib.request.urlopen(f"{self.http_url}/health", timeout=1) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError, OSError):
                # The Gateway may still be binding; retry until the bounded deadline.
                pass
            time.sleep(0.25)
        raise DriverConfigurationError("Gateway did not become healthy")

    def stop(self, *, force: bool = False) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            if force:
                if os.name == "nt":
                    # ``Popen.kill`` terminates only the direct process on
                    # Windows.  A child which inherited a Gateway log or
                    # SQLite handle can otherwise outlive it and make the
                    # privacy cleanup permanently fail.  The PID is the exact
                    # process created above; suppress command output so no
                    # machine-local path or process detail reaches reports.
                    try:
                        subprocess.run(  # noqa: S603 - fixed OS command and owned PID.
                            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                            check=False,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=10,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        # Fall through to the direct-process kill below. The
                        # bounded artifact deletion remains the final proof
                        # that no inherited handle survived.
                        pass
                    if proc.poll() is None:
                        proc.kill()
                else:
                    proc.kill()
            else:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self.proc = None
        for stream_name in ("_stdout", "_stderr"):
            stream = getattr(self, stream_name)
            if stream is not None:
                stream.close()
                setattr(self, stream_name, None)

    def restart(self, *, force: bool) -> None:
        self.stop(force=force)
        self.start()

    def raw_records(self) -> list[dict[str, Any]]:
        return _read_turn_call_records(self.turn_log_dir)

    def cleanup(self) -> None:
        self.stop(force=True)
        # Windows can retain a just-closed SQLite or log handle for a short
        # interval after the child has exited. Re-run the complete secret scan
        # before each bounded delete retry; persistent deletion failures still
        # fail the release row. The scanner owns its separate transient-I/O
        # retry; credential and non-I/O scan failures remain fail-closed.
        cleanup_attempts = 30 if os.name == "nt" else 10
        for attempt in range(cleanup_attempts):
            try:
                scan_and_remove_temporary_tree(self.root, self.secret_values)
                return
            except OSError:
                if attempt + 1 == cleanup_attempts:
                    raise
                time.sleep(min(0.05 * (2**attempt), 0.5))


def _artifact_cleanup_stage(error: Exception) -> str:
    """Project cleanup failures to one stable, non-sensitive diagnostic code."""

    if isinstance(error, OSError):
        return "artifact_delete_failed"
    if isinstance(error, RuntimeError):
        message = str(error)
        if message == "unable to scan temporary live artifacts before deletion":
            return "artifact_scan_failed"
        if message == "credential detected in temporary live artifacts":
            return "artifact_secret_detected"
    return "artifact_cleanup_failed"


def _synthetic_marker(case: LiveCase, suffix: str = "") -> str:
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"{case.case_id}:{suffix}").hex[:16].upper()
    return f"OSQ_{digest}"


def _write_tool_fixture(gateway: GatewayProcess) -> list[Path]:
    fixture_dir = gateway.workspace_dir / "synthetic-tool-fixture"
    fixture_dir.mkdir(mode=0o700)
    paths: list[Path] = []
    for index in range(20):
        path = fixture_dir / f"item-{index:02d}.txt"
        path.write_text(f"synthetic item {index:02d}\n", encoding="utf-8")
        paths.append(path)
    return paths


def prompt_for_case(case: LiveCase, gateway: GatewayProcess) -> tuple[str, str]:
    """Return a synthetic prompt and marker; neither is written to the report."""

    marker = _synthetic_marker(case)
    scenario = case.scenario
    if scenario == "direct":
        return (
            "Do not call tools. Reply with one short sentence containing exactly "
            f"this synthetic marker: {marker}",
            marker,
        )
    if scenario == "router":
        return (
            "Do not call tools. Briefly explain why a deterministic checksum is useful, "
            f"then end with this exact synthetic marker: {marker}",
            marker,
        )
    if scenario == "long_reasoning":
        return (
            "Do not call tools. Work through a rigorous independent derivation of the "
            "finite identity sum_{k=0}^n (-1)^k C(n,k)^3, checking parity and boundary "
            "cases. Use deep internal reasoning, but return a concise proof and end with "
            f"this exact synthetic marker: {marker}",
            marker,
        )
    if scenario == "tool_compaction":
        paths = _write_tool_fixture(gateway)
        rendered = ", ".join(str(path) for path in paths)
        return (
            "Use the read_file tool exactly once for every one of these twenty synthetic "
            f"files, with no other tools: {rendered}. After all twenty results arrive, "
            "summarize their item numbers in one sentence and end with the exact marker "
            f"{marker}.",
            marker,
        )
    if scenario == "long_answer":
        return (
            "Do not call tools. Generate a synthetic Markdown performance fixture between "
            "18,000 and 24,000 UTF-8 bytes. Use numbered sections, fenced code, tables, "
            "inline math, and citations that point only to example.test. Begin and end with "
            f"the exact marker {marker}. Do not stop before the lower byte bound.",
            marker,
        )
    if scenario.startswith("fault_") or scenario == "fallback":
        return (
            f"Do not call tools. Reply with the exact synthetic marker and no other text: {marker}",
            marker,
        )
    if scenario.startswith("queue_"):
        return (
            "Do not call tools. Produce 250 numbered synthetic lines slowly enough for a "
            "queued follow-up to be staged, and end with this exact marker: "
            f"{marker}",
            marker,
        )
    if scenario == "browser_hidden_11_minutes":
        return (
            "Do not call tools. Perform a careful long derivation, then return 300 numbered "
            f"synthetic verification statements and end with {marker}.",
            marker,
        )
    return (
        "Do not call tools. Produce 220 numbered synthetic lines and end with this exact "
        f"marker: {marker}",
        marker,
    )


def _fault_sequence(scenario: str) -> tuple[FaultScenario, ...]:
    mapping = {
        "fault_429_retry_after": (FaultScenario.RATE_LIMITED, FaultScenario.OK),
        "fault_503": (FaultScenario.OVERLOADED, FaultScenario.OK),
        "fault_reset_before_first_token": (
            FaultScenario.RESET_BEFORE_FIRST_TOKEN,
            FaultScenario.OK,
        ),
        "fault_partial_then_reset": (FaultScenario.PARTIAL_THEN_RESET, FaultScenario.OK),
        "fault_reasoning_only": (FaultScenario.REASONING_ONLY, FaultScenario.OK),
        "fault_late_terminal": (FaultScenario.LATE_TERMINAL,),
        "fallback": (FaultScenario.OVERLOADED,),
    }
    try:
        return mapping[scenario]
    except KeyError as exc:
        raise ValueError(f"no fault sequence for {scenario!r}") from exc


async def _history_evidence(
    client: GatewayRPCClient,
    *,
    session_key: str,
    assistant_marker: str = "",
    user_marker: str = "",
) -> tuple[int, int, int, int]:
    history = await client.call(
        "chat.history",
        {"sessionKey": session_key, "limit": 1000},
    )
    messages = history.get("messages", []) if isinstance(history, Mapping) else []
    if not isinstance(messages, list):
        return 0, 0, 0, 0
    assistant_bytes = 0
    assistant_occurrences = 0
    user_occurrences = 0
    user_attachment_occurrences = 0
    for raw_message in messages:
        if not isinstance(raw_message, Mapping):
            continue
        role = str(raw_message.get("role") or "")
        text = str(raw_message.get("text", raw_message.get("content", "")) or "")
        if role == "assistant":
            assistant_bytes = len(text.encode("utf-8"))
            if assistant_marker and assistant_marker in text:
                assistant_occurrences += 1
        elif role == "user" and user_marker and user_marker in text:
            user_occurrences += 1
            attachments = raw_message.get("attachments")
            if isinstance(attachments, list):
                user_attachment_occurrences += len(attachments)
    return (
        assistant_bytes,
        assistant_occurrences,
        user_occurrences,
        user_attachment_occurrences,
    )


async def _wait_for_assistant_history_evidence(
    client: GatewayRPCClient,
    observation: TurnObservation,
    *,
    session_key: str,
    assistant_marker: str,
    deadline: float,
) -> tuple[int, int, int, int]:
    # The terminal stream event can win a narrow race with the assistant
    # transcript commit. Keep the durable marker assertion, but let history
    # converge within its own bounded phase. Waiting on the event queue avoids
    # a blind sleep and keeps the client responsive to late stream frames.
    settle_deadline = min(
        deadline,
        time.monotonic() + _HISTORY_SETTLE_TIMEOUT_SECONDS,
    )
    while True:
        evidence = await _history_evidence(
            client,
            session_key=session_key,
            assistant_marker=assistant_marker,
        )
        if evidence[1] > 0:
            return evidence
        remaining = settle_deadline - time.monotonic()
        if remaining <= 0:
            return evidence
        try:
            frame = await client.recv_event(
                timeout=min(remaining, _HISTORY_SETTLE_EVENT_WAIT_SECONDS)
            )
        except TimeoutError:
            continue
        observation.consume(frame)


async def _cancelled_webui_stop_count(
    client: GatewayRPCClient,
    *,
    session_key: str,
) -> int:
    history = await client.call(
        "chat.history",
        {"sessionKey": session_key, "limit": 1000},
    )
    outcomes = history.get("turn_outcomes", []) if isinstance(history, Mapping) else []
    if not isinstance(outcomes, list):
        return 0
    count = 0
    for raw_outcome in outcomes:
        if not isinstance(raw_outcome, Mapping):
            continue
        outcome = raw_outcome.get("outcome")
        if not isinstance(outcome, Mapping):
            continue
        status = str(raw_outcome.get("status") or "").lower()
        source = str(outcome.get("cancellation_source") or "").lower()
        if status == "cancelled" and source == "webui_stop":
            count += 1
    return count


async def _send_and_observe(
    gateway: GatewayProcess,
    *,
    prompt: str,
    marker: str,
    session_key: str,
    timeout_seconds: float,
) -> tuple[TurnObservation, int, int]:
    client = GatewayRPCClient(scopes=["operator.admin"], request_timeout_s=60.0)
    await client.connect(gateway.ws_url)
    try:
        await client.call(
            "sessions.messages.subscribe",
            {"key": session_key, "fast_ack": True},
        )
        started = time.monotonic()
        observation = TurnObservation(
            session_key=session_key,
            marker=marker,
            started_monotonic=started,
        )
        request_id = f"live-{uuid.uuid4()}"
        message_id = f"msg-{uuid.uuid4()}"
        await client.call(
            "sessions.send",
            {
                "key": session_key,
                "message": prompt,
                "intent": "new_chat",
                "queueMode": "followup",
                "clientRequestId": request_id,
                "clientMessageId": message_id,
                "_source": {
                    "surface_id": "long-task-release-gate",
                    "client_request_id": request_id,
                    "client_message_id": message_id,
                },
            },
        )
        deadline = time.monotonic() + timeout_seconds
        while not observation.terminal_event:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("live turn exceeded its case timeout")
            frame = await client.recv_event(timeout=min(remaining, 30.0))
            observation.consume(frame)
        if observation.completed and observation.marker_seen_in_stream:
            assistant_bytes, assistant_markers, _, _ = (
                await _wait_for_assistant_history_evidence(
                    client,
                    observation,
                    session_key=session_key,
                    assistant_marker=marker,
                    deadline=deadline,
                )
            )
        else:
            assistant_bytes, assistant_markers, _, _ = await _history_evidence(
                client,
                session_key=session_key,
                assistant_marker=marker,
            )
        return observation, assistant_bytes, assistant_markers
    finally:
        await client.close()


async def _manual_compaction(gateway: GatewayProcess, session_key: str) -> bool:
    client = GatewayRPCClient(scopes=["operator.admin"], request_timeout_s=180.0)
    await client.connect(gateway.ws_url)
    try:
        payload = await client.call(
            "sessions.compact",
            {
                "key": session_key,
                "wait": True,
                "instructions": (
                    "Compact the synthetic tool results while preserving their item numbers."
                ),
            },
        )
    finally:
        await client.close()
    return bool(
        isinstance(payload, Mapping)
        and (
            payload.get("compacted") is True
            or payload.get("applied") is True
            or str(payload.get("status") or "").lower() in {"completed", "applied"}
        )
    )


async def _router_decision_count(gateway: GatewayProcess, session_key: str) -> int:
    client = GatewayRPCClient(scopes=["operator.admin"], request_timeout_s=30.0)
    await client.connect(gateway.ws_url)
    try:
        payload = await client.call(
            "router.decisions.list",
            {"sessionKey": session_key, "limit": 5},
        )
    except Exception:
        return 0
    finally:
        await client.close()
    decisions = payload.get("decisions", []) if isinstance(payload, Mapping) else []
    return len(decisions) if isinstance(decisions, list) else 0


async def _durable_accounting_evidence(
    gateway: GatewayProcess,
    *,
    expected_provider_legs: int,
) -> DurableAccountingEvidence:
    """Return bounded evidence that every physical call reached the usage ledger.

    The Gateway persists one usage event per provider execution leg, including
    failed and interrupted legs. Poll briefly because finalization is queued;
    after a forced Gateway restart, boot first recovers a started event as
    ``unknown``. The query and returned evidence contain no request or response
    content.
    """

    client = GatewayRPCClient(scopes=["operator.admin"], request_timeout_s=30.0)
    await client.connect(gateway.ws_url)
    deadline = time.monotonic() + 5.0
    try:
        while True:
            payload = await client.call(
                "usage.query",
                {
                    "schemaVersion": 1,
                    "timezone": "UTC",
                    "range": {"preset": "all"},
                    "include": {"days": False, "models": False, "sessions": False},
                },
            )
            totals = payload.get("attributedTotals") if isinstance(payload, Mapping) else None
            if not isinstance(totals, Mapping):
                totals = {}
            event_count = _nonnegative_int(
                payload.get("eventCount") if isinstance(payload, Mapping) else None,
                default=0,
            )
            missing_cost_entries = _nonnegative_int(
                totals.get("missingCostEntries"),
                default=0,
            )
            latest = DurableAccountingEvidence(
                event_count=event_count,
                missing_cost_entries=missing_cost_entries,
                input_tokens=_nonnegative_int(totals.get("inputTokens"), default=0),
                output_tokens=_nonnegative_int(totals.get("outputTokens"), default=0),
                reasoning_tokens=_nonnegative_int(totals.get("reasoningTokens"), default=0),
                cached_tokens=_nonnegative_int(totals.get("cacheReadTokens"), default=0),
                billed_cost_usd=_nonnegative_float(totals.get("billedCostUsd", 0.0)),
            )
            if event_count >= expected_provider_legs or time.monotonic() >= deadline:
                return latest
            await asyncio.sleep(0.1)
    finally:
        await client.close()


def _accounting_from_records(
    records: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, float]]:
    terminal_records = [
        record for record in records if record.get("kind") in {"llm_response", "llm_error"}
    ]
    raw = _usage_from_llm_responses(terminal_records)
    input_tokens = _nonnegative_int(raw.get("totalInputTokens"), default=0)
    output_tokens = _nonnegative_int(raw.get("totalOutputTokens"), default=0)
    reasoning_tokens = _nonnegative_int(raw.get("reasoningTokens"), default=0)
    cached_tokens = _nonnegative_int(raw.get("cachedTokens"), default=0)
    total_tokens = _nonnegative_int(raw.get("totalTokens"), default=0)
    billed_cost = max(0.0, float(raw.get("totalCostUsd") or 0.0))
    return (
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cached_tokens": cached_tokens,
            "total_tokens": total_tokens,
        },
        {"billed_cost_usd": billed_cost},
    )


def _failure_class_from_records(records: Iterable[Mapping[str, Any]]) -> str:
    fragments: list[str] = []
    for record in records:
        if record.get("kind") != "llm_error":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        nested_error = payload.get("error")
        sources = [payload]
        if isinstance(nested_error, Mapping):
            sources.append(nested_error)
        for source in sources:
            for key in ("code", "error_code", "exception_type", "message"):
                value = source.get(key)
                if isinstance(value, str):
                    fragments.append(value[:200])
    return classify_failure(" ".join(fragments)) if fragments else "assertion"


def _max_reasoning_gap_ms(observation: TurnObservation) -> float:
    times = observation.reasoning_pulse_times
    if len(times) < 2:
        return 0.0
    return max((right - left) * 1000 for left, right in zip(times, times[1:]))


def _gateway_session_database(gateway: GatewayProcess) -> Path | None:
    candidates = [gateway.state_dir / "sessions.db"]
    root = getattr(gateway, "root", None)
    if isinstance(root, Path):
        candidates.extend(path for path in root.rglob("sessions.db") if path.is_file())
    return next((path for path in candidates if path.is_file()), None)


def _durable_accounting_from_database(gateway: GatewayProcess) -> tuple[int, int]:
    database = _gateway_session_database(gateway)
    if database is None:
        return 0, 0
    try:
        # sqlite3.Connection's context manager controls only the transaction;
        # it does not close the database.  Keep the read handle bounded so a
        # Windows release runner can remove the isolated state tree afterward.
        with contextlib.closing(
            sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        ) as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(
                        CASE
                            WHEN status IN ('started', 'unknown')
                                 AND missing_cost_entries = 0 THEN 1
                            ELSE missing_cost_entries
                        END
                    ), 0)
                FROM usage_events
                """
            ).fetchone()
    except sqlite3.Error:
        return 0, 0
    return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)


def _fallback_preceded_backup_usage_start(
    gateway: GatewayProcess,
    observation: TurnObservation,
) -> bool:
    fallback_emitted_at = min(
        (
            emitted_at
            for phase, _when, emitted_at, _heartbeat in observation.activity_phases
            if phase == "fallback" and emitted_at > 0
        ),
        default=0,
    )
    if fallback_emitted_at <= 0:
        return False
    database = _gateway_session_database(gateway)
    if database is None:
        return False
    try:
        with contextlib.closing(
            sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        ) as connection:
            row = connection.execute(
                """
                SELECT MAX(started_at_ms)
                FROM usage_events
                """
            ).fetchone()
    except sqlite3.Error:
        return False
    backup_started_at = int(row[0] or 0) if row else 0
    return backup_started_at > 0 and fallback_emitted_at <= backup_started_at


def _base_evidence(
    observation: TurnObservation,
    *,
    physical_requests: int,
    records: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, float]]:
    phases = [phase for phase, _when, _epoch, _heartbeat in observation.activity_phases]
    retry_phases = sum(phase in {"retry_wait", "retrying"} for phase in phases)
    fallback_phases = sum(phase == "fallback" for phase in phases)
    counts = {
        "provider_legs": physical_requests,
        "tool_legs": len(observation.tool_ids),
        "retry_legs": max(retry_phases, max(0, physical_requests - 1 - fallback_phases)),
        "fallback_legs": fallback_phases,
        "activity_events": observation.activity_events,
        "tokens_rendered": observation.text_bytes,
        "output_bytes": observation.text_bytes,
        "incremental_chunks": observation.text_chunks,
        "reasoning_pulses": len(observation.reasoning_pulse_times),
    }
    metrics: dict[str, float] = {
        "max_reasoning_pulse_gap_ms": _max_reasoning_gap_ms(observation),
    }
    if observation.first_reasoning_ms is not None:
        metrics["first_reasoning_ms"] = observation.first_reasoning_ms
    if observation.first_token_ms is not None:
        metrics["first_token_ms"] = observation.first_token_ms
    if observation.activity_latency_ms is not None:
        metrics["activity_latency_ms"] = observation.activity_latency_ms
    elif observation.reasoning_pulse_times:
        metrics["activity_latency_ms"] = 0.0
    del records
    return counts, metrics


def _add_durable_accounting_evidence(
    gateway: GatewayProcess,
    *,
    counts: dict[str, int],
    expected_provider_legs: int,
) -> DurableAccountingEvidence:
    evidence = asyncio.run(
        _durable_accounting_evidence(
            gateway,
            expected_provider_legs=expected_provider_legs,
        )
    )
    counts["accounted_provider_legs"] = evidence.event_count
    counts["usage_missing_cost_entries"] = evidence.missing_cost_entries
    return evidence


def _accounting_from_durable(
    evidence: DurableAccountingEvidence,
) -> tuple[dict[str, int], dict[str, float]]:
    return (
        {
            "input_tokens": evidence.input_tokens,
            "output_tokens": evidence.output_tokens,
            "reasoning_tokens": evidence.reasoning_tokens,
            "cached_tokens": evidence.cached_tokens,
            "total_tokens": (
                evidence.input_tokens + evidence.output_tokens + evidence.reasoning_tokens
            ),
        },
        {"billed_cost_usd": evidence.billed_cost_usd},
    )


def _merge_accounting_lower_bounds(
    first_usage: Mapping[str, int],
    first_cost: Mapping[str, float],
    second_usage: Mapping[str, int],
    second_cost: Mapping[str, float],
) -> tuple[dict[str, int], dict[str, float]]:
    usage = {
        key: max(int(first_usage.get(key, 0)), int(second_usage.get(key, 0)))
        for key in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens")
    }
    usage["total_tokens"] = max(
        sum(usage[key] for key in ("input_tokens", "output_tokens", "reasoning_tokens")),
        int(first_usage.get("total_tokens", 0)),
        int(second_usage.get("total_tokens", 0)),
    )
    return usage, {
        "billed_cost_usd": max(
            float(first_cost.get("billed_cost_usd", 0.0)),
            float(second_cost.get("billed_cost_usd", 0.0)),
        )
    }


def _browser_number(value: Any, *, integer: bool) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DriverAssertionError("browser helper returned an invalid numeric metric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise DriverAssertionError("browser helper returned an invalid numeric metric")
    if integer:
        if not number.is_integer():
            raise DriverAssertionError("browser helper returned a fractional count")
        return int(number)
    return number


def _load_browser_evidence(path: Path, *, return_code: int) -> BrowserEvidence:
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise DriverAssertionError("browser helper result is not a regular file")
        if path.stat().st_size > _MAX_BROWSER_RESULT_BYTES:
            raise DriverAssertionError("browser helper result is too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DriverAssertionError("browser helper did not produce a valid result") from exc
    if not isinstance(payload, Mapping) or set(payload) != {"status", "counts", "metrics"}:
        raise DriverAssertionError("browser helper result has an unsupported schema")
    status = str(payload.get("status") or "")
    if status not in {"passed", "failed"}:
        raise DriverAssertionError("browser helper result status is invalid")
    if return_code != 0 and status == "passed":
        raise DriverAssertionError("failed browser process reported a passing result")
    raw_counts = payload.get("counts")
    raw_metrics = payload.get("metrics")
    if not isinstance(raw_counts, Mapping) or not isinstance(raw_metrics, Mapping):
        raise DriverAssertionError("browser helper evidence must be numeric mappings")
    counts = {
        str(key): cast(int, _browser_number(value, integer=True))
        for key, value in raw_counts.items()
        if isinstance(key, str)
    }
    metrics = {
        str(key): float(_browser_number(value, integer=False))
        for key, value in raw_metrics.items()
        if isinstance(key, str)
    }
    return BrowserEvidence(status=cast(Any, status), counts=counts, metrics=metrics)


def _load_performance_report(name: str) -> Mapping[str, Any]:
    env_name = PERFORMANCE_REPORT_ENV[name]
    raw_path = os.environ.get(env_name, "").strip()
    if not raw_path:
        raise DriverAssertionError("required deterministic performance report is missing")
    path = Path(raw_path)
    if not path.is_absolute() or not is_temporary_report_path(path):
        raise DriverAssertionError("deterministic performance report is outside system temp")
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise DriverAssertionError("deterministic performance report is not a regular file")
        if path.stat().st_size > _MAX_BROWSER_RESULT_BYTES:
            raise DriverAssertionError("deterministic performance report is too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DriverAssertionError("deterministic performance report is unreadable") from exc
    if not isinstance(payload, Mapping):
        raise DriverAssertionError("deterministic performance report must be an object")
    return payload


def _report_number(
    report: Mapping[str, Any],
    key: str,
    *,
    positive: bool = False,
    integer: bool = False,
) -> float:
    value = report.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DriverAssertionError("deterministic performance evidence is not numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        raise DriverAssertionError("deterministic performance evidence is outside its domain")
    if integer and not number.is_integer():
        raise DriverAssertionError("deterministic performance count is fractional")
    return number


def _validate_performance_fixture(
    report: Mapping[str, Any],
    *,
    require_text_bytes: bool,
) -> None:
    fixture = report.get("fixture")
    if not isinstance(fixture, Mapping):
        raise DriverAssertionError("deterministic performance fixture is missing")
    expected = dict(_PERFORMANCE_FIXTURE)
    if require_text_bytes:
        expected["textBytes"] = 128 * 1_024
    if set(fixture) != set(expected):
        raise DriverAssertionError("deterministic performance fixture schema differs")
    for key, expected_value in expected.items():
        value = fixture.get(key)
        if isinstance(value, bool) or value != expected_value:
            raise DriverAssertionError("deterministic performance fixture differs")


def _reduction_percent(baseline: float, candidate: float) -> float:
    return max(0.0, (1.0 - candidate / baseline) * 100.0)


def _load_performance_gate_evidence() -> PerformanceGateEvidence:
    """Load fixed-Chromium evidence produced by the checked-in offline specs.

    The real-provider browser case proves integration and output size. Absolute
    and relative performance thresholds come from one deterministic fixture so
    provider latency/content cannot make the release gate nondeterministic.
    """

    baseline = _load_performance_report("baseline")
    candidate = _load_performance_report("candidate")
    resilience = _load_performance_report("resilience")
    if baseline.get("schemaVersion") != 1 or baseline.get("mode") != "baseline":
        raise DriverAssertionError("baseline characterization schema is invalid")
    if candidate.get("schemaVersion") != 1 or candidate.get("mode") != "candidate":
        raise DriverAssertionError("candidate characterization schema is invalid")
    if resilience.get("schemaVersion") != 1:
        raise DriverAssertionError("resilience performance schema is invalid")
    _validate_performance_fixture(baseline, require_text_bytes=True)
    _validate_performance_fixture(candidate, require_text_bytes=True)
    if baseline.get("fixture") != candidate.get("fixture"):
        raise DriverAssertionError("baseline and candidate fixtures differ")
    _validate_performance_fixture(resilience, require_text_bytes=False)

    baseline_heap = _report_number(baseline, "peakHeapDeltaBytes", positive=True)
    candidate_heap = _report_number(candidate, "peakHeapDeltaBytes")
    baseline_recalc = _report_number(baseline, "recalcStyleCount", positive=True)
    candidate_recalc = _report_number(candidate, "recalcStyleCount")
    parse_reduction = _report_number(resilience, "liveParseReduction")
    if parse_reduction > 1:
        raise DriverAssertionError("live parse reduction is outside its domain")

    counts = {
        "dom_nodes": int(_report_number(resilience, "domNodes", integer=True)),
        "mounted_rows": int(_report_number(resilience, "ordinaryRows", integer=True)),
    }
    metrics = {
        "input_next_paint_p95_ms": _report_number(resilience, "inputP95"),
        "input_next_paint_max_ms": _report_number(resilience, "inputMax"),
        "max_main_thread_task_ms": _report_number(resilience, "longestTask"),
        "peak_heap_delta_bytes": _report_number(resilience, "peakHeapDeltaBytes"),
        "post_gc_heap_delta_bytes": _report_number(resilience, "postGcHeapDeltaBytes"),
        "post_gc_growth_bytes": _report_number(
            resilience,
            "maxRetentionGrowthPerTurnBytes",
        ),
        "anchor_drift_px": _report_number(resilience, "upscrollAnchorDrift"),
        "bottom_gap_px": _report_number(resilience, "bottomGapWhileFollowing"),
        "markdown_parse_reduction_pct": parse_reduction * 100.0,
        "recalc_style_reduction_pct": _reduction_percent(
            baseline_recalc,
            candidate_recalc,
        ),
        "peak_heap_reduction_pct": _reduction_percent(
            baseline_heap,
            candidate_heap,
        ),
    }
    return PerformanceGateEvidence(counts=counts, metrics=metrics)


def _browser_child_environment() -> dict[str, str]:
    env = minimal_child_environment(os.environ)
    # Browser discovery is path based. No provider credential, config value,
    # or arbitrary OPENSQUILLA_* variable crosses this boundary.
    env["CI"] = "1"
    return env


def _run_browser_helper(
    case: LiveCase,
    gateway: GatewayProcess,
    *,
    prompt: str,
    marker: str,
    restart_callback: Any | None = None,
) -> tuple[BrowserEvidence, str, str, set[str]]:
    if not _BROWSER_HELPER.is_file():
        raise DriverConfigurationError("checked-in browser helper is missing")
    input_path = gateway.root / "browser-input.json"
    output_path = gateway.root / "browser-result.json"
    command_path = gateway.root / "browser-command.json"
    ready_path = gateway.root / "browser-ready.json"
    queue_marker = _synthetic_marker(case, "queued")
    recovery_marker = _synthetic_marker(case, "recovered")
    timeout_ms = min(case.remaining_budget.wall_ms - 15_000, 29 * 60 * 1000)
    if timeout_ms <= 0:
        raise DriverBudgetError("browser case has no usable wall-clock budget")
    payload = {
        "schemaVersion": 1,
        "scenario": case.scenario,
        "gatewayUrl": gateway.http_url,
        "sessionKey": f"agent:main:webchat:{uuid.uuid4().hex[:12]}",
        "alternateSessionKey": f"agent:main:webchat:{uuid.uuid4().hex[:12]}",
        "prompt": prompt,
        "marker": marker,
        "queuePrompt": (
            "Describe the attached synthetic text in one sentence and include exactly "
            f"this marker: {queue_marker}"
        ),
        "queueMarker": queue_marker,
        "recoveryPrompt": (
            "Do not call tools. Reply in at least eight streamed chunks and end with "
            f"this exact marker: {recovery_marker}"
        ),
        "recoveryMarker": recovery_marker,
        "timeoutMs": timeout_ms,
        "commandPath": str(command_path),
        "readyPath": str(ready_path),
        "stopPrompts": {
            "reasoning": (
                "Do not call tools. Use deep reasoning to derive a difficult finite-sum proof, "
                f"then end with {marker}."
            ),
            "tool": (
                "Use read_file repeatedly on synthetic-tool-fixture/item-00.txt through "
                f"item-19.txt, then end with {marker}."
            ),
            "output": (
                f"Do not call tools. Produce 300 numbered synthetic lines and end with {marker}."
            ),
            "retry": f"Do not call tools. Reply with {marker}.",
        },
    }
    input_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(input_path, 0o600)
    completed: subprocess.Popen[bytes] | None = None
    try:
        completed = subprocess.Popen(
            [
                "node",
                str(_BROWSER_HELPER),
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ],
            cwd=WEBUI_ROOT,
            env=_browser_child_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Leave the helper enough time to serialize its bounded failure result
        # after the last Playwright assertion reaches its own timeout.
        deadline = (
            time.monotonic()
            + min(
                case.remaining_budget.wall_ms - 5_000,
                timeout_ms + 10_000,
            )
            / 1000
        )
        handled_commands = 0
        while completed.poll() is None:
            if time.monotonic() >= deadline:
                completed.kill()
                completed.wait(timeout=5)
                raise DriverBudgetError("browser case exhausted its wall-clock allowance")
            if command_path.exists():
                try:
                    command = json.loads(command_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise DriverAssertionError("browser restart command is invalid") from exc
                command_path.unlink(missing_ok=True)
                ready_path.unlink(missing_ok=True)
                action = str(command.get("action") or "") if isinstance(command, Mapping) else ""
                if action not in {"restart_graceful", "restart_forced", "reconfigure_retry"}:
                    raise DriverAssertionError("browser requested an unsupported lifecycle action")
                handled_commands += 1
                if handled_commands > 2:
                    raise DriverAssertionError(
                        "browser requested too many Gateway lifecycle actions"
                    )
                if restart_callback is not None:
                    restart_callback(action)
                else:
                    gateway.restart(force=action != "restart_graceful")
                ready_path.write_text('{"ready":true}\n', encoding="utf-8")
                os.chmod(ready_path, 0o600)
            time.sleep(0.05)
        evidence = _load_browser_evidence(output_path, return_code=completed.returncode or 0)
        primary_session = str(payload["sessionKey"])
        return (
            evidence,
            queue_marker,
            primary_session,
            {primary_session, str(payload["alternateSessionKey"])},
        )
    finally:
        if completed is not None and completed.poll() is None:
            completed.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                completed.wait(timeout=5)


def _payload(
    *,
    status: str,
    stage: str,
    latency_ms: int,
    physical_requests: int,
    usage: Mapping[str, int],
    cost: Mapping[str, float],
    counts: Mapping[str, int],
    metrics: Mapping[str, float],
    failure_class: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "stage": stage,
        "physical_requests": physical_requests,
        "latency_ms": max(0, int(latency_ms)),
        "usage": {str(key): int(value) for key, value in usage.items()},
        "cost": {str(key): float(value) for key, value in cost.items()},
        "counts": {str(key): int(value) for key, value in counts.items()},
        "metrics": {str(key): float(value) for key, value in metrics.items()},
    }
    if failure_class is not None:
        result["failure_class"] = failure_class
    return result


def _budget_was_exceeded(case: LiveCase, result: Mapping[str, Any]) -> bool:
    raw_usage = result.get("usage")
    raw_cost = result.get("cost")
    usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}
    cost: Mapping[str, Any] = raw_cost if isinstance(raw_cost, Mapping) else {}
    return bool(
        int(result.get("physical_requests") or 0) > case.remaining_budget.physical_requests
        or int(usage.get("total_tokens") or 0) > case.remaining_budget.billed_tokens
        or float(cost.get("billed_cost_usd") or 0.0) > case.remaining_budget.billed_cost_usd
    )


def _run_rpc_case(
    case: LiveCase,
    gateway: GatewayProcess,
    *,
    proxy: DeterministicFaultProxy | None,
) -> dict[str, Any]:
    prompt, marker = prompt_for_case(case, gateway)
    expected_marker = "synthetic complete" if case.scenario.startswith("fault_") else marker
    session_key = f"agent:main:webchat:{uuid.uuid4().hex[:12]}"
    timeout_seconds = max(
        30.0,
        min(28 * 60.0, case.remaining_budget.wall_ms / 1000 - 15.0),
    )
    started = time.monotonic()
    observation, assistant_bytes, assistant_markers = asyncio.run(
        _send_and_observe(
            gateway,
            prompt=prompt,
            marker=expected_marker,
            session_key=session_key,
            timeout_seconds=timeout_seconds,
        )
    )
    if case.scenario == "tool_compaction":
        if asyncio.run(_manual_compaction(gateway, session_key)):
            observation.compactions += 1
    # The case owns an isolated Gateway and disables provider-backed naming.
    # Count every call record so an unexpected auxiliary request cannot hide
    # outside the selected session and silently evade the hard budget.
    records = gateway.raw_records()
    physical_requests = sum(record.get("kind") == "llm_request" for record in records)
    if proxy is not None:
        physical_requests = max(physical_requests, len(proxy.records))
    raw_usage, raw_cost = _accounting_from_records(records)
    counts, metrics = _base_evidence(
        observation,
        physical_requests=physical_requests,
        records=records,
    )
    counts["output_bytes"] = assistant_bytes
    counts["compactions"] = observation.compactions
    if case.scenario == "router":
        counts["router_decisions"] = asyncio.run(_router_decision_count(gateway, session_key))
    if case.scenario == "fault_429_retry_after" and proxy is not None:
        records_snapshot = proxy.records
        if len(records_snapshot) >= 2:
            retry_wait_ms = max(
                0.0,
                (
                    records_snapshot[1].received_monotonic_ns
                    - records_snapshot[0].received_monotonic_ns
                )
                / 1_000_000,
            )
            metrics["retry_wait_ms"] = retry_wait_ms
            counts["retry_after_honored"] = int(retry_wait_ms >= 8_000)
    if case.scenario == "fallback":
        assert case.fallback_provider is not None
        counts["fallback_before_request"] = int(
            _fallback_preceded_backup_usage_start(
                gateway,
                observation,
            )
        )

    durable_accounting = _add_durable_accounting_evidence(
        gateway,
        counts=counts,
        expected_provider_legs=max(
            physical_requests,
            _MINIMUM_PHYSICAL_REQUESTS[case.scenario],
        ),
    )
    physical_requests = max(physical_requests, durable_accounting.event_count)
    counts["provider_legs"] = physical_requests
    accounting_complete = durable_accounting.event_count == physical_requests
    durable_usage, durable_cost = _accounting_from_durable(durable_accounting)
    usage, cost = _merge_accounting_lower_bounds(
        raw_usage,
        raw_cost,
        durable_usage,
        durable_cost,
    )

    partial_terminal_is_expected = case.scenario == "fault_partial_then_reset"
    passed = observation.completed or (
        partial_terminal_is_expected
        and bool(observation.terminal_event)
        and observation.text_chunks > 0
    )
    if not partial_terminal_is_expected and assistant_markers < 1:
        passed = False
    if case.scenario == "tool_compaction" and observation.compactions < 1:
        passed = False
    if not accounting_complete:
        passed = False
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return _payload(
        status="passed" if passed else "failed",
        stage="terminal" if passed else "provider",
        latency_ms=elapsed_ms,
        physical_requests=physical_requests,
        usage=usage,
        cost=cost,
        counts=counts,
        metrics=metrics,
        failure_class=None if passed else _failure_class_from_records(records),
    )


def _run_browser_case(
    case: LiveCase,
    gateway: GatewayProcess,
    *,
    retry_proxy: DeterministicFaultProxy | None = None,
    performance_evidence: PerformanceGateEvidence | None = None,
) -> dict[str, Any]:
    prompt, marker = prompt_for_case(case, gateway)
    started = time.monotonic()

    def restart_callback(action: str) -> None:
        if action == "reconfigure_retry":
            if retry_proxy is None:
                raise DriverAssertionError("retry phase requested without its fault proxy")
            gateway.stop(force=True)
            gateway.write_config(
                primary_base_url=retry_proxy.base_url,
                force_router=False,
            )
            gateway.start()
            return
        gateway.restart(force=action == "restart_forced")

    evidence, queue_marker, primary_session, _session_keys = _run_browser_helper(
        case,
        gateway,
        prompt=prompt,
        marker=marker,
        restart_callback=restart_callback,
    )
    records = gateway.raw_records()
    physical_requests = sum(record.get("kind") == "llm_request" for record in records)
    if retry_proxy is not None:
        physical_requests = max(physical_requests, len(retry_proxy.records))
    raw_usage, raw_cost = _accounting_from_records(records)
    counts = dict(evidence.counts)
    metrics = dict(evidence.metrics)
    if case.scenario == "long_answer":
        if performance_evidence is None:
            raise DriverAssertionError("long answer is missing deterministic performance evidence")
        counts.update(performance_evidence.counts)
        metrics.update(performance_evidence.metrics)
    counts["provider_legs"] = physical_requests
    durable_accounting = _add_durable_accounting_evidence(
        gateway,
        counts=counts,
        expected_provider_legs=max(
            physical_requests,
            _MINIMUM_PHYSICAL_REQUESTS[case.scenario],
        ),
    )
    physical_requests = max(physical_requests, durable_accounting.event_count)
    counts["provider_legs"] = physical_requests
    accounting_complete = durable_accounting.event_count == physical_requests
    durable_usage, durable_cost = _accounting_from_durable(durable_accounting)
    usage, cost = _merge_accounting_lower_bounds(
        raw_usage,
        raw_cost,
        durable_usage,
        durable_cost,
    )

    if case.scenario.startswith("queue_"):
        client = GatewayRPCClient(scopes=["operator.admin"], request_timeout_s=30.0)

        async def queue_history_count() -> tuple[int, int, int]:
            await client.connect(gateway.ws_url)
            try:
                (
                    _assistant_bytes,
                    _assistant_count,
                    user_count,
                    attachment_count,
                ) = await _history_evidence(
                    client,
                    session_key=primary_session,
                    user_marker=queue_marker,
                )
                pending = await client.call(
                    "sessions.pending_inputs.list",
                    {"key": primary_session},
                )
                pending_items = pending.get("items", []) if isinstance(pending, Mapping) else []
                pending_count = len(pending_items) if isinstance(pending_items, list) else -1
                return user_count, attachment_count, pending_count
            finally:
                await client.close()

        transcript_occurrences, attachment_occurrences, pending_count = asyncio.run(
            queue_history_count()
        )
        counts["transcript_occurrences"] = transcript_occurrences
        counts["dispatched_inputs"] = transcript_occurrences
        counts["attachment_inputs"] = attachment_occurrences
        counts["pending_inputs_remaining"] = max(0, pending_count)
        counts["queue_exact_once"] = int(
            transcript_occurrences == 1 and attachment_occurrences == 1 and pending_count == 0
        )
    elif case.scenario == "browser_stop_each_phase":
        client = GatewayRPCClient(scopes=["operator.admin"], request_timeout_s=30.0)

        async def stop_history_count() -> int:
            await client.connect(gateway.ws_url)
            try:
                return await _cancelled_webui_stop_count(
                    client,
                    session_key=primary_session,
                )
            finally:
                await client.close()

        # Do not trust the optimistic UI click count as cancellation proof.
        # The durable task ledger must project four explicit webui_stop outcomes.
        counts["cancelled_turns"] = asyncio.run(stop_history_count())

    passed = evidence.status == "passed" and accounting_complete
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return _payload(
        status="passed" if passed else "failed",
        stage="browser" if passed else "browser_assertion",
        latency_ms=elapsed_ms,
        physical_requests=physical_requests,
        usage=usage,
        cost=cost,
        counts=counts,
        metrics=metrics,
        failure_class=None if passed else "assertion",
    )


def _accounted_runtime_failure(
    gateway: GatewayProcess,
    *,
    proxy: DeterministicFaultProxy | None,
    retry_proxy: DeterministicFaultProxy | None,
    stage: str,
    failure_class: str,
    started_monotonic: float,
) -> dict[str, Any]:
    records = gateway.raw_records()
    physical_requests = sum(record.get("kind") == "llm_request" for record in records)
    if proxy is not None:
        physical_requests = max(physical_requests, len(proxy.records))
    if retry_proxy is not None:
        physical_requests = max(physical_requests, len(retry_proxy.records))
    accounted_legs, missing_cost_entries = _durable_accounting_from_database(gateway)
    physical_requests = max(physical_requests, accounted_legs)
    usage, cost = _accounting_from_records(records)
    counts = {
        "provider_legs": physical_requests,
        "accounted_provider_legs": accounted_legs,
        "usage_missing_cost_entries": missing_cost_entries,
    }
    return _payload(
        status="inconclusive" if failure_class == "budget" else "failed",
        stage=stage,
        latency_ms=int((time.monotonic() - started_monotonic) * 1000),
        physical_requests=physical_requests,
        usage=usage,
        cost=cost,
        counts=counts,
        metrics={},
        failure_class=failure_class,
    )


def execute_case(case: LiveCase) -> tuple[dict[str, Any], int]:
    required_envs = {CREDENTIAL_ENV_BY_PROVIDER[case.provider]}
    if case.fallback_provider:
        required_envs.add(CREDENTIAL_ENV_BY_PROVIDER[case.fallback_provider])
    secret_values = tuple(
        value for name in sorted(required_envs) if (value := os.environ.get(name, "").strip())
    )
    gateway = GatewayProcess(case, secret_values=secret_values)
    proxy: DeterministicFaultProxy | None = None
    retry_proxy: DeterministicFaultProxy | None = None
    result: dict[str, Any] | None = None
    gateway_started = False
    performance_evidence: PerformanceGateEvidence | None = None
    started_monotonic = time.monotonic()
    try:
        if case.scenario.startswith("fault_") or case.scenario == "fallback":
            proxy = DeterministicFaultProxy(
                _fault_sequence(case.scenario),
                late_terminal_delay_seconds=0.25,
            ).start()
        if case.scenario == "browser_stop_each_phase":
            _write_tool_fixture(gateway)
            retry_proxy = DeterministicFaultProxy(
                (FaultScenario.RATE_LIMITED, FaultScenario.OK),
            ).start()
        if case.scenario == "fallback":
            assert proxy is not None
            gateway.write_config(routed_base_url=proxy.base_url)
        elif case.scenario.startswith("fault_"):
            assert proxy is not None
            gateway.write_config(primary_base_url=proxy.base_url)
        else:
            gateway.write_config()
        gateway.start()
        gateway_started = True
        if case.scenario == "long_answer":
            # Reports are read only after the isolated Gateway exists so a
            # malformed or absent report becomes a normal assertion row with
            # zero provider requests, not a coordinator configuration crash.
            performance_evidence = _load_performance_gate_evidence()

        if (
            case.scenario in {"long_answer", "long_reasoning"}
            or case.scenario.startswith("browser_")
            or case.scenario.startswith("queue_")
        ):
            result = _run_browser_case(
                case,
                gateway,
                retry_proxy=retry_proxy,
                performance_evidence=performance_evidence,
            )
        else:
            result = _run_rpc_case(case, gateway, proxy=proxy)
        if _budget_was_exceeded(case, result):
            result["status"] = "inconclusive"
            result["stage"] = "budget"
            result["failure_class"] = "budget"
            exit_code = EXIT_BUDGET
        else:
            exit_code = EXIT_PASSED if result["status"] == "passed" else EXIT_FAILED
    except KeyboardInterrupt:
        raise
    except DriverBudgetError:
        if not gateway_started:
            raise
        result = _accounted_runtime_failure(
            gateway,
            proxy=proxy,
            retry_proxy=retry_proxy,
            stage="budget",
            failure_class="budget",
            started_monotonic=started_monotonic,
        )
        exit_code = EXIT_BUDGET
    except DriverConfigurationError:
        if not gateway_started:
            raise
        result = _accounted_runtime_failure(
            gateway,
            proxy=proxy,
            retry_proxy=retry_proxy,
            stage="runtime_configuration",
            failure_class="configuration",
            started_monotonic=started_monotonic,
        )
        exit_code = EXIT_CONFIGURATION
    except DriverAssertionError:
        result = _accounted_runtime_failure(
            gateway,
            proxy=proxy,
            retry_proxy=retry_proxy,
            stage="browser_assertion",
            failure_class="assertion",
            started_monotonic=started_monotonic,
        )
        exit_code = EXIT_FAILED
    except TimeoutError:
        result = _accounted_runtime_failure(
            gateway,
            proxy=proxy,
            retry_proxy=retry_proxy,
            stage="case_timeout",
            failure_class="inconclusive",
            started_monotonic=started_monotonic,
        )
        exit_code = EXIT_FAILED
    except Exception:
        result = _accounted_runtime_failure(
            gateway,
            proxy=proxy,
            retry_proxy=retry_proxy,
            stage="driver",
            failure_class="implementation",
            started_monotonic=started_monotonic,
        )
        exit_code = EXIT_FAILED
    finally:
        if proxy is not None:
            proxy.close()
        if retry_proxy is not None:
            retry_proxy.close()
        try:
            gateway.cleanup()
        except Exception as exc:
            if result is None:
                raise
            result.update(
                {
                    "status": "failed",
                    "stage": _artifact_cleanup_stage(exc),
                    "failure_class": "implementation",
                }
            )
            exit_code = EXIT_FAILED
    assert result is not None
    return result, exit_code


def _failure_payload(*, stage: str, failure_class: str) -> dict[str, Any]:
    return _payload(
        status="failed" if failure_class != "budget" else "inconclusive",
        stage=stage,
        latency_ms=0,
        physical_requests=0,
        usage={},
        cost={},
        counts={},
        metrics={},
        failure_class=failure_class,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    case_path: Path | None = None
    output_path: Path | None = None
    secret_values: tuple[str, ...] = ()
    result: dict[str, Any]
    try:
        case_path = _safe_case_path(args.case_file)
        output_path = _safe_output_path(args.output, case_path=case_path)
        case = load_case(case_path)
        required_envs = {CREDENTIAL_ENV_BY_PROVIDER[case.provider]}
        if case.fallback_provider:
            required_envs.add(CREDENTIAL_ENV_BY_PROVIDER[case.fallback_provider])
        secret_values = tuple(
            value for name in sorted(required_envs) if (value := os.environ.get(name, "").strip())
        )
        result, exit_code = execute_case(case)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except DriverBudgetError:
        result = _failure_payload(stage="budget", failure_class="budget")
        exit_code = EXIT_BUDGET
    except DriverConfigurationError:
        result = _failure_payload(stage="driver_preflight", failure_class="configuration")
        exit_code = EXIT_CONFIGURATION
    except DriverAssertionError:
        result = _failure_payload(stage="browser_assertion", failure_class="assertion")
        exit_code = EXIT_FAILED
    except TimeoutError:
        result = _failure_payload(stage="case_timeout", failure_class="inconclusive")
        exit_code = EXIT_FAILED
    except Exception:
        # Never serialize an exception: provider errors can contain headers or
        # response bodies. The bounded taxonomy is the complete public detail.
        result = _failure_payload(stage="driver", failure_class="implementation")
        exit_code = EXIT_FAILED

    if output_path is None:
        if case_path is None:
            with contextlib.suppress(Exception):
                case_path = _safe_case_path(args.case_file)
        if case_path is None:
            return EXIT_CONFIGURATION
        try:
            output_path = _safe_output_path(args.output, case_path=case_path)
        except DriverConfigurationError:
            return EXIT_CONFIGURATION
    write_safe_report(output_path, result, secret_values)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
