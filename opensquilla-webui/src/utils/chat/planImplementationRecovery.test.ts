// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

beforeEach(() => {
  vi.resetModules()
  // Happy DOM caches bound methods; use a fresh instance after quota spies.
  vi.stubGlobal('sessionStorage', new Storage())
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('retains the implementation receipt and new-session destination across a reload', async () => {
  const first = await import('./planImplementationRecovery')
  const identity = first.planImplementationIdentity('source-session', 3, 'plan-revision', true)
  const pending = first.recoverPlanImplementation(identity, () => 'destination-one')
  vi.resetModules()
  const reloaded = await import('./planImplementationRecovery')
  expect(reloaded.recoverPlanImplementation(identity, () => 'destination-two')).toEqual(pending)
  reloaded.forgetPlanImplementation(identity)
  expect(reloaded.recoverPlanImplementation(identity, () => 'destination-three')).toMatchObject({
    targetSessionKey: 'destination-three',
    clientRequestId: expect.not.stringMatching(pending.clientRequestId),
  })
})

it('does not reuse a request from another session generation or implementation intent', async () => {
  const { planImplementationIdentity, recoverPlanImplementation } = await import('./planImplementationRecovery')
  const targets = [[2, false], [3, false], [2, true]] as const
  const ids = targets.map(([epoch, inNewSession]) => recoverPlanImplementation(
    planImplementationIdentity('source-session', epoch, 'revision', inNewSession),
    () => 'destination',
  ).clientRequestId)
  expect(new Set(ids).size).toBe(3)
})

it('retains the receipt and destination when readable storage rejects writes', async () => {
  const { planImplementationIdentity, recoverPlanImplementation } = await import('./planImplementationRecovery')
  vi.spyOn(sessionStorage, 'setItem').mockImplementation(() => {
    throw new DOMException('Storage quota exceeded', 'QuotaExceededError')
  })
  const identity = planImplementationIdentity('source', 1, 'revision', true)
  const pending = recoverPlanImplementation(identity, () => 'destination-one')
  const createRetryDestination = vi.fn(() => 'destination-two')

  expect(sessionStorage.getItem('opensquilla.planImplementationRecovery.v1')).toBeNull()
  expect(recoverPlanImplementation(identity, createRetryDestination)).toEqual(pending)
  expect(createRetryDestination).not.toHaveBeenCalled()
})

it('does not resurrect a forgotten implementation from stale storage', async () => {
  const recovery = await import('./planImplementationRecovery')
  const identity = recovery.planImplementationIdentity('source', 1, 'revision', true)
  const otherIdentity = recovery.planImplementationIdentity('other', 1, 'revision', true)
  const original = recovery.recoverPlanImplementation(identity, () => 'destination-one')
  const other = recovery.recoverPlanImplementation(otherIdentity, () => 'other-destination')
  const persisted = sessionStorage.getItem('opensquilla.planImplementationRecovery.v1')
  vi.spyOn(sessionStorage, 'setItem').mockImplementation(() => {
    throw new DOMException('Storage quota exceeded', 'QuotaExceededError')
  })

  recovery.forgetPlanImplementation(identity)
  expect(sessionStorage.getItem('opensquilla.planImplementationRecovery.v1')).toBe(persisted)
  const replacement = recovery.recoverPlanImplementation(identity, () => 'destination-two')
  expect(replacement.clientRequestId).not.toBe(original.clientRequestId)
  expect(replacement.targetSessionKey).toBe('destination-two')
  expect(recovery.recoverPlanImplementation(identity, () => 'destination-three')).toEqual(replacement)
  expect(recovery.recoverPlanImplementation(otherIdentity, () => 'unused')).toEqual(other)
})

it('retains a recovered implementation when a later storage read fails', async () => {
  const first = await import('./planImplementationRecovery')
  const identity = first.planImplementationIdentity('source', 1, 'revision', true)
  const pending = first.recoverPlanImplementation(identity, () => 'destination-one')
  vi.resetModules()
  const reloaded = await import('./planImplementationRecovery')
  const createRetryDestination = vi.fn(() => 'destination-two')
  expect(reloaded.recoverPlanImplementation(identity, createRetryDestination)).toEqual(pending)
  vi.spyOn(sessionStorage, 'getItem').mockImplementationOnce(() => {
    throw new DOMException('Storage temporarily unavailable', 'SecurityError')
  })

  expect(reloaded.recoverPlanImplementation(identity, createRetryDestination)).toEqual(pending)
  expect(createRetryDestination).not.toHaveBeenCalled()
  expect(reloaded.recoverPlanImplementation(identity, createRetryDestination)).toEqual(pending)
})
