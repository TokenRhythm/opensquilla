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
  it('does not retain the migrated session-read and inspection operations', () => {
    const { api } = makeAdapter()

    for (const operation of [
      'fork',
      'subscribe',
      'hydrate',
      'snapshot',
      'unsubscribe',
      'history',
      'preview',
      'abort',
    ]) {
      expect(api).not.toHaveProperty(operation)
    }
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
