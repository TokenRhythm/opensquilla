import assert from 'node:assert/strict'
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  unlinkSync,
  utimesSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { writeConsentMirror } from '../dist/telemetry/consent-mirror.js'
import { CURRENT_NOTICE_VERSION_BY_SCOPE } from '../dist/telemetry/contracts.js'
import {
  clearEarlyTelemetryScope,
  DesktopTelemetryRuntimeGate,
  EARLY_SPOOL_MAX_FILES,
} from '../dist/telemetry/early-spool.js'
import { runTelemetrySideEffectFailOpen } from '../dist/telemetry/fail-open.js'
import {
  DesktopReliabilityTelemetry,
} from '../dist/telemetry/reliability.js'

function uuid(counter) {
  return `00000000-0000-4000-8000-${counter.toString(16).padStart(12, '0')}`
}

function deterministicIds(start = 1) {
  let counter = start
  return () => uuid(counter++)
}

const SOURCE_COMMIT = '0123456789abcdef0123456789abcdef01234567'
const SOURCE_VERSION_053 = `0.5.3+source.${SOURCE_COMMIT}`
const SOURCE_VERSION_054 = `0.5.4+source.${SOURCE_COMMIT}`

function clock(start = Date.parse('2026-09-02T00:00:00.000Z')) {
  let now = start
  return {
    nowMs: () => now,
    nowDate: () => new Date(now),
    advance: (durationMs) => { now += durationMs },
  }
}

async function writeReliabilityConsent(
  path,
  enabled,
  consentedAtUtc = '2026-09-02T00:00:00.000Z',
) {
  mkdirSync(join(path, '..'), { recursive: true })
  await writeConsentMirror(path, {
    schema_version: 1,
    reliability: enabled
      ? {
          enabled: true,
          notice_version: CURRENT_NOTICE_VERSION_BY_SCOPE.reliability,
          consented_at_utc: consentedAtUtc,
          forced_off: false,
        }
      : {
          enabled: false,
          notice_version: null,
          consented_at_utc: null,
          forced_off: false,
        },
    growth: {
      enabled: false,
      notice_version: null,
      consented_at_utc: null,
      forced_off: false,
    },
  })
}

function paths(root) {
  return {
    spoolRoot: join(root, 'desktop-early-spool'),
    consentMirrorPath: join(root, 'desktop-consent-mirror.json'),
  }
}

function openGate() {
  const gate = new DesktopTelemetryRuntimeGate()
  gate.openAfterConsentSync()
  return gate
}

function readyEvents(root) {
  const directory = join(paths(root).spoolRoot, 'reliability')
  if (!existsSync(directory)) return []
  return readdirSync(directory)
    .filter((name) => name.endsWith('.ready'))
    .map((name) => JSON.parse(readFileSync(join(directory, name), 'utf8')))
}

function telemetry(options) {
  const telemetryOptions = {
    runtimeGate: options.runtimeGate ?? openGate(),
    appVersion: () => options.appVersion ?? '0.5.3',
    platform: 'macos',
    processStartedAtMs: options.processStartedAtMs ?? options.clock.nowMs(),
    appSessionId: options.appSessionId,
    nowMs: options.clock.nowMs,
    nowDate: options.clock.nowDate,
    randomId: options.randomId,
    env: {},
  }
  if (options.telemetryAppVersion !== undefined) {
    telemetryOptions.telemetryAppVersion = () => options.telemetryAppVersion
  }
  return new DesktopReliabilityTelemetry(telemetryOptions)
}

const root = mkdtempSync(join(tmpdir(), 'opensquilla-reliability-'))
try {
  // No decision, decline, and a closed process gate are all strict no-write states.
  {
    const disabledRoot = join(root, 'disabled')
    const disabledPaths = paths(disabledRoot)
    await writeReliabilityConsent(disabledPaths.consentMirrorPath, false)
    const fakeClock = clock()
    const runtime = telemetry({
      clock: fakeClock,
      appSessionId: uuid(1),
      randomId: deterministicIds(10),
    })
    runtime.synchronize(disabledPaths)
    runtime.recordAppStartResult({
      outcome: 'success',
      durationMs: 10,
      failureStage: null,
      errorCode: null,
    })
    assert.equal(existsSync(disabledPaths.spoolRoot), false)

    await writeReliabilityConsent(disabledPaths.consentMirrorPath, true)
    const closedGate = new DesktopTelemetryRuntimeGate()
    const closedRuntime = telemetry({
      clock: fakeClock,
      runtimeGate: closedGate,
      appSessionId: uuid(2),
      randomId: deterministicIds(20),
    })
    closedRuntime.synchronize(disabledPaths)
    closedRuntime.recordCrash({
      component: 'desktop_main',
      errorCode: 'uncaught_exception',
      reason: 'uncaught_exception',
    })
    assert.equal(existsSync(disabledPaths.spoolRoot), false)
  }

  // Telemetry-only filesystem failure closes the local gate but never replaces
  // the surrounding settings/onboarding operation's outcome.
  {
    const gate = openGate()
    let failureObserved = false
    const completed = await runTelemetrySideEffectFailOpen(
      async () => { throw new Error('synthetic local telemetry I/O failure') },
      () => {
        failureObserved = true
        gate.close()
      },
    )
    assert.equal(completed, false)
    assert.equal(failureObserved, true)
    assert.equal(gate.isOpen(), false)
    let productOperationContinued = false
    productOperationContinued = true
    assert.equal(productOperationContinued, true)
    assert.equal(
      await runTelemetrySideEffectFailOpen(async () => undefined, () => assert.fail()),
      true,
    )
  }

  // Closing the process gate for a consent resync only pauses persistence. It
  // must not reinterpret the temporary veto as a durable withdrawal.
  {
    const pausedRoot = join(root, 'temporarily-paused')
    const pausedPaths = paths(pausedRoot)
    await writeReliabilityConsent(pausedPaths.consentMirrorPath, true)
    const fakeClock = clock()
    const gate = openGate()
    const runtime = telemetry({
      clock: fakeClock,
      runtimeGate: gate,
      appSessionId: uuid(40),
      randomId: deterministicIds(41),
    })
    runtime.synchronize(pausedPaths)
    const sessionPath = join(
      pausedPaths.spoolRoot,
      'reliability',
      '.desktop-reliability-session.tmp',
    )
    const before = JSON.parse(readFileSync(sessionPath, 'utf8'))
    gate.close()
    runtime.setForeground(true)
    runtime.recordCrash({
      component: 'desktop_main',
      errorCode: 'uncaught_exception',
      reason: 'uncaught_exception',
      signature: 'type_error',
    })
    assert.equal(existsSync(sessionPath), true)
    assert.equal(JSON.parse(readFileSync(sessionPath, 'utf8')).app_session_id, before.app_session_id)
    gate.openAfterConsentSync()
    runtime.synchronize(pausedPaths)
    assert.equal(JSON.parse(readFileSync(sessionPath, 'utf8')).crash?.component, 'desktop_main')
    runtime.finishSession()
  }

  // A startup that dies before reaching a terminal seam is recovered exactly
  // once with its last closed stage, fixed id, original version, and process duration.
  {
    const interruptedRoot = join(root, 'interrupted-start')
    const interruptedPaths = paths(interruptedRoot)
    await writeReliabilityConsent(interruptedPaths.consentMirrorPath, true)
    const fakeClock = clock()
    const first = telemetry({
      clock: fakeClock,
      processStartedAtMs: fakeClock.nowMs(),
      appSessionId: uuid(50),
      randomId: deterministicIds(51),
      appVersion: '0.5.3',
    })
    first.synchronize(interruptedPaths)
    first.observeAppStartStage('gateway_health')
    const sessionPath = join(
      interruptedPaths.spoolRoot,
      'reliability',
      '.desktop-reliability-session.tmp',
    )
    const pending = JSON.parse(readFileSync(sessionPath, 'utf8'))
    fakeClock.advance(2_500)
    first.setForeground(true)

    const second = telemetry({
      clock: fakeClock,
      processStartedAtMs: fakeClock.nowMs(),
      appSessionId: uuid(60),
      randomId: deterministicIds(61),
      appVersion: '0.5.4',
    })
    second.synchronize(interruptedPaths)
    const recoveredStarts = readyEvents(interruptedRoot).filter(
      (event) => event.event_name === 'app_start_result' && event.app_session_id === uuid(50),
    )
    assert.equal(recoveredStarts.length, 1)
    assert.equal(recoveredStarts[0].event_id, pending.app_start_event_id)
    assert.equal(recoveredStarts[0].outcome, 'fail')
    assert.equal(recoveredStarts[0].error_code, 'internal_error')
    assert.equal(recoveredStarts[0].failure_stage, 'gateway_health')
    assert.equal(recoveredStarts[0].duration_ms, 2_500)
    assert.equal(recoveredStarts[0].app_version, '0.5.3')
    second.synchronize(interruptedPaths)
    assert.equal(
      readyEvents(interruptedRoot).filter(
        (event) => event.event_name === 'app_start_result' && event.app_session_id === uuid(50),
      ).length,
      1,
    )
    second.finishSession()
  }

  // A committed clean exit before readiness is a cancellation, never a crash.
  {
    const cancelledRoot = join(root, 'cancelled-start')
    const cancelledPaths = paths(cancelledRoot)
    await writeReliabilityConsent(cancelledPaths.consentMirrorPath, true)
    const fakeClock = clock()
    const runtime = telemetry({
      clock: fakeClock,
      processStartedAtMs: fakeClock.nowMs(),
      appSessionId: uuid(70),
      randomId: deterministicIds(71),
    })
    runtime.synchronize(cancelledPaths)
    runtime.observeAppStartStage('control_ui')
    fakeClock.advance(400)
    runtime.setForeground(true)
    runtime.finishSession()
    const events = readyEvents(cancelledRoot)
    const start = events.find((event) => event.event_name === 'app_start_result')
    assert.equal(start?.outcome, 'cancel')
    assert.equal(start?.error_code, 'startup_cancelled')
    assert.equal(start?.failure_stage, 'control_ui')
    assert.equal(start?.duration_ms, 400)
    assert.equal(events.some((event) => event.event_name === 'app_crash_detected'), false)
  }

  // Terminal facts are strict, content-free events and a clean exit emits one summary.
  {
    const factRoot = join(root, 'facts')
    const factPaths = paths(factRoot)
    await writeReliabilityConsent(factPaths.consentMirrorPath, true)
    const fakeClock = clock()
    const runtime = telemetry({
      clock: fakeClock,
      appSessionId: uuid(100),
      randomId: deterministicIds(101),
      appVersion: '0.5.4',
      telemetryAppVersion: SOURCE_VERSION_054,
    })
    runtime.setForeground(true)
    runtime.synchronize(factPaths)
    fakeClock.advance(500)
    runtime.recordMonitoredRequest(30_001)
    runtime.recordAppStartResult({
      outcome: 'success',
      durationMs: 500,
      failureStage: null,
      errorCode: null,
    })
    runtime.recordGatewayStartResult({
      outcome: 'success',
      durationMs: 400,
      failureStage: null,
      errorCode: null,
      startupMode: 'spawned',
    })
    runtime.recordUpdateResult({
      outcome: 'success',
      durationMs: 200,
      updateStage: 'check',
      errorCode: null,
      oldVersion: '0.5.3',
      newVersion: '0.5.4',
      result: 'available',
    })
    fakeClock.advance(500)
    runtime.finishSession()

    const events = readyEvents(factRoot)
    assert.deepEqual(
      events.map((event) => event.event_name).sort(),
      ['app_start_result', 'gateway_start_result', 'performance_summary', 'update_result'],
    )
    const summary = events.find((event) => event.event_name === 'performance_summary')
    assert.ok(events.every((event) => event.app_version === SOURCE_VERSION_054))
    assert.equal(summary.summary_kind, 'session_end')
    assert.equal(summary.coverage, 'complete')
    assert.equal(summary.monitored_request_count, 1)
    assert.equal(summary.slow_request_count, 1)
    assert.equal(summary.foreground_duration_ms, 1_000)
    assert.equal(summary.background_duration_ms, 0)
    assert.equal(
      existsSync(join(factPaths.spoolRoot, 'reliability', '.desktop-reliability-session.tmp')),
      false,
    )
    const serialized = JSON.stringify(events)
    for (const forbidden of [
      'prompt', 'response', 'message', 'stack', 'path', 'payload_json', 'user_id', 'analytics_user_id',
    ]) {
      assert.equal(serialized.includes(forbidden), false, `forbidden telemetry field: ${forbidden}`)
    }
  }

  // A renderer crash is persisted as closed facts and reported once on the next launch.
  {
    const crashRoot = join(root, 'crash')
    const crashPaths = paths(crashRoot)
    await writeReliabilityConsent(crashPaths.consentMirrorPath, true)
    const fakeClock = clock()
    const first = telemetry({
      clock: fakeClock,
      appSessionId: uuid(200),
      randomId: deterministicIds(201),
      appVersion: '0.5.3',
    })
    first.synchronize(crashPaths)
    first.beginStall()
    fakeClock.advance(16_000)
    first.recordCrash({
      component: 'desktop_renderer',
      errorCode: 'renderer_crashed',
      reason: 'crashed',
    })

    fakeClock.advance(1_000)
    const second = telemetry({
      clock: fakeClock,
      appSessionId: uuid(300),
      randomId: deterministicIds(301),
      appVersion: '0.5.4',
    })
    second.synchronize(crashPaths)
    const events = readyEvents(crashRoot)
    const crash = events.find((event) => event.event_name === 'app_crash_detected')
    assert.ok(crash)
    assert.equal(crash.component, 'desktop_renderer')
    assert.equal(crash.error_code, 'renderer_crashed')
    assert.equal(crash.app_version, '0.5.3')
    assert.match(crash.error_fingerprint, /^[a-f0-9]{64}$/)
    assert.equal(crash.app_session_id, uuid(200))
    const recovered = events.find(
      (event) => event.event_name === 'performance_summary'
        && event.summary_kind === 'recovered_abnormal',
    )
    assert.ok(recovered)
    assert.equal(recovered.coverage, 'partial')
    assert.equal(recovered.stall_count, 1)
    second.finishSession()
  }

  // If the process dies after enqueue but before persisting the ACK bit, the
  // next launch reuses the fixed id and treats the existing ready file as success.
  {
    const duplicateRoot = join(root, 'app-start-duplicate')
    const duplicatePaths = paths(duplicateRoot)
    await writeReliabilityConsent(duplicatePaths.consentMirrorPath, true)
    const fakeClock = clock()
    const first = telemetry({
      clock: fakeClock,
      appSessionId: uuid(310),
      randomId: deterministicIds(311),
    })
    first.synchronize(duplicatePaths)
    first.recordAppStartResult({
      outcome: 'success',
      durationMs: 100,
      failureStage: null,
      errorCode: null,
    })
    const sessionPath = join(
      duplicatePaths.spoolRoot,
      'reliability',
      '.desktop-reliability-session.tmp',
    )
    const marker = JSON.parse(readFileSync(sessionPath, 'utf8'))
    marker.app_start_result_emitted = false
    writeFileSync(sessionPath, `${JSON.stringify(marker)}\n`)
    fakeClock.advance(100)
    const second = telemetry({
      clock: fakeClock,
      appSessionId: uuid(315),
      randomId: deterministicIds(316),
    })
    second.synchronize(duplicatePaths)
    const starts = readyEvents(duplicateRoot).filter(
      (event) => event.event_name === 'app_start_result' && event.app_session_id === uuid(310),
    )
    assert.equal(starts.length, 1)
    assert.equal(starts[0].event_id, marker.app_start_event_id)
    second.finishSession()
  }

  // A v1 marker may already have emitted app_start_result outside its durable
  // state. Upgrade recovery keeps crash/performance but never invents a duplicate.
  {
    const legacyRoot = join(root, 'legacy-session-marker')
    const legacyPaths = paths(legacyRoot)
    await writeReliabilityConsent(legacyPaths.consentMirrorPath, true)
    const fakeClock = clock()
    const first = telemetry({
      clock: fakeClock,
      appSessionId: uuid(317),
      randomId: deterministicIds(318),
    })
    first.synchronize(legacyPaths)
    const sessionPath = join(
      legacyPaths.spoolRoot,
      'reliability',
      '.desktop-reliability-session.tmp',
    )
    const marker = JSON.parse(readFileSync(sessionPath, 'utf8'))
    for (const field of [
      'app_start_event_id',
      'app_start_started_at_ms',
      'app_start_stage',
      'app_start_result',
      'app_start_result_emitted',
    ]) delete marker[field]
    marker.schema_version = 1
    writeFileSync(sessionPath, `${JSON.stringify(marker)}\n`)
    fakeClock.advance(100)
    const second = telemetry({
      clock: fakeClock,
      appSessionId: uuid(319),
      randomId: deterministicIds(320),
    })
    second.synchronize(legacyPaths)
    const events = readyEvents(legacyRoot)
    assert.equal(
      events.some((event) => event.event_name === 'app_start_result' && event.app_session_id === uuid(317)),
      false,
    )
    assert.equal(
      events.some((event) => event.event_name === 'app_crash_detected' && event.app_session_id === uuid(317)),
      true,
    )
    second.finishSession()
  }

  // Main-process fingerprinting distinguishes closed built-in exception types
  // without persisting exception messages, paths, or stacks.
  {
    const fingerprints = []
    for (const [index, signature] of ['type_error', 'range_error'].entries()) {
      const fingerprintRoot = join(root, `fingerprint-${index}`)
      const fingerprintPaths = paths(fingerprintRoot)
      await writeReliabilityConsent(fingerprintPaths.consentMirrorPath, true)
      const fakeClock = clock()
      const runtime = telemetry({
        clock: fakeClock,
        appSessionId: uuid(325 + index),
        randomId: deterministicIds(330 + index * 10),
      })
      runtime.synchronize(fingerprintPaths)
      runtime.recordCrash({
        component: 'desktop_main',
        errorCode: 'uncaught_exception',
        reason: 'uncaught_exception',
        signature,
      })
      const marker = readFileSync(join(
        fingerprintPaths.spoolRoot,
        'reliability',
        '.desktop-reliability-session.tmp',
      ), 'utf8')
      const parsed = JSON.parse(marker)
      fingerprints.push(parsed.crash.error_fingerprint)
      for (const forbidden of ['message', 'stack', 'path', signature]) {
        assert.equal(marker.includes(forbidden), false)
      }
    }
    assert.notEqual(fingerprints[0], fingerprints[1])
  }

  // A durable clean marker left behind by a failed unlink is never reclassified as a crash.
  {
    const cleanRoot = join(root, 'clean-marker')
    const cleanPaths = paths(cleanRoot)
    await writeReliabilityConsent(cleanPaths.consentMirrorPath, true)
    const fakeClock = clock()
    const first = telemetry({
      clock: fakeClock,
      appSessionId: uuid(320),
      randomId: deterministicIds(321),
    })
    first.synchronize(cleanPaths)
    fakeClock.advance(1_000)
    first.setForeground(true)
    const sessionPath = join(
      cleanPaths.spoolRoot,
      'reliability',
      '.desktop-reliability-session.tmp',
    )
    const cleanMarker = JSON.parse(readFileSync(sessionPath, 'utf8'))
    cleanMarker.clean_exit = true
    cleanMarker.performance_summary_emitted = true
    writeFileSync(sessionPath, `${JSON.stringify(cleanMarker)}\n`)

    const second = telemetry({
      clock: fakeClock,
      appSessionId: uuid(330),
      randomId: deterministicIds(331),
    })
    second.synchronize(cleanPaths)
    assert.equal(
      readyEvents(cleanRoot).some((event) => event.event_name === 'app_crash_detected'),
      false,
    )
    second.finishSession()
  }

  // A full queue cannot grow a second durable marker. The canonical marker
  // remains stable until capacity returns, then recovers with its original facts.
  {
    const retryRoot = join(root, 'recovery-retry')
    const retryPaths = paths(retryRoot)
    await writeReliabilityConsent(retryPaths.consentMirrorPath, true)
    const fakeClock = clock()
    const first = telemetry({
      clock: fakeClock,
      appSessionId: uuid(340),
      randomId: deterministicIds(341),
    })
    first.synchronize(retryPaths)
    fakeClock.advance(1_000)
    first.setForeground(true)
    const scopeDirectory = join(retryPaths.spoolRoot, 'reliability')
    const canonicalPath = join(scopeDirectory, '.desktop-reliability-session.tmp')
    const canonical = JSON.parse(readFileSync(canonicalPath, 'utf8'))
    for (let index = 0; index < EARLY_SPOOL_MAX_FILES - 1; index += 1) {
      writeFileSync(join(scopeDirectory, `.quota-${index}`), '')
    }
    fakeClock.advance(5 * 24 * 60 * 60 * 1_000)
    const second = telemetry({
      clock: fakeClock,
      appSessionId: uuid(350),
      randomId: deterministicIds(351),
    })
    second.synchronize(retryPaths)
    assert.equal(
      readdirSync(scopeDirectory).some((name) => name.startsWith('.desktop-reliability-recovery-')),
      false,
    )
    assert.equal(JSON.parse(readFileSync(canonicalPath, 'utf8')).app_session_id, canonical.app_session_id)
    const fullCount = readdirSync(scopeDirectory).length
    second.synchronize(retryPaths)
    assert.equal(readdirSync(scopeDirectory).length, fullCount)
    for (let index = 0; index < EARLY_SPOOL_MAX_FILES - 1; index += 1) {
      unlinkSync(join(scopeDirectory, `.quota-${index}`))
    }
    second.synchronize(retryPaths)
    const stale = readyEvents(retryRoot).find(
      (event) => event.event_name === 'app_crash_detected'
        && event.app_session_id === uuid(340),
    )
    assert.ok(stale)
    assert.equal(stale.runtime_ms, 1_000)
    assert.equal(stale.occurred_at_utc, '2026-09-02T00:00:01.000Z')
    const recoveredStart = readyEvents(retryRoot).find(
      (event) => event.event_name === 'app_start_result'
        && event.app_session_id === uuid(340),
    )
    assert.equal(recoveredStart?.event_id, canonical.app_start_event_id)
    assert.equal(recoveredStart?.duration_ms, 1_000)
    assert.equal(
      readdirSync(scopeDirectory).some((name) => name.startsWith('.desktop-reliability-recovery-')),
      false,
    )
    second.finishSession()
  }

  // A pre-cap pathological backlog is deterministically bounded to the newest
  // markers, releasing enough quota for recovery and a new canonical session.
  {
    const backlogRoot = join(root, 'legacy-recovery-backlog')
    const backlogPaths = paths(backlogRoot)
    await writeReliabilityConsent(backlogPaths.consentMirrorPath, true)
    const fakeClock = clock()
    const seed = telemetry({
      clock: fakeClock,
      appSessionId: uuid(900),
      randomId: deterministicIds(901),
    })
    seed.synchronize(backlogPaths)
    const scopeDirectory = join(backlogPaths.spoolRoot, 'reliability')
    const canonicalPath = join(scopeDirectory, '.desktop-reliability-session.tmp')
    const template = JSON.parse(readFileSync(canonicalPath, 'utf8'))
    unlinkSync(canonicalPath)
    for (const field of [
      'app_start_event_id',
      'app_start_started_at_ms',
      'app_start_stage',
      'app_start_result',
      'app_start_result_emitted',
    ]) delete template[field]
    template.schema_version = 1
    template.clean_exit = false
    template.performance_summary_emitted = false
    const legacyMarkerCount = EARLY_SPOOL_MAX_FILES
    for (let index = 0; index < legacyMarkerCount; index += 1) {
      const marker = {
        ...template,
        app_session_id: uuid(2_000 + index),
        crash_event_id: uuid(3_000 + index),
        recovered_performance_event_id: uuid(4_000 + index),
      }
      writeFileSync(
        join(scopeDirectory, `.desktop-reliability-recovery-${marker.app_session_id}.tmp`),
        `${JSON.stringify(marker)}\n`,
      )
    }
    const recovery = telemetry({
      clock: fakeClock,
      appSessionId: uuid(5_000),
      randomId: deterministicIds(5_001),
    })
    recovery.synchronize(backlogPaths)
    assert.equal(
      readdirSync(scopeDirectory).filter(
        (name) => name.startsWith('.desktop-reliability-recovery-'),
      ).length,
      0,
    )
    assert.equal(
      readyEvents(backlogRoot).filter((event) => event.event_name === 'performance_summary').length,
      32,
    )
    assert.equal(
      readyEvents(backlogRoot).filter((event) => event.event_name === 'app_crash_detected').length,
      32,
    )
    assert.equal(existsSync(canonicalPath), true)
    recovery.finishSession()
  }

  // Native updater handoff is resolved only after the next binary version starts and is ready.
  {
    const updateRoot = join(root, 'update')
    const updatePaths = paths(updateRoot)
    await writeReliabilityConsent(updatePaths.consentMirrorPath, true)
    const fakeClock = clock()
    const oldRuntime = telemetry({
      clock: fakeClock,
      appSessionId: uuid(400),
      randomId: deterministicIds(401),
      appVersion: '0.5.3',
      telemetryAppVersion: SOURCE_VERSION_053,
    })
    oldRuntime.synchronize(updatePaths)
    assert.equal(oldRuntime.markUpdateHandoff('0.5.4'), true)
    fakeClock.advance(2_000)
    oldRuntime.finishSession()

    const newProcessStartedAt = fakeClock.nowMs()
    const newRuntime = telemetry({
      clock: fakeClock,
      processStartedAtMs: newProcessStartedAt,
      appSessionId: uuid(500),
      randomId: deterministicIds(501),
      appVersion: '0.5.4',
      telemetryAppVersion: SOURCE_VERSION_054,
    })
    newRuntime.synchronize(updatePaths)
    fakeClock.advance(800)
    newRuntime.recordAppStartResult({
      outcome: 'success',
      durationMs: 800,
      failureStage: null,
      errorCode: null,
    })
    const updates = readyEvents(updateRoot).filter((event) => event.event_name === 'update_result')
    const install = updates.find((event) => event.update_stage === 'install')
    const restart = updates.find((event) => event.update_stage === 'restart')
    assert.equal(install?.outcome, 'success')
    assert.equal(install?.old_version, '0.5.3')
    assert.equal(install?.new_version, '0.5.4')
    assert.equal(install?.app_version, SOURCE_VERSION_054)
    assert.equal(restart?.outcome, 'success')
    assert.equal(restart?.app_session_id, uuid(500))
    assert.equal(restart?.app_version, SOURCE_VERSION_054)
    assert.equal(
      existsSync(join(updatePaths.spoolRoot, 'reliability', '.desktop-update-transition.tmp')),
      false,
    )
    newRuntime.finishSession()
  }

  // Install/restart terminal facts survive a full queue and retry with their
  // original event ids and completion timestamps after capacity returns.
  {
    const updateRetryRoot = join(root, 'update-retry')
    const updateRetryPaths = paths(updateRetryRoot)
    await writeReliabilityConsent(updateRetryPaths.consentMirrorPath, true)
    const fakeClock = clock()
    const oldRuntime = telemetry({
      clock: fakeClock,
      appSessionId: uuid(520),
      randomId: deterministicIds(521),
      appVersion: '0.5.3',
    })
    oldRuntime.synchronize(updateRetryPaths)
    assert.equal(oldRuntime.markUpdateHandoff('0.5.4'), true)
    oldRuntime.finishSession()
    const scopeDirectory = join(updateRetryPaths.spoolRoot, 'reliability')
    for (let index = 0; index < 512; index += 1) {
      writeFileSync(join(scopeDirectory, `.quota-${index}`), '')
    }
    fakeClock.advance(2_000)
    const newRuntime = telemetry({
      clock: fakeClock,
      processStartedAtMs: fakeClock.nowMs(),
      appSessionId: uuid(530),
      randomId: deterministicIds(531),
      appVersion: '0.5.4',
    })
    newRuntime.synchronize(updateRetryPaths)
    assert.equal(
      existsSync(join(scopeDirectory, '.desktop-update-transition.tmp')),
      true,
    )
    for (let index = 0; index < 512; index += 1) {
      unlinkSync(join(scopeDirectory, `.quota-${index}`))
    }
    fakeClock.advance(800)
    newRuntime.recordAppStartResult({
      outcome: 'success',
      durationMs: 800,
      failureStage: null,
      errorCode: null,
    })
    const updates = readyEvents(updateRetryRoot).filter(
      (event) => event.event_name === 'update_result',
    )
    assert.equal(updates.filter((event) => event.update_stage === 'install').length, 1)
    assert.equal(updates.filter((event) => event.update_stage === 'restart').length, 1)
    assert.equal(
      existsSync(join(scopeDirectory, '.desktop-update-transition.tmp')),
      false,
    )
    newRuntime.finishSession()
  }

  // A sink/path failure is invisible to the observed lifecycle.
  {
    const failureRoot = join(root, 'failure')
    const failurePaths = paths(failureRoot)
    await writeReliabilityConsent(failurePaths.consentMirrorPath, true)
    writeFileSync(failurePaths.spoolRoot, 'not a directory')
    const fakeClock = clock()
    const runtime = telemetry({
      clock: fakeClock,
      appSessionId: uuid(600),
      randomId: deterministicIds(601),
    })
    assert.doesNotThrow(() => runtime.synchronize(failurePaths))
    assert.doesNotThrow(() => runtime.recordAppStartResult({
      outcome: 'fail',
      durationMs: 1,
      failureStage: 'profile',
      errorCode: 'internal_error',
    }))
    assert.doesNotThrow(() => runtime.finishSession())
  }

  // Durable transition files remain eligible for withdrawal cleanup but are
  // never expired by the ordinary seven-day temporary-file pruning pass.
  {
    const durableRoot = join(root, 'durable-marker')
    const durablePaths = paths(durableRoot)
    await writeReliabilityConsent(durablePaths.consentMirrorPath, true)
    const fakeClock = clock()
    const runtime = telemetry({
      clock: fakeClock,
      appSessionId: uuid(650),
      randomId: deterministicIds(651),
    })
    runtime.synchronize(durablePaths)
    assert.equal(runtime.markUpdateHandoff('0.5.4'), true)
    const scopeDirectory = join(durablePaths.spoolRoot, 'reliability')
    const sessionPath = join(scopeDirectory, '.desktop-reliability-session.tmp')
    const updatePath = join(scopeDirectory, '.desktop-update-transition.tmp')
    const oldSeconds = (fakeClock.nowMs() - 10 * 24 * 60 * 60 * 1_000) / 1_000
    utimesSync(sessionPath, oldSeconds, oldSeconds)
    utimesSync(updatePath, oldSeconds, oldSeconds)
    runtime.recordUpdateResult({
      outcome: 'success',
      durationMs: 1,
      updateStage: 'check',
      errorCode: null,
      oldVersion: '0.5.3',
      newVersion: null,
      result: 'not_available',
    })
    assert.equal(existsSync(sessionPath), true)
    assert.equal(existsSync(updatePath), true)
    runtime.abandonSession()
  }

  // A profile/state-dir switch first preserves the old crash/summary in that
  // profile, even while its queue is full, and never checkpoints A into B.
  {
    const profileARoot = join(root, 'profile-a')
    const profileBRoot = join(root, 'profile-b')
    const profileAPaths = paths(profileARoot)
    const profileBPaths = paths(profileBRoot)
    await writeReliabilityConsent(profileAPaths.consentMirrorPath, true)
    await writeReliabilityConsent(profileBPaths.consentMirrorPath, true)
    const fakeClock = clock()
    const runtime = telemetry({
      clock: fakeClock,
      appSessionId: uuid(675),
      randomId: deterministicIds(676),
    })
    runtime.synchronize(profileAPaths)
    runtime.recordCrash({
      component: 'desktop_renderer',
      errorCode: 'renderer_crashed',
      reason: 'crashed',
    })
    const profileAScope = join(profileAPaths.spoolRoot, 'reliability')
    for (let index = 0; index < EARLY_SPOOL_MAX_FILES - 1; index += 1) {
      writeFileSync(join(profileAScope, `.quota-${index}`), '')
    }
    runtime.synchronize(profileBPaths)
    assert.equal(
      existsSync(join(profileAPaths.spoolRoot, 'reliability', '.desktop-reliability-session.tmp')),
      true,
    )
    assert.equal(
      readdirSync(profileAScope).some((name) => name.startsWith('.desktop-reliability-recovery-')),
      false,
    )
    assert.equal(
      existsSync(join(profileBPaths.spoolRoot, 'reliability', '.desktop-reliability-session.tmp')),
      true,
    )
    for (let index = 0; index < EARLY_SPOOL_MAX_FILES - 1; index += 1) {
      unlinkSync(join(profileAScope, `.quota-${index}`))
    }
    runtime.synchronize(profileAPaths)
    assert.equal(
      readyEvents(profileARoot).some((event) => (
        event.event_name === 'app_crash_detected'
          && event.app_session_id === uuid(675)
      )),
      true,
    )
    runtime.finishSession()
  }

  // Gateway withdrawal and re-grant happen in another process, so Electron may
  // never observe the disabled mirror. A new grant generation must still reset
  // the in-memory session before its next checkpoint can recreate any marker.
  {
    const revokedRoot = join(root, 'revoked')
    const revokedPaths = paths(revokedRoot)
    await writeReliabilityConsent(revokedPaths.consentMirrorPath, true)
    const fakeClock = clock()
    const runtime = telemetry({
      clock: fakeClock,
      appSessionId: uuid(700),
      randomId: deterministicIds(701),
    })
    runtime.synchronize(revokedPaths)
    runtime.recordAppStartResult({
      outcome: 'success',
      durationMs: 10,
      failureStage: null,
      errorCode: null,
    })
    assert.equal(runtime.markUpdateHandoff('0.5.4'), true)
    await writeReliabilityConsent(revokedPaths.consentMirrorPath, false)
    assert.deepEqual(clearEarlyTelemetryScope(revokedPaths.spoolRoot, 'reliability'), {
      removed: 3,
      failed: 0,
      unsafe: false,
    })
    const scopeDirectory = join(revokedPaths.spoolRoot, 'reliability')
    assert.deepEqual(
      existsSync(scopeDirectory)
        ? readdirSync(scopeDirectory).filter((name) => name.endsWith('.ready') || name.endsWith('.tmp'))
        : [],
      [],
    )
    await writeReliabilityConsent(
      revokedPaths.consentMirrorPath,
      true,
      '2026-09-02T00:00:01.000Z',
    )
    runtime.setForeground(true)
    const renewedMarker = JSON.parse(readFileSync(
      join(scopeDirectory, '.desktop-reliability-session.tmp'),
      'utf8',
    ))
    assert.notEqual(renewedMarker.app_session_id, uuid(700))
    assert.equal(
      readdirSync(scopeDirectory).some((name) => name === '.desktop-update-transition.tmp'),
      false,
    )
    runtime.finishSession()
    assert.equal(
      readyEvents(revokedRoot).some((event) => event.app_session_id === uuid(700)),
      false,
    )
  }

  // Keep the production seams explicit: lifecycle facts stay in the existing
  // local spool path and never gain a separate network uploader.
  {
    const mainSource = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
    for (const pattern of [
      /readSourceCommitId\(repoRoot\)/,
      /telemetryAppVersion: \(\) => sourceTelemetryVersion\(app\.getVersion\(\), desktopSourceCommitId\)/,
      /desktopReliabilityTelemetry\.synchronize\(/,
      /process\.on\('uncaughtExceptionMonitor'/,
      /app\.on\('child-process-gone'/,
      /webContents\.on\('render-process-gone'/,
      /desktopReliabilityTelemetry\.markUpdateHandoff\(/,
      /desktopReliabilityTelemetry\.finishSession\(/,
      /desktopReliabilityTelemetry\.recordMonitoredRequest\(/,
      /desktopReliabilityTelemetry\.observeAppStartStage\(/,
      /signature: normalizedCrashFingerprintSignature\(error\)/,
      /runDesktopTelemetryConsentSideEffect\(/,
    ]) {
      assert.match(mainSource, pattern)
    }
    const openFlow = mainSource.slice(
      mainSource.indexOf('async function openOrResumeDesktopApp'),
      mainSource.indexOf('// SIGKILL deadline for the owned gateway child'),
    )
    assert.ok(
      openFlow.indexOf('await inspectActiveProfileBeforeStartup()')
        < openFlow.indexOf('await syncDesktopConsentMirror()')
        && openFlow.indexOf('await syncDesktopConsentMirror()')
          < openFlow.indexOf('finishAppStartFailure(new Error(\'profile recovery required\')'),
      'consent sync must follow safe profile inspection but precede its terminal result',
    )
    assert.doesNotMatch(mainSource, /desktopReliabilityTelemetry\.(?:upload|send|post)\(/)
    const saveCredential = mainSource.slice(
      mainSource.indexOf('async function saveDesktopCredential'),
      mainSource.indexOf('function buildImportedDesktopCredential'),
    )
    assert.ok(
      saveCredential.indexOf("'pre_commit'")
        < saveCredential.indexOf('await applyDesktopSettingsPair(')
        && saveCredential.indexOf('await applyDesktopSettingsPair(')
          < saveCredential.indexOf("'post_commit'"),
      'telemetry I/O must remain fail-open on both sides of the settings transaction',
    )
    const saveImported = mainSource.slice(
      mainSource.indexOf('async function saveImportedDesktopCredential'),
      mainSource.indexOf('// Sections the desktop config template owns'),
    )
    assert.ok(
      saveImported.indexOf("'pre_commit'")
        < saveImported.indexOf('const inspection = await preflightDesktopConfigWrite')
        && saveImported.indexOf('const inspection = await preflightDesktopConfigWrite')
          < saveImported.indexOf("'post_commit'"),
      'import adoption must keep telemetry I/O outside its authoritative outcome',
    )
    const crashSignatureNormalizer = mainSource.slice(
      mainSource.indexOf('function normalizedCrashFingerprintSignature'),
      mainSource.indexOf('function recordRendererCrash'),
    )
    assert.doesNotMatch(crashSignatureNormalizer, /\.message|\.stack|String\(error\)/)
  }

  console.log('Desktop reliability telemetry tests passed.')
} finally {
  rmSync(root, { recursive: true, force: true })
}
