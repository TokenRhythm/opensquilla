import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useRpcStore } from '@/stores/rpc'
import type { SessionsListResponse } from '@/types/rpc'
import { sessionMatches, useSessions } from './useSessions'

function rows(start: number, end: number) {
  return Array.from({ length: end - start }, (_, offset) => ({
    key: `agent:main:webchat:session-${start + offset}`,
    title: `Task ${start + offset}`,
    updatedAt: 10_000 - start - offset,
  }))
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

describe('useSessions pagination', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  function setup(responses: Array<SessionsListResponse | Promise<SessionsListResponse>>) {
    const rpc = useRpcStore()
    vi.spyOn(rpc, 'waitForConnection').mockResolvedValue()
    const call = vi.spyOn(rpc, 'call').mockImplementation(async () => {
      const response = responses.shift()
      if (!response) throw new Error('unexpected sessions.list call')
      return await response
    })
    return { rpc, call, sessions: useSessions() }
  }

  it('loads all 401 sessions across three pages and de-duplicates page boundaries', async () => {
    const { call, sessions } = setup([
      { sessions: rows(0, 200), has_more: true, next_cursor: 'cursor-1' },
      { sessions: rows(199, 399), hasMore: true, nextCursor: 'cursor-2' },
      { sessions: rows(398, 401), hasMore: false, nextCursor: null },
    ])

    await sessions.loadSessions()
    expect(sessions.sessionsList.value).toHaveLength(200)
    expect(sessions.hasMore.value).toBe(true)

    await sessions.loadMoreSessions()
    await sessions.loadMoreSessions()

    expect(sessions.sessionsList.value).toHaveLength(401)
    expect(new Set(
      sessions.sessionsList.value.map(row => typeof row === 'string' ? row : row.key),
    ).size).toBe(401)
    expect(sessions.hasMore.value).toBe(false)
    expect(call).toHaveBeenNthCalledWith(2, 'sessions.list', {
      limit: 200,
      view: 'session-list-v1',
      cursor: 'cursor-1',
    })
    expect(call).toHaveBeenNthCalledWith(3, 'sessions.list', {
      limit: 200,
      view: 'session-list-v1',
      cursor: 'cursor-2',
    })
    expect(sessionMatches(
      sessions.allSessions.value[sessions.allSessions.value.length - 1]!,
      'task 400',
    )).toBe(true)
  })

  it('replays the loaded page depth when an authoritative refresh resets cursors', async () => {
    const { call, sessions } = setup([
      { sessions: rows(0, 200), has_more: true, next_cursor: 'old-cursor-1' },
      { sessions: rows(200, 400), has_more: true, next_cursor: 'old-cursor-2' },
      { sessions: rows(400, 401), has_more: false, next_cursor: null },
      { sessions: rows(0, 200), has_more: true, next_cursor: 'new-cursor-1' },
      { sessions: rows(200, 400), has_more: true, next_cursor: 'new-cursor-2' },
      { sessions: rows(400, 401), has_more: false, next_cursor: null },
    ])

    await sessions.loadSessions()
    await sessions.loadMoreSessions()
    await sessions.loadMoreSessions()
    expect(sessions.sessionsList.value).toHaveLength(401)

    await sessions.loadSessions()

    expect(sessions.sessionsList.value).toHaveLength(401)
    expect(sessions.hasMore.value).toBe(false)
    expect(call).toHaveBeenNthCalledWith(5, 'sessions.list', {
      limit: 200,
      view: 'session-list-v1',
      cursor: 'new-cursor-1',
    })
    expect(call).toHaveBeenNthCalledWith(6, 'sessions.list', {
      limit: 200,
      view: 'session-list-v1',
      cursor: 'new-cursor-2',
    })
  })

  it('keeps the last complete multi-page snapshot when a refresh page fails', async () => {
    const errorLog = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { sessions } = setup([
      { sessions: rows(0, 200), has_more: true, next_cursor: 'old-cursor-1' },
      { sessions: rows(200, 400), has_more: true, next_cursor: 'old-cursor-2' },
      { sessions: rows(400, 401), has_more: false, next_cursor: null },
      { sessions: rows(0, 200), has_more: true, next_cursor: 'new-cursor-1' },
      Promise.reject(new Error('connection changed during refresh')),
    ])

    await sessions.loadSessions()
    await sessions.loadMoreSessions()
    await sessions.loadMoreSessions()
    await sessions.loadSessions()

    expect(sessions.sessionsList.value).toHaveLength(401)
    expect(sessions.sessionListError.value).toBe(false)
    expect(sessions.hasMore.value).toBe(false)
    errorLog.mockRestore()
  })

  it('discards an append from an old traversal when a concurrent refresh wins', async () => {
    const stalePage = deferred<SessionsListResponse>()
    const { sessions } = setup([
      { sessions: rows(0, 200), has_more: true, next_cursor: 'old-cursor-1' },
      { sessions: rows(200, 400), has_more: true, next_cursor: 'old-cursor-2' },
      stalePage.promise,
      { sessions: rows(0, 200), has_more: true, next_cursor: 'new-cursor-1' },
      { sessions: rows(200, 400), has_more: true, next_cursor: 'new-cursor-2' },
    ])

    await sessions.loadSessions()
    await sessions.loadMoreSessions()
    const append = sessions.loadMoreSessions()
    await Promise.resolve()

    await sessions.loadSessions()
    stalePage.resolve({
      sessions: [{ key: 'agent:stale:webchat:session-400', title: 'Synthetic stale row' }],
      has_more: false,
      next_cursor: null,
    })
    await append

    expect(sessions.sessionsList.value).toHaveLength(400)
    expect(sessions.sessionsList.value.some(
      item => (typeof item === 'string' ? item : item.key)?.includes('stale'),
    )).toBe(false)
    expect(sessions.hasMore.value).toBe(true)
  })

  it('does not dispatch a refresh that became stale while waiting for connection', async () => {
    const connection = deferred<void>()
    const { rpc, call, sessions } = setup([
      { sessions: [{ key: 'agent:main:webchat:current', title: 'Current' }] },
    ])
    vi.mocked(rpc.waitForConnection)
      .mockReset()
      .mockReturnValueOnce(connection.promise)
      .mockResolvedValue(undefined)

    const staleRefresh = sessions.loadSessions()
    const currentRefresh = sessions.loadSessions()
    await currentRefresh
    connection.resolve()
    await staleRefresh

    expect(call).toHaveBeenCalledTimes(1)
    expect(sessions.sessionsList.value).toEqual([
      { key: 'agent:main:webchat:current', title: 'Current' },
    ])
  })

  it('does not dispatch load-more after a refresh wins during connection wait', async () => {
    const connection = deferred<void>()
    const { rpc, call, sessions } = setup([
      { sessions: rows(0, 200), has_more: true, next_cursor: 'old-cursor' },
      { sessions: [{ key: 'agent:main:webchat:current', title: 'Current' }] },
    ])
    await sessions.loadSessions()
    vi.mocked(rpc.waitForConnection)
      .mockReset()
      .mockReturnValueOnce(connection.promise)
      .mockResolvedValue(undefined)

    const staleAppend = sessions.loadMoreSessions()
    const currentRefresh = sessions.loadSessions()
    await currentRefresh
    connection.resolve()
    await staleAppend

    expect(call).toHaveBeenCalledTimes(2)
    expect(sessions.sessionsList.value).toEqual([
      { key: 'agent:main:webchat:current', title: 'Current' },
    ])
  })

  it('treats a legacy response without page metadata as terminal', async () => {
    const legacyKeys = rows(0, 200).map(row => row.key)
    const { call, sessions } = setup([{ keys: legacyKeys }])

    await sessions.loadSessions()
    await sessions.loadMoreSessions()

    expect(sessions.sessionsList.value).toEqual(legacyKeys)
    expect(sessions.hasMore.value).toBe(false)
    expect(call).toHaveBeenCalledTimes(1)
  })

  it('stops when a server repeats the requested cursor', async () => {
    const { sessions } = setup([
      { sessions: rows(0, 200), has_more: true, next_cursor: 'cursor-1' },
      { sessions: rows(200, 201), has_more: true, next_cursor: 'cursor-1' },
    ])

    await sessions.loadSessions()
    await sessions.loadMoreSessions()

    expect(sessions.sessionsList.value).toHaveLength(201)
    expect(sessions.hasMore.value).toBe(false)
  })

  it('stops when a server cycles back to any cursor from the traversal', async () => {
    const { sessions } = setup([
      { sessions: rows(0, 200), has_more: true, next_cursor: 'cursor-1' },
      { sessions: rows(200, 400), has_more: true, next_cursor: 'cursor-2' },
      { sessions: rows(400, 401), has_more: true, next_cursor: 'cursor-1' },
    ])

    await sessions.loadSessions()
    await sessions.loadMoreSessions()
    await sessions.loadMoreSessions()

    expect(sessions.sessionsList.value).toHaveLength(401)
    expect(sessions.hasMore.value).toBe(false)
  })

  it('stops when a page advances its cursor without adding a unique session', async () => {
    const firstPage = rows(0, 200)
    const { call, sessions } = setup([
      { sessions: firstPage, has_more: true, next_cursor: 'cursor-1' },
      { sessions: firstPage, has_more: true, next_cursor: 'cursor-2' },
    ])

    await sessions.loadSessions()
    await sessions.loadMoreSessions()
    await sessions.loadMoreSessions()

    expect(sessions.sessionsList.value).toHaveLength(200)
    expect(sessions.hasMore.value).toBe(false)
    expect(call).toHaveBeenCalledTimes(2)
  })

  it('keeps the cursor available for an explicit retry after a page error', async () => {
    const errorLog = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { sessions } = setup([
      { sessions: rows(0, 200), has_more: true, next_cursor: 'cursor-1' },
      Promise.reject(new Error('temporary failure')),
      { sessions: rows(200, 201), has_more: false, next_cursor: null },
    ])
    await sessions.loadSessions()

    await sessions.loadMoreSessions()
    expect(sessions.loadMoreError.value).toBe(true)
    expect(sessions.hasMore.value).toBe(true)

    await sessions.loadMoreSessions()
    expect(sessions.loadMoreError.value).toBe(false)
    expect(sessions.sessionsList.value).toHaveLength(201)
    errorLog.mockRestore()
  })

  it('discards a late append after refresh resets the traversal', async () => {
    const latePage = deferred<SessionsListResponse>()
    const { sessions } = setup([
      { sessions: rows(0, 200), has_more: true, next_cursor: 'cursor-1' },
      latePage.promise,
      { sessions: [{ key: 'agent:main:webchat:refreshed', title: 'Refreshed' }] },
    ])
    await sessions.loadSessions()

    const append = sessions.loadMoreSessions()
    await Promise.resolve()
    await sessions.loadSessions()
    latePage.resolve({ sessions: rows(200, 201), has_more: false })
    await append

    expect(sessions.sessionsList.value).toEqual([
      { key: 'agent:main:webchat:refreshed', title: 'Refreshed' },
    ])
  })
})
