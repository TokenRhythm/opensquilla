import assert from 'node:assert/strict'

import {
  lifecycleAllowsProcessSpawn,
  stopAndJoinLifecycleProcesses,
} from '../dist/gateway-lifecycle.js'

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

console.log('desktop gateway lifecycle tests passed')
