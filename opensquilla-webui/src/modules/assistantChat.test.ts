import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'
import { assistantChatBindingKey, closeAssistantChatOwner, createAssistantChatOwner, type AssistantChatOwnerOptions, type AssistantChatOwnerState } from './assistantChat'
import { assistantChatPendingOwner } from './assistantChatPendingOwner'
import { SessionLifecycleError, type SidechatSession } from './sessionLifecycle'
import { useChatDraftPersistence } from '@/composables/chat/useChatDraftPersistence'

let sequence = 0
const cleanup: (() => Promise<void>)[] = []
afterEach(async () => {
  for (const close of cleanup.splice(0)) await close()
  vi.useRealTimers()
})

function fixture() {
  const id = ++sequence
  let current = true
  let available = true
  const child: SidechatSession = {
    key: `child-${id}`, sessionId: `child-id-${id}`, parentKey: `parent-${id}`,
    inherited: { model: 'parent-model', provider: 'provider', reasoningLevel: 'high', modelRoutingMode: 'auto', modelRoutingRevision: 2, runMode: 'safe' },
  }
  const createSidechat = vi.fn(async () => child)
  const closeSidechat = vi.fn(async () => ({ key: child.key, closed: true as const }))
  const options: AssistantChatOwnerOptions = {
    parentKey: child.parentKey, ownerId: `owner-${id}`, identity: 'authority-a',
    isCurrent: () => current, ownerAvailable: () => available,
    lifecycle: { create: vi.fn(), fork: vi.fn(), rename: vi.fn(), remove: vi.fn(), createSidechat, closeSidechat },
    onChange: vi.fn<(state: AssistantChatOwnerState) => void>(),
  }
  const owner = createAssistantChatOwner(options)
  cleanup.push(async () => { owner.dispose(); closeSidechat.mockResolvedValue({ key: child.key, closed: true }); await closeAssistantChatOwner(options).catch(() => {}) })
  return { owner, options, child, createSidechat, closeSidechat,
    expire: () => { current = false }, setAvailable: (value: boolean) => { available = value } }
}

describe('side owner lifecycle around the shared chat runtime', () => {
  it('does not take a snapshot or create a child merely by opening/restoring an empty panel', () => {
    const f = fixture()
    expect(f.owner.restore()).toBeNull()
    expect(f.createSidechat).not.toHaveBeenCalled()
    expect(f.options.lifecycle.create).not.toHaveBeenCalled()
    expect(f.options.lifecycle.fork).not.toHaveBeenCalled()
    expect(f.owner.state).toEqual({ child: null, preparing: false, closing: false, error: '', requiresNewSession: false })
  })

  it('freezes inheritance at the first explicit preparation and retains the child identity', async () => {
    const f = fixture()
    f.child.inherited.model = 'model-at-first-send'
    expect(await f.owner.prepare()).toEqual(f.child)
    expect(f.createSidechat).toHaveBeenCalledExactlyOnceWith({ parentKey: f.options.parentKey, clientRequestId: f.options.ownerId })
    expect(await f.owner.prepare()).toBe(f.child)
    expect(f.createSidechat).toHaveBeenCalledTimes(1)
    expect(f.owner.state.child?.inherited.model).toBe('model-at-first-send')
  })

  it('coalesces preparation across overlapping adapters for the same owner', async () => {
    const f = fixture()
    let release!: (child: SidechatSession) => void
    f.createSidechat.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const first = f.owner.prepare()
    const next = createAssistantChatOwner(f.options)
    const second = next.prepare()
    expect(f.createSidechat).toHaveBeenCalledTimes(1)
    release(f.child)
    expect(await first).toBe(f.child)
    expect(await second).toBe(f.child)
    next.dispose()
  })

  it('retains child and owner-owned draft across adapter disposal/remount without closing it', async () => {
    const f = fixture()
    await f.owner.prepare()
    const memory = assistantChatPendingOwner(f.owner.binding)
    memory.draftStorage.setItem('draft', 'unfinished question')
    f.owner.dispose()
    const restored = createAssistantChatOwner(f.options)
    expect(restored.state.child).toBe(f.child)
    expect(assistantChatPendingOwner(restored.binding).draftStorage.getItem('draft')).toBe('unfinished question')
    expect(f.createSidechat).toHaveBeenCalledTimes(1)
    expect(f.closeSidechat).not.toHaveBeenCalled()
    expect(f.options.lifecycle.remove).not.toHaveBeenCalled()
    restored.dispose()
  })

  it('retains an unsent draft across reader remount and first child preparation, then clears the accepted composer', async () => {
    const f = fixture()
    const mountDraft = (binding: string) => {
      const scope = effectScope()
      const inputText = ref('')
      const localPaths = ref<string[]>([])
      const sessionKey = ref(binding)
      const storage = assistantChatPendingOwner(binding).draftStorage
      const api = scope.run(() => useChatDraftPersistence({ sessionKey, inputText, localPaths, storage }))!
      return { scope, inputText, localPaths, api }
    }
    const initial = mountDraft(f.owner.binding)
    initial.inputText.value = 'Explain this file'
    initial.localPaths.value = ['C:/project/readme.md']
    await nextTick()
    expect(f.createSidechat).not.toHaveBeenCalled()
    initial.scope.stop()
    f.owner.dispose()

    // Switching to a file tab retires only the reader, including before any
    // child session exists. Both adapters retain the same draft owner.
    const restored = createAssistantChatOwner(f.options)
    const draft = mountDraft(restored.binding)
    expect(restored.state.child).toBeNull()
    expect(draft.inputText.value).toBe('Explain this file')
    expect(draft.localPaths.value).toEqual(['C:/project/readme.md'])

    await restored.prepare()
    await nextTick()
    expect(restored.state.child?.key).toBe(f.child.key)
    expect(draft.inputText.value).toBe('Explain this file')
    expect(draft.api.loadDraft(restored.binding)).toBe('Explain this file')
    expect(draft.api.loadDraft(f.child.key)).toBe('')

    // The shared send path clears the accepted composer; persistence consumes
    // the same stable owner record rather than leaving a pre-child draft.
    draft.inputText.value = ''
    draft.localPaths.value = []
    await nextTick()
    expect(draft.api.loadDraft(restored.binding)).toBe('')
    draft.scope.stop()
    restored.dispose()
    const afterAccepted = mountDraft(restored.binding)
    expect(afterAccepted.inputText.value).toBe('')
    expect(afterAccepted.localPaths.value).toEqual([])
    afterAccepted.scope.stop()
  })

  it('does not expose an asynchronous child to a retired adapter but lets its owner restore it', async () => {
    const f = fixture()
    let release!: (child: SidechatSession) => void
    f.createSidechat.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const preparing = f.owner.prepare()
    f.owner.dispose()
    release(f.child)
    expect(await preparing).toBeNull()
    const next = createAssistantChatOwner(f.options)
    expect(next.restore()).toBe(f.child)
    expect(f.createSidechat).toHaveBeenCalledTimes(1)
    next.dispose()
  })

  it('does not return an asynchronously created child after its parent/authority scope changes', async () => {
    const f = fixture()
    f.createSidechat.mockImplementationOnce(async () => { f.expire(); return f.child })
    expect(await f.owner.prepare()).toBeNull()
    expect(f.owner.state.child).toBeNull()
  })

  it.each(['parent-key', 'other-parent', 'empty-key'])('rejects an invalid child identity (%s)', async shape => {
    const f = fixture()
    f.createSidechat.mockResolvedValue({ ...f.child,
      key: shape === 'parent-key' ? f.options.parentKey : shape === 'empty-key' ? '' : f.child.key,
      parentKey: shape === 'other-parent' ? 'different-parent' : f.options.parentKey,
    })
    expect(await f.owner.prepare()).toBeNull()
    expect(f.owner.state.requiresNewSession).toBe(true)
    expect(f.owner.state.child).toBeNull()
  })

  it('keeps a transient preparation failure retryable with the same idempotent owner request', async () => {
    const f = fixture()
    f.createSidechat.mockRejectedValueOnce(new Error('Gateway unavailable'))
    expect(await f.owner.prepare()).toBeNull()
    expect(f.owner.state).toMatchObject({ preparing: false, requiresNewSession: false, error: 'Gateway unavailable' })
    expect(await f.owner.prepare()).toBe(f.child)
    expect(f.createSidechat.mock.calls[0]).toEqual(f.createSidechat.mock.calls[1])
    expect(f.owner.state.error).toBe('')
  })

  it('permanently fences an explicitly closed empty owner without creating a child', async () => {
    const f = fixture()
    await closeAssistantChatOwner(f.options)
    expect(await f.owner.prepare()).toBeNull()
    const next = createAssistantChatOwner(f.options)
    expect(await next.prepare()).toBeNull()
    expect(f.createSidechat).not.toHaveBeenCalled()
    expect(f.closeSidechat).not.toHaveBeenCalled()
    next.dispose()
  })

  it('awaits authoritative close and clears ephemeral data only after success', async () => {
    const f = fixture()
    await f.owner.prepare()
    const memory = assistantChatPendingOwner(f.owner.binding)
    memory.draftStorage.setItem('draft', 'text')
    let release!: () => void
    f.closeSidechat.mockImplementationOnce(() => new Promise(resolve => { release = () => resolve({ key: f.child.key, closed: true }) }))
    const closing = closeAssistantChatOwner(f.options)
    expect(f.owner.state.closing).toBe(true)
    expect(f.options.onChange).toHaveBeenLastCalledWith(expect.objectContaining({ closing: true }))
    expect(await f.owner.prepare()).toBeNull()
    expect(memory.isClosed()).toBe(false)
    release(); await closing
    expect(f.owner.state.closing).toBe(true)
    expect(memory.isClosed()).toBe(true)
    expect(memory.draftStorage.getItem('draft')).toBeNull()
  })

  it('waits for in-flight creation before explicit close and never hands it to the pending sender', async () => {
    const f = fixture()
    let release!: (child: SidechatSession) => void
    f.createSidechat.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const preparing = f.owner.prepare()
    const closing = closeAssistantChatOwner(f.options)
    release(f.child)
    expect(await preparing).toBeNull()
    await closing
    expect(f.closeSidechat).toHaveBeenCalledExactlyOnceWith({ key: f.child.key })
  })

  it('retains a failed close for explicit retry rather than losing its child', async () => {
    const f = fixture()
    await f.owner.prepare()
    const memory = assistantChatPendingOwner(f.owner.binding)
    f.closeSidechat.mockRejectedValueOnce(new Error('close failed'))
    await expect(closeAssistantChatOwner(f.options)).rejects.toThrow('close failed')
    expect(f.owner.state.closing).toBe(false)
    expect(f.options.onChange).toHaveBeenLastCalledWith(expect.objectContaining({ closing: false }))
    expect(memory.isClosed()).toBe(false)
    expect(await f.owner.prepare()).toBe(f.child)
    await closeAssistantChatOwner(f.options)
    expect(f.closeSidechat).toHaveBeenCalledTimes(2)
  })

  it('rejects an incorrect close receipt without clearing the owner', async () => {
    const f = fixture()
    await f.owner.prepare()
    f.closeSidechat.mockResolvedValueOnce({ key: 'wrong-child', closed: true })
    await expect(closeAssistantChatOwner(f.options)).rejects.toThrow('Gateway did not close')
    expect(await f.owner.prepare()).toBe(f.child)
  })

  it('uses isolated namespaces for authorities, parents and tabs without exposing authority text', () => {
    const key = assistantChatBindingKey('private-authority', 'parent', 'tab-a')
    expect(key).not.toContain('private-authority')
    expect(key).not.toBe(assistantChatBindingKey('other', 'parent', 'tab-a'))
    expect(key).not.toBe(assistantChatBindingKey('private-authority', 'other', 'tab-a'))
    expect(key).not.toBe(assistantChatBindingKey('private-authority', 'parent', 'tab-b'))
  })

  it('renews a hidden owner independently of its chat reader until explicit close', async () => {
    vi.useFakeTimers()
    const f = fixture()
    await f.owner.prepare()
    f.owner.dispose()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(f.createSidechat).toHaveBeenCalledTimes(2)
    await closeAssistantChatOwner(f.options)
    await vi.advanceTimersByTimeAsync(120_000)
    expect(f.createSidechat).toHaveBeenCalledTimes(2)
  })

  it('preserves the original inheritance when an idempotent renewal returns newer parent settings', async () => {
    vi.useFakeTimers()
    const f = fixture()
    await f.owner.prepare()
    f.createSidechat.mockResolvedValue({ ...f.child, inherited: { ...f.child.inherited, model: 'new-parent-model' } })
    await vi.advanceTimersByTimeAsync(60_000)
    expect(f.owner.state.child?.inherited.model).toBe('parent-model')
  })

  it.each(['not-found', 'forbidden', 'invalid', 'unsupported'] as const)('retires ownership on terminal renewal rejection %s', async code => {
    vi.useFakeTimers()
    const f = fixture()
    await f.owner.prepare()
    f.createSidechat.mockRejectedValue(new SessionLifecycleError(code, 'owner rejected'))
    await vi.advanceTimersByTimeAsync(60_000)
    expect(f.owner.state).toMatchObject({ requiresNewSession: true, error: 'owner rejected' })
    expect(await f.owner.prepare()).toBeNull()
    await vi.advanceTimersByTimeAsync(120_000)
    expect(f.createSidechat).toHaveBeenCalledTimes(2)
    f.owner.dispose()
    const restored = createAssistantChatOwner(f.options)
    expect(restored.state.requiresNewSession).toBe(true)
    expect(await restored.prepare()).toBeNull()
    restored.dispose()
  })

  it('bounds transient renewal failures to the owner lease and clears memory on expiry', async () => {
    vi.useFakeTimers()
    const f = fixture()
    await f.owner.prepare()
    const memory = assistantChatPendingOwner(f.owner.binding)
    f.createSidechat.mockRejectedValue(new Error('temporary network failure'))
    await vi.advanceTimersByTimeAsync(10 * 60_000)
    expect(f.owner.state.requiresNewSession).toBe(true)
    expect(memory.isClosed()).toBe(true)
    const count = f.createSidechat.mock.calls.length
    await vi.advanceTimersByTimeAsync(120_000)
    expect(f.createSidechat).toHaveBeenCalledTimes(count)
  })

  it('does not renew against an unavailable authority and expires instead of changing owner', async () => {
    vi.useFakeTimers()
    const f = fixture()
    await f.owner.prepare()
    f.setAvailable(false)
    await vi.advanceTimersByTimeAsync(10 * 60_000)
    expect(f.createSidechat).toHaveBeenCalledTimes(1)
    expect(f.owner.state.requiresNewSession).toBe(true)
  })
})
