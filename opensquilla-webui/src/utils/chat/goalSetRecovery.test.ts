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

it('retains both Goal ingress identities across refresh until acceptance is known', async () => {
  const first = await import('./goalSetRecovery')
  const identity = first.goalSetIdentity('source-session', 3, 'Synthetic objective', { executionPolicy: 'background' })
  const pending = first.recoverGoalSet(identity)
  vi.resetModules()
  const reloaded = await import('./goalSetRecovery')
  expect(reloaded.recoverGoalSet(identity)).toEqual(pending)
  reloaded.forgetGoalSet(identity)
  expect(reloaded.recoverGoalSet(identity).clientRequestId).not.toBe(pending.clientRequestId)
})

it('separates session generations and changed Goal intent', async () => {
  const { goalSetIdentity, recoverGoalSet } = await import('./goalSetRecovery')
  const intents = [
    goalSetIdentity('source', 1, 'Synthetic objective', {}),
    goalSetIdentity('source', 2, 'Synthetic objective', {}),
    goalSetIdentity('source', 1, 'Changed objective', {}),
    goalSetIdentity('source', 1, 'Synthetic objective', { tokenBudget: 5000 }),
    goalSetIdentity('source', 1, 'Synthetic objective', { executionPolicy: 'background' }),
  ]
  expect(new Set(intents.map(identity => recoverGoalSet(identity).clientRequestId)).size).toBe(5)
})

it('retains both ingress identities when storage remains readable but writes fail', async () => {
  const { goalSetIdentity, recoverGoalSet } = await import('./goalSetRecovery')
  vi.spyOn(sessionStorage, 'setItem').mockImplementation(() => {
    throw new DOMException('Storage quota exceeded', 'QuotaExceededError')
  })
  const identity = goalSetIdentity('source', 1, 'Synthetic objective', {})
  const pending = recoverGoalSet(identity)

  expect(sessionStorage.getItem('opensquilla.goalSetRecovery.v1')).toBeNull()
  expect(recoverGoalSet(identity)).toEqual(pending)
})

it.each(['request', 'session'] as const)(
  'does not resurrect a forgotten %s from stale storage after a failed write',
  async (scope) => {
    const recovery = await import('./goalSetRecovery')
    const identity = recovery.goalSetIdentity('source', 1, 'Synthetic objective', {})
    const otherIdentity = recovery.goalSetIdentity('other', 1, 'Other objective', {})
    const original = recovery.recoverGoalSet(identity)
    const other = recovery.recoverGoalSet(otherIdentity)
    const persisted = sessionStorage.getItem('opensquilla.goalSetRecovery.v1')
    vi.spyOn(sessionStorage, 'setItem').mockImplementation(() => {
      throw new DOMException('Storage quota exceeded', 'QuotaExceededError')
    })

    if (scope === 'request') recovery.forgetGoalSet(identity)
    else recovery.forgetGoalSetsForSession('source')
    expect(sessionStorage.getItem('opensquilla.goalSetRecovery.v1')).toBe(persisted)

    const replacement = recovery.recoverGoalSet(identity)
    expect(replacement.clientRequestId).not.toBe(original.clientRequestId)
    expect(replacement.clientMessageId).not.toBe(original.clientMessageId)
    expect(recovery.recoverGoalSet(identity)).toEqual(replacement)
    expect(recovery.recoverGoalSet(otherIdentity)).toEqual(other)
  },
)

it('retains recovered Goal identities when a later storage read fails', async () => {
  const first = await import('./goalSetRecovery')
  const identity = first.goalSetIdentity('source', 1, 'Synthetic objective', {})
  const pending = first.recoverGoalSet(identity)
  vi.resetModules()
  const reloaded = await import('./goalSetRecovery')
  expect(reloaded.recoverGoalSet(identity)).toEqual(pending)
  vi.spyOn(sessionStorage, 'getItem').mockImplementationOnce(() => {
    throw new DOMException('Storage temporarily unavailable', 'SecurityError')
  })

  expect(reloaded.recoverGoalSet(identity)).toEqual(pending)
  expect(reloaded.recoverGoalSet(identity)).toEqual(pending)
})
