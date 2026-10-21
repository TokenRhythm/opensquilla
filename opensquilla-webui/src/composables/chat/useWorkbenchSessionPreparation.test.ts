import { describe, expect, it, vi } from 'vitest'
import type { SessionCreateRequest } from '@/modules/sessionLifecycle'
import type { SessionRouting, SessionRoutingSnapshot } from '@/modules/sessionRouting'
import { PlanModeConflictError, type PlanCenter } from '@/modules/planCenter'
import { configureWorkbenchSessionCollaboration, configureWorkbenchSessionRouting, useWorkbenchSessionPreparation, type WorkbenchSessionPreparationState } from './useWorkbenchSessionPreparation'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}

function harness() {
  const state: WorkbenchSessionPreparationState = {
    key: 'agent:main:webchat:draft', durable: false, canCreate: true, scope: 'project-a:connection-1',
    request: { agentId: 'main', kind: 'webchat', workspaceId: 'project-a', model: 'chosen-model', provider: 'chosen-provider' },
  }
  const created = deferred<{ key: string; sessionId: string }>()
  const create = vi.fn((_request?: SessionCreateRequest) => created.promise)
  const configure = vi.fn(async () => {})
  const commit = vi.fn(async () => { state.durable = true; return true })
  const preparation = useWorkbenchSessionPreparation({ state: () => ({ ...state }), lifecycle: { create }, configure, commit })
  return { state, created, create, configure, commit, preparation }
}

describe('Workbench session preparation', () => {
  it('accepts an already committed Plan selection after the first response was lost', async () => {
    const setMode = vi.fn().mockRejectedValue(new PlanModeConflictError({ mode: 'plan', revision: 1 }))
    await configureWorkbenchSessionCollaboration({ setMode } as unknown as PlanCenter,
      'draft-key', 'plan', 0, () => true)
    expect(setMode).toHaveBeenCalledOnce()
  })

  it('clears a partial Plan attempt with one authoritative CAS retry when the draft returns to Default', async () => {
    const setMode = vi.fn()
      .mockRejectedValueOnce(new PlanModeConflictError({ mode: 'plan', revision: 1 }))
      .mockResolvedValue({ collaboration: { mode: 'default', revision: 2 } })
    await configureWorkbenchSessionCollaboration({ setMode } as unknown as PlanCenter,
      'draft-key', 'default', 0, () => true)
    expect(setMode.mock.calls).toEqual([['draft-key', 'default', 0], ['draft-key', 'default', 1]])
  })

  it('does not retry collaboration conflicts after navigation or repeatedly', async () => {
    const conflict = new PlanModeConflictError({ mode: 'plan', revision: 1 })
    const setMode = vi.fn().mockRejectedValue(conflict)
    const plans = { setMode } as unknown as PlanCenter
    await expect(configureWorkbenchSessionCollaboration(plans, 'draft-key', 'default', 0, () => false)).rejects.toBe(conflict)
    expect(setMode).toHaveBeenCalledOnce()
    setMode.mockClear()
    await expect(configureWorkbenchSessionCollaboration(plans, 'draft-key', 'default', 0, () => true)).rejects.toBe(conflict)
    expect(setMode).toHaveBeenCalledTimes(2)
  })

  it.each([
    { model: 'model-b', provider: 'provider-b' },
    null,
  ])('reconciles a previous partial creation pin with the current draft selection %s', async modelSelection => {
    const snapshot: SessionRoutingSnapshot = {
      key: 'draft-key', mode: 'direct', revision: 3, source: 'session', initialized: true, appliesTo: 'current',
      modelSelection: { model: 'model-a', provider: 'provider-a' },
    }
    const routing: SessionRouting = {
      available: () => true, get: vi.fn(async () => snapshot), set: vi.fn(async () => snapshot),
      subscribe: () => ({ close() {} }), dispose() {},
    }
    await configureWorkbenchSessionRouting(routing, 'draft-key', null, modelSelection, () => true)
    expect(routing.set).toHaveBeenCalledExactlyOnceWith({
      sessionKey: 'draft-key', mode: 'direct', expectedRevision: 3, modelSelection,
    })
  })

  it('does not configure the old draft after its routing read outlives navigation', async () => {
    const routing = {
      get: vi.fn(async () => ({ mode: 'direct', revision: 0, modelSelection: null })),
      set: vi.fn(),
    } as unknown as SessionRouting
    await configureWorkbenchSessionRouting(routing, 'draft-key', 'router', null, () => false)
    expect(routing.set).not.toHaveBeenCalled()
  })

  it('creates no session until activation and shares one same-key creation across concurrent panel clicks', async () => {
    const h = harness()
    expect(h.create).not.toHaveBeenCalled()
    const first = h.preparation.prepare(h.state.key)
    const second = h.preparation.prepare(h.state.key)
    expect(second).toBe(first)
    expect(h.preparation.busy.value).toBe(true)
    await Promise.resolve()
    expect(h.create).toHaveBeenCalledExactlyOnceWith({
      sessionKey: h.state.key, agentId: 'main', kind: 'webchat', workspaceId: 'project-a',
      model: 'chosen-model', provider: 'chosen-provider',
    })
    expect(h.create.mock.calls[0]?.[0]).not.toHaveProperty('message')
    h.created.resolve({ key: h.state.key, sessionId: 'durable-id' })
    expect(await first).toBe(h.state.key)
    expect(h.commit).toHaveBeenCalledExactlyOnceWith(h.state.key)
    expect(h.preparation.busy.value).toBe(false)
    expect(await h.preparation.prepare(h.state.key)).toBe(h.state.key)
    expect(h.create).toHaveBeenCalledOnce()
  })

  it.each(['project-b:connection-1', 'project-a:connection-2', 'navigated-away'])('ignores stale creation after %s', async scope => {
    const h = harness()
    const pending = h.preparation.prepare(h.state.key)
    await Promise.resolve()
    h.state.scope = scope
    h.created.resolve({ key: h.state.key, sessionId: 'durable-id' })
    expect(await pending).toBeNull()
    expect(h.configure).not.toHaveBeenCalled()
    expect(h.commit).not.toHaveBeenCalled()
    expect(h.preparation.busy.value).toBe(false)
  })

  it('rejects a newly allocated key instead of silently dropping draft-owned data', async () => {
    const h = harness()
    const pending = h.preparation.prepare(h.state.key)
    h.created.resolve({ key: 'agent:main:webchat:other', sessionId: 'different' })
    await expect(pending).rejects.toThrow('preserve the draft session identity')
    expect(h.commit).not.toHaveBeenCalled()
    expect(h.preparation.busy.value).toBe(false)
  })

  it('refuses unavailable or busy draft sessions and stale expected keys', async () => {
    const h = harness()
    h.state.canCreate = false
    expect(await h.preparation.prepare(h.state.key)).toBeNull()
    expect(await h.preparation.prepare('previous-key')).toBeNull()
    expect(h.create).not.toHaveBeenCalled()
  })

  it('rechecks ownership after routing configuration before changing the visible session', async () => {
    const h = harness()
    const configured = deferred<void>()
    h.configure.mockImplementation(() => configured.promise)
    const pending = h.preparation.prepare(h.state.key)
    h.created.resolve({ key: h.state.key, sessionId: 'durable-id' })
    await Promise.resolve()
    await Promise.resolve()
    h.state.canCreate = false
    configured.resolve()
    expect(await pending).toBeNull()
    expect(h.commit).not.toHaveBeenCalled()
  })

  it('releases the busy gate after failure so the draft can retry', async () => {
    const h = harness()
    const pending = h.preparation.prepare(h.state.key)
    h.created.reject(new Error('offline'))
    await expect(pending).rejects.toThrow('offline')
    expect(h.preparation.busy.value).toBe(false)
    h.create.mockResolvedValue({ key: h.state.key, sessionId: 'durable-id' })
    expect(await h.preparation.prepare(h.state.key)).toBe(h.state.key)
    expect(h.create).toHaveBeenCalledTimes(2)
  })
})
