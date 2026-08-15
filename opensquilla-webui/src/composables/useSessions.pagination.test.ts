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

  it('appends the 201st session and de-duplicates page boundaries', async () => {
    const { call, sessions } = setup([
      { sessions: rows(0, 200), has_more: true, next_cursor: 'cursor-1' },
      { sessions: rows(199, 401), hasMore: false, nextCursor: null },
    ])

    await sessions.loadSessions()
    expect(sessions.sessionsList.value).toHaveLength(200)
    expect(sessions.hasMore.value).toBe(true)

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
    expect(sessionMatches(
      sessions.allSessions.value[sessions.allSessions.value.length - 1]!,
      'task 400',
    )).toBe(true)
  })

  it('treats a legacy response without page metadata as terminal', async () => {
    const { call, sessions } = setup([{ sessions: rows(0, 200) }])

    await sessions.loadSessions()
    await sessions.loadMoreSessions()

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
