from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import live_long_task_case_driver as driver
from scripts.long_task_fault_proxy import (
    DeterministicFaultProxy,
    FaultRequestRecord,
    FaultScenario,
)


def _case_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "opensquilla-long-task-case-synthetic"
    directory.mkdir()
    return directory


def _case_payload(
    *,
    provider: str = "deepseek",
    model: str = "deepseek-v4-flash",
    scenario: str = "direct",
    fallback_provider: str | None = None,
    physical_requests: int = 10,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "case_id": f"{provider}-{scenario}-synthetic-1".replace("_", "-"),
        "provider": provider,
        "model": model,
        "scenario": scenario,
        "repeat_index": 1,
        "fallback_provider": fallback_provider,
        "remaining_budget": {
            "wall_ms": 60_000,
            "billed_cost_usd": 1.0,
            "physical_requests": physical_requests,
            "billed_tokens": 10_000,
        },
    }


def _write_case(directory: Path, payload: dict[str, object]) -> Path:
    path = directory / "case.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_case_accepts_only_matrix_identity_and_env_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _case_directory(tmp_path)
    path = _write_case(directory, _case_payload())
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-not-a-real-key")

    case = driver.load_case(path)

    assert case.provider == "deepseek"
    assert case.model == "deepseek-v4-flash"
    assert case.scenario == "direct"


def test_load_case_rejects_extra_fields_before_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _case_directory(tmp_path)
    payload = _case_payload()
    payload["prompt"] = "must never enter the coordinator protocol"
    path = _write_case(directory, payload)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-not-a-real-key")

    with pytest.raises(driver.DriverConfigurationError):
        driver.load_case(path)


def test_load_case_fails_when_remaining_requests_cannot_cover_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _case_directory(tmp_path)
    path = _write_case(
        directory,
        _case_payload(
            scenario="browser_stop_each_phase",
            model="deepseek-v4-pro",
            physical_requests=3,
        ),
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-not-a-real-key")

    with pytest.raises(driver.DriverBudgetError):
        driver.load_case(path)


def test_long_reasoning_is_executed_through_real_browser_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = driver.LiveCase(
        case_id="deepseek-long-reasoning-synthetic-1",
        provider="deepseek",
        model="deepseek-v4-pro",
        scenario="long_reasoning",
        repeat_index=1,
        fallback_provider=None,
        remaining_budget=driver.CaseBudget(
            wall_ms=60_000,
            billed_cost_usd=1,
            physical_requests=2,
            billed_tokens=10_000,
        ),
    )
    calls: list[str] = []

    class Gateway:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def write_config(self, **_kwargs: object) -> None:
            calls.append("write_config")

        def start(self) -> None:
            calls.append("start")

        def cleanup(self) -> None:
            calls.append("cleanup")

    def browser_case(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("browser")
        return {
            "status": "passed",
            "stage": "browser",
            "physical_requests": 1,
            "latency_ms": 1,
            "usage": {"total_tokens": 1},
            "cost": {"billed_cost_usd": 0.0},
            "counts": {"provider_legs": 1, "accounted_provider_legs": 1},
            "metrics": {},
        }

    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.setattr(driver, "GatewayProcess", Gateway)
    monkeypatch.setattr(driver, "_run_browser_case", browser_case)
    monkeypatch.setattr(
        driver,
        "_run_rpc_case",
        lambda *_args, **_kwargs: pytest.fail("long reasoning must not use the RPC-only path"),
    )

    result, exit_code = driver.execute_case(case)

    assert exit_code == driver.EXIT_PASSED
    assert result["status"] == "passed"
    assert calls == ["write_config", "start", "browser", "cleanup"]


def test_tool_compaction_reserves_provider_tool_followup_and_summary_legs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _case_directory(tmp_path)
    path = _write_case(
        directory,
        _case_payload(
            scenario="tool_compaction",
            model="deepseek-v4-pro",
            physical_requests=2,
        ),
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-not-a-real-key")

    with pytest.raises(driver.DriverBudgetError):
        driver.load_case(path)


def test_send_and_observe_waits_for_assistant_history_after_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "agent:main:webchat:synthetic"
    marker = "synthetic complete"
    events = [
        {
            "event": "session.event.text_delta",
            "payload": {"session_key": session_key, "text": marker},
        },
        {
            "event": "session.event.done",
            "payload": {"session_key": session_key, "reason": "completed"},
        },
    ]
    calls: list[str] = []
    history_calls = 0

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def connect(self, _url: str) -> None:
            calls.append("connect")

        async def call(
            self,
            method: str,
            _params: dict[str, object] | None = None,
        ) -> dict[str, object]:
            nonlocal history_calls
            calls.append(method)
            if method == "chat.history":
                history_calls += 1
                if history_calls == 1:
                    return {"messages": []}
                return {"messages": [{"role": "assistant", "text": marker}]}
            return {}

        async def recv_event(self, *, timeout: float) -> dict[str, object]:
            assert timeout > 0
            if events:
                return events.pop(0)
            raise TimeoutError

        async def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(driver, "GatewayRPCClient", Client)

    observation, assistant_bytes, assistant_markers = asyncio.run(
        driver._send_and_observe(
            SimpleNamespace(ws_url="ws://synthetic"),
            prompt="synthetic prompt",
            marker=marker,
            session_key=session_key,
            timeout_seconds=5,
        )
    )

    assert observation.completed is True
    assert assistant_bytes == len(marker.encode("utf-8"))
    assert assistant_markers == 1
    assert history_calls == 2
    assert calls == [
        "connect",
        "sessions.messages.subscribe",
        "sessions.send",
        "chat.history",
        "chat.history",
        "close",
    ]


def test_gateway_config_contains_env_names_but_not_credential_values(
    tmp_path: Path,
) -> None:
    case = driver.LiveCase(
        case_id="tokenrhythm-fallback-synthetic-1",
        provider="tokenrhythm",
        model="deepseek-v4-pro",
        scenario="fallback",
        repeat_index=1,
        fallback_provider="deepseek",
        remaining_budget=driver.CaseBudget(
            wall_ms=60_000,
            billed_cost_usd=1,
            physical_requests=4,
            billed_tokens=10_000,
        ),
    )
    credential = "synthetic-provider-secret-that-must-not-be-rendered"

    rendered = driver.render_gateway_config(
        case,
        workspace_dir=tmp_path,
        routed_base_url="http://127.0.0.1:12345/v1",
    )

    assert credential not in rendered
    assert 'api_key_env = "DEEPSEEK_API_KEY"' in rendered
    assert 'api_key_env = "TOKENRHYTHM_API_KEY"' in rendered
    assert "http://127.0.0.1:12345/v1" in rendered
    assert "enabled = true" in rendered


def test_gateway_cleanup_retries_transient_windows_file_handle_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = driver.LiveCase(
        case_id="deepseek-direct-cleanup-synthetic-1",
        provider="deepseek",
        model="deepseek-v4-flash",
        scenario="direct",
        repeat_index=1,
        fallback_provider=None,
        remaining_budget=driver.CaseBudget(
            wall_ms=60_000,
            billed_cost_usd=1,
            physical_requests=1,
            billed_tokens=1_000,
        ),
    )
    gateway = driver.GatewayProcess(case, secret_values=())
    real_cleanup = driver.scan_and_remove_temporary_tree
    attempts = 0

    def transient_cleanup(path: Path, secrets: tuple[str, ...]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("synthetic transient Windows file handle")
        real_cleanup(path, secrets)

    monkeypatch.setattr(driver, "scan_and_remove_temporary_tree", transient_cleanup)
    monkeypatch.setattr(driver.time, "sleep", lambda _seconds: None)

    gateway.cleanup()

    assert attempts == 3
    assert not gateway.root.exists()


def test_gateway_cleanup_does_not_retry_security_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = driver.LiveCase(
        case_id="deepseek-direct-cleanup-security-synthetic-1",
        provider="deepseek",
        model="deepseek-v4-flash",
        scenario="direct",
        repeat_index=1,
        fallback_provider=None,
        remaining_budget=driver.CaseBudget(
            wall_ms=60_000,
            billed_cost_usd=1,
            physical_requests=1,
            billed_tokens=1_000,
        ),
    )
    gateway = driver.GatewayProcess(case, secret_values=())
    real_cleanup = driver.scan_and_remove_temporary_tree
    attempts = 0

    def unsafe_cleanup(_path: Path, _secrets: tuple[str, ...]) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("credential detected in temporary live artifacts")

    monkeypatch.setattr(driver, "scan_and_remove_temporary_tree", unsafe_cleanup)

    with pytest.raises(RuntimeError, match="credential detected"):
        gateway.cleanup()

    assert attempts == 1
    real_cleanup(gateway.root, ())


def test_gateway_force_stop_terminates_owned_windows_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = driver.LiveCase(
        case_id="deepseek-direct-stop-synthetic-1",
        provider="deepseek",
        model="deepseek-v4-flash",
        scenario="direct",
        repeat_index=1,
        fallback_provider=None,
        remaining_budget=driver.CaseBudget(
            wall_ms=60_000,
            billed_cost_usd=1,
            physical_requests=1,
            billed_tokens=1_000,
        ),
    )
    gateway = driver.GatewayProcess(case, secret_values=())
    process_calls: list[str] = []
    poll_results = iter((None, 0))
    proc = SimpleNamespace(
        pid=4312,
        poll=lambda: next(poll_results, 0),
        kill=lambda: process_calls.append("kill"),
        wait=lambda **_kwargs: 0,
    )
    gateway.proc = proc
    calls: list[tuple[object, ...]] = []
    original_os_name = driver.os.name
    monkeypatch.setattr(driver.os, "name", "nt")
    monkeypatch.setattr(
        driver.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    gateway.stop(force=True)

    assert calls[0][0] == (["taskkill", "/PID", "4312", "/T", "/F"],)
    assert calls[0][1]["timeout"] == 10
    assert process_calls == []
    assert gateway.proc is None
    monkeypatch.setattr(driver.os, "name", original_os_name)
    driver.scan_and_remove_temporary_tree(gateway.root, ())


@pytest.mark.parametrize(
    ("error", "stage"),
    [
        (PermissionError("synthetic"), "artifact_delete_failed"),
        (
            RuntimeError("unable to scan temporary live artifacts before deletion"),
            "artifact_scan_failed",
        ),
        (
            RuntimeError("credential detected in temporary live artifacts"),
            "artifact_secret_detected",
        ),
        (RuntimeError("synthetic unknown cleanup failure"), "artifact_cleanup_failed"),
    ],
)
def test_artifact_cleanup_stage_is_stable_and_non_sensitive(
    error: Exception,
    stage: str,
) -> None:
    assert driver._artifact_cleanup_stage(error) == stage


def test_turn_observation_keeps_only_bounded_marker_tail_and_numeric_evidence() -> None:
    observation = driver.TurnObservation(
        session_key="agent:main:webchat:synthetic",
        marker="OSQ_MARKER",
        started_monotonic=0.0,
    )
    observation.consume(
        {
            "event": "session.event.provider_activity",
            "payload": {
                "session_key": observation.session_key,
                "phase": "reasoning",
                "started_at": 1000,
                "emitted_at": 1050,
                "heartbeat": False,
            },
        }
    )
    observation.consume(
        {
            "event": "session.event.text_delta",
            "payload": {
                "session_key": observation.session_key,
                "text": "synthetic OSQ_MARKER",
            },
        }
    )

    assert observation.marker_seen_in_stream is True
    assert observation.activity_latency_ms == 50
    assert observation.text_bytes == len(b"synthetic OSQ_MARKER")
    assert len(observation._marker_tail) <= 1024


def test_usage_aggregation_accounts_failed_and_successful_execution_legs() -> None:
    records = [
        {
            "kind": "llm_error",
            "payload": {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "reasoning_tokens": 3,
                    "billed_cost": 0.01,
                }
            },
        },
        {
            "kind": "llm_response",
            "payload": {
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 5,
                    "reasoning_tokens": 7,
                    "billed_cost": 0.02,
                }
            },
        },
    ]

    usage, cost = driver._accounting_from_records(records)

    assert usage == {
        "input_tokens": 30,
        "output_tokens": 7,
        "reasoning_tokens": 10,
        "cached_tokens": 0,
        "total_tokens": 47,
    }
    assert cost["billed_cost_usd"] == pytest.approx(0.03)


def test_failure_classification_reads_bounded_nested_provider_error() -> None:
    assert (
        driver._failure_class_from_records(
            [
                {
                    "kind": "llm_error",
                    "payload": {
                        "error": {
                            "code": "model_not_found",
                            "message": "synthetic model unavailable",
                        }
                    },
                }
            ]
        )
        == "model-unavailable"
    )


def test_browser_result_rejects_non_numeric_or_extra_evidence(tmp_path: Path) -> None:
    path = tmp_path / "browser.json"
    path.write_text(
        json.dumps(
            {
                "status": "passed",
                "counts": {"incremental_chunks": 2},
                "metrics": {},
                "response": "raw response must never be accepted",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(driver.DriverAssertionError):
        driver._load_browser_evidence(path, return_code=0)


def _write_performance_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path]:
    fixture = {
        "historyMessages": 200,
        "reasoningDeltas": 20_000,
        "toolFragments": 10_000,
        "textDeltas": 4_000,
        "textBytes": 128 * 1_024,
    }
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    resilience = tmp_path / "resilience.json"
    baseline.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "mode": "baseline",
                "fixture": fixture,
                "peakHeapDeltaBytes": 100_000,
                "recalcStyleCount": 10_000,
            }
        ),
        encoding="utf-8",
    )
    candidate.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "mode": "candidate",
                "fixture": fixture,
                "peakHeapDeltaBytes": 49_000,
                "recalcStyleCount": 2_900,
            }
        ),
        encoding="utf-8",
    )
    resilience.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "fixture": {key: value for key, value in fixture.items() if key != "textBytes"},
                "inputP95": 40,
                "inputMax": 80,
                "longestTask": 100,
                "domNodes": 2_000,
                "ordinaryRows": 20,
                "bottomGapWhileFollowing": 1,
                "upscrollAnchorDrift": 1,
                "peakHeapDeltaBytes": 40_000_000,
                "postGcHeapDeltaBytes": 10_000_000,
                "maxRetentionGrowthPerTurnBytes": 1_000_000,
                "liveParseReduction": 0.96,
            }
        ),
        encoding="utf-8",
    )
    for name, path in zip(
        ("baseline", "candidate", "resilience"),
        (baseline, candidate, resilience),
        strict=True,
    ):
        monkeypatch.setenv(driver.PERFORMANCE_REPORT_ENV[name], str(path))
    return baseline, candidate, resilience


def test_performance_gate_uses_shared_fixed_fixture_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_performance_reports(tmp_path, monkeypatch)

    evidence = driver._load_performance_gate_evidence()

    assert evidence.counts == {"dom_nodes": 2_000, "mounted_rows": 20}
    assert evidence.metrics["markdown_parse_reduction_pct"] == pytest.approx(96)
    assert evidence.metrics["recalc_style_reduction_pct"] == pytest.approx(71)
    assert evidence.metrics["peak_heap_reduction_pct"] == pytest.approx(51)
    assert evidence.metrics["post_gc_growth_bytes"] == pytest.approx(1_000_000)


def test_performance_gate_fails_closed_on_missing_or_mismatched_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _baseline, candidate, _resilience = _write_performance_reports(tmp_path, monkeypatch)
    candidate.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "mode": "candidate",
                "fixture": {},
                "peakHeapDeltaBytes": 1,
                "recalcStyleCount": 1,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(driver.DriverAssertionError):
        driver._load_performance_gate_evidence()

    monkeypatch.delenv(driver.PERFORMANCE_REPORT_ENV["candidate"])
    with pytest.raises(driver.DriverAssertionError):
        driver._load_performance_gate_evidence()


def test_fault_retry_wait_uses_non_sensitive_monotonic_arrival_metadata() -> None:
    # The record field is sufficient to prove request spacing and does not
    # retain headers, authorization, prompt, or response bodies.
    record_fields = set(FaultRequestRecord.__dataclass_fields__)
    assert "received_monotonic_ns" in record_fields
    assert not record_fields.intersection({"headers", "authorization", "prompt", "body"})


def test_fallback_order_uses_activity_and_durable_backup_start(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE usage_events (provider TEXT, started_at_ms INTEGER)")
        connection.execute(
            "INSERT INTO usage_events(provider, started_at_ms) VALUES (?, ?)",
            ("deepseek", 2_000),
        )
    observation = driver.TurnObservation(
        session_key="agent:main:webchat:synthetic",
        marker="OSQ_SYNTHETIC",
        started_monotonic=0,
        activity_phases=[("fallback", 1.0, 1_999, False)],
    )

    assert driver._fallback_preceded_backup_usage_start(
        SimpleNamespace(state_dir=tmp_path),
        observation,
    )

    observation.activity_phases = [("fallback", 1.0, 2_001, False)]
    assert not driver._fallback_preceded_backup_usage_start(
        SimpleNamespace(state_dir=tmp_path),
        observation,
    )


@pytest.mark.parametrize(
    ("reader", "expected"),
    [
        (driver._durable_accounting_from_database, (2, 0)),
        (
            lambda gateway: driver._fallback_preceded_backup_usage_start(
                gateway,
                driver.TurnObservation(
                    session_key="agent:main:webchat:synthetic",
                    marker="OSQ_SYNTHETIC",
                    started_monotonic=0,
                    activity_phases=[("fallback", 1.0, 1_999, False)],
                ),
            ),
            True,
        ),
    ],
)
def test_durable_usage_readers_close_sqlite_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: object,
    expected: object,
) -> None:
    database = tmp_path / "sessions.db"
    database.touch()
    closed = False

    class Cursor:
        def __init__(self, row: tuple[int, int]) -> None:
            self.row = row

        def fetchone(self) -> tuple[int, int]:
            return self.row

    class Connection:
        def execute(self, query: str) -> Cursor:
            return Cursor((2, 0) if "COUNT(*)" in query else (2_000, 0))

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(driver.sqlite3, "connect", lambda *_args, **_kwargs: Connection())

    assert callable(reader)
    assert reader(SimpleNamespace(state_dir=tmp_path)) == expected
    assert closed is True


def test_durable_usage_reader_closes_sqlite_handle_after_query_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "sessions.db").touch()
    closed = False

    class Connection:
        def execute(self, _query: str) -> object:
            raise sqlite3.OperationalError("synthetic query failure")

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(driver.sqlite3, "connect", lambda *_args, **_kwargs: Connection())

    assert driver._durable_accounting_from_database(SimpleNamespace(state_dir=tmp_path)) == (0, 0)
    assert closed is True


def test_stop_count_requires_durable_webui_stop_outcomes() -> None:
    class Client:
        async def call(self, _method: str, _params: object) -> dict[str, object]:
            return {
                "turn_outcomes": [
                    {
                        "status": "cancelled",
                        "outcome": {"cancellation_source": "webui_stop"},
                    },
                    {
                        "status": "cancelled",
                        "outcome": {"cancellation_source": "gateway_restart"},
                    },
                    {
                        "status": "succeeded",
                        "outcome": {"cancellation_source": "webui_stop"},
                    },
                ]
            }

    count = asyncio.run(
        driver._cancelled_webui_stop_count(
            Client(),
            session_key="agent:main:webchat:synthetic",
        )
    )

    assert count == 1


def test_terminal_history_evidence_waits_for_durable_assistant_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = iter(
        [
            (0, 0, 0, 0),
            (18, 1, 0, 0),
        ]
    )
    calls = 0

    async def history_evidence(
        _client: object,
        *,
        session_key: str,
        assistant_marker: str,
        user_marker: str = "",
    ) -> tuple[int, int, int, int]:
        nonlocal calls
        calls += 1
        assert session_key == "agent:main:webchat:synthetic"
        assert assistant_marker == "synthetic complete"
        assert user_marker == ""
        return next(observations)

    monkeypatch.setattr(driver, "_history_evidence", history_evidence)

    class Client:
        recv_calls = 0

        async def recv_event(self, *, timeout: float) -> dict[str, object]:
            self.recv_calls += 1
            assert timeout > 0
            return {"event": "history-settle"}

    class Observation:
        frames: list[dict[str, object]] = []

        def consume(self, frame: dict[str, object]) -> None:
            self.frames.append(frame)

    client = Client()
    observation = Observation()

    result = asyncio.run(
        driver._wait_for_assistant_history_evidence(
            client,
            observation,
            session_key="agent:main:webchat:synthetic",
            assistant_marker="synthetic complete",
            deadline=driver.time.monotonic() + 1.0,
        )
    )

    assert result == (18, 1, 0, 0)
    assert calls == 2
    assert client.recv_calls == 1
    assert observation.frames == [{"event": "history-settle"}]


def test_terminal_history_evidence_does_not_extend_case_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def history_evidence(
        _client: object,
        *,
        session_key: str,
        assistant_marker: str,
        user_marker: str = "",
    ) -> tuple[int, int, int, int]:
        nonlocal calls
        calls += 1
        return (0, 0, 0, 0)

    monkeypatch.setattr(driver, "_history_evidence", history_evidence)

    class Client:
        async def recv_event(self, *, timeout: float) -> dict[str, object]:
            raise AssertionError(f"expired deadline waited for {timeout}")

    class Observation:
        def consume(self, frame: dict[str, object]) -> None:
            raise AssertionError(f"expired deadline consumed {frame}")

    result = asyncio.run(
        driver._wait_for_assistant_history_evidence(
            Client(),
            Observation(),
            session_key="agent:main:webchat:synthetic",
            assistant_marker="synthetic complete",
            deadline=driver.time.monotonic(),
        )
    )

    assert result == (0, 0, 0, 0)
    assert calls == 1


def test_runtime_failure_preserves_physical_usage_and_cost_budget_evidence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE usage_events (
                status TEXT,
                missing_cost_entries INTEGER
            )
            """
        )
        connection.executemany(
            "INSERT INTO usage_events(status, missing_cost_entries) VALUES (?, ?)",
            [("unknown", 0), ("finalized", 0)],
        )
    gateway = SimpleNamespace(
        state_dir=tmp_path,
        root=tmp_path,
        raw_records=lambda: [
            {"kind": "llm_request", "payload": {}},
            {
                "kind": "llm_error",
                "payload": {
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "billed_cost": 0.01,
                    }
                },
            },
        ],
    )

    result = driver._accounted_runtime_failure(
        gateway,
        proxy=None,
        retry_proxy=None,
        stage="driver",
        failure_class="implementation",
        started_monotonic=0,
    )

    assert result["physical_requests"] == 2
    assert result["counts"]["accounted_provider_legs"] == 2
    assert result["counts"]["usage_missing_cost_entries"] == 1
    assert result["usage"]["total_tokens"] == 3
    assert result["cost"]["billed_cost_usd"] == pytest.approx(0.01)


def test_fault_case_executes_through_isolated_gateway_without_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-not-a-real-key")
    case = driver.LiveCase(
        case_id="deepseek-fault-503-synthetic-1",
        provider="deepseek",
        model="deepseek-v4-flash",
        scenario="fault_503",
        repeat_index=1,
        fallback_provider=None,
        remaining_budget=driver.CaseBudget(
            wall_ms=120_000,
            billed_cost_usd=1,
            physical_requests=4,
            billed_tokens=10_000,
        ),
    )

    result, exit_code = driver.execute_case(case)

    assert exit_code == driver.EXIT_PASSED, json.dumps(result, sort_keys=True)
    assert result["status"] == "passed"
    assert result["physical_requests"] == 2
    assert result["counts"]["retry_legs"] >= 1
    assert result["counts"]["accounted_provider_legs"] == 2
    assert result["counts"]["usage_missing_cost_entries"] >= 1
    assert result["usage"]["total_tokens"] == 0
    assert result["cost"]["billed_cost_usd"] == 0


def test_fault_429_case_proves_retry_after_was_not_violated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-not-a-real-key")
    case = driver.LiveCase(
        case_id="deepseek-fault-429-retry-after-synthetic-1",
        provider="deepseek",
        model="deepseek-v4-flash",
        scenario="fault_429_retry_after",
        repeat_index=1,
        fallback_provider=None,
        remaining_budget=driver.CaseBudget(
            wall_ms=120_000,
            billed_cost_usd=1,
            physical_requests=4,
            billed_tokens=10_000,
        ),
    )

    result, exit_code = driver.execute_case(case)

    assert exit_code == driver.EXIT_PASSED
    assert result["metrics"]["retry_wait_ms"] >= 8_000
    assert result["counts"]["retry_after_honored"] == 1
    assert result["counts"]["accounted_provider_legs"] == 2


def test_fallback_case_proves_activity_precedes_backup_request_without_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    case = driver.LiveCase(
        case_id="deepseek-fallback-openrouter-synthetic-1",
        provider="deepseek",
        model="deepseek-v4-pro",
        scenario="fallback",
        repeat_index=1,
        fallback_provider="openrouter",
        remaining_budget=driver.CaseBudget(
            wall_ms=120_000,
            billed_cost_usd=1,
            physical_requests=4,
            billed_tokens=10_000,
        ),
    )
    backup = DeterministicFaultProxy(
        (FaultScenario.OK,),
        completion_text=driver._synthetic_marker(case),
    ).start()
    original_registry_endpoint = driver.registry_endpoint
    monkeypatch.setattr(
        driver,
        "registry_endpoint",
        lambda provider: (
            backup.base_url if provider == "openrouter" else original_registry_endpoint(provider)
        ),
    )

    try:
        result, exit_code = driver.execute_case(case)
    finally:
        backup.close()

    assert exit_code == driver.EXIT_PASSED, json.dumps(result, sort_keys=True)
    assert result["physical_requests"] == 2
    assert result["counts"]["fallback_legs"] >= 1
    assert result["counts"]["fallback_before_request"] == 1
    assert result["counts"]["accounted_provider_legs"] == 2
