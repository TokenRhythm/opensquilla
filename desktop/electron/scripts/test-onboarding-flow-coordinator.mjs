import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { OnboardingFlowCoordinator } from '../dist/onboarding-flow-coordinator.js'
import { OnboardingSaveTelemetry } from '../dist/onboarding-save-telemetry.js'

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

function compiledSaveHarness(coordinator, stopGateway) {
  const main = readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8')
  const start = main.indexOf('async function performOnboardingSave(')
  const end = main.indexOf('async function withRecoveryOperation', start)
  assert.ok(start !== -1 && end > start, 'compiled main must expose the real save operation')
  // Exercise the actual compiled orchestration without launching Electron. The
  // recovery-required result bounds this regression before any filesystem write.
  return runInNewContext(`${main.slice(start, end)}; performOnboardingSave`, {
    OnboardingSaveTelemetry,
    onboardingSaveTelemetryAttempt: 0,
    app: { isPackaged: false },
    desktopLog: () => {},
    desktopProfileKey: () => 'primary',
    gatewayProcess: {},
    gatewayState: { owned: true },
    onboardingFlows: coordinator,
    stopOwnedGatewayAndWait: stopGateway,
    refreshPrimaryRecoveryAfterImportAttempt: async () => true,
    onboardingSaveFailure: (code, error) => ({ ok: false, code, error }),
    desktopWriters: { closed: false },
    isQuitting: false,
    appExitPhase: 'running',
    clearReusableGatewayState: () => {},
    bootError: null,
    openOrResumeDesktopApp: async () => {},
  })
}

async function verifyGatewayStopFailureCanBeRetried() {
  const coordinator = new OnboardingFlowCoordinator()
  const current = flow()
  assert.equal(coordinator.activate(current), true)
  let stops = 0
  const perform = compiledSaveHarness(coordinator, async () => {
    stops += 1
    if (stops === 1) throw new Error('Synthetic Gateway stop timed out.')
  })
  const first = coordinator.requestSave(current, {}, () => perform(current, {}))
  assert.equal(first.kind, 'started')
  await assert.rejects(first.promise, /Synthetic Gateway stop timed out/)
  assert.equal(current.state, 'editing', 'a stop failure must restore a retryable flow')
  assert.equal(current.savePromise, null)

  const retry = coordinator.requestSave(current, {}, () => perform(current, {}))
  assert.equal(retry.kind, 'started', 'retry must enter the real save operation again')
  assert.equal((await retry.promise).code, 'recovery_required')
  assert.equal(stops, 2)
  assert.equal(current.state, 'editing')
}

async function verifyGatewayStopFailureDoesNotReviveDismissedFlow() {
  const coordinator = new OnboardingFlowCoordinator()
  const current = flow()
  const gate = deferred()
  assert.equal(coordinator.activate(current), true)
  const perform = compiledSaveHarness(coordinator, () => gate.promise)
  const request = coordinator.requestSave(current, {}, () => perform(current, {}))
  assert.equal(request.kind, 'started')
  await Promise.resolve()
  assert.equal(coordinator.abandon(current), true)
  const rejected = assert.rejects(request.promise, /Synthetic late stop failure/)
  gate.reject(new Error('Synthetic late stop failure.'))
  await rejected
  assert.equal(current.state, 'abandoned', 'late stop errors must not reopen dismissed setup')
  assert.equal(coordinator.active, null)
}

function compiledMigrationAdmissionHarness(coordinator, admitted) {
  const main = readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8')
  const start = main.indexOf("ipcMain.handle('desktop:migration:run',")
  const end = main.indexOf('let report = null;', start)
  assert.ok(start !== -1 && end > start, 'compiled main must expose import admission')
  let handler
  // Run the real trusted import preflight and admission, stopping before the
  // filesystem transaction. The onboarding state must already be retired here.
  runInNewContext(`${main.slice(start, end)} return { admitted: true }; });`, {
    ipcMain: { handle: (_name, callback) => { handler = callback } },
    trustedRecoveryIpc: () => true,
    trustedDesktopMigrationPreview: {
      id: 'synthetic-preview',
      createdAt: Date.now(),
      candidate: { path: '/synthetic/import' },
      report: {},
    },
    DESKTOP_MIGRATION_PREVIEW_TTL_MS: 60_000,
    migrationPreviewAllowsApply: () => true,
    looksLikeOpenSquillaHome: () => true,
    desktopWriters: { tryBeginExclusive: () => admitted ? {} : null },
    onboardingFlows: coordinator,
    dismissOnboardingFlow: (current) => coordinator.abandon(current),
  })
  assert.equal(typeof handler, 'function')
  return () => handler({}, { previewId: 'synthetic-preview' })
}

async function verifyImportDoesNotReuseThePreviousProviderDraft() {
  const coordinator = new OnboardingFlowCoordinator()
  const previous = flow()
  const previousPayload = { provider: 'tokenrhythm', apiKey: 'synthetic-old-key' }
  assert.equal(coordinator.activate(previous), true)

  const refused = await compiledMigrationAdmissionHarness(coordinator, false)()
  assert.equal(refused.ok, false)
  assert.strictEqual(coordinator.active, previous, 'a refused import must retain the draft')
  assert.equal(previous.state, 'editing')

  const admitted = await compiledMigrationAdmissionHarness(coordinator, true)()
  assert.equal(admitted.admitted, true)
  assert.equal(previous.state, 'abandoned', 'import must retire the pre-import provider draft')
  assert.equal(coordinator.active, null)
  let oldDraftWrites = 0
  const stale = coordinator.requestSave(previous, previousPayload, async () => {
    oldDraftWrites += 1
    return { ok: true }
  })
  assert.equal(stale.kind, 'inactive')

  const imported = flow()
  assert.equal(coordinator.activate(imported), true, 'imported provider may open a fresh invitation')
  const saved = coordinator.requestSave(imported, { provider: 'openai' }, async () => ({ ok: true }))
  assert.equal(saved.kind, 'started')
  assert.deepEqual(await saved.promise, { ok: true })
  assert.equal(oldDraftWrites, 0, 'the previous API key must never enter the imported save')
}

await verifyExactPayloadSingleFlight()
await verifyAbandonedFlowCannotCompleteOrReplaceCurrentFlow()
await verifyGatewayStopFailureCanBeRetried()
await verifyGatewayStopFailureDoesNotReviveDismissedFlow()
await verifyImportDoesNotReuseThePreviousProviderDraft()
console.log('onboarding flow coordinator tests passed')
