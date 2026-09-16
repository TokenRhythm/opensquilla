import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope } from 'vue'
import { createSkillInstallReceipts } from './skillInstallReceipts'
import { useSkillRegistry } from './useSkillRegistry'
import type { SkillCatalog } from '@/modules/skillCatalog'

vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: vi.fn() }) }))
const SCOPE = 'a'.repeat(64)
const ID = '00000000-0000-4000-8000-000000000001'
const rows = new Map<string, string>()

beforeEach(() => {
  rows.clear()
  vi.useFakeTimers()
  vi.stubGlobal('sessionStorage', {
    getItem: (key: string) => rows.get(key) ?? null,
    setItem: (key: string, value: string) => rows.set(key, value),
  })
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

function catalog() {
  return {
    supportsInstallStatus: () => true,
    supportsInstallCancellation: () => true,
    install: vi.fn(async () => { throw new Error('RPC timed out') }),
    installStatus: vi.fn(async (operationId: string) => ({
      operationId, scope: SCOPE, state: 'unknown', phase: 'unknown', terminal: true,
    })),
  } as unknown as SkillCatalog
}

describe('durable Skill receipts', () => {
  it('waits after timeout and refreshes exactly once without another install', async () => {
    const service = catalog()
    let reads = 0
    vi.mocked(service.installStatus!).mockImplementation(async operationId => {
      if (operationId.startsWith('00000000-0000-0000')) {
        return { operationId, scope: SCOPE, state: 'unknown', phase: 'unknown', terminal: true }
      }
      reads++
      return reads === 1
        ? { operationId, scope: SCOPE, state: 'running', phase: 'downloading', terminal: false }
        : { operationId, scope: SCOPE, state: 'succeeded', phase: 'complete', terminal: true,
          result: { success: true, installed: true, effectiveFrom: 'next_turn' } }
    })
    const load = vi.fn(async () => true)
    const scope = effectScope()
    const registry = scope.run(() => useSkillRegistry(service, load))!
    registry.githubUrl.value = 'https://github.com/acme/demo'
    const run = registry.installGithub()
    await vi.advanceTimersByTimeAsync(0)
    expect(registry.installActivities.value.github.items[0]?.status).toBe('waiting')
    await vi.advanceTimersByTimeAsync(2000)
    expect(load).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(5000)
    await run
    expect(registry.installActivities.value.github.items[0]?.status).toBe('installed')
    expect(service.install).toHaveBeenCalledTimes(1)
    expect(load).toHaveBeenCalledTimes(1)
    scope.stop()
  })

  it('recovers an outstanding ID after remount without replaying installation', async () => {
    const service = catalog()
    const controller = new AbortController()
    const receipts = createSkillInstallReceipts(service, controller.signal)
    await receipts.remember({ operationId: ID, source: 'github',
      identifier: 'https://github.com/acme/demo', displayName: 'demo' })
    vi.mocked(service.installStatus!).mockImplementation(async operationId => ({
      operationId, scope: SCOPE, state: 'succeeded', phase: 'complete', terminal: true,
      result: { success: true, name: 'demo' },
    }))
    const load = vi.fn(async () => true)
    const scope = effectScope()
    const registry = scope.run(() => useSkillRegistry(service, load))!
    await vi.advanceTimersByTimeAsync(2000)
    expect(registry.installActivities.value.github.items[0]?.status).toBe('installed')
    expect(service.install).not.toHaveBeenCalled()
    expect(load).toHaveBeenCalledTimes(1)
    expect(await receipts.pending()).toEqual([])
    scope.stop()
  })

  it('does not restore IDs from a different profile or caller', async () => {
    const service = catalog()
    const receipts = createSkillInstallReceipts(service, new AbortController().signal)
    await receipts.remember({ operationId: ID, source: 'github',
      identifier: 'https://github.com/acme/demo', displayName: 'demo' })
    vi.mocked(service.installStatus!).mockResolvedValue({
      operationId: ID, scope: 'b'.repeat(64), state: 'unknown', phase: 'unknown', terminal: true,
    })
    expect(await receipts.pending()).toEqual([])
    expect(service.install).not.toHaveBeenCalled()
  })

  it('retains every recovered result from the same source after remount', async () => {
    const service = catalog()
    const receipts = createSkillInstallReceipts(service, new AbortController().signal)
    const secondId = '00000000-0000-4000-8000-000000000002'
    await receipts.remember({ operationId: ID, source: 'github',
      identifier: 'https://github.com/acme/first', displayName: 'first' })
    await receipts.remember({ operationId: secondId, source: 'github',
      identifier: 'https://github.com/acme/second', displayName: 'second' })
    vi.mocked(service.installStatus!).mockImplementation(async operationId => (
      operationId === ID
        ? { operationId, scope: SCOPE, state: 'recovery_required', phase: 'complete', terminal: true,
          result: { success: false, recoveryRequired: true, message: 'Recovery is required' } }
        : { operationId, scope: SCOPE, state: 'succeeded', phase: 'complete', terminal: true,
          result: { success: true, name: 'second' } }
    ))
    const scope = effectScope()
    try {
      const registry = scope.run(() => useSkillRegistry(service, vi.fn(async () => true)))!
      await vi.advanceTimersByTimeAsync(4000)
      expect(registry.installActivities.value.github.items.map(item => item.displayName))
        .toEqual(['first', 'second'])
      expect(registry.installActivities.value.github.items.map(item => item.status))
        .toEqual(['unknown', 'installed'])
      expect(registry.installActivities.value.github.items[0]?.error).toBe('Recovery is required')
      expect(service.install).not.toHaveBeenCalled()
      expect((await receipts.pending()).map(row => row.operationId)).toEqual([ID])
    } finally {
      scope.stop()
    }
  })
})


it('stops waiting when status access is revoked without replaying installation', async () => {
  const service = catalog()
  vi.mocked(service.installStatus!).mockRejectedValue(Object.assign(new Error('access denied'), { code: 'UNAUTHORIZED' }))
  const receipts = createSkillInstallReceipts(service, new AbortController().signal)
  const waiting = receipts.wait(ID, vi.fn())
  const rejected = expect(waiting).rejects.toThrow('access denied')
  await vi.advanceTimersByTimeAsync(2000)
  await rejected
  expect(service.installStatus).toHaveBeenCalledTimes(1)
  expect(service.install).not.toHaveBeenCalled()
})
