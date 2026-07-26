import { beforeEach, describe, expect, it, vi } from 'vitest'

const rpc = vi.hoisted(() => ({
  waitForConnection: vi.fn(),
  call: vi.fn(),
}))

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => rpc,
}))

import { useSessionInspect } from './useSessionInspect'

function page(id: string, cursor: string, hasMore: boolean) {
  return {
    messages: [{ id, message_id: id, role: 'assistant', text: id }],
    has_more: hasMore,
    oldest_cursor: cursor,
    canonical_available: true,
    canonical_complete: true,
  }
}

beforeEach(() => {
  rpc.waitForConnection.mockReset().mockResolvedValue(undefined)
  rpc.call.mockReset().mockImplementation(async (method: string) => {
    if (method === 'sessions.preview') return { previews: [] }
    return page('m2', 'cursor-2', true)
  })
})

describe('useSessionInspect canonical pagination', () => {
  it('requests canonical transcript pages without summaries', async () => {
    const inspect = useSessionInspect()

    await inspect.load('agent:main:webchat:test')

    expect(rpc.call).toHaveBeenCalledWith('chat.history', {
      sessionKey: 'agent:main:webchat:test',
      limit: 20,
      includeCanonical: true,
      includeSummaries: false,
    })
    expect(inspect.canonicalComplete.value).toBe(true)
    expect(inspect.canonicalAvailable.value).toBe(true)
  })

  it('deduplicates prepended rows and allows retrying a failed cursor', async () => {
    let historyCall = 0
    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'sessions.preview') return { previews: [] }
      historyCall++
      if (historyCall === 1) return page('m2', 'cursor-2', true)
      if (historyCall === 2) throw new Error('offline')
      return {
        messages: [
          { id: 'm1', message_id: 'm1', role: 'assistant', text: 'm1' },
          { id: 'm2', message_id: 'm2', role: 'assistant', text: 'm2 duplicate' },
        ],
        has_more: false,
        oldest_cursor: 'cursor-1',
        canonical_complete: true,
      }
    })
    const inspect = useSessionInspect()

    await inspect.load('agent:main:webchat:test')
    await inspect.loadEarlier()
    expect(inspect.loadEarlierError.value).toBe(true)

    await inspect.loadEarlier()
    expect(inspect.loadEarlierError.value).toBe(false)
    expect(inspect.messages.value.map(message => message.message_id)).toEqual(['m1', 'm2'])
    expect(historyCall).toBe(3)
  })

  it('does not apply unavailable fallback rows and retries the same earlier page', async () => {
    let historyCall = 0
    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'sessions.preview') return { previews: [] }
      historyCall++
      if (historyCall === 1) return page('m4', 'cursor-4', true)
      if (historyCall === 2) {
        return {
          ...page('fallback', 'fallback-cursor', false),
          canonical_available: false,
          canonical_complete: false,
        }
      }
      return page('m3', 'cursor-3', false)
    })
    const inspect = useSessionInspect()

    await inspect.load('agent:main:webchat:test')
    await inspect.loadEarlier()

    expect(inspect.messages.value.map(message => message.message_id)).toEqual(['m4'])
    expect(inspect.oldestCursor.value).toBe('cursor-4')
    expect(inspect.hasEarlier.value).toBe(true)
    expect(inspect.canonicalAvailable.value).toBe(false)

    await inspect.retryHistory()

    expect(inspect.messages.value.map(message => message.message_id)).toEqual(['m3', 'm4'])
    expect(rpc.call).toHaveBeenLastCalledWith('chat.history', expect.objectContaining({
      before: 'cursor-4',
    }))
  })

  it('marks a legacy transcript incomplete only when the server says so', async () => {
    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'sessions.preview') return { previews: [] }
      return { ...page('m1', 'cursor-1', false), canonical_complete: false }
    })
    const inspect = useSessionInspect()

    await inspect.load('agent:main:webchat:legacy')

    expect(inspect.canonicalComplete.value).toBe(false)
    expect(inspect.hasEarlier.value).toBe(false)
  })

  it('keeps canonical read unavailability distinct from legacy incompleteness', async () => {
    let historyCall = 0
    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'sessions.preview') return { previews: [] }
      historyCall++
      return {
        ...page('m1', 'cursor-1', false),
        canonical_available: historyCall > 1,
        canonical_complete: historyCall > 1,
      }
    })
    const inspect = useSessionInspect()

    await inspect.load('agent:main:webchat:retry')

    expect(inspect.canonicalAvailable.value).toBe(false)
    expect(inspect.canonicalComplete.value).toBe(false)
    expect(inspect.messages.value.map(message => message.message_id)).toEqual(['m1'])

    await inspect.retryHistory()
    expect(historyCall).toBe(2)
    expect(inspect.canonicalAvailable.value).toBe(true)
  })

  it('invokes the prepend hook immediately before applying the returned page', async () => {
    let historyCall = 0
    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'sessions.preview') return { previews: [] }
      historyCall++
      return historyCall === 1
        ? page('m2', 'cursor-2', true)
        : page('m1', 'cursor-1', false)
    })
    const inspect = useSessionInspect()
    let visibleBeforeApply: string[] = []

    await inspect.load('agent:main:webchat:test')
    await inspect.loadEarlier(() => {
      visibleBeforeApply = inspect.messages.value.map(message => String(message.message_id))
    })

    expect(visibleBeforeApply).toEqual(['m2'])
    expect(inspect.messages.value.map(message => message.message_id)).toEqual(['m1', 'm2'])
  })

  it('clears a stale earlier-page loading state when switching sessions', async () => {
    let historyCall = 0
    let resolveOldEarlier!: (value: ReturnType<typeof page>) => void
    const oldEarlier = new Promise<ReturnType<typeof page>>(resolve => { resolveOldEarlier = resolve })
    rpc.call.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'sessions.preview') return { previews: [] }
      historyCall++
      if (historyCall === 1) return page('a2', 'cursor-a2', true)
      if (historyCall === 2) return oldEarlier
      if (params?.sessionKey === 'agent:main:webchat:b') return page('b2', 'cursor-b2', true)
      return page('a1', 'cursor-a1', false)
    })
    const inspect = useSessionInspect()

    await inspect.load('agent:main:webchat:a')
    const staleLoad = inspect.loadEarlier()
    await vi.waitFor(() => expect(inspect.loadingEarlier.value).toBe(true))

    await inspect.load('agent:main:webchat:b')
    expect(inspect.loadingEarlier.value).toBe(false)
    resolveOldEarlier(page('a1', 'cursor-a1', false))
    await staleLoad

    await inspect.loadEarlier()
    expect(rpc.call).toHaveBeenLastCalledWith('chat.history', expect.objectContaining({
      sessionKey: 'agent:main:webchat:b',
      before: 'cursor-b2',
    }))
  })
})
