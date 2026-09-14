import { describe, expect, it, vi } from 'vitest'
import type { RpcCallOptions } from '@/lib/rpc'
import {
  CHAT_HISTORY_METHOD,
  type ChatHistoryResult,
} from '@/contracts/generated/v4/chatHistory'
import {
  SESSIONS_PREVIEW_METHOD,
  type SessionsPreviewResult,
} from '@/contracts/generated/v4/sessionsPreview'
import { SessionInspectionContractError, SessionInspectionLogNotReadyError } from '@/modules/sessionInspection'
import { createV4SessionInspection } from './sessionInspectionV4'

interface Call {
  readonly method: string
  readonly params?: Record<string, unknown>
  readonly options?: RpcCallOptions
}

function historyResult(patch: Partial<ChatHistoryResult> = {}): ChatHistoryResult {
  return {
    messages: [{
      id: 1,
      message_id: 'message-1',
      role: 'assistant',
      text: 'hello',
      reasoning_content: '  exact reasoning  ',
      input: 3,
      output: 5,
      turn_context: {
        turn_id: 'turn-1',
        promoted_turn_id: 'turn-0',
        applied_iteration: 2,
        activity_markers: [{ marker_id: 'marker-1' }],
      },
    }],
    has_more: true,
    oldest_cursor: 'oldest-1',
    newest_cursor: 'newest-1',
    history_scope: 'complete',
    loaded_count: 1,
    page_size: 20,
    canonical_available: true,
    canonical_complete: true,
    compaction_summaries: [],
    turn_outcomes: [],
    ...patch,
  }
}

function makeHarness() {
  const calls: Call[] = []
  const results = new Map<string, unknown>([
    [SESSIONS_PREVIEW_METHOD, {
      ts: 10,
      previews: [{
        key: 'alpha',
        title: 'Alpha',
        lastMessage: 'hello',
        updatedAt: 9,
      }],
    } satisfies SessionsPreviewResult],
    [CHAT_HISTORY_METHOD, historyResult()],
  ])
  const request = vi.fn((
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<unknown> => {
    calls.push({ method, params, options })
    return Promise.resolve(results.get(method))
  })
  return {
    calls,
    results,
    rpc: {
      request<T = unknown>(
        method: string,
        params?: Record<string, unknown>,
        options?: RpcCallOptions,
      ): Promise<T> {
        return request(method, params, options) as Promise<T>
      },
    },
  }
}

describe('v4 SessionInspection Adapter', () => {
  it('projects a bounded preview without opening a live subscription', async () => {
    const harness = makeHarness()
    const signal = new AbortController().signal
    const inspection = createV4SessionInspection(harness.rpc, {
      concurrentHistoryReads: () => true,
      now: () => 1_000,
    })

    await expect(inspection.preview(' alpha ', {
      signal,
      budgetMs: 5_000,
      deadlineAt: 1_600,
    })).resolves.toEqual({
      key: 'alpha',
      title: 'Alpha',
      lastMessage: 'hello',
      updatedAt: 9,
    })
    expect(harness.calls).toEqual([{
      method: SESSIONS_PREVIEW_METHOD,
      params: { keys: ['alpha'] },
      options: {
        signal,
        timeoutMs: 600,
        timeoutAction: 'reject',
        abortAction: 'reject',
      },
    }])
    expect(Object.keys(inspection).sort()).toEqual(['history', 'preview', 'readExecutionLog'])
    expect('open' in inspection).toBe(false)
    expect('subscribe' in inspection).toBe(false)
  })

  it('reads only latest and before rich history through the shared projection', async () => {
    const harness = makeHarness()
    const inspection = createV4SessionInspection(harness.rpc, {
      concurrentHistoryReads: () => false,
      now: () => 10_000,
    })
    const signal = new AbortController().signal

    const latest = await inspection.history.latest('alpha', {
      signal,
      budgetMs: 5_000,
      deadlineAt: 11_250,
    })
    expect(latest).toMatchObject({
      canonicalAvailable: true,
      canonicalComplete: true,
      messages: [{
        reasoningContent: '  exact reasoning  ',
        inputTokens: 3,
        outputTokens: 5,
        turnContext: {
          turnId: 'turn-1',
          promotedTurnId: 'turn-0',
          appliedIteration: 2,
          activityMarkers: [{ marker_id: 'marker-1' }],
        },
      }],
    })
    await inspection.history.before('alpha', ' older ', { limit: 12 })

    const historyCalls = harness.calls.filter(call => call.method === CHAT_HISTORY_METHOD)
    expect(historyCalls[0]).toMatchObject({
      params: {
        sessionKey: 'alpha',
        limit: 20,
        includeCanonical: true,
        includeSummaries: false,
      },
      options: {
        signal,
        timeoutMs: 1_250,
        timeoutAction: 'reconnect',
        abortAction: 'reject',
      },
    })
    expect(historyCalls[0]?.params).not.toHaveProperty('before')
    expect(historyCalls[0]?.params).not.toHaveProperty('after')
    expect(historyCalls[1]?.params).toMatchObject({ before: 'older', limit: 12 })
    expect(historyCalls[1]?.params).not.toHaveProperty('after')
    expect(Object.keys(inspection.history).sort()).toEqual(['before', 'latest'])
    expect('after' in inspection.history).toBe(false)
  })

  it('normalizes legacy canonical proof absence but rejects other malformed wire values', async () => {
    const harness = makeHarness()
    const legacy = { ...historyResult() } as Record<string, unknown>
    delete legacy.canonical_available
    delete legacy.canonical_complete
    harness.results.set(CHAT_HISTORY_METHOD, legacy)
    const inspection = createV4SessionInspection(harness.rpc, {
      concurrentHistoryReads: () => true,
    })

    await expect(inspection.history.latest('alpha')).resolves.toMatchObject({
      canonicalAvailable: null,
      canonicalComplete: null,
    })

    harness.results.set(CHAT_HISTORY_METHOD, { ...legacy, messages: 'malformed' })
    await expect(inspection.history.latest('alpha')).rejects.toBeInstanceOf(
      SessionInspectionContractError,
    )
    harness.results.set(SESSIONS_PREVIEW_METHOD, { ts: 1, previews: 'malformed' })
    await expect(inspection.preview('alpha')).rejects.toBeInstanceOf(
      SessionInspectionContractError,
    )
  })

  it('validates semantic arguments before touching transport', async () => {
    const harness = makeHarness()
    const inspection = createV4SessionInspection(harness.rpc, {
      concurrentHistoryReads: () => true,
    })

    await expect(inspection.preview(' ')).rejects.toBeInstanceOf(TypeError)
    expect(() => inspection.history.before('alpha', ' ')).toThrow(TypeError)
    expect(() => inspection.history.latest('alpha', { limit: 201 }))
      .toThrow(RangeError)
    expect(() => inspection.history.latest('alpha', { budgetMs: 0 }))
      .toThrow(RangeError)
    expect(harness.calls).toHaveLength(0)
  })
})

describe('execution log inspection', () => {
  const method = 'sessions.executionLog.read'
  const handle = `tr-${'a'.repeat(32)}`
  const firstPage = {
    storage_kind: 'execution_log', handle, content: '故障🙂\n', offset: 0,
    returned_chars: 4, next_offset: 4, chars: 8, complete: true,
  }

  it('validates Unicode character offsets independently from JavaScript string length', async () => {
    const harness = makeHarness()
    harness.results.set(method, firstPage)
    const inspection = createV4SessionInspection(harness.rpc, { concurrentHistoryReads: () => true })
    const signal = new AbortController().signal
    await expect(inspection.readExecutionLog(' alpha ', handle, 0, { signal })).resolves.toEqual({
      handle, content: '故障🙂\n', offset: 0, nextOffset: 4, chars: 8, complete: true,
    })
    expect(harness.calls[0]).toMatchObject({
      method, params: { sessionKey: 'alpha', handle, offset: 0, limit: 12000 },
      options: { signal, timeoutAction: 'reject', abortAction: 'reject' },
    })
    harness.results.set(method, {
      ...firstPage, content: 'next', offset: 4, next_offset: null, complete: false,
    })
    await expect(inspection.readExecutionLog('alpha', handle, 4)).resolves.toMatchObject({
      content: 'next', offset: 4, nextOffset: null, complete: false,
    })
  })

  it.each([
    { content: 'x'.repeat(12001) }, { handle: `tr-${'b'.repeat(32)}` },
    { returned_chars: 5 }, { next_offset: 5 }, { next_offset: null },
    { chars: 2 }, { offset: 1 }, { content: '', returned_chars: 0, next_offset: 0 },
    { complete: 'yes' },
  ])('rejects malformed or inconsistent pages %j', async patch => {
    const harness = makeHarness()
    harness.results.set(method, { ...firstPage, ...patch })
    const inspection = createV4SessionInspection(harness.rpc, { concurrentHistoryReads: () => true })
    await expect(inspection.readExecutionLog('alpha', handle, 0)).rejects.toBeInstanceOf(
      SessionInspectionContractError,
    )
  })

  it('rejects invalid handles and offsets before making a request', async () => {
    const harness = makeHarness()
    const inspection = createV4SessionInspection(harness.rpc, { concurrentHistoryReads: () => true })
    for (const [id, offset] of [[handle, -1], [handle, 0.5], ['../log', 0]] as const) {
      await expect(inspection.readExecutionLog('alpha', id, offset)).rejects.toBeInstanceOf(TypeError)
    }
    expect(harness.calls).toHaveLength(0)
  })
})


it('maps a pending execution log response to a domain error', async () => {
  const request = vi.fn().mockRejectedValue({ code: 'NOT_READY', retryable: true })
  const inspection = createV4SessionInspection({ request }, { concurrentHistoryReads: () => true })
  await expect(inspection.readExecutionLog('alpha', `tr-${'a'.repeat(32)}`, 0))
    .rejects.toBeInstanceOf(SessionInspectionLogNotReadyError)
})
