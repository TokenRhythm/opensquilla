import { strict as assert } from 'node:assert'
import { OnboardingSaveTelemetry } from '../dist/onboarding-save-telemetry.js'

function clock(...ticks) {
  return () => {
    assert.ok(ticks.length > 0, 'test clock exhausted')
    return ticks.shift()
  }
}

async function verifyCompletedStageAndSuccessfulTerminalRecord() {
  const entries = []
  const telemetry = new OnboardingSaveTelemetry(
    7,
    true,
    (event, detail) => entries.push({ event, detail: structuredClone(detail) }),
    clock(100, 110, 119, 125, 132, 140),
  )

  const inspected = await telemetry.stage('primary_recovery_inspect', async () => 'ready')
  const handedOff = telemetry.stageSync('flow_handoff', () => true)
  telemetry.markWriterAdmitted()
  telemetry.markSettingsPersistedConfirmed()
  const returned = telemetry.recordReturned({ ok: true })
  telemetry.finish()
  telemetry.finish()

  assert.equal(inspected, 'ready')
  assert.equal(handedOff, true)
  assert.deepEqual(returned, { ok: true })
  assert.deepEqual(entries, [
    {
      event: 'onboarding_save_started',
      detail: { attempt: 7, packaged: true },
    },
    {
      event: 'onboarding_save_stage_started',
      detail: { attempt: 7, stage: 'primary_recovery_inspect' },
    },
    {
      event: 'onboarding_save_stage_finished',
      detail: {
        attempt: 7,
        stage: 'primary_recovery_inspect',
        durationMs: 9,
        outcome: 'completed',
      },
    },
    {
      event: 'onboarding_save_stage_started',
      detail: { attempt: 7, stage: 'flow_handoff' },
    },
    {
      event: 'onboarding_save_stage_finished',
      detail: {
        attempt: 7,
        stage: 'flow_handoff',
        durationMs: 7,
        outcome: 'completed',
      },
    },
    {
      event: 'onboarding_save_finished',
      detail: {
        attempt: 7,
        totalDurationMs: 40,
        outcome: 'ok',
        writerAdmitted: true,
        settingsPersistedConfirmed: true,
        lastStage: 'flow_handoff',
      },
    },
  ])
}

async function verifyThrownStageIsRethrownAndTerminallyRecorded() {
  const entries = []
  const sentinel = new Error('must remain private')
  const telemetry = new OnboardingSaveTelemetry(
    8,
    false,
    (event, detail) => entries.push({ event, detail: structuredClone(detail) }),
    clock(5, 10, 16, 20),
  )

  await assert.rejects(
    telemetry.stage('settings_persist', async () => {
      throw sentinel
    }),
    (error) => error === sentinel,
  )
  telemetry.finish()

  assert.deepEqual(entries.at(-2), {
    event: 'onboarding_save_stage_finished',
    detail: {
      attempt: 8,
      stage: 'settings_persist',
      durationMs: 6,
      outcome: 'threw',
    },
  })
  assert.deepEqual(entries.at(-1), {
    event: 'onboarding_save_finished',
    detail: {
      attempt: 8,
      totalDurationMs: 15,
      outcome: 'threw',
      writerAdmitted: false,
      settingsPersistedConfirmed: false,
      lastStage: 'settings_persist',
    },
  })
  assert.equal(JSON.stringify(entries).includes(sentinel.message), false)
}

function verifyReturnedFailureLogsOnlyTheTypedCode() {
  const entries = []
  const telemetry = new OnboardingSaveTelemetry(
    9,
    false,
    (event, detail) => entries.push({ event, detail: structuredClone(detail) }),
    clock(0, 25),
  )
  const returned = telemetry.recordReturned({
    ok: false,
    code: 'recovery_required',
    error: 'secret-bearing renderer message',
  })
  telemetry.finish()

  assert.equal(returned.error, 'secret-bearing renderer message')
  assert.deepEqual(entries.at(-1), {
    event: 'onboarding_save_finished',
    detail: {
      attempt: 9,
      totalDurationMs: 25,
      outcome: 'returned_error',
      writerAdmitted: false,
      settingsPersistedConfirmed: false,
      lastStage: null,
      code: 'recovery_required',
    },
  })
  assert.equal(JSON.stringify(entries).includes(returned.error), false)
}

async function verifyTelemetrySinkCannotBreakObservedWork() {
  const telemetry = new OnboardingSaveTelemetry(
    10,
    false,
    () => {
      throw new Error('log sink unavailable')
    },
    clock(0, 1, 2, 3),
  )
  assert.equal(await telemetry.stage('pending_setup_read', async () => 42), 42)
  assert.deepEqual(telemetry.recordReturned({ ok: true }), { ok: true })
  assert.doesNotThrow(() => telemetry.finish())
}

async function verifyInvalidClockValuesProduceFiniteDurations() {
  const entries = []
  const telemetry = new OnboardingSaveTelemetry(
    11,
    false,
    (event, detail) => entries.push({ event, detail: structuredClone(detail) }),
    clock(100, Number.POSITIVE_INFINITY, Number.POSITIVE_INFINITY, Number.NaN),
  )
  await telemetry.stage('local_finalize', async () => undefined)
  telemetry.recordReturned({ ok: true })
  telemetry.finish()

  const stageFinished = entries.find((entry) => entry.event === 'onboarding_save_stage_finished')
  const saveFinished = entries.find((entry) => entry.event === 'onboarding_save_finished')
  assert.equal(stageFinished.detail.durationMs, 0)
  assert.equal(saveFinished.detail.totalDurationMs, 0)
  assert.equal(Number.isFinite(stageFinished.detail.durationMs), true)
  assert.equal(Number.isFinite(saveFinished.detail.totalDurationMs), true)
}

async function verifyDecreasingClockClampsDurationsToZero() {
  const entries = []
  const telemetry = new OnboardingSaveTelemetry(
    12,
    false,
    (event, detail) => entries.push({ event, detail: structuredClone(detail) }),
    clock(100, 50, 40, 30),
  )
  await telemetry.stage('pending_setup_read', async () => undefined)
  telemetry.recordReturned({ ok: true })
  telemetry.finish()

  const stageFinished = entries.find((entry) => entry.event === 'onboarding_save_stage_finished')
  const saveFinished = entries.find((entry) => entry.event === 'onboarding_save_finished')
  assert.equal(stageFinished.detail.durationMs, 0)
  assert.equal(saveFinished.detail.totalDurationMs, 0)
}

await verifyCompletedStageAndSuccessfulTerminalRecord()
await verifyThrownStageIsRethrownAndTerminallyRecorded()
verifyReturnedFailureLogsOnlyTheTypedCode()
await verifyTelemetrySinkCannotBreakObservedWork()
await verifyInvalidClockValuesProduceFiniteDurations()
await verifyDecreasingClockClampsDurationsToZero()
console.log('onboarding save telemetry tests passed')
