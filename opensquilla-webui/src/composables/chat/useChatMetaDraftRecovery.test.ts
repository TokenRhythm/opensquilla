import { describe, expect, it, vi } from 'vitest'

import {
  createChatMetaDraftRecovery,
  listServerMetaDrafts,
  queryServerMetaDrafts,
  type MetaDraftListRpc,
  type MetaDraftListResult,
} from './useChatMetaDraftRecovery'
import type { DurableMetaDraft } from './useChatSlashCommands'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

function draft(sessionKey = 'agent:main:webchat:server-draft'): DurableMetaDraft {
  return {
    sessionKey,
    clientRequestId: 'request-1',
    name: 'meta-paper-write',
    launchText: '/meta run meta-paper-write',
    createdAt: 1,
    expiresAt: 2,
    sessionExists: false,
  }
}

function rpcHarness(overrides: Partial<MetaDraftListRpc> = {}): MetaDraftListRpc {
  return {
    waitForConnection: vi.fn(async () => {}),
    supportsMethod: vi.fn(() => true),
    markMethodUnavailable: vi.fn(),
    call: vi.fn(async () => ({ ok: true, durable: true, drafts: [] })),
    ...overrides,
  }
}

function result(
  drafts: DurableMetaDraft[] = [],
  retryable = false,
): MetaDraftListResult {
  return { drafts, retryable }
}

describe('listServerMetaDrafts', () => {
  it('does not call an older gateway that does not advertise the method', async () => {
    const rpc = rpcHarness({ supportsMethod: vi.fn(() => false) })

    await expect(listServerMetaDrafts(rpc, { agentId: 'main' })).resolves.toEqual([])

    expect(rpc.waitForConnection).toHaveBeenCalledWith(15_000)
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('marks a falsely advertised method unavailable', async () => {
    const error = Object.assign(new Error('method not found'), { code: 'METHOD_NOT_FOUND' })
    const rpc = rpcHarness({ call: vi.fn(async () => { throw error }) })

    await expect(listServerMetaDrafts(rpc, { agentId: 'main' })).resolves.toEqual([])

    expect(rpc.markMethodUnavailable).toHaveBeenCalledWith('meta.drafts.list')
  })

  it('classifies a transient connection failure as retryable', async () => {
    const rpc = rpcHarness({
      waitForConnection: vi.fn(async () => { throw new Error('connection timed out') }),
    })

    await expect(queryServerMetaDrafts(rpc, { agentId: 'main' })).resolves.toEqual(
      result([], true),
    )
  })
})

describe('createChatMetaDraftRecovery', () => {
  it('starts discovery without waiting for a slow Meta RPC', () => {
    const pending = deferred<MetaDraftListResult>()
    const rebindDraftSession = vi.fn()
    const recovery = createChatMetaDraftRecovery({
      currentSessionKey: () => 'agent:main:webchat:local-draft',
      listDrafts: () => pending.promise,
      isPristineDraft: () => true,
      rebindDraftSession,
      onAuthoritativeSubscription: vi.fn(),
    })

    expect(recovery.start('main')).toBeUndefined()
    expect(rebindDraftSession).not.toHaveBeenCalled()
  })

  it('rebinds an untouched draft and restores only after an authoritative subscription', async () => {
    let sessionKey = 'agent:main:webchat:local-draft'
    const serverDraft = draft()
    const restore = vi.fn()
    const recovery = createChatMetaDraftRecovery({
      currentSessionKey: () => sessionKey,
      listDrafts: vi.fn(async () => result([serverDraft])),
      isPristineDraft: key => key === sessionKey,
      rebindDraftSession: vi.fn(async (key, guard) => {
        expect(guard(sessionKey)).toBe(true)
        sessionKey = key
        return { authoritative: true, live: false, backgroundOnly: false }
      }),
      onAuthoritativeSubscription: restore,
    })

    recovery.start('main')
    await vi.waitFor(() => expect(restore).toHaveBeenCalledOnce())

    expect(restore).toHaveBeenCalledWith(serverDraft.sessionKey, [serverDraft])
  })

  it('ignores a late result after typing or navigation invalidates the draft', async () => {
    const pending = deferred<MetaDraftListResult>()
    let pristine = true
    let sessionKey = 'agent:main:webchat:local-draft'
    const rebindDraftSession = vi.fn()
    const recovery = createChatMetaDraftRecovery({
      currentSessionKey: () => sessionKey,
      listDrafts: () => pending.promise,
      isPristineDraft: () => pristine,
      rebindDraftSession,
      onAuthoritativeSubscription: vi.fn(),
    })

    recovery.start('main')
    pristine = false
    sessionKey = 'agent:main:webchat:other'
    recovery.invalidate()
    pending.resolve(result([draft()]))
    await Promise.resolve()
    await Promise.resolve()

    expect(rebindDraftSession).not.toHaveBeenCalled()
  })

  it('keeps recovery pending when the rebound subscription is non-authoritative', async () => {
    let sessionKey = 'agent:main:webchat:local-draft'
    const restore = vi.fn()
    const recovery = createChatMetaDraftRecovery({
      currentSessionKey: () => sessionKey,
      listDrafts: vi.fn(async () => result([draft()])),
      isPristineDraft: key => key === sessionKey,
      rebindDraftSession: vi.fn(async (key) => {
        sessionKey = key
        return { authoritative: false, live: false, backgroundOnly: false }
      }),
      onAuthoritativeSubscription: restore,
    })

    recovery.start('main')
    await Promise.resolve()
    await Promise.resolve()

    expect(restore).not.toHaveBeenCalled()
    expect(sessionKey).toBe(draft().sessionKey)
  })

  it('does not restore durable controls when typing starts during the rebound subscription', async () => {
    let sessionKey = 'agent:main:webchat:local-draft'
    let pristine = true
    const serverDraft = draft()
    const subscription = deferred<{
      authoritative: boolean
      live: boolean
      backgroundOnly: boolean
    }>()
    const restore = vi.fn()
    const isPristineDraft = vi.fn((key: string) => key === sessionKey && pristine)
    const rebindDraftSession = vi.fn(async (key: string) => {
      sessionKey = key
      return subscription.promise
    })
    const recovery = createChatMetaDraftRecovery({
      currentSessionKey: () => sessionKey,
      listDrafts: vi.fn(async () => result([serverDraft])),
      isPristineDraft,
      rebindDraftSession,
      onAuthoritativeSubscription: restore,
    })

    recovery.start('main')
    await vi.waitFor(() => expect(rebindDraftSession).toHaveBeenCalledOnce())
    pristine = false
    subscription.resolve({ authoritative: true, live: false, backgroundOnly: false })
    await vi.waitFor(() => {
      expect(isPristineDraft).toHaveBeenCalledWith(serverDraft.sessionKey, 'main')
    })

    expect(restore).not.toHaveBeenCalled()
  })

  it('retries a transient discovery failure after an authoritative reconnect', async () => {
    let sessionKey = 'agent:main:webchat:local-draft'
    const serverDraft = draft()
    const restore = vi.fn()
    const listDrafts = vi.fn()
      .mockResolvedValueOnce(result([], true))
      .mockResolvedValueOnce(result([serverDraft]))
    const recovery = createChatMetaDraftRecovery({
      currentSessionKey: () => sessionKey,
      listDrafts,
      isPristineDraft: key => key === sessionKey,
      rebindDraftSession: vi.fn(async (key) => {
        sessionKey = key
        return { authoritative: true, live: false, backgroundOnly: false }
      }),
      onAuthoritativeSubscription: restore,
    })

    recovery.start('main')
    await vi.waitFor(() => expect(listDrafts).toHaveBeenCalledOnce())
    await Promise.resolve()
    recovery.retry('main')
    await vi.waitFor(() => expect(restore).toHaveBeenCalledOnce())

    expect(listDrafts).toHaveBeenCalledTimes(2)
  })

  it('does not repeat a successful empty discovery on reconnect', async () => {
    const listDrafts = vi.fn(async () => result())
    const recovery = createChatMetaDraftRecovery({
      currentSessionKey: () => 'agent:main:webchat:local-draft',
      listDrafts,
      isPristineDraft: () => true,
      rebindDraftSession: vi.fn(),
      onAuthoritativeSubscription: vi.fn(),
    })

    recovery.start('main')
    await vi.waitFor(() => expect(listDrafts).toHaveBeenCalledOnce())
    recovery.retry('main')
    await Promise.resolve()

    expect(listDrafts).toHaveBeenCalledOnce()
  })
})
