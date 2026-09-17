import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useChatSessionRuntime, type ChatUsageAccumulator, type UseChatSessionRuntimeOptions } from './useChatSessionRuntime'
import { useChatAttachments } from './useChatAttachments'
import type { ChatMessage } from '@/types/chat'

const pushToast = vi.hoisted(() => vi.fn())

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast }),
}))

function emptyUsage(): ChatUsageAccumulator {
  return {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    cost: null,
    routedTurns: 0,
    sessionSaved: 0,
  }
}

describe('useChatSessionRuntime attachment ownership', () => {
  class DeferredFileReader {
    onload: ((event: { target: { result: string } }) => void) | null = null
    onerror: (() => void) | null = null

    readAsDataURL() {
      readers.push(this)
    }

    finish() {
      this.onload?.({ target: { result: 'data:image/jpeg;base64,/9j/' } })
    }
  }
  let readers: DeferredFileReader[]

  beforeEach(() => {
    readers = []
    pushToast.mockClear()
    vi.stubGlobal('FileReader', DeferredFileReader)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function createRuntime(overrides: Partial<UseChatSessionRuntimeOptions> = {}) {
    const attachments = useChatAttachments()
    const sessionKey = ref('agent:main:webchat:first')
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent: ref(null),
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref(emptyUsage()),
      usageModel: ref(''),
      createSessionKey: () => 'agent:main:webchat:new-draft',
      persistSession: key => { sessionKey.value = key },
      cancelSessionBootstrap: vi.fn(),
      startSessionBootstrap: () => ({
        generation: 1,
        criticalRequestsQueued: Promise.resolve(),
        history: Promise.resolve({ ok: true }),
        live: Promise.resolve({ authoritative: true, live: false, backgroundOnly: false }),
      }),
      loadCurrentSessionUsage: vi.fn(),
      applySessionRunState: vi.fn(),
      setCompactInFlight: vi.fn(),
      hideCompactStatus: vi.fn(),
      clearPendingQueue: vi.fn(),
      switchPendingQueue: vi.fn(),
      adoptPendingQueue: vi.fn(),
      resetSavingsPopupCooldown: vi.fn(),
      restoreWidgetState: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
      retireAttachments: attachments.retireAttachments,
      ...overrides,
    })
    const addImage = (name: string) => attachments.addAttachment(
      new File([new Uint8Array([0xff, 0xd8, 0xff])], name, { type: 'image/jpeg' }),
    )
    return { runtime, attachments, sessionKey, addImage }
  }

  it.each(['navigate', 'new task', 'slash reset'])(
    'retires unsent files and late reads on %s',
    async (transition) => {
      const { runtime, attachments, addImage } = createRuntime()
      await addImage('ready.jpg')
      readers[0].finish()
      await addImage('reading.jpg')

      if (transition === 'navigate') await runtime.switchToSession('agent:main:webchat:second')
      else if (transition === 'new task') await runtime.startDraftSession()
      else runtime.resetCurrentSessionAfterSlash()

      expect(attachments.pendingAttachments.value).toEqual([])
      expect(attachments.hasPendingAttachmentWork()).toBe(false)
      await addImage('current.jpg')
      readers[1].finish()
      readers[1].onerror?.()
      readers[2].finish()

      expect(attachments.pendingAttachments.value).toMatchObject([
        { kind: 'inline', name: 'current.jpg' },
      ])
      expect(pushToast).not.toHaveBeenCalled()
    },
  )

  it.each(['response handoff', 'draft rebind'])(
    'preserves the composer and in-flight reads during %s',
    async (transition) => {
      const { runtime, attachments, addImage } = createRuntime()
      await addImage('ready.jpg')
      readers[0].finish()
      await addImage('reading.jpg')

      if (transition === 'response handoff') {
        await runtime.adoptResponseSession('agent:main:webchat:accepted', 'request-1')
      } else {
        await runtime.rebindDraftSession('agent:main:webchat:server-draft', () => true)
      }
      readers[1].finish()

      expect(attachments.pendingAttachments.value).toMatchObject([
        { kind: 'inline', name: 'ready.jpg' },
        { kind: 'inline', name: 'reading.jpg' },
      ])
      expect(pushToast).not.toHaveBeenCalled()
    },
  )

  it('retires attachments only when delayed navigation commits', async () => {
    let finishQueue!: () => void
    const queue = new Promise<void>(resolve => { finishQueue = resolve })
    const { runtime, attachments, addImage } = createRuntime({
      switchPendingQueue: () => queue,
    })
    await addImage('source.jpg')

    const switching = runtime.switchToSession('agent:main:webchat:second')
    readers[0].finish()
    expect(attachments.pendingAttachments.value).toMatchObject([{ kind: 'inline', name: 'source.jpg' }])

    finishQueue()
    await switching
    expect(attachments.pendingAttachments.value).toEqual([])
  })

  it.each(['failed', 'superseded', 'unchanged'])(
    'preserves attachments after %s navigation',
    async (outcome) => {
      let finishQueue!: () => void
      const queue = new Promise<void>(resolve => { finishQueue = resolve })
      const { runtime, attachments, sessionKey, addImage } = createRuntime({
        switchPendingQueue: () => outcome === 'failed'
          ? Promise.reject(new Error('queue unavailable'))
          : queue,
      })
      await addImage('source.jpg')

      if (outcome === 'unchanged') {
        await runtime.switchToSession(sessionKey.value)
      } else {
        const switching = runtime.switchToSession('agent:main:webchat:second')
        if (outcome === 'failed') {
          await expect(switching).rejects.toThrow('queue unavailable')
        } else {
          await runtime.switchToSession(sessionKey.value)
          finishQueue()
          await switching
        }
      }
      readers[0].finish()

      expect(sessionKey.value).toBe('agent:main:webchat:first')
      expect(attachments.pendingAttachments.value).toMatchObject([{ kind: 'inline', name: 'source.jpg' }])
    },
  )
})

describe('useChatSessionRuntime Meta draft recovery', () => {
  it('rebinds an untouched provisional draft without persisting it', async () => {
    const sessionKey = ref('agent:main:webchat:local-draft')
    const pendingSessionIntent = ref<string | null>('new_chat')
    const switchPendingQueue = vi.fn()
    const persistSession = vi.fn((key: string) => { sessionKey.value = key })
    const cancelSessionBootstrap = vi.fn()
    const liveOutcome = {
      authoritative: true,
      live: false,
      backgroundOnly: false,
    }
    const startSessionBootstrap = vi.fn(() => ({
      generation: 2,
      criticalRequestsQueued: Promise.resolve(),
      history: Promise.resolve({ ok: true }),
      live: Promise.resolve(liveOutcome),
    }))
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent,
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref(emptyUsage()),
      usageModel: ref(''),
      createSessionKey: () => 'agent:main:webchat:draft',
      persistSession,
      cancelSessionBootstrap,
      startSessionBootstrap,
      loadCurrentSessionUsage: vi.fn(),
      applySessionRunState: vi.fn(),
      setCompactInFlight: vi.fn(),
      hideCompactStatus: vi.fn(),
      clearPendingQueue: vi.fn(),
      switchPendingQueue,
      adoptPendingQueue: vi.fn(),
      resetSavingsPopupCooldown: vi.fn(),
      restoreWidgetState: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
    })

    await expect(runtime.rebindDraftSession(
      'agent:main:webchat:server-draft',
      () => true,
    )).resolves.toEqual(liveOutcome)

    expect(cancelSessionBootstrap).toHaveBeenCalledOnce()
    expect(sessionKey.value).toBe('agent:main:webchat:server-draft')
    expect(pendingSessionIntent.value).toBe('new_chat')
    expect(switchPendingQueue).toHaveBeenCalledWith(
      'agent:main:webchat:server-draft',
      expect.any(Function),
      expect.anything(),
    )
    expect(startSessionBootstrap).toHaveBeenCalledWith({ includeHistory: false })
    expect(persistSession).not.toHaveBeenCalled()
  })

  it('does not rebind after the draft ownership guard fails', async () => {
    const sessionKey = ref('agent:main:webchat:local-draft')
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent: ref('new_chat'),
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref(emptyUsage()),
      usageModel: ref(''),
      createSessionKey: () => 'agent:main:webchat:draft',
      persistSession: vi.fn(),
      cancelSessionBootstrap: vi.fn(),
      startSessionBootstrap: vi.fn(),
      loadCurrentSessionUsage: vi.fn(),
      applySessionRunState: vi.fn(),
      setCompactInFlight: vi.fn(),
      hideCompactStatus: vi.fn(),
      clearPendingQueue: vi.fn(),
      switchPendingQueue: vi.fn(),
      adoptPendingQueue: vi.fn(),
      resetSavingsPopupCooldown: vi.fn(),
      restoreWidgetState: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
    })

    await expect(runtime.rebindDraftSession(
      'agent:main:webchat:server-draft',
      () => false,
    )).resolves.toBe(false)
    expect(sessionKey.value).toBe('agent:main:webchat:local-draft')
  })

  it('keeps the source bootstrap until a delayed queue switch can commit', async () => {
    const sessionKey = ref('agent:main:webchat:a')
    let finishQueue!: () => void
    const queue = new Promise<void>(resolve => { finishQueue = resolve })
    const cancelSessionBootstrap = vi.fn()
    const persistSession = vi.fn((key: string) => { sessionKey.value = key })
    const setSessionHandoffTarget = vi.fn()
    const beginSessionResolution = vi.fn()
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent: ref(null),
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref(emptyUsage()),
      usageModel: ref(''),
      createSessionKey: () => '',
      persistSession,
      beginSessionResolution,
      cancelSessionBootstrap,
      setSessionHandoffTarget,
      startSessionBootstrap: vi.fn(() => ({
        generation: 1,
        criticalRequestsQueued: Promise.resolve(),
        history: Promise.resolve({ ok: true }),
        live: Promise.resolve({
          authoritative: true,
          live: false,
          backgroundOnly: false,
        }),
      })),
      loadCurrentSessionUsage: vi.fn(),
      applySessionRunState: vi.fn(),
      setCompactInFlight: vi.fn(),
      hideCompactStatus: vi.fn(),
      clearPendingQueue: vi.fn(),
      switchPendingQueue: vi.fn(() => queue),
      adoptPendingQueue: vi.fn(),
      resetSavingsPopupCooldown: vi.fn(),
      restoreWidgetState: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
    })

    const switching = runtime.switchToSession('agent:main:webchat:b')
    expect(sessionKey.value).toBe('agent:main:webchat:a')
    expect(cancelSessionBootstrap).not.toHaveBeenCalled()
    expect(beginSessionResolution).not.toHaveBeenCalled()

    finishQueue()
    await switching

    expect(cancelSessionBootstrap).toHaveBeenCalledOnce()
    expect(beginSessionResolution).toHaveBeenCalledOnce()
    expect(beginSessionResolution).toHaveBeenCalledWith('agent:main:webchat:b')
    expect(persistSession).toHaveBeenCalledWith(
      'agent:main:webchat:b',
      { source: 'runtime.switchToSession' },
    )
    expect(setSessionHandoffTarget).toHaveBeenNthCalledWith(
      1,
      'agent:main:webchat:b',
      1,
    )
    expect(setSessionHandoffTarget).toHaveBeenLastCalledWith(null, 1, 'committed')
  })

  it('supersedes delayed A to B when navigation returns to A', async () => {
    const sessionKey = ref('agent:main:webchat:a')
    let finishQueue!: () => void
    const queue = new Promise<void>(resolve => { finishQueue = resolve })
    const cancelSessionBootstrap = vi.fn()
    const persistSession = vi.fn((key: string) => { sessionKey.value = key })
    const beginSessionResolution = vi.fn()
    const switchPendingQueue = vi.fn((
      _key: string,
      shouldCommit?: () => boolean,
      _handoffSignal?: AbortSignal,
    ) => queue.then(() => { shouldCommit?.() }))
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent: ref(null),
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref(emptyUsage()),
      usageModel: ref(''),
      createSessionKey: () => '',
      persistSession,
      beginSessionResolution,
      cancelSessionBootstrap,
      startSessionBootstrap: vi.fn(),
      loadCurrentSessionUsage: vi.fn(),
      applySessionRunState: vi.fn(),
      setCompactInFlight: vi.fn(),
      hideCompactStatus: vi.fn(),
      clearPendingQueue: vi.fn(),
      switchPendingQueue,
      adoptPendingQueue: vi.fn(),
      resetSavingsPopupCooldown: vi.fn(),
      restoreWidgetState: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
    })

    const toB = runtime.switchToSession('agent:main:webchat:b')
    const supersededSignal = switchPendingQueue.mock.calls[0]?.[2]
    expect(supersededSignal?.aborted).toBe(false)
    await runtime.switchToSession('agent:main:webchat:a')
    expect(supersededSignal?.aborted).toBe(true)
    finishQueue()
    await toB

    expect(sessionKey.value).toBe('agent:main:webchat:a')
    expect(cancelSessionBootstrap).not.toHaveBeenCalled()
    expect(beginSessionResolution).not.toHaveBeenCalled()
    expect(persistSession).not.toHaveBeenCalled()
    const commitGuard = switchPendingQueue.mock.calls[0]?.[1]
    expect(commitGuard?.()).toBe(false)
  })

  it('leaves the source bootstrap active when queue adoption fails', async () => {
    const sessionKey = ref('agent:main:webchat:a')
    const cancelSessionBootstrap = vi.fn()
    const beginSessionResolution = vi.fn()
    const failure = new Error('queue adoption failed')
    const runtime = useChatSessionRuntime({
      sessionKey,
      messages: ref<ChatMessage[]>([]),
      pendingSessionIntent: ref(null),
      routerDecisionPending: ref(null),
      currentEpoch: ref(0),
      lastStreamSeq: ref(0),
      activeTaskGroups: ref(new Set<string>()),
      aborted: ref(false),
      lastHeaderRole: ref(''),
      lastHeaderDay: ref(''),
      usageAccum: ref(emptyUsage()),
      usageModel: ref(''),
      createSessionKey: () => '',
      persistSession: vi.fn(),
      beginSessionResolution,
      cancelSessionBootstrap,
      startSessionBootstrap: vi.fn(),
      loadCurrentSessionUsage: vi.fn(),
      applySessionRunState: vi.fn(),
      setCompactInFlight: vi.fn(),
      hideCompactStatus: vi.fn(),
      clearPendingQueue: vi.fn(),
      switchPendingQueue: vi.fn(async () => { throw failure }),
      adoptPendingQueue: vi.fn(),
      resetSavingsPopupCooldown: vi.fn(),
      restoreWidgetState: vi.fn(),
      resetStreamLiveTurnState: vi.fn(),
    })

    await expect(runtime.switchToSession('agent:main:webchat:b')).rejects.toBe(failure)
    expect(sessionKey.value).toBe('agent:main:webchat:a')
    expect(cancelSessionBootstrap).not.toHaveBeenCalled()
    expect(beginSessionResolution).not.toHaveBeenCalled()
  })
})
