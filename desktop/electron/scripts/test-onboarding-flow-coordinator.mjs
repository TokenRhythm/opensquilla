import { strict as assert } from 'node:assert'
import { OnboardingFlowCoordinator } from '../dist/onboarding-flow-coordinator.js'

function deferred() {
  let resolvePromise
  let rejectPromise
  const promise = new Promise((resolve, reject) => {
    resolvePromise = resolve
    rejectPromise = reject
  })
  return { promise, resolve: resolvePromise, reject: rejectPromise }
}

function flow() {
  return {
    state: 'editing',
    savePayload: null,
    savePromise: null,
  }
}

async function verifyExactPayloadSingleFlight() {
  const coordinator = new OnboardingFlowCoordinator()
  const current = flow()
  const gate = deferred()
  let runs = 0
  assert.equal(coordinator.activate(current), true)

  const payload = {
    provider: 'synthetic',
    model: 'model-a',
    routerTiers: { c1: { provider: 'synthetic', model: 'model-a' } },
  }
  const first = coordinator.requestSave(current, payload, async () => {
    runs += 1
    await gate.promise
    assert.equal(coordinator.complete(current), true)
    return { ok: true }
  })
  const same = coordinator.requestSave(
    current,
    structuredClone(payload),
    async () => ({ ok: false }),
  )
  const different = coordinator.requestSave(
    current,
    { ...structuredClone(payload), model: 'model-b' },
    async () => ({ ok: false }),
  )

  assert.equal(first.kind, 'started')
  assert.equal(same.kind, 'joined')
  assert.equal(different.kind, 'conflict')
  assert.strictEqual(same.promise, first.promise)
  assert.equal(runs, 0, 'save work must not start before the flight is published')
  await Promise.resolve()
  assert.equal(runs, 1, 'joined requests must execute the save exactly once')

  gate.resolve()
  assert.deepEqual(await first.promise, { ok: true })
  assert.deepEqual(await same.promise, { ok: true })
  assert.equal(current.state, 'completed')
  assert.equal(current.savePromise, null)
  assert.equal(coordinator.active, null)
}

async function verifyAbandonedFlowCannotCompleteOrReplaceCurrentFlow() {
  const coordinator = new OnboardingFlowCoordinator()
  const abandoned = flow()
  const abandonedGate = deferred()
  assert.equal(coordinator.activate(abandoned), true)

  const abandonedSave = coordinator.requestSave(abandoned, { provider: 'old' }, async () => {
    await abandonedGate.promise
    return { completed: coordinator.complete(abandoned) }
  })
  assert.equal(abandonedSave.kind, 'started')
  assert.equal(coordinator.abandon(abandoned), true)
  assert.equal(coordinator.activate(flow()), false, 'a detached save must retain flow ownership')

  abandonedGate.resolve()
  assert.deepEqual(await abandonedSave.promise, { completed: false })
  assert.equal(abandoned.state, 'abandoned')
  assert.equal(coordinator.active, null)

  const replacement = flow()
  const replacementGate = deferred()
  assert.equal(coordinator.activate(replacement), true)
  const replacementSave = coordinator.requestSave(
    replacement,
    { provider: 'new' },
    async () => {
      await replacementGate.promise
      return { completed: coordinator.complete(replacement) }
    },
  )
  assert.equal(replacementSave.kind, 'started')
  assert.equal(coordinator.complete(abandoned), false)
  assert.strictEqual(coordinator.active, replacement)

  replacementGate.resolve()
  assert.deepEqual(await replacementSave.promise, { completed: true })
  assert.equal(replacement.state, 'completed')
  assert.equal(coordinator.active, null)
}

await verifyExactPayloadSingleFlight()
await verifyAbandonedFlowCannotCompleteOrReplaceCurrentFlow()
console.log('onboarding flow coordinator tests passed')
