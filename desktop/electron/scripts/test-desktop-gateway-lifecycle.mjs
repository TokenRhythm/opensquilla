import assert from 'node:assert/strict'

import {
  lifecycleAllowsProcessSpawn,
  stopAndJoinLifecycleProcesses,
  waitForGatewayReadiness,
} from '../dist/gateway-lifecycle.js'

function fakeClock() {
  let current = 0
  return {
    now: () => current,
    advance: (milliseconds) => {
      current += milliseconds
    },
    sleep: async (milliseconds) => {
      current += milliseconds
    },
  }
}

async function runReadinessBeforePrimaryDeadlineCase() {
  const clock = fakeClock()
  let probes = 0
  const result = await waitForGatewayReadiness({
    probe: async () => ++probes === 2,
    primaryTimeoutMs: 10,
    lateGraceMs: 10,
    pollIntervalMs: 5,
    ...clock,
  })

  assert.deepEqual(result, { status: 'ready', late: false })
  assert.equal(probes, 2)
}

async function runLateReadinessCase() {
  const clock = fakeClock()
  const result = await waitForGatewayReadiness({
    probe: async () => clock.now() >= 15,
    primaryTimeoutMs: 10,
    lateGraceMs: 10,
    pollIntervalMs: 5,
    ...clock,
  })

  assert.deepEqual(result, { status: 'ready', late: true })
  assert.equal(clock.now(), 15)
}

async function runReadinessTimeoutCase() {
  const clock = fakeClock()
  const result = await waitForGatewayReadiness({
    probe: async () => false,
    primaryTimeoutMs: 10,
    lateGraceMs: 10,
    pollIntervalMs: 6,
    ...clock,
  })

  assert.deepEqual(result, { status: 'timeout' })
  assert.equal(clock.now(), 20)
}

async function runReadinessExitCase() {
  const clock = fakeClock()
  const result = await waitForGatewayReadiness({
    probe: async () => false,
    exitMessage: () => clock.now() >= 5 ? 'gateway exited' : null,
    primaryTimeoutMs: 10,
    lateGraceMs: 10,
    pollIntervalMs: 5,
    ...clock,
  })

  assert.deepEqual(result, { status: 'exited', message: 'gateway exited' })
  assert.equal(clock.now(), 5)
}

async function runReadinessExitDuringSuccessfulProbeCase() {
  let exited = false
  const result = await waitForGatewayReadiness({
    probe: async () => {
      exited = true
      return true
    },
    exitMessage: () => exited ? 'gateway exited during probe' : null,
    primaryTimeoutMs: 10,
    lateGraceMs: 10,
    pollIntervalMs: 5,
  })

  assert.deepEqual(result, { status: 'exited', message: 'gateway exited during probe' })
}

async function runProbeCrossesDeadlineCase() {
  const clock = fakeClock()
  const result = await waitForGatewayReadiness({
    probe: async (remainingMs) => {
      clock.advance(remainingMs + 1)
      return true
    },
    primaryTimeoutMs: 10,
    lateGraceMs: 10,
    pollIntervalMs: 5,
    ...clock,
  })

  assert.deepEqual(result, { status: 'timeout' })
  assert.equal(clock.now(), 21, 'readiness after the hard deadline is rejected')
}

async function runNeverResolvingProbeCase() {
  const startedAt = performance.now()
  const result = await waitForGatewayReadiness({
    probe: async () => await new Promise(() => {}),
    primaryTimeoutMs: 5,
    lateGraceMs: 10,
    pollIntervalMs: 2,
  })

  assert.deepEqual(result, { status: 'timeout' })
  assert.ok(performance.now() - startedAt < 250, 'a stuck probe cannot escape the hard budget')
}

async function runStoppingSetOnlyCase() {
  const stopping = { name: 'already-stopping', live: true }
  const stopped = []
  const joined = []

  const exited = await stopAndJoinLifecycleProcesses({
    currentProcess: () => null,
    stopCurrentProcess: (process) => stopped.push(process.name),
    liveProcesses: () => stopping.live ? [stopping] : [],
    waitForExit: async (process) => {
      joined.push(process.name)
      process.live = false
      return true
    },
  })

  assert.equal(exited, true)
  assert.deepEqual(stopped, [])
  assert.deepEqual(joined, ['already-stopping'])
}

async function runCurrentPlusStoppingCase() {
  const current = { name: 'current', live: true }
  const stopping = { name: 'already-stopping', live: true }
  let currentSlot = current
  const joined = []

  const exited = await stopAndJoinLifecycleProcesses({
    currentProcess: () => currentSlot,
    stopCurrentProcess: (process) => {
      assert.equal(process, current)
      currentSlot = null
    },
    liveProcesses: () => [current, stopping].filter((process) => process.live),
    waitForExit: async (process) => {
      joined.push(process.name)
      process.live = false
      return true
    },
  })

  assert.equal(exited, true)
  assert.deepEqual(new Set(joined), new Set(['current', 'already-stopping']))
}

async function runLatePublishedChildCase() {
  const first = { name: 'first', live: true }
  const late = { name: 'late', live: false }
  const joined = []

  const exited = await stopAndJoinLifecycleProcesses({
    currentProcess: () => null,
    stopCurrentProcess: () => assert.fail('there is no current process'),
    liveProcesses: () => [first, late].filter((process) => process.live),
    waitForExit: async (process) => {
      joined.push(process.name)
      process.live = false
      if (process === first) late.live = true
      return true
    },
  })

  assert.equal(exited, true)
  assert.deepEqual(joined, ['first', 'late'])
}

async function runFailClosedCase() {
  const stuck = { name: 'stuck', live: true }
  let handoff = false
  const exited = await stopAndJoinLifecycleProcesses({
    currentProcess: () => null,
    stopCurrentProcess: () => {},
    liveProcesses: () => [stuck],
    waitForExit: async () => false,
  })
  if (exited) handoff = true

  assert.equal(exited, false)
  assert.equal(handoff, false)
}

async function runPendingSpawnAdmissionCase() {
  let lifecycleClosing = false
  let writerAdmissionClosed = false
  let published = false

  const pendingStart = Promise.resolve().then(() => {
    if (lifecycleAllowsProcessSpawn(lifecycleClosing, writerAdmissionClosed)) {
      published = true
    }
  })

  // The lifecycle closes admission before checking the (still empty) published
  // set. When the pending start resumes, its final pre-spawn check must reject
  // publication even though there was no ChildProcess handle to join.
  lifecycleClosing = true
  writerAdmissionClosed = true
  assert.equal(await stopAndJoinLifecycleProcesses({
    currentProcess: () => null,
    stopCurrentProcess: () => {},
    liveProcesses: () => [],
    waitForExit: async () => true,
  }), true)
  await pendingStart

  assert.equal(lifecycleAllowsProcessSpawn(true, false), false)
  assert.equal(lifecycleAllowsProcessSpawn(false, true), false)
  assert.equal(lifecycleAllowsProcessSpawn(false, false, 1), false)
  assert.equal(lifecycleAllowsProcessSpawn(false, false, 0), true)
  assert.equal(published, false)
}

await runStoppingSetOnlyCase()
await runCurrentPlusStoppingCase()
await runLatePublishedChildCase()
await runFailClosedCase()
await runPendingSpawnAdmissionCase()
await runReadinessBeforePrimaryDeadlineCase()
await runLateReadinessCase()
await runReadinessTimeoutCase()
await runReadinessExitCase()
await runReadinessExitDuringSuccessfulProbeCase()
await runProbeCrossesDeadlineCase()
await runNeverResolvingProbeCase()

console.log('desktop gateway lifecycle tests passed')
