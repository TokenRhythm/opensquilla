// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'
import { nextTick, ref, type Ref } from 'vue'

import { useChatHistory } from './useChatHistory'
import type { ChatMessage } from '@/types/chat'
import type { ChatHistoryResponse } from '@/types/rpc'

function makeHistory(autoScroll = true, overrides: {
  response?: ChatHistoryResponse
  messages?: ChatMessage[]
  preserveLiveTail?: boolean
  sessionKey?: Ref<string>
  threadRef?: Ref<HTMLElement | null>
} = {}) {
  const response: ChatHistoryResponse = overrides.response || {
    messages: [
      {
        id: 'm1',
        message_id: 'm1',
        role: 'assistant',
        text: 'hello',
        timestamp: '2026-07-06T00:00:00Z',
      },
    ],
    has_more: false,
    oldest_cursor: null,
    newest_cursor: null,
    history_scope: 'session',
  }
  const messages = ref<ChatMessage[]>(overrides.messages || [])
  const rpc = {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn().mockResolvedValue(response),
  }
  const scrollToBottom = vi.fn()
  const api = useChatHistory({
    rpc,
    sessionKey: overrides.sessionKey || ref('agent:main:webchat:test'),
    messages,
    threadRef: overrides.threadRef,
    lastHeaderRole: ref(''),
    lastHeaderDay: ref(''),
    preserveLiveTail: ref(overrides.preserveLiveTail ?? false),
    autoScroll: ref(autoScroll),
    stripTimePrefix: text => text,
    scrollToBottom,
  })
  return { api, rpc, scrollToBottom, messages }
}

function historyMessage(id: string): NonNullable<ChatHistoryResponse['messages']>[number] {
  return {
    id,
    message_id: id,
    role: 'assistant',
    text: id,
    timestamp: `2026-07-06T00:00:${id.replace(/\D/g, '').padStart(2, '0')}Z`,
  }
}

describe('useChatHistory canonical pagination', () => {
  it('requests canonical messages without compaction summaries', async () => {
    const { api, rpc } = makeHistory()

    await api.loadHistory()

    expect(rpc.call).toHaveBeenCalledWith('chat.history', expect.objectContaining({
      includeCanonical: true,
      includeSummaries: false,
    }))
  })

  it('restores the durable causal turn identity from canonical history', async () => {
    const { api, messages } = makeHistory(false, {
      response: {
        messages: [{
          id: 'assistant-1',
          message_id: 'assistant-1',
          role: 'assistant',
          text: 'partial answer',
          timestamp: '2026-07-06T00:00:00Z',
          turn_context: {
            turn_id: 'turn-1',
            intent: 'send',
          },
        }],
        has_more: false,
      },
    })

    await api.loadHistory()

    expect(messages.value[0]?.turnId).toBe('turn-1')
  })

  it('prepends one page per cursor and preserves the reader scroll anchor', async () => {
    const thread = document.createElement('div')
    let height = 400
    Object.defineProperties(thread, {
      scrollHeight: { configurable: true, get: () => height },
      scrollTop: { configurable: true, value: 120, writable: true },
    })
    const threadRef = ref<HTMLElement | null>(thread)
    const { api, rpc, messages } = makeHistory(false, {
      threadRef,
      response: {
        messages: [historyMessage('m3'), historyMessage('m4')],
        has_more: true,
        oldest_cursor: 'cursor-3',
        newest_cursor: 'cursor-4',
        canonical_complete: true,
      },
    })
    const anchor = document.createElement('article')
    anchor.dataset.messageId = 'm3'
    thread.append(anchor)
    document.body.append(thread)
    thread.getBoundingClientRect = () => ({ top: 0, bottom: 500 } as DOMRect)
    anchor.getBoundingClientRect = () => {
      const top = messages.value.length > 2 ? 300 : 100
      return { top, bottom: top + 60 } as DOMRect
    }
    rpc.call.mockImplementationOnce(async () => ({
      messages: [historyMessage('m3'), historyMessage('m4')],
      has_more: true,
      oldest_cursor: 'cursor-3',
      newest_cursor: 'cursor-4',
      canonical_complete: true,
    })).mockImplementationOnce(async () => {
      // Simulate unrelated live-tail growth while the page request is in
      // flight. The visible durable message still moves by exactly 200px.
      height = 900
      return {
        messages: [historyMessage('m1'), historyMessage('m2')],
        has_more: false,
        oldest_cursor: 'cursor-1',
        newest_cursor: 'cursor-2',
        canonical_complete: true,
      }
    })

    await api.loadHistory()
    await api.loadEarlierHistory()
    await nextTick()
    await api.loadEarlierHistory()

    expect(messages.value.map(message => message.messageId)).toEqual(['m1', 'm2', 'm3', 'm4'])
    expect(thread.scrollTop).toBe(320)
    expect(rpc.call).toHaveBeenCalledTimes(2)
    expect(api.historyState.value.canonicalComplete).toBe(true)
    expect(api.historyState.value.newestCursor).toBe('cursor-4')
    thread.remove()
  })

  it('queues a threshold crossing during latest-window refresh without consuming its cursor', async () => {
    let resolveRefresh!: (value: ChatHistoryResponse) => void
    const refresh = new Promise<ChatHistoryResponse>(resolve => { resolveRefresh = resolve })
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('m4')],
        has_more: true,
        oldest_cursor: 'cursor-4',
        newest_cursor: 'cursor-4',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m3')],
        has_more: true,
        oldest_cursor: 'cursor-3',
        newest_cursor: 'cursor-3',
        canonical_available: true,
      })
      .mockImplementationOnce(() => refresh)
      .mockResolvedValueOnce({
        messages: [historyMessage('m2')],
        has_more: false,
        oldest_cursor: 'cursor-2',
        newest_cursor: 'cursor-2',
        canonical_available: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    const refreshing = api.loadHistory()
    await vi.waitFor(() => expect(api.historyState.value.loading).toBe(true))

    api.loadEarlierHistory()
    resolveRefresh({
      messages: [historyMessage('m4'), historyMessage('m5')],
      has_more: true,
      oldest_cursor: 'cursor-4',
      newest_cursor: 'cursor-5',
      canonical_available: true,
    })
    await refreshing
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledTimes(4))

    expect(rpc.call).toHaveBeenNthCalledWith(4, 'chat.history', expect.objectContaining({
      before: 'cursor-3',
    }))
    await vi.waitFor(() => {
      expect(messages.value.map(message => message.messageId)).toEqual(['m2', 'm3', 'm4', 'm5'])
    })
  })

  it('does not apply an unavailable fallback page and retries the exact prepend boundary', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('m4')],
        has_more: true,
        oldest_cursor: 'cursor-4',
        newest_cursor: 'cursor-4',
        canonical_available: true,
        canonical_complete: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('fallback')],
        has_more: false,
        oldest_cursor: 'fallback-cursor',
        newest_cursor: 'fallback-cursor',
        canonical_available: false,
        canonical_complete: false,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m2'), historyMessage('m3')],
        has_more: false,
        oldest_cursor: 'cursor-2',
        newest_cursor: 'cursor-3',
        canonical_available: true,
        canonical_complete: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()

    expect(messages.value.map(message => message.messageId)).toEqual(['m4'])
    expect(api.historyState.value).toMatchObject({
      hasMore: true,
      oldestCursor: 'cursor-4',
      newestCursor: 'cursor-4',
      canonicalAvailable: false,
    })

    await api.retryHistory()

    expect(messages.value.map(message => message.messageId)).toEqual(['m2', 'm3', 'm4'])
    expect(rpc.call).toHaveBeenNthCalledWith(3, 'chat.history', expect.objectContaining({
      before: 'cursor-4',
    }))
  })

  it('keeps more than 200 loaded canonical messages during a latest-window refresh', async () => {
    const loaded = Array.from({ length: 250 }, (_, index): ChatMessage => ({
      role: 'assistant',
      text: `old ${index}`,
      ts: `old-${index}`,
      messageId: `m-${index}`,
      restoredFromHistory: true,
    }))
    const latest = Array.from({ length: 200 }, (_, index) => historyMessage(`m-${index + 50}`))
    const { api, messages } = makeHistory(false, {
      messages: loaded,
      response: {
        messages: latest,
        has_more: true,
        oldest_cursor: 'cursor-50',
        newest_cursor: 'cursor-249',
      },
    })

    await api.loadHistory()

    expect(messages.value).toHaveLength(250)
    expect(messages.value.slice(0, 51).map(message => message.messageId)).toEqual([
      ...Array.from({ length: 50 }, (_, index) => `m-${index}`),
      'm-50',
    ])
  })

  it('bridges forward without dropping loaded pages when a refresh has no message-id overlap', async () => {
    const initial = Array.from({ length: 50 }, (_, index) => historyMessage(`m-${index + 250}`))
    const earlier = Array.from({ length: 50 }, (_, index) => historyMessage(`m-${index + 200}`))
    const latest = Array.from({ length: 199 }, (_, index) => historyMessage(`m-${index + 500}`))
    latest.push({
      id: 'live-user-server',
      message_id: 'live-user-server',
      role: 'user',
      text: 'still running',
      timestamp: '2026-07-06T01:00:00Z',
    })
    const { api, rpc, messages } = makeHistory(false, { preserveLiveTail: true })
    rpc.call
      .mockResolvedValueOnce({
        messages: initial,
        has_more: true,
        oldest_cursor: 'cursor-250',
        newest_cursor: 'cursor-299',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: earlier,
        has_more: true,
        oldest_cursor: 'cursor-200',
        newest_cursor: 'cursor-249',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: latest,
        has_more: true,
        oldest_cursor: 'cursor-500',
        newest_cursor: 'cursor-live',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: Array.from({ length: 200 }, (_, index) => historyMessage(`m-${index + 300}`)),
        has_more: true,
        oldest_cursor: 'cursor-300',
        newest_cursor: 'cursor-499',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: latest,
        has_more: false,
        oldest_cursor: 'cursor-500',
        newest_cursor: 'cursor-live',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m-199')],
        has_more: true,
        oldest_cursor: 'cursor-199',
        newest_cursor: 'cursor-199',
        canonical_available: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    messages.value.push(
      {
        role: 'user',
        text: 'still running',
        ts: '2026-07-06T01:00:00Z',
        messageId: 'live-user-server',
      },
      {
        role: 'assistant',
        text: 'Stopped locally',
        ts: '2026-07-06T01:00:01Z',
        stopNotice: true,
      },
    )

    await api.loadHistory()

    expect(messages.value[0].messageId).toBe('m-200')
    expect(messages.value.some(message => message.messageId === 'm-300')).toBe(true)
    expect(messages.value.some(message => message.messageId === 'm-500')).toBe(true)
    expect(messages.value[messages.value.length - 1]?.stopNotice).toBe(true)
    expect(api.historyState.value).toMatchObject({
      hasMore: true,
      oldestCursor: 'cursor-200',
      newestCursor: 'cursor-live',
    })
    expect(rpc.call).toHaveBeenNthCalledWith(4, 'chat.history', expect.objectContaining({
      after: 'cursor-299',
      limit: 200,
    }))
    expect(rpc.call).toHaveBeenNthCalledWith(5, 'chat.history', expect.objectContaining({
      after: 'cursor-499',
      limit: 200,
    }))

    await api.loadEarlierHistory()
    expect(rpc.call).toHaveBeenNthCalledWith(6, 'chat.history', expect.objectContaining({
      before: 'cursor-200',
    }))
  })

  it('bounds each disconnected forward bridge and resumes from the saved cursor', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('m8')],
        has_more: true,
        oldest_cursor: 'cursor-8',
        newest_cursor: 'cursor-8',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m7')],
        has_more: true,
        oldest_cursor: 'cursor-7',
        newest_cursor: 'cursor-7',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m20')],
        has_more: true,
        oldest_cursor: 'cursor-20',
        newest_cursor: 'cursor-20',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m9'), historyMessage('m10')],
        has_more: true,
        oldest_cursor: 'cursor-9',
        newest_cursor: 'cursor-10',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m11'), historyMessage('m12')],
        has_more: true,
        oldest_cursor: 'cursor-11',
        newest_cursor: 'cursor-12',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m20')],
        has_more: true,
        oldest_cursor: 'cursor-20',
        newest_cursor: 'cursor-20',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: Array.from({ length: 8 }, (_, index) => historyMessage(`m${index + 13}`)),
        has_more: false,
        oldest_cursor: 'cursor-13',
        newest_cursor: 'cursor-20',
        canonical_available: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    await api.loadHistory()

    expect(rpc.call).toHaveBeenCalledTimes(5)
    expect(messages.value.map(message => message.messageId)).toEqual([
      'm7', 'm8', 'm9', 'm10', 'm11', 'm12',
    ])
    expect(api.historyState.value.newestCursor).toBe('cursor-12')

    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledTimes(7))
    expect(rpc.call).toHaveBeenNthCalledWith(7, 'chat.history', expect.objectContaining({
      after: 'cursor-12',
    }))
    expect(messages.value.map(message => message.messageId)).toEqual([
      'm7', 'm8', 'm9', 'm10', 'm11', 'm12', 'm13', 'm14', 'm15', 'm16',
      'm17', 'm18', 'm19', 'm20',
    ])
  })

  it('keeps expanded history untouched when a forward bridge is unavailable', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('m4')],
        has_more: true,
        oldest_cursor: 'cursor-4',
        newest_cursor: 'cursor-4',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m3')],
        has_more: true,
        oldest_cursor: 'cursor-3',
        newest_cursor: 'cursor-3',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m9')],
        has_more: true,
        oldest_cursor: 'cursor-9',
        newest_cursor: 'cursor-9',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('fallback')],
        has_more: false,
        oldest_cursor: 'fallback-cursor',
        newest_cursor: 'fallback-cursor',
        canonical_available: false,
        canonical_complete: false,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m9')],
        has_more: true,
        oldest_cursor: 'cursor-9',
        newest_cursor: 'cursor-9',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: Array.from({ length: 5 }, (_, index) => historyMessage(`m${index + 5}`)),
        has_more: false,
        oldest_cursor: 'cursor-5',
        newest_cursor: 'cursor-9',
        canonical_available: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    await api.loadHistory()

    expect(messages.value.map(message => message.messageId)).toEqual(['m3', 'm4'])
    expect(api.historyState.value).toMatchObject({
      hasMore: true,
      oldestCursor: 'cursor-3',
      newestCursor: 'cursor-4',
      canonicalAvailable: false,
    })

    await api.retryHistory()

    expect(messages.value.map(message => message.messageId)).toEqual([
      'm3',
      'm4',
      'm5',
      'm6',
      'm7',
      'm8',
      'm9',
    ])
    expect(rpc.call).toHaveBeenNthCalledWith(6, 'chat.history', expect.objectContaining({
      after: 'cursor-4',
    }))
  })

  it('stops a forward bridge when its cursor does not advance', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('m4')],
        has_more: true,
        oldest_cursor: 'cursor-4',
        newest_cursor: 'cursor-4',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m3')],
        has_more: true,
        oldest_cursor: 'cursor-3',
        newest_cursor: 'cursor-3',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m9')],
        has_more: true,
        oldest_cursor: 'cursor-9',
        newest_cursor: 'cursor-9',
        canonical_available: true,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('m5')],
        has_more: true,
        oldest_cursor: 'cursor-5',
        newest_cursor: 'cursor-4',
        canonical_available: true,
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    await api.loadHistory()

    expect(rpc.call).toHaveBeenCalledTimes(4)
    expect(messages.value.map(message => message.messageId)).toEqual(['m3', 'm4'])
    expect(api.historyState.value).toMatchObject({
      oldestCursor: 'cursor-3',
      newestCursor: 'cursor-4',
      loadEarlierError: true,
    })
  })

  it('allows the same cursor to be retried after a failed earlier-page request', async () => {
    const { api, rpc } = makeHistory(false, {
      response: {
        messages: [historyMessage('m2')],
        has_more: true,
        oldest_cursor: 'cursor-2',
      },
    })
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('m2')],
        has_more: true,
        oldest_cursor: 'cursor-2',
      })
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({
        messages: [historyMessage('m1')],
        has_more: false,
        oldest_cursor: 'cursor-1',
      })

    await api.loadHistory()
    await api.loadEarlierHistory()
    expect(api.historyState.value.loadEarlierError).toBe(true)

    await api.loadEarlierHistory()
    expect(api.historyState.value.loadEarlierError).toBe(false)
    expect(rpc.call).toHaveBeenCalledTimes(3)
  })

  it('surfaces and retries an initial history request failure', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({
        messages: [historyMessage('m1')],
        has_more: false,
        oldest_cursor: 'cursor-1',
        canonical_available: true,
      })

    await api.loadHistory()
    expect(api.historyState.value.loadEarlierError).toBe(true)

    await api.retryHistory()
    expect(api.historyState.value.loadEarlierError).toBe(false)
    expect(messages.value.map(message => message.messageId)).toEqual(['m1'])
  })

  it('retries the current canonical window when the canonical reader was unavailable', async () => {
    const { api, rpc, messages } = makeHistory(false)
    rpc.call
      .mockResolvedValueOnce({
        messages: [historyMessage('fallback')],
        has_more: false,
        oldest_cursor: null,
        canonical_available: false,
        canonical_complete: false,
      })
      .mockResolvedValueOnce({
        messages: [historyMessage('canonical')],
        has_more: false,
        oldest_cursor: null,
        canonical_available: true,
        canonical_complete: true,
      })

    await api.loadHistory()
    expect(api.historyState.value.canonicalAvailable).toBe(false)
    expect(api.historyState.value.loadingEarlier).toBe(false)
    expect(messages.value.map(message => message.messageId)).toEqual(['fallback'])

    await api.retryHistory()
    expect(api.historyState.value.canonicalAvailable).toBe(true)
    expect(rpc.call).toHaveBeenCalledTimes(2)
  })

  it('discards a stale response after switching sessions', async () => {
    const sessionKey = ref('agent:main:webchat:old')
    let resolveOld!: (value: ChatHistoryResponse) => void
    const oldResponse = new Promise<ChatHistoryResponse>(resolve => { resolveOld = resolve })
    const { api, rpc, messages } = makeHistory(false, {
      sessionKey,
      messages: [{
        role: 'assistant',
        text: 'old loaded row',
        ts: 'old',
        messageId: 'old-loaded',
        restoredFromHistory: true,
      }],
    })
    rpc.call
      .mockImplementationOnce(() => oldResponse)
      .mockResolvedValueOnce({
        messages: [historyMessage('new-message')],
        has_more: false,
        oldest_cursor: null,
      })

    const oldLoad = api.loadHistory()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledTimes(1))
    sessionKey.value = 'agent:main:webchat:new'
    const newLoad = api.loadHistory()
    await newLoad
    resolveOld({
      messages: [historyMessage('old-message')],
      has_more: false,
      oldest_cursor: null,
    })
    await oldLoad

    expect(messages.value.map(message => message.messageId)).toEqual(['new-message'])
    expect(api.historyState.value.loading).toBe(false)
  })
})

describe('useChatHistory scroll anchoring', () => {
  it('does not force the thread to the latest message when the reader has scrolled up', async () => {
    const { api, scrollToBottom } = makeHistory(false)

    await api.loadHistory()
    await nextTick()

    expect(scrollToBottom).not.toHaveBeenCalled()
  })

  it('keeps the initial pinned load behavior when the thread is still at the bottom', async () => {
    const { api, scrollToBottom } = makeHistory(true)

    await api.loadHistory()
    await nextTick()

    expect(scrollToBottom).toHaveBeenCalledTimes(1)
  })

  it('keeps protocol-shaped assistant documentation canonical', async () => {
    const text = [
      'Document `<tool_calls>` inline.',
      '```xml',
      '<tool_calls><invoke name="demo"></invoke></tool_calls>',
      '```',
      'Keep `<｜DSML｜tool_calls>` too.',
      '<details><summary>View areas around line 10</summary>Visible note.</details>',
      'Final suffix.',
    ].join('\n')
    const { api, messages } = makeHistory(true, {
      response: {
        messages: [{
          id: 'literal-1',
          message_id: 'literal-1',
          role: 'assistant',
          text,
          timestamp: '2026-07-06T00:00:00Z',
        }],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value[0]?.text).toBe(text)
  })
})

describe('useChatHistory optimistic local rows', () => {
  it('does not erase local user text when an immediate history sync is still empty', async () => {
    const localMessages: ChatMessage[] = [
      { role: 'user', text: '上下文相关SOTA论文', ts: '2026-07-07T10:00:00Z' },
    ]
    const { api, messages } = makeHistory(true, {
      messages: localMessages,
      response: {
        messages: [],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value).toEqual(localMessages)
  })

  it('keeps a local stopped-output notice when the settled server history only has the user turn', async () => {
    const { api, messages } = makeHistory(true, {
      messages: [
        { role: 'user', text: 'stop immediately', ts: '2026-07-07T10:00:00Z', messageId: 'user-1' },
        {
          role: 'assistant',
          text: 'Stopped after 1s',
          ts: '2026-07-07T10:00:01Z',
          messageId: 'client-stop-notice:task-1',
          stopNotice: true,
          interrupted: true,
        },
      ],
      response: {
        messages: [
          {
            id: 'user-1',
            message_id: 'user-1',
            role: 'user',
            text: 'stop immediately',
            timestamp: '2026-07-07T10:00:00Z',
          },
        ],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value.map(message => [message.role, message.text])).toEqual([
      ['user', 'stop immediately'],
      ['assistant', 'Stopped after 1s'],
    ])
    expect(messages.value[1]?.stopNotice).toBe(true)
  })

  it('keeps a terminal replay error until server history contains a durable error row', async () => {
    const { api, messages } = makeHistory(true, {
      messages: [
        { role: 'user', text: 'retry this turn', ts: 'local-user' },
        {
          role: 'error',
          text: 'Activation failed; retry this message.',
          ts: 'local-error',
          errorCode: 'failed',
          terminalNotice: true,
        },
      ],
      response: {
        messages: [
          {
            id: 'server-user',
            message_id: 'server-user',
            role: 'user',
            text: 'retry this turn',
            timestamp: 'server-user',
          },
        ],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value.map(message => [message.role, message.text])).toEqual([
      ['user', 'retry this turn'],
      ['error', 'Activation failed; retry this message.'],
    ])
    expect(messages.value[1]).toMatchObject({
      errorCode: 'failed',
      terminalNotice: true,
    })
  })

  it('keeps multiple local stopped-output notices when repeated user prompts reload with server ids', async () => {
    const prompt = '调研一下上下文相关的sota论文'
    const { api, messages } = makeHistory(true, {
      messages: [
        { role: 'user', text: prompt, ts: 'local-1' },
        {
          role: 'assistant',
          text: '输出被中断',
          ts: 'local-stop-1',
          messageId: 'client-stop-notice:task-1',
          stopNotice: true,
          interrupted: true,
        },
        { role: 'user', text: prompt, ts: 'local-2' },
        {
          role: 'assistant',
          text: '输出被中断',
          ts: 'local-stop-2',
          messageId: 'client-stop-notice:task-2',
          stopNotice: true,
          interrupted: true,
        },
        { role: 'user', text: prompt, ts: 'local-3' },
        {
          role: 'assistant',
          text: '输出被中断',
          ts: 'local-stop-3',
          messageId: 'client-stop-notice:task-3',
          stopNotice: true,
          interrupted: true,
        },
      ],
      response: {
        messages: [
          {
            id: 'server-user-1',
            message_id: 'server-user-1',
            role: 'user',
            text: prompt,
            timestamp: 'server-1',
          },
          {
            id: 'server-user-2',
            message_id: 'server-user-2',
            role: 'user',
            text: prompt,
            timestamp: 'server-2',
          },
          {
            id: 'server-user-3',
            message_id: 'server-user-3',
            role: 'user',
            text: prompt,
            timestamp: 'server-3',
          },
        ],
        has_more: false,
        oldest_cursor: null,
        newest_cursor: null,
        history_scope: 'session',
      },
    })

    await api.loadHistory()

    expect(messages.value.map(message => [message.role, message.text])).toEqual([
      ['user', prompt],
      ['assistant', '输出被中断'],
      ['user', prompt],
      ['assistant', '输出被中断'],
      ['user', prompt],
      ['assistant', '输出被中断'],
    ])
    expect(messages.value.filter(message => message.stopNotice)).toHaveLength(3)
  })
})
