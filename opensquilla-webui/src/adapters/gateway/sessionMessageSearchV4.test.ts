import { describe, expect, it, vi } from 'vitest'
import { createV4SessionMessageSearch } from './sessionMessageSearchV4'
import type { Result as SearchResult } from '@/contracts/generated/v4/sessionsMessagesSearch'

const key = 'agent:main:webchat:search-test'
const query = '中文🙂'

function searchResult(overrides: Partial<SearchResult> = {}): SearchResult {
  return {
    session_id: 'sid', session_epoch: 2, query,
    matches: [{ message_id: 'mid', entry_cursor: '1000|12', content_revision: 3,
      offset_utf16: 4, length_utf16: 4, snippet: '🙂之前中文🙂内容' }],
    next_cursor: null, complete: true, coverage_complete: true,
    scanned_entries: 20, unavailable_entries: 0, ...overrides,
  }
}

function contextResult() {
  return {
    session_id: 'sid', session_epoch: 2, projection_revision: 2,
    before_cursor: 'before-opaque', after_cursor: 'after-opaque',
    has_more_before: true, has_more_after: true, complete_for_requested_window: true,
    canonical_available: true, canonical_complete: true, history_scope: 'complete',
    compaction_summaries: [], turn_outcomes: [],
    items: [{
      message_id: 'mid', item_id: 'mid', order: '1', role: 'assistant', preview: 'preview',
      preview_complete: false,
      source_revision: 'legacy-v1:compacted:12:1000:2:16735', content_availability: 'ready',
      message: { message_id: 'mid', role: 'assistant', text: 'preview' },
      contents: [{ availability: 'ready', ref: {
        content_id: 'legacy:sid:mid', session_id: 'sid', session_epoch: 2,
        revision: 'legacy-v1:compacted:12:1000:2:16735', source: 'compacted', view: 'display',
        representation: 'text-utf8', byte_length: 16384, sha256: 'a'.repeat(64),
        status: 'sealed', durable: true, expires_at: null,
      } }],
    }],
  }
}

function setup(result: unknown = searchResult()) {
  const request = vi.fn().mockResolvedValue(result)
  const rpc = { generation: 7, supports: vi.fn((_method: string) => true), request }
  return { request, rpc, adapter: createV4SessionMessageSearch(rpc) }
}

describe('Gateway current-session message search adapter', () => {
  it('uses a bounded cancellable read and projects UTF-16 coordinates without changing them', async () => {
    const { adapter, request } = setup()
    const signal = new AbortController().signal
    const result = await adapter.search({ key, query, signal })
    expect(request).toHaveBeenCalledExactlyOnceWith('sessions.messages.search', { key, query, limit: 50 }, {
      expectedGeneration: 7, timeoutMs: 10_000, timeoutAction: 'reject', abortAction: 'reject',
      recoveryClass: 'safe-read', signal, cancelOnAbort: true,
    })
    expect(result).toEqual({
      sessionId: 'sid', sessionEpoch: 2, query,
      matches: [{ messageId: 'mid', entryCursor: '1000|12', contentRevision: 3,
        offsetUtf16: 4, lengthUtf16: 4, snippet: '🙂之前中文🙂内容' }],
      nextCursor: null, complete: true, coverageComplete: true, scannedEntries: 20, unavailableEntries: 0,
    })
    expect(Object.isFrozen(result)).toBe(true)
    expect(Object.isFrozen(result.matches[0])).toBe(true)
  })

  it('binds continuation to the returned session identity and preserves incomplete empty scan pages', async () => {
    const { adapter, request } = setup(searchResult({ matches: [], next_cursor: 'page-3', complete: false,
      coverage_complete: false, unavailable_entries: 2 }))
    const result = await adapter.search({ key, query, sessionId: 'sid', sessionEpoch: 2, cursor: 'page-2', limit: 1 })
    expect(request).toHaveBeenCalledWith('sessions.messages.search', {
      key, query, session_id: 'sid', session_epoch: 2, cursor: 'page-2', limit: 1,
    }, expect.any(Object))
    expect(result).toMatchObject({ matches: [], nextCursor: 'page-3', complete: false,
      coverageComplete: false, unavailableEntries: 2 })
  })

  it.each([
    { query: '' }, { query: '字'.repeat(257) }, { limit: 0 }, { limit: 101 },
    { sessionEpoch: -1 }, { cursor: 'x'.repeat(4097) },
  ])('rejects malformed requests before transport: %j', async invalid => {
    const { adapter, request } = setup()
    await expect(adapter.search({ key, query, ...invalid })).rejects.toBeInstanceOf(TypeError)
    expect(request).not.toHaveBeenCalled()
  })

  it.each([
    { sessionId: 'different' }, { sessionEpoch: 3 },
  ])('rejects search results from a different session identity: %j', async identity => {
    const { adapter } = setup()
    await expect(adapter.search({ key, query, ...identity })).rejects.toMatchObject({ reason: 'stale' })
  })

  it.each([
    { query: 'different' }, { complete: false }, { next_cursor: 'cursor', complete: true },
    { next_cursor: 'page-2', complete: false }, { scanned_entries: -1 },
  ])('rejects inconsistent or malformed search results: %j', async result => {
    const { adapter } = setup(searchResult(result))
    await expect(adapter.search({ key, query, cursor: 'page-2' })).rejects.toMatchObject({ reason: 'contract' })
  })

  it('rejects a result exceeding the requested page size', async () => {
    const value = searchResult()
    value.matches.push({ ...value.matches[0]!, offset_utf16: 10 })
    const { adapter } = setup(value)
    await expect(adapter.search({ key, query, limit: 1 })).rejects.toMatchObject({ reason: 'contract' })
  })

  it('does not substitute global search when the Gateway lacks the current-session capability', async () => {
    const { adapter, rpc, request } = setup()
    rpc.supports.mockReturnValue(false)
    await expect(adapter.search({ key, query })).rejects.toMatchObject({ reason: 'unsupported' })
    expect(request).not.toHaveBeenCalled()
  })

  it('discards a result completed after the Gateway connection changed', async () => {
    const { adapter, rpc, request } = setup()
    request.mockImplementation(async () => { rpc.generation += 1; return searchResult() })
    await expect(adapter.search({ key, query })).rejects.toMatchObject({ reason: 'stale' })
  })

  it('classifies an old-connection rejection as stale rather than a retryable read error', async () => {
    const { adapter, rpc, request } = setup()
    request.mockImplementation(async () => { rpc.generation += 1; throw new Error('Disconnected') })
    await expect(adapter.search({ key, query })).rejects.toMatchObject({ reason: 'stale' })
  })

  it('does not send already cancelled requests', async () => {
    const { adapter, request } = setup()
    const controller = new AbortController()
    controller.abort()
    await expect(adapter.search({ key, query, signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
    expect(request).not.toHaveBeenCalled()
  })

  it('discards late success from a transport after cancellation', async () => {
    const { adapter, request } = setup()
    const controller = new AbortController()
    request.mockImplementation(async () => { controller.abort(); return searchResult() })
    await expect(adapter.search({ key, query, signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('normalizes cancellation even when the transport uses its own abort error type', async () => {
    const { adapter, request } = setup()
    const controller = new AbortController()
    request.mockImplementation(async () => {
      controller.abort()
      throw Object.assign(new Error('Request aborted'), { name: 'RpcAbortError' })
    })
    await expect(adapter.search({ key, query, signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
  })

  it.each([
    ['HISTORY_STALE', 'stale'], ['REBASE_REQUIRED', 'stale'],
    ['METHOD_NOT_FOUND', 'unsupported'], ['UNKNOWN_METHOD', 'unsupported'],
    ['STORAGE_BUSY', 'busy'], ['NOT_FOUND', 'unavailable'],
  ])('maps %s into a domain failure without exposing raw error details', async (code, reason) => {
    const { adapter, request } = setup()
    request.mockRejectedValue(Object.assign(new Error('private backend details'), { data: { code } }))
    await expect(adapter.search({ key, query })).rejects.toMatchObject({ name: 'SessionMessageSearchError', reason })
    await expect(adapter.search({ key, query })).rejects.not.toThrow('private backend details')
  })

  it('preserves a transport AbortError', async () => {
    const { adapter, request } = setup()
    const cancelled = new DOMException('cancelled', 'AbortError')
    request.mockRejectedValue(cancelled)
    await expect(adapter.search({ key, query })).rejects.toBe(cancelled)
  })

  it('preserves the retry delay for admission backpressure', async () => {
    const { adapter, request } = setup()
    request.mockRejectedValue(Object.assign(new Error('private details'), { code: 'STORAGE_BUSY', retry_after_ms: 750 }))
    await expect(adapter.search({ key, query })).rejects.toMatchObject({ reason: 'busy', retryAfterMs: 750 })
  })

  it('loads a separate anchored history window and preserves compacted content references', async () => {
    const { adapter, request } = setup(contextResult())
    const result = await adapter.context({ key, messageId: 'mid', sessionId: 'sid', sessionEpoch: 2 })
    expect(request).toHaveBeenCalledExactlyOnceWith('sessions.history.page.v2', {
      key, session_id: 'sid', session_epoch: 2, anchor_message_id: 'mid',
      direction: 'before', target_items: 20, target_bytes: 1_048_576,
      projection: { include_content_refs: true, include_tool_metadata: true, include_outcomes: true },
    }, expect.objectContaining({ expectedGeneration: 7, timeoutAction: 'reject', recoveryClass: 'safe-read' }))
    expect(result).toMatchObject({ additional: { sessionId: 'sid', sessionEpoch: 2 },
      oldestCursor: 'before-opaque', newestCursor: 'after-opaque' })
    expect(result.messages[0]).toMatchObject({ messageId: 'mid', contentRef: {
      version: 1, sessionKey: key, sessionId: 'sid', messageId: 'mid',
      source: 'compacted', view: 'display', revision: 'legacy-v1:compacted:12:1000:2:16735',
    } })
  })

  it('rejects an anchored page that no longer contains the matching message', async () => {
    const { adapter } = setup(contextResult())
    await expect(adapter.context({ key, messageId: 'deleted-message' })).rejects.toMatchObject({ reason: 'stale' })
  })

  it('rejects anchored history for a different epoch', async () => {
    const { adapter } = setup(contextResult())
    await expect(adapter.context({ key, messageId: 'mid', sessionEpoch: 3 })).rejects.toMatchObject({ reason: 'stale' })
  })

  it('requires the history capability before fetching an anchored window', async () => {
    const { adapter, rpc, request } = setup(contextResult())
    rpc.supports.mockImplementation(method => method === 'sessions.messages.search')
    await expect(adapter.context({ key, messageId: 'mid' })).rejects.toMatchObject({ reason: 'unsupported' })
    expect(request).not.toHaveBeenCalled()
  })
})
