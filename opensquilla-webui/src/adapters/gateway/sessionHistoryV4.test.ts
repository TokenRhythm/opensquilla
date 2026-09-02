import { describe, expect, it, vi } from 'vitest'
import { CHAT_HISTORY_METHOD, type ChatHistoryResult } from '@/contracts/generated/v4/chatHistory'
import type { RpcCallOptions } from '@/lib/rpc'
import { SessionReadSessionMissingError } from '@/modules/sessionReadLifecycle'
import {
  requestV4SessionHistory,
  type SessionHistoryV4Transport,
} from './sessionHistoryV4'

describe('v4 SessionHistory Adapter', () => {
  it.each([
    ['NOT_FOUND', { code: 'NOT_FOUND' }],
    ['SESSION_NOT_FOUND', { code: 'SESSION_NOT_FOUND' }],
    ['lowercase data.code', { data: { code: 'session_not_found' } }],
  ])(
    'maps %s into the session-missing domain failure',
    async (_label, shape) => {
      const cause = Object.assign(new Error('history missing'), shape)
      const transport: SessionHistoryV4Transport = {
        request: vi.fn(async () => { throw cause }),
      }

      const request = requestV4SessionHistory(
        transport,
        'session-missing',
        {
          direction: 'latest',
          limit: 100,
          signal: new AbortController().signal,
        },
        {
          includeSummaries: true,
          policy: { concurrentHistoryReads: () => true },
          contractError: message => new Error(message),
        },
      )

      await expect(request).rejects.toMatchObject({
        name: 'SessionReadSessionMissingError',
        code: 'session-missing',
        cause,
      } satisfies Partial<SessionReadSessionMissingError>)
    },
  )

  it('preserves and deeply freezes opaque history payload keys', async () => {
    const result: ChatHistoryResult = {
      messages: [{
        id: 'message-1',
        role: 'assistant',
        text: 'done',
        tool_calls: [{
          tool_use_id: 'tool-1',
          execution_status: { status: 'success', result_code: 'ok' },
          is_error: false,
          sources: [{ source_url: 'https://example.test/result' }],
        }],
        turn_context: {
          turn_id: 'turn-1',
          future_context: { inner_snake: true },
        },
        additive_message: { nested_snake: true },
      }],
      has_more: false,
      oldest_cursor: null,
      newest_cursor: null,
      history_scope: 'complete',
      loaded_count: 1,
      page_size: 100,
      canonical_available: true,
      canonical_complete: true,
      compaction_summaries: [],
      turn_outcomes: [],
    }
    const requestSpy = vi.fn()
    const transport: SessionHistoryV4Transport = {
      async request<T = unknown>(
        method: string,
        params?: Record<string, unknown>,
        options?: RpcCallOptions,
      ): Promise<T> {
        requestSpy(method, params, options)
        return result as T
      },
    }

    const page = await requestV4SessionHistory(
      transport,
      'session-1',
      {
        direction: 'latest',
        limit: 100,
        signal: new AbortController().signal,
      },
      {
        includeSummaries: true,
        policy: { concurrentHistoryReads: () => true },
        contractError: message => new Error(message),
      },
    )

    expect(requestSpy).toHaveBeenCalledWith(
      CHAT_HISTORY_METHOD,
      expect.objectContaining({ sessionKey: 'session-1', includeCanonical: true }),
      expect.objectContaining({ timeoutAction: 'reject' }),
    )
    const message = page.messages[0]
    const toolCall = message?.toolCalls[0] as Record<string, unknown>
    expect(toolCall).toMatchObject({
      tool_use_id: 'tool-1',
      execution_status: { status: 'success', result_code: 'ok' },
      is_error: false,
      sources: [{ source_url: 'https://example.test/result' }],
    })
    expect(toolCall).not.toHaveProperty('toolUseId')
    expect(toolCall).not.toHaveProperty('executionStatus')
    expect(toolCall).not.toHaveProperty('isError')
    expect(message?.turnContext?.additional).toEqual({
      future_context: { inner_snake: true },
    })
    expect(message?.additional).toEqual({
      additive_message: { nested_snake: true },
    })

    expect(Object.isFrozen(toolCall)).toBe(true)
    expect(Object.isFrozen(toolCall.execution_status)).toBe(true)
    expect(Object.isFrozen(toolCall.sources)).toBe(true)
    expect(Object.isFrozen((toolCall.sources as readonly unknown[])[0])).toBe(true)
    expect(Object.isFrozen(message?.turnContext?.additional)).toBe(true)
    expect(Object.isFrozen(message?.turnContext?.additional.future_context)).toBe(true)
    expect(Object.isFrozen(message?.additional.additive_message)).toBe(true)
  })
})
