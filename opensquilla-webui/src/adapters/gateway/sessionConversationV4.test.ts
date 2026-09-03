import { describe, expect, it, vi } from 'vitest'
import { createV4SessionConversation } from './sessionConversationV4'

function makeAdapter() {
  const subscribe = vi.fn().mockReturnValue({ close: vi.fn() })
  const events = { subscribe, supports: vi.fn().mockReturnValue(true) }
  return { api: createV4SessionConversation(events), subscribe, events }
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
      'ready',
      'usage',
      'listCommands',
      'submitRouteFeedback',
      'promptCacheStatus',
      'setPromptCacheStatus',
      'submitClarify',
    ]) {
      expect(api).not.toHaveProperty(operation)
    }
  })

  it('keeps only the residual typed event seams', () => {
    const { api, subscribe } = makeAdapter()
    const listener = vi.fn()

    api.subscribeToolResults(listener)
    api.subscribeRoutingChanged(listener)

    expect(subscribe).toHaveBeenNthCalledWith(1, 'session.event.tool_result', expect.any(Function))
    expect(subscribe).toHaveBeenNthCalledWith(2, 'models.routing.changed', expect.any(Function))
  })
})
