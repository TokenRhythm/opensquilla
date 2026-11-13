import { afterEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'
import type { JevRouting, JevSnapshot } from '@/modules/jevRouting'
import { jevDecision, jevSnapshot, jevHandoffDecision } from '@/testing/jevRouting'
import { useChatJevRouting } from './useChatJevRouting'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
function harness(overrides: Partial<JevRouting> = {}, onOpenSession = vi.fn()) {
  let listener: (value: JevSnapshot) => void = () => {}
  const close = vi.fn()
  const routing = {
    available: () => true,
    get: vi.fn().mockResolvedValue(jevSnapshot()),
    configure: vi.fn().mockResolvedValue(jevSnapshot()),
    respond: vi.fn().mockResolvedValue(jevSnapshot()),
    subscribe: (next: typeof listener) => { listener = next; return { close } },
    dispose: vi.fn(),
    ...overrides,
  }
  const sessionKey = ref(jevSnapshot().sessionKey)
  const available = ref(true)
  const connectionEpoch = ref(1)
  const draftText = ref('')
  const composing = ref(false)
  const draftConsumed = ref(0)
  const api = useChatJevRouting({ routing, sessionKey, available, connectionEpoch, draftText, composing, draftConsumed, onOpenSession })
  return { api, routing, onOpenSession, sessionKey, available, connectionEpoch, draftText, composing, draftConsumed, event: (value: JevSnapshot) => listener(value), close }
}

afterEach(() => { vi.useRealTimers() })

describe('chat JEV routing lifecycle', () => {
  it('ignores late reads when a live decision has already arrived', async () => {
    const read = deferred<JevSnapshot>()
    const h = harness({ get: () => read.promise })
    h.event(jevSnapshot({ recommendation: jevDecision() }))
    read.resolve(jevSnapshot())
    await Promise.resolve()
    expect(h.api.snapshot.value?.recommendation?.id).toBe('decision-synthetic')
    h.api.dispose()
    expect(h.close).toHaveBeenCalledOnce()
  })

  it('ignores other sessions and clears the previous session on navigation', async () => {
    const h = harness()
    await Promise.resolve()
    h.event(jevSnapshot({ sessionKey: 'other' }))
    expect(h.api.snapshot.value?.sessionKey).toBe(h.sessionKey.value)
    h.sessionKey.value = 'other'
    await nextTick()
    await Promise.resolve()
    expect(h.api.snapshot.value).toBeNull()
    h.api.dispose()
  })

  it('rejects duplicate actions and does not apply a response after navigation', async () => {
    const response = deferred<JevSnapshot>()
    const respond = vi.fn(() => response.promise)
    const h = harness({ respond })
    await Promise.resolve()
    const pending = h.api.respond('decision-synthetic', true)
    expect(h.api.busy.value).toBe(true)
    expect(await h.api.respond('decision-synthetic', true)).toBe(false)
    expect(respond).toHaveBeenCalledOnce()
    h.sessionKey.value = 'other'
    await nextTick()
    response.resolve(jevSnapshot({ recommendation: jevDecision({ status: 'applied' }) }))
    expect(await pending).toBe(false)
    expect(h.api.snapshot.value).toBeNull()
    expect(h.api.busy.value).toBe(false)
    h.api.dispose()
  })

  it('leaves old Gateways usable without issuing optional requests', async () => {
    const get = vi.fn()
    const h = harness({ available: () => false, get })
    await Promise.resolve()
    expect(get).not.toHaveBeenCalled()
    expect(h.api.snapshot.value).toBeNull()
    expect(await h.api.configure({ enabled: true })).toBe(false)
    h.api.dispose()
  })

  it('does not invalidate a new session read when an older session response arrives', async () => {
    const response = deferred<JevSnapshot>()
    const nextRead = deferred<JevSnapshot>()
    const get = vi.fn().mockResolvedValueOnce(jevSnapshot()).mockReturnValueOnce(nextRead.promise)
    const h = harness({ get, respond: () => response.promise })
    await Promise.resolve()
    const pending = h.api.respond('decision-synthetic', false)
    h.sessionKey.value = 'other'
    await nextTick()
    response.resolve(jevSnapshot())
    expect(await pending).toBe(false)
    nextRead.resolve(jevSnapshot({ sessionKey: 'other' }))
    await Promise.resolve()
    expect(h.api.snapshot.value?.sessionKey).toBe('other')
    h.api.dispose()
  })

  it('recovers snapshots after reconnect and reports a mutation error locally', async () => {
    const h = harness({ configure: vi.fn().mockRejectedValue(new Error('unavailable')) })
    await Promise.resolve()
    expect(await h.api.configure({ mode: 'trust' })).toBe(false)
    expect(h.api.error.value).toBe(true)
    h.connectionEpoch.value += 1
    await nextTick()
    await Promise.resolve()
    expect(h.routing.get).toHaveBeenCalledTimes(2)
    expect(h.api.error.value).toBe(false)
    h.api.dispose()
  })
})

describe('draft JEV assessment', () => {
  it('debounces edits and sends only the latest nonempty draft asynchronously', async () => {
    vi.useFakeTimers()
    const preview = vi.fn().mockImplementation((sessionKey, draftId) => Promise.resolve(jevSnapshot({ sessionKey, draftId, status: 'checking' })))
    const h = harness({ preview, previewAvailable: () => true })
    await Promise.resolve()
    h.draftText.value = 'First version'
    await vi.advanceTimersByTimeAsync(400)
    h.draftText.value = 'Final version'
    await vi.advanceTimersByTimeAsync(649)
    expect(preview).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(preview).toHaveBeenCalledOnce()
    expect(preview.mock.calls[0]!.slice(0, 3)).toEqual([h.sessionKey.value, expect.stringMatching(/^jev-draft-/), 'Final version'])
    expect(h.api.busy.value).toBe(false)
    expect(h.api.snapshot.value?.status).toBe('checking')
    h.draftText.value = ''
    await vi.advanceTimersByTimeAsync(1000)
    expect(preview).toHaveBeenCalledTimes(2)
    expect(preview.mock.calls[1]![2]).toBe('')
    h.api.dispose()
  })

  it('waits for IME composition to finish', async () => {
    vi.useFakeTimers()
    const preview = vi.fn().mockResolvedValue(jevSnapshot())
    const h = harness({ preview })
    await Promise.resolve()
    h.composing.value = true
    h.draftText.value = 'Synthetic composition'
    await vi.advanceTimersByTimeAsync(1000)
    expect(preview).not.toHaveBeenCalled()
    h.composing.value = false
    await vi.advanceTimersByTimeAsync(650)
    expect(preview).toHaveBeenCalledOnce()
    h.api.dispose()
  })

  it('ignores stale preview results and stale input events after an edit', async () => {
    vi.useFakeTimers()
    const response = deferred<JevSnapshot>()
    const preview = vi.fn((..._args: Parameters<NonNullable<JevRouting['preview']>>) => response.promise)
    const h = harness({ preview })
    await Promise.resolve()
    h.draftText.value = 'Earlier draft'
    await vi.advanceTimersByTimeAsync(650)
    const oldDraftId = preview.mock.calls[0]![1] as string
    h.draftText.value = 'Newer draft'
    await nextTick()
    expect((preview.mock.calls[0]![3] as AbortSignal).aborted).toBe(true)
    const oldSnapshot = jevSnapshot({ draftId: oldDraftId, recommendation: jevDecision({ source: 'input' }) })
    h.event(oldSnapshot)
    response.resolve(oldSnapshot)
    await Promise.resolve()
    expect(h.api.snapshot.value?.recommendation).toBeNull()
    h.api.dispose()
  })

  it('accepts only a matching input assessment while typing but preserves loop events', async () => {
    vi.useFakeTimers()
    const preview = vi.fn().mockImplementation((sessionKey, draftId) => Promise.resolve(jevSnapshot({ sessionKey, draftId, status: 'checking' })))
    const h = harness({ preview })
    await Promise.resolve()
    h.draftText.value = 'Current draft'
    await vi.advanceTimersByTimeAsync(650)
    const draftId = preview.mock.calls[0]![1] as string
    h.event(jevSnapshot({ draftId, recommendation: jevDecision({ source: 'input' }) }))
    expect(h.api.snapshot.value?.recommendation?.source).toBe('input')
    h.draftText.value = 'Edited draft'
    await nextTick()
    expect(h.api.snapshot.value?.recommendation).toBeNull()
    h.event(jevSnapshot({ draftId, recommendation: jevDecision({ source: 'loop' }), blocked: true }))
    expect(h.api.snapshot.value?.recommendation?.source).toBe('loop')
    h.api.dispose()
  })

  it.each(['navigation', 'reconnect', 'disconnect', 'dispose'])('cancels an active draft request on %s', async event => {
    vi.useFakeTimers()
    const response = deferred<JevSnapshot>()
    const preview = vi.fn((..._args: Parameters<NonNullable<JevRouting['preview']>>) => response.promise)
    const h = harness({ preview })
    await Promise.resolve()
    h.draftText.value = 'Synthetic draft'
    await vi.advanceTimersByTimeAsync(650)
    const signal = preview.mock.calls[0]![3] as AbortSignal
    if (event === 'navigation') h.sessionKey.value = 'other'
    if (event === 'reconnect') h.connectionEpoch.value += 1
    if (event === 'disconnect') h.available.value = false
    if (event === 'dispose') h.api.dispose()
    await nextTick()
    expect(signal.aborted).toBe(true)
    response.resolve(jevSnapshot({ recommendation: jevDecision() }))
    await Promise.resolve()
    expect(h.api.snapshot.value?.recommendation).toBeFalsy()
    h.api.dispose()
  })

  it('leaves unsupported, disabled and oversized drafts to the normal send path', async () => {
    vi.useFakeTimers()
    const preview = vi.fn().mockRejectedValue(new Error('optional unavailable'))
    const h = harness({ preview, previewAvailable: () => false })
    await Promise.resolve()
    h.draftText.value = 'Synthetic draft'
    await vi.advanceTimersByTimeAsync(650)
    expect(preview).not.toHaveBeenCalled()
    h.api.dispose()
    const h2 = harness({ preview })
    await Promise.resolve()
    h2.draftText.value = 'x'.repeat(12001)
    await vi.advanceTimersByTimeAsync(650)
    expect(preview).not.toHaveBeenCalled()
    h2.draftText.value = 'Allowed draft'
    await vi.advanceTimersByTimeAsync(650)
    expect(preview).toHaveBeenCalledOnce()
    expect(h2.api.error.value).toBe(false)
    h2.api.dispose()
  })

  it('preserves the submitted assessment when the sender consumes the composer', async () => {
    vi.useFakeTimers()
    const preview = vi.fn().mockImplementation((sessionKey, draftId) => Promise.resolve(jevSnapshot({ sessionKey, draftId })))
    const h = harness({ preview })
    await Promise.resolve()
    h.draftText.value = 'Submitted draft'
    await vi.advanceTimersByTimeAsync(650)
    expect(preview).toHaveBeenCalledOnce()
    const draftId = preview.mock.calls[0]![1] as string
    h.draftConsumed.value += 1
    h.draftText.value = ''
    await vi.advanceTimersByTimeAsync(1000)
    expect(preview).toHaveBeenCalledOnce()
    h.event(jevSnapshot({ draftId, blocked: true, recommendation: jevDecision({ source: 'input' }) }))
    expect(h.api.snapshot.value?.blocked).toBe(true)
    expect(h.api.snapshot.value?.recommendation?.source).toBe('input')
    h.api.dispose()
    expect(preview).toHaveBeenCalledOnce()
  })

  it('invalidates an edited assessment immediately and waits for its acknowledgement before evaluating again', async () => {
    vi.useFakeTimers()
    const cancellation = deferred<JevSnapshot>()
    const preview = vi.fn().mockImplementation((sessionKey, draftId, text) => text
      ? Promise.resolve(jevSnapshot({ sessionKey, draftId })) : cancellation.promise)
    const h = harness({ preview })
    await Promise.resolve()
    h.draftText.value = 'Original draft'
    await vi.advanceTimersByTimeAsync(650)
    h.draftText.value = 'Edited draft'
    await nextTick()
    await Promise.resolve()
    expect(preview.mock.calls.map(call => call[2])).toEqual(['Original draft', ''])
    await vi.advanceTimersByTimeAsync(650)
    expect(preview).toHaveBeenCalledTimes(2)
    cancellation.resolve(jevSnapshot())
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
    expect(preview.mock.calls.map(call => call[2])).toEqual(['Original draft', '', 'Edited draft'])
    h.api.dispose()
  })

  it('keeps a paused submitted-input choice visible while the user types a followup', async () => {
    vi.useFakeTimers()
    const preview = vi.fn().mockResolvedValue(jevSnapshot())
    const h = harness({ preview })
    await Promise.resolve()
    h.draftText.value = 'A different followup draft'
    await nextTick()
    h.event(jevSnapshot({ draftId: 'submitted-input', blocked: true, recommendation: jevDecision({ source: 'input' }) }))
    expect(h.api.snapshot.value?.blocked).toBe(true)
    expect(h.api.snapshot.value?.recommendation?.id).toBe('decision-synthetic')
    h.draftText.value = 'An edited followup draft'
    await nextTick()
    expect(h.api.snapshot.value?.recommendation?.id).toBe('decision-synthetic')
    h.api.dispose()
  })
})

describe('confirmed conversation handoff ownership', () => {
  const applied = () => jevSnapshot({ recommendation: null, decisions: [jevHandoffDecision({ status: 'applied', handoff: {
    sourceSessionKey: jevSnapshot().sessionKey, contextTokens: 64000, contextWindowTokens: 80000,
    targetSessionKey: 'agent:main:webchat:continuation',
  } })] })

  it('opens an explicitly accepted destination once across an event and RPC response', async () => {
    const response = deferred<JevSnapshot>()
    const h = harness({ respond: () => response.promise })
    await Promise.resolve()
    h.event(jevSnapshot({ recommendation: jevHandoffDecision() }))
    const pending = h.api.respond('decision-synthetic', true)
    h.event(applied())
    response.resolve(applied())
    expect(await pending).toBe(true)
    expect(h.onOpenSession).toHaveBeenCalledExactlyOnceWith({
      decisionId: 'decision-synthetic', sourceSessionKey: jevSnapshot().sessionKey,
      targetSessionKey: 'agent:main:webchat:continuation',
    })
    h.api.dispose()
  })

  it('does not navigate for historical, declined, or unconfirmed applied decisions', async () => {
    const h = harness({ get: vi.fn().mockResolvedValue(applied()), respond: vi.fn().mockResolvedValue(applied()) })
    await Promise.resolve()
    h.event(applied())
    h.event(jevSnapshot({ recommendation: jevHandoffDecision() }))
    await h.api.respond('decision-synthetic', false)
    expect(h.onOpenSession).not.toHaveBeenCalled()
    h.api.dispose()
  })

  it('does not let a completed handoff navigate another source session', async () => {
    const response = deferred<JevSnapshot>()
    const h = harness({ respond: () => response.promise })
    await Promise.resolve()
    h.event(jevSnapshot({ recommendation: jevHandoffDecision() }))
    const pending = h.api.respond('decision-synthetic', true)
    h.sessionKey.value = 'agent:main:webchat:other'
    await nextTick()
    h.event(applied())
    response.resolve(applied())
    expect(await pending).toBe(false)
    expect(h.onOpenSession).not.toHaveBeenCalled()
    h.api.dispose()
  })

  it('keeps a context notice visible when an unsent draft changes', async () => {
    const h = harness()
    await Promise.resolve()
    h.event(jevSnapshot({ recommendation: jevHandoffDecision({ source: 'input' }) }))
    h.draftText.value = 'An unsent follow-up'
    await nextTick()
    expect(h.api.snapshot.value?.recommendation?.action).toBe('new_session')
    h.api.dispose()
  })
})
