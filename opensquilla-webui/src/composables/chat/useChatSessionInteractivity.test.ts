import { nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useChatSessionInteractivity } from './useChatSessionInteractivity'
import type { SessionDirectory, SessionItem } from '@/modules/sessionDirectory'

function session(key: string, overrides: Partial<SessionItem> = {}): SessionItem {
  return {
    key,
    title: key,
    subtitle: '',
    groupLabel: 'main',
    effectiveAgentId: 'main',
    sessionKind: 'chat',
    sessionKindAuthoritative: true,
    interactive: true,
    surface: 'webchat',
    conversationKind: 'direct',
    status: 'idle',
    runStatus: 'idle',
    runLabel: 'Idle',
    messageCount: 0,
    updatedAt: 1,
    model: '',
    parent: null,
    forkedFromParent: false,
    hasContractGaps: false,
    ...overrides,
  }
}

function directory(pages: Array<{ items: SessionItem[], hasMore: boolean, nextCursor: string | null }>) {
  let index = 0
  return {
    listPage: vi.fn(async () => pages[index++] || { items: [], hasMore: false, nextCursor: null }),
    count: vi.fn(async () => null),
    resolve: vi.fn(async ({ key }) => ({ key, id: key })),
    search: vi.fn(async () => ({ sessions: [], messages: [] })),
  } satisfies SessionDirectory
}

describe('useChatSessionInteractivity', () => {
  it('uses loaded navigation metadata for a noncanonical legacy Cron session', () => {
    const key = ref('legacy-scheduled-run')
    const knownSessions = ref<SessionItem[]>([
      session(key.value, { sessionKind: 'cron', interactive: null }),
    ])
    const source = directory([])
    const policy = useChatSessionInteractivity({ sessionKey: key, directory: source, knownSessions })

    expect(policy.isCronSession.value).toBe(true)
    expect(policy.turnActionsBlocked.value).toBe(true)
    expect(source.listPage).not.toHaveBeenCalled()
    policy.dispose()
  })

  it('keeps a direct route blocked until a later page proves the legacy Cron policy', async () => {
    const key = ref('legacy-scheduled-run')
    const source = directory([
      { items: [], hasMore: true, nextCursor: 'page-2' },
      {
        items: [session(key.value, { sessionKind: 'chat', interactive: false })],
        hasMore: false,
        nextCursor: null,
      },
    ])
    const policy = useChatSessionInteractivity({ sessionKey: key, directory: source })

    expect(policy.policyPending.value).toBe(true)
    expect(policy.turnActionsBlocked.value).toBe(true)
    await vi.waitFor(() => expect(policy.isNoninteractiveSession.value).toBe(true))
    expect(policy.policyPending.value).toBe(false)
    expect(policy.isNoninteractiveSession.value).toBe(true)
    expect(policy.isCronSession.value).toBe(false)
    expect(source.listPage).toHaveBeenCalledTimes(2)
    policy.dispose()
  })

  it('accepts an authoritative same-key policy refresh', async () => {
    const key = ref('legacy-scheduled-run')
    const knownSessions = ref<SessionItem[]>([session(key.value)])
    const source = directory([])
    const policy = useChatSessionInteractivity({ sessionKey: key, directory: source, knownSessions })
    expect(policy.turnActionsBlocked.value).toBe(false)

    knownSessions.value = [session(key.value, { interactive: false })]
    await nextTick()

    expect(policy.isNoninteractiveSession.value).toBe(true)
    expect(policy.turnActionsBlocked.value).toBe(true)
    policy.dispose()
  })

  it('keeps a direct route blocked when authoritative lookup fails', async () => {
    const key = ref('legacy-scheduled-run')
    const source = directory([])
    source.listPage.mockRejectedValueOnce(new Error('gateway unavailable'))
    const policy = useChatSessionInteractivity({ sessionKey: key, directory: source })

    expect(policy.policyPending.value).toBe(true)
    await vi.waitFor(() => expect(policy.policyUnavailable.value).toBe(true))
    expect(policy.policyPending.value).toBe(false)
    expect(policy.turnActionsBlocked.value).toBe(true)
    policy.dispose()
  })

  it('keeps a direct route blocked when the terminal page does not contain it', async () => {
    const key = ref('session-missing-from-terminal-page')
    const source = directory([{
      items: [session('another-session')],
      hasMore: false,
      nextCursor: null,
    }])
    const policy = useChatSessionInteractivity({ sessionKey: key, directory: source })

    await vi.waitFor(() => expect(policy.policyUnavailable.value).toBe(true))
    expect(policy.policyPending.value).toBe(false)
    expect(policy.turnActionsBlocked.value).toBe(true)
    policy.dispose()
  })

  it.each([
    {
      name: 'a missing cursor',
      pages: [{ items: [], hasMore: true, nextCursor: null }],
      calls: 1,
    },
    {
      name: 'a repeated cursor',
      pages: [
        { items: [], hasMore: true, nextCursor: 'page-2' },
        { items: [], hasMore: true, nextCursor: 'page-2' },
      ],
      calls: 2,
    },
    {
      name: 'a cursor loop',
      pages: [
        { items: [], hasMore: true, nextCursor: 'page-2' },
        { items: [], hasMore: true, nextCursor: 'page-3' },
        { items: [], hasMore: true, nextCursor: 'page-2' },
      ],
      calls: 3,
    },
  ])('fails closed when pagination ends with $name', async ({ pages, calls }) => {
    const key = ref('session-behind-invalid-pagination')
    const source = directory(pages)
    const policy = useChatSessionInteractivity({ sessionKey: key, directory: source })

    await vi.waitFor(() => expect(policy.policyUnavailable.value).toBe(true))
    expect(policy.policyPending.value).toBe(false)
    expect(policy.turnActionsBlocked.value).toBe(true)
    expect(source.listPage).toHaveBeenCalledTimes(calls)
    policy.dispose()
  })

  it('stays blocked after pagination reordering until a directory refresh supplies authority', async () => {
    const key = ref('session-reordered-into-an-earlier-page')
    const knownSessions = ref<SessionItem[]>([])
    const source = directory([
      { items: [session('first-page-peer')], hasMore: true, nextCursor: 'page-2' },
      { items: [session('second-page-peer')], hasMore: false, nextCursor: null },
    ])
    const policy = useChatSessionInteractivity({ sessionKey: key, directory: source, knownSessions })

    await vi.waitFor(() => expect(policy.policyUnavailable.value).toBe(true))
    expect(policy.turnActionsBlocked.value).toBe(true)

    knownSessions.value = [session(key.value)]
    await nextTick()

    expect(policy.policyUnavailable.value).toBe(false)
    expect(policy.turnActionsBlocked.value).toBe(false)
    policy.dispose()
  })

  it('does not treat a display-only inferred Cron kind as policy authority', () => {
    const key = ref('legacy-source-labelled-cron')
    const knownSessions = ref<SessionItem[]>([
      session(key.value, {
        sessionKind: 'cron',
        sessionKindAuthoritative: false,
        interactive: null,
      }),
    ])
    const source = directory([])
    const policy = useChatSessionInteractivity({ sessionKey: key, directory: source, knownSessions })

    expect(policy.isCronSession.value).toBe(false)
    expect(policy.turnActionsBlocked.value).toBe(false)
    policy.dispose()
  })

  it('releases an ordinary direct route and re-evaluates later navigation', async () => {
    const key = ref('agent:main:webchat:ordinary')
    const knownSessions = ref<SessionItem[]>([])
    const source = directory([{
      items: [session(key.value)],
      hasMore: false,
      nextCursor: null,
    }])
    const policy = useChatSessionInteractivity({ sessionKey: key, directory: source, knownSessions })

    await vi.waitFor(() => expect(policy.policyPending.value).toBe(false))
    expect(policy.turnActionsBlocked.value).toBe(false)

    key.value = 'legacy-scheduled-run'
    knownSessions.value = [session(key.value, { interactive: false, sessionKind: 'cron' })]
    await nextTick()
    expect(policy.isCronSession.value).toBe(true)
    expect(policy.turnActionsBlocked.value).toBe(true)
    policy.dispose()
  })

  it('keeps the exact lowercase Cron namespace as a synchronous fallback', () => {
    const key = ref('cron:job:run:one')
    const source = directory([])
    const policy = useChatSessionInteractivity({ sessionKey: key, directory: source })

    expect(policy.isCronSession.value).toBe(true)
    expect(policy.policyPending.value).toBe(false)
    expect(source.listPage).not.toHaveBeenCalled()
    policy.dispose()
  })

  it('does not delay a provisional new-chat key that has no route authority yet', () => {
    const key = ref('agent:main:webchat:new-draft')
    const resolveEnabled = ref(false)
    const source = directory([])
    const policy = useChatSessionInteractivity({
      sessionKey: key,
      directory: source,
      resolveEnabled,
    })

    expect(policy.turnActionsBlocked.value).toBe(false)
    expect(source.listPage).not.toHaveBeenCalled()
    policy.dispose()
  })

  it('resolves a recovered existing session when the provisional gate opens', async () => {
    const key = ref('legacy-scheduled-run')
    const resolveEnabled = ref(false)
    const source = directory([{
      items: [session(key.value, { sessionKind: 'cron', interactive: false })],
      hasMore: false,
      nextCursor: null,
    }])
    const policy = useChatSessionInteractivity({
      sessionKey: key,
      directory: source,
      resolveEnabled,
    })

    expect(source.listPage).not.toHaveBeenCalled()
    resolveEnabled.value = true

    await vi.waitFor(() => expect(policy.isCronSession.value).toBe(true))
    expect(source.listPage).toHaveBeenCalledOnce()
    expect(policy.turnActionsBlocked.value).toBe(true)
    policy.dispose()
  })

  it.each(['terminal miss', 'lookup error'] as const)(
    'keeps a recovered existing session fail-closed after a %s',
    async failure => {
      const key = ref('legacy-scheduled-run')
      const resolveEnabled = ref(false)
      const source = directory([{
        items: [session('another-session')],
        hasMore: false,
        nextCursor: null,
      }])
      if (failure === 'lookup error') {
        source.listPage.mockRejectedValueOnce(new Error('gateway unavailable'))
      }
      const policy = useChatSessionInteractivity({
        sessionKey: key,
        directory: source,
        resolveEnabled,
      })

      resolveEnabled.value = true

      await vi.waitFor(() => expect(policy.policyUnavailable.value).toBe(true))
      expect(policy.policyPending.value).toBe(false)
      expect(policy.turnActionsBlocked.value).toBe(true)
      policy.dispose()
    },
  )
})
