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
      session(key.value, { sessionKind: 'cron', interactive: false }),
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
        items: [session(key.value, { sessionKind: 'cron', interactive: false })],
        hasMore: false,
        nextCursor: null,
      },
    ])
    const policy = useChatSessionInteractivity({ sessionKey: key, directory: source })

    expect(policy.policyPending.value).toBe(true)
    expect(policy.turnActionsBlocked.value).toBe(true)
    await vi.waitFor(() => expect(policy.isCronSession.value).toBe(true))
    expect(policy.policyPending.value).toBe(false)
    expect(source.listPage).toHaveBeenCalledTimes(2)
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
    const source = directory([])
    const policy = useChatSessionInteractivity({
      sessionKey: key,
      directory: source,
      shouldResolve: () => false,
    })

    expect(policy.turnActionsBlocked.value).toBe(false)
    expect(source.listPage).not.toHaveBeenCalled()
    policy.dispose()
  })
})
