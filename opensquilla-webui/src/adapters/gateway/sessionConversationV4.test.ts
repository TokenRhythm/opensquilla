import { describe, expect, it, vi } from 'vitest'
import { createV4SessionConversation } from './sessionConversationV4'

function makeAdapter() {
  const request = vi.fn().mockResolvedValue({})
  const ready = vi.fn().mockResolvedValue(undefined)
  const subscribe = vi.fn().mockReturnValue({ close: vi.fn() })
  const rpc = { request, ready, supports: vi.fn().mockReturnValue(true) }
  const events = { subscribe, supports: vi.fn().mockReturnValue(true) }
  return { api: createV4SessionConversation(rpc, events), request, ready, subscribe, events }
}

describe('SessionConversation v4 adapter', () => {
  it('keeps wire method and aliases private while mapping history and stream reads', async () => {
    const { api, request } = makeAdapter()
    request
      .mockResolvedValueOnce({ subscribed: true, hydration_complete: true })
      .mockResolvedValueOnce({ messages: [], has_more: false })
      .mockResolvedValueOnce({
        key: 'agent:main:webchat:test',
        events: [{
          event: 'session.event.text_delta',
          payload: { session_key: 'agent:main:webchat:test', text: 'hello' },
        }],
      })

    await api.subscribe({ key: 'agent:main:webchat:test', since_stream_seq: 4 })
    await api.history({
      sessionKey: 'agent:main:webchat:test',
      limit: 50,
      includeCanonical: true,
      includeSummaries: false,
    })
    const snapshot = await api.snapshot('agent:main:webchat:test')

    expect(request.mock.calls.map(([method, params]) => [method, params])).toEqual([
      ['sessions.messages.subscribe', { key: 'agent:main:webchat:test', since_stream_seq: 4 }],
      ['chat.history', {
        sessionKey: 'agent:main:webchat:test',
        limit: 50,
        includeCanonical: true,
        includeSummaries: false,
      }],
      ['sessions.messages.snapshot', { key: 'agent:main:webchat:test' }],
    ])
    expect(snapshot.events?.[0]).toMatchObject({
      event: 'text-delta',
      payload: { session_key: 'agent:main:webchat:test', text: 'hello' },
    })
  })

  it('selects through-turn fork only when that capability is advertised', async () => {
    const { api, request } = makeAdapter()
    await api.fork({ key: 'parent', throughTurnId: 'turn-1' })
    expect(request).toHaveBeenCalledWith(
      'sessions.forkThroughTurn',
      { key: 'parent', throughTurnId: 'turn-1' },
    )

    const fallback = makeAdapter()
    fallback.api = createV4SessionConversation(
      { ...fallback.api, request: fallback.request, ready: fallback.ready, supports: vi.fn().mockReturnValue(false) },
      fallback.events,
    )
    await fallback.api.fork({ key: 'parent', throughTurnId: 'turn-1' })
    expect(fallback.request).toHaveBeenCalledWith(
      'sessions.fork',
      { key: 'parent', throughTurnId: 'turn-1' },
    )
  })

  it('maps semantic mutation inputs and connection/event seams', async () => {
    const { api, request, ready, subscribe } = makeAdapter()
    request.mockResolvedValue({ enabled: true, ttlSeconds: 300, state: 'scheduled' })
    const listener = vi.fn()

    await api.ready({ timeoutMs: 2_000, timeoutAction: 'reconnect' })
    await api.setPromptCacheStatus({
      key: 'session',
      enabled: true,
      ttlSeconds: 300,
      idleTimeoutSeconds: 3_600,
    })
    api.subscribeToolResults(listener)
    api.subscribeRoutingChanged(listener)

    expect(ready).toHaveBeenCalledWith({
      timeoutMs: 2_000,
      signal: undefined,
      timeoutAction: 'reject',
      abortAction: 'reject',
    })
    expect(request).toHaveBeenCalledWith('sessions.promptCacheKeepalive.set', {
      key: 'session',
      enabled: true,
      ttlSeconds: 300,
      idleTimeoutSeconds: 3_600,
    })
    expect(subscribe).toHaveBeenNthCalledWith(1, 'session.event.tool_result', expect.any(Function))
    expect(subscribe).toHaveBeenNthCalledWith(2, 'models.routing.changed', expect.any(Function))
  })
})
