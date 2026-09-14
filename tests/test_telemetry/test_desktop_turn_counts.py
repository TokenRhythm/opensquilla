from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.telemetry.consent import TelemetryScope, resolve_scope_consent
from opensquilla.telemetry.contracts import CURRENT_NOTICE_VERSION_BY_SCOPE
from opensquilla.telemetry.contracts.common import Platform, ResultOutcome
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.desktop_state import clear_desktop_early_spool_scope
from opensquilla.telemetry.desktop_turn_counts import (
    SESSION_MARKER_NAME,
    TURN_COUNTS_PREFIX,
)
from opensquilla.telemetry.recorder import RecordStatus
from opensquilla.telemetry.reliability_sink import ReliabilityEventSink
from opensquilla.telemetry.runtime import ScopedTelemetryRuntime

APP_SESSION = "00000000-0000-4000-8000-000000000001"
STARTED_AT = datetime(2026, 9, 2, tzinfo=UTC)
CONSENTED_AT = "2026-09-02T00:00:00.000Z"
NOTICE = CURRENT_NOTICE_VERSION_BY_SCOPE["reliability"]


def _config(state_dir: Path):
    config = SimpleNamespace(
        state_dir=state_dir,
        privacy=SimpleNamespace(
            reliability_diagnostics_enabled=True,
            reliability_notice_version=NOTICE,
            reliability_consented_at_utc=CONSENTED_AT,
        ),
    )
    scope_consent_coordinator_for(
        config,
        state_provider=lambda scope: resolve_scope_consent(scope, config=config, env={}),
    )
    return config


def _active_session(state_dir: Path) -> Path:
    directory = state_dir / "telemetry" / "desktop-early-spool" / "reliability"
    directory.mkdir(parents=True)
    (directory / SESSION_MARKER_NAME).write_text(
        json.dumps(
            {
                "schema_version": 3,
                "marker_kind": "desktop_reliability_session",
                "app_session_id": APP_SESSION,
                "started_at_ms": int(STARTED_AT.timestamp() * 1000),
                "consent_generation": f"{NOTICE}\n{CONSENTED_AT}",
                "clean_exit": False,
                "performance_summary_emitted": False,
                "gateway_turn_counts_applied": False,
            }
        )
    )
    (state_dir / "telemetry" / "desktop-consent-mirror.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "reliability": {
                    "enabled": True,
                    "forced_off": False,
                    "notice_version": NOTICE,
                    "consented_at_utc": CONSENTED_AT,
                },
            }
        )
    )
    return directory / f"{TURN_COUNTS_PREFIX}{APP_SESSION}.tmp"


async def _observe_turn(runtime: ScopedTelemetryRuntime, stalls: int) -> None:
    sink = ReliabilityEventSink(
        runtime,
        app_version="1.2.3",
        platform=Platform.MACOS,
        clock=lambda: datetime(2026, 9, 2, 0, 0, 1, tzinfo=UTC),
    )
    sink.observe_turn(
        SimpleNamespace(
            outcome=ResultOutcome.SUCCESS,
            error_code=None,
            failure_stage=None,
            duration_ms=1000,
            ttft_ms=100,
            stall_count=stalls,
        )
    )
    # The real producer is fire-and-forget; wait for its accepted local work.
    await asyncio.gather(*tuple(runtime._record_tasks))


@pytest.fixture(autouse=True)
def offline_uploader(monkeypatch):
    async def no_network(self, scope):
        return None

    monkeypatch.setattr(ScopedTelemetryRuntime, "upload_once", no_network)


async def test_terminal_observer_counts_survive_gateway_restart_and_queue_replay(tmp_path):
    target = _active_session(tmp_path)
    config = _config(tmp_path)
    runtime = ScopedTelemetryRuntime(config=config, env={})
    await _observe_turn(runtime, 0)
    # Re-enqueuing the same accepted event must not count another terminal turn.
    outbox = runtime._scopes[TelemetryScope.RELIABILITY].outbox
    lease = await outbox.claim_batch()
    assert lease is not None
    from opensquilla.telemetry.contracts import TELEMETRY_EVENT_ADAPTER

    event = TELEMETRY_EVENT_ADAPTER.validate_json(lease.events[0].payload, strict=True)
    assert (await runtime.record(event)).status is RecordStatus.DUPLICATE
    await runtime.close()
    second = ScopedTelemetryRuntime(config=config, env={})
    await _observe_turn(second, 2)
    await second.close()
    counts = json.loads(target.read_text())
    assert (counts["turn_count"], counts["stalled_turn_count"], counts["stall_count"]) == (2, 1, 2)
    assert "event_id" not in counts


async def test_withdrawal_does_not_recreate_turn_counter_state(tmp_path):
    target = _active_session(tmp_path)
    config = _config(tmp_path)
    runtime = ScopedTelemetryRuntime(config=config, env={})
    await _observe_turn(runtime, 1)
    config.privacy.reliability_diagnostics_enabled = False
    cleanup = clear_desktop_early_spool_scope(tmp_path, TelemetryScope.RELIABILITY)
    assert cleanup.complete
    await _observe_turn(runtime, 1)
    await runtime.close()
    assert not target.parent.exists()


@pytest.mark.parametrize("change", ["rotated", "closed", "renewed", "symlink"])
async def test_late_or_untrusted_terminal_counter_write_is_ignored(tmp_path, change):
    target = _active_session(tmp_path)
    directory = target.parent
    marker_path = directory / SESSION_MARKER_NAME
    marker = json.loads(marker_path.read_text())
    if change == "rotated":
        marker["started_at_ms"] += 10_000
    elif change == "closed":
        marker["clean_exit"] = True
    elif change == "renewed":
        marker["consent_generation"] = "old-grant"
    else:
        outside = tmp_path / "outside.json"
        outside.write_text("{}")
        try:
            target.symlink_to(outside)
        except OSError:
            pytest.skip("filesystem does not permit symlink creation")
    marker_path.write_text(json.dumps(marker))
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), env={})
    await _observe_turn(runtime, 1)
    await runtime.close()
    if change == "symlink":
        assert outside.read_text() == "{}"
    else:
        assert not target.exists()


async def test_desktop_time_checkpoint_does_not_discard_a_terminal_count(tmp_path, monkeypatch):
    from opensquilla.telemetry import desktop_turn_counts

    target = _active_session(tmp_path)
    mkstemp = desktop_turn_counts.tempfile.mkstemp

    def checkpoint_during_write(*args, **kwargs):
        result = mkstemp(*args, **kwargs)
        if kwargs.get("prefix") == ".desktop-turn-write-":
            path = target.parent / SESSION_MARKER_NAME
            marker = json.loads(path.read_text())
            marker["last_observed_at_ms"] = marker["started_at_ms"] + 500
            path.write_text(json.dumps(marker))
        return result

    monkeypatch.setattr(desktop_turn_counts.tempfile, "mkstemp", checkpoint_during_write)
    runtime = ScopedTelemetryRuntime(config=_config(tmp_path), env={})
    await _observe_turn(runtime, 1)
    await runtime.close()
    assert json.loads(target.read_text())["turn_count"] == 1


@pytest.mark.parametrize("ending", ["finish", "recover"])
async def test_gateway_observer_reaches_real_desktop_performance_summary(tmp_path, ending):
    """Run the Python producer against the built Desktop module, without Electron."""
    desktop = Path(__file__).parents[2] / "desktop" / "electron"
    node = shutil.which("node")
    module = desktop / "dist" / "telemetry" / "reliability.js"
    if node is None or not module.exists():
        pytest.skip("requires Node and npm run build in desktop/electron")
    program = """
      import { createInterface } from 'node:readline';
      import { join } from 'node:path';
      const { DesktopReliabilityTelemetry } = await import(process.argv[2]);
      const { DesktopTelemetryRuntimeGate } = await import(process.argv[3]);
      const { writeConsentMirror } = await import(process.argv[4]);
      const data = JSON.parse(process.argv[1]);
      const paths = { spoolRoot: join(data.state, 'telemetry', 'desktop-early-spool'),
        consentMirrorPath: join(data.state, 'telemetry', 'desktop-consent-mirror.json') };
      await writeConsentMirror(paths.consentMirrorPath, {schema_version: 1,
        reliability: { enabled: true, forced_off: false, notice_version: data.notice,
          consented_at_utc: data.consented },
        growth: { enabled: false, forced_off: false, notice_version: null,
          consented_at_utc: null } });
      const gate = new DesktopTelemetryRuntimeGate(); gate.openAfterConsentSync();
      let now = Date.parse(data.consented);
      const options = { runtimeGate: gate, appVersion: () => '1.2.3', platform: 'macos',
        processStartedAtMs: now, nowMs: () => now, nowDate: () => new Date(now), env: {} };
      const first = new DesktopReliabilityTelemetry({...options, appSessionId: data.session});
      first.synchronize(paths);
      first.recordAppStartResult({outcome: 'success', durationMs: 0,
        errorCode: null, failureStage: null});
      console.log('ready');
      for await (const action of createInterface({input: process.stdin})) {
        now += 5000;
        if (action === 'finish') first.finishSession();
        else new DesktopReliabilityTelemetry(options).synchronize(paths);
        break;
      }
    """
    process = await asyncio.create_subprocess_exec(
        node,
        "--input-type=module",
        "-e",
        program,
        json.dumps(
            {
                "state": str(tmp_path),
                "notice": NOTICE,
                "consented": CONSENTED_AT,
                "session": APP_SESSION,
            }
        ),
        module.as_uri(),
        (module.parent / "early-spool.js").as_uri(),
        (module.parent / "consent-mirror.js").as_uri(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
        runtime = ScopedTelemetryRuntime(config=_config(tmp_path), env={})
        await _observe_turn(runtime, 0)
        await _observe_turn(runtime, 2)
        await runtime.close()
        _, stderr = await asyncio.wait_for(process.communicate(f"{ending}\n".encode()), 10)
        assert process.returncode == 0, stderr.decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    directory = tmp_path / "telemetry" / "desktop-early-spool" / "reliability"
    events = [json.loads(path.read_text()) for path in directory.glob("*.ready")]
    summaries = [event for event in events if event["event_name"] == "performance_summary"]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["app_session_id"] == APP_SESSION
    assert (summary["turn_count"], summary["stalled_turn_count"], summary["stall_count"]) == (
        2,
        1,
        2,
    )
    assert summary["summary_kind"] == (
        "session_end" if ending == "finish" else "recovered_abnormal"
    )
    assert not (directory / f"{TURN_COUNTS_PREFIX}{APP_SESSION}.tmp").exists()
