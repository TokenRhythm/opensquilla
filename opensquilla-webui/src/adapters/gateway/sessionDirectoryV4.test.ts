import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import {
  createV4SessionDirectory,
  normalizeV4SessionItem,
} from './sessionDirectoryV4'
import { SESSIONS_LIST_METHOD } from '@/contracts/generated/v4/sessionsList'
import { SESSIONS_RESOLVE_METHOD } from '@/contracts/generated/v4/sessionsResolve'
import { SESSIONS_SEARCH_METHOD } from '@/contracts/generated/v4/sessionsSearch'
import type { SessionDirectory } from '@/modules/sessionDirectory'
import { isCronSessionKey } from '@/modules/sessionDirectory'
import { useSessions } from '@/composables/useSessions'

type SessionDirectoryTransport = Parameters<typeof createV4SessionDirectory>[0]

interface WireFixture {
  type: string
  id: string
  method?: string
  params?: Record<string, unknown>
  payload?: Record<string, unknown>
}

interface FixtureDocument {
  cases: Array<{ id: string, wire: WireFixture }>
}

const callPolicy = {
  timeoutMs: 10_000,
  timeoutAction: 'reject',
  abortAction: 'reject',
}

function fixtureCase(document: string, caseId: string): WireFixture {
  const url = new URL(
    `../../../../contracts/gateway/v4/sessions/fixtures/${document}`,
    import.meta.url,
  )
  const parsed = JSON.parse(readFileSync(url, 'utf8')) as FixtureDocument
  const found = parsed.cases.find(testCase => testCase.id === caseId)
  if (!found) throw new Error(`missing fixture ${caseId}`)
  return found.wire
}

describe('v4 SessionDirectory Adapter', () => {
  it('recognizes isolated cron session keys as read-only', () => {
    expect(isCronSessionKey('cron:job-1:run:1')).toBe(true)
    expect(isCronSessionKey(' CRON:job-1:run:1 ')).toBe(true)
    expect(isCronSessionKey('')).toBe(false)
    expect(isCronSessionKey('cron')).toBe(false)
    expect(isCronSessionKey('agent:main:webchat:cron:run')).toBe(false)
    expect(isCronSessionKey('agent:main:webchat:one')).toBe(false)
  })

  it('uses the pinned legacy Gateway wire without requiring a new envelope', async () => {
    const request = fixtureCase('requests.json', 'request.page-first')
    const response = fixtureCase('responses.json', 'response.empty-page')
    const requestTransport = vi.fn().mockResolvedValue(response.payload)
    const directory = createV4SessionDirectory({
      request: requestTransport as SessionDirectoryTransport['request'],
    })

    const page = await directory.listPage({
      limit: Number(request.params?.limit),
    })

    expect(requestTransport).toHaveBeenCalledWith(
      request.method,
      request.params,
      callPolicy,
    )
    expect(page).toEqual({ items: [], hasMore: false, nextCursor: null })
  })

  it('hides wire method, view, aliases, and extension fields behind the seam', async () => {
    const requestTransport = vi.fn().mockResolvedValue({
      sessions: [{
        key: 'agent:main:subagent:child',
        title: 'Inspect checkout failures',
        updated_at: '1234',
        message_count: 3,
        run_status: 'cancelled',
        last_task: {
          task_id: 'task-1',
          status: 'cancelled',
          started_at: 1000,
          finished_at: 2240,
        },
        parent: { key: 'agent:main:webchat:parent', taskId: 'task-parent', spawnDepth: 1 },
        model: 'gpt-test',
        extension_from_future_gateway: { accepted: true },
      }],
      has_more: true,
      next_cursor: 'cursor-2',
    })
    const directory = createV4SessionDirectory({
      request: requestTransport as SessionDirectoryTransport['request'],
    })

    const page = await directory.listPage({ limit: 25, cursor: 'cursor-1' })

    expect(requestTransport).toHaveBeenCalledWith(SESSIONS_LIST_METHOD, {
      limit: 25,
      view: 'session-list-v1',
      cursor: 'cursor-1',
    }, callPolicy)
    expect(page).toMatchObject({ hasMore: true, nextCursor: 'cursor-2' })
    expect(page.items[0]).toMatchObject({
      key: 'agent:main:subagent:child',
      title: 'Inspect checkout failures',
      updatedAt: 1234,
      messageCount: 3,
      model: 'gpt-test',
      runStatus: 'cancelled',
      runLabel: 'Stopped after 1s',
      parent: { key: 'agent:main:webchat:parent', spawnDepth: 1 },
    })
    expect(page.items[0]).not.toHaveProperty('raw')
  })

  it('normalizes the production task and parent golden without synthetic fields', async () => {
    const response = fixtureCase('responses.json', 'response.current-task-parent')
    const requestTransport = vi.fn().mockResolvedValue(response.payload)
    const directory = createV4SessionDirectory({
      request: requestTransport as SessionDirectoryTransport['request'],
    })

    const page = await directory.listPage({ limit: 200 })

    expect(page.items).toHaveLength(1)
    expect(page.items[0]).toMatchObject({
      key: 'agent:main:subagent:child',
      model: 'openai/gpt-5',
      runStatus: 'cancelled',
      runLabel: 'Stopped after 4s',
      parent: { key: 'agent:main:webchat:parent', spawnDepth: 1 },
    })
    expect(page.items[0]).not.toHaveProperty('costUsd')
    expect(page.items[0].parent).not.toHaveProperty('title')
  })

  it('returns exact counts and keeps the legacy bounded-list fallback', async () => {
    const exactRequest = vi.fn().mockResolvedValue({ total_count: 42 })
    const exactDirectory = createV4SessionDirectory({
      request: exactRequest as SessionDirectoryTransport['request'],
    })
    await expect(exactDirectory.count()).resolves.toEqual({ value: 42, exact: true })

    const legacyRequest = vi.fn().mockResolvedValue({
      keys: ['agent:main:webchat:one', 'unknown', 'agent:main:webchat:two'],
    })
    const legacyDirectory = createV4SessionDirectory({
      request: legacyRequest as SessionDirectoryTransport['request'],
    })
    await expect(legacyDirectory.count()).resolves.toEqual({ value: 2, exact: false })
  })

  it('resolves a session through the typed Adapter and preserves v4 call policy', async () => {
    const ready = vi.fn().mockResolvedValue(undefined)
    const requestTransport = vi.fn().mockResolvedValue({
      session_key: 'agent:main:webchat:default',
      session_id: 'session-default',
      future: { retained: true },
    })
    const directory = createV4SessionDirectory({
      ready,
      request: requestTransport as SessionDirectoryTransport['request'],
    })
    const controller = new AbortController()

    await expect(directory.resolve({
      key: 'session-default',
      signal: controller.signal,
    })).resolves.toEqual({
      key: 'agent:main:webchat:default',
      id: 'session-default',
    })

    expect(ready).toHaveBeenCalledWith({
      timeoutMs: 10_000,
      signal: controller.signal,
      timeoutAction: 'reject',
      abortAction: 'reject',
    })
    expect(requestTransport).toHaveBeenCalledWith(
      SESSIONS_RESOLVE_METHOD,
      { key: 'session-default' },
      { ...callPolicy, signal: controller.signal },
    )
  })

  it('maps legacy Gateway errors to domain error codes', async () => {
    const failure = Object.assign(new Error('missing'), { code: 'NOT_FOUND' })
    const directory = createV4SessionDirectory({
      request: vi.fn().mockRejectedValue(failure) as SessionDirectoryTransport['request'],
    })

    await expect(directory.resolve({ key: 'missing' })).rejects.toMatchObject({
      name: 'SessionDirectoryError',
      code: 'not-found',
    })
  })

  it('rejects a malformed resolve result at the domain boundary', async () => {
    const directory = createV4SessionDirectory({
      request: vi.fn().mockResolvedValue({ session_key: 'only-key' }) as
        SessionDirectoryTransport['request'],
    })

    await expect(directory.resolve({ key: 'missing-id' })).rejects.toMatchObject({
      name: 'SessionDirectoryError',
      code: 'unavailable',
    })
  })

  it('preserves caller cancellation instead of translating it to a transport error', async () => {
    const controller = new AbortController()
    const abortError = Object.assign(new Error('cancelled'), { name: 'AbortError' })
    const directory = createV4SessionDirectory({
      request: vi.fn().mockRejectedValue(abortError) as SessionDirectoryTransport['request'],
    })

    await expect(directory.resolve({
      key: 'session-1',
      signal: controller.signal,
    })).rejects.toBe(abortError)
  })

  it('searches through the typed Adapter and preserves the v4 call policy', async () => {
    const ready = vi.fn().mockResolvedValue(undefined)
    const requestTransport = vi.fn().mockResolvedValue({
      sessions: [{
        key: 'agent:main:s1',
        title: 'Deploy planning',
        effectiveAgentId: 'main',
        surface: 'webchat',
        updatedAt: 1700000000000,
        extension: { retained: true },
      }],
      messages: [{
        key: 'agent:main:s2',
        title: 'Grocery list',
        role: 'user',
        snippet: 'buy >>>milk<<< today',
        createdAt: 1700000000001,
        future_field: 'ignored by the domain projection',
      }],
      query: 'milk',
      ts: 1700000000002,
      delivery_context: { source: 'search-index' },
    })
    const directory = createV4SessionDirectory({
      ready,
      request: requestTransport as SessionDirectoryTransport['request'],
    })
    const controller = new AbortController()

    await expect(directory.search({
      query: 'milk',
      limit: 12,
      signal: controller.signal,
    })).resolves.toEqual({
      sessions: [{
        key: 'agent:main:s1',
        title: 'Deploy planning',
        surface: 'webchat',
      }],
      messages: [{
        key: 'agent:main:s2',
        title: 'Grocery list',
        snippet: 'buy >>>milk<<< today',
        createdAt: 1700000000001,
      }],
    })
    expect(ready).toHaveBeenCalledWith({
      timeoutMs: 10_000,
      signal: controller.signal,
      timeoutAction: 'reject',
      abortAction: 'reject',
    })
    expect(requestTransport).toHaveBeenCalledWith(
      SESSIONS_SEARCH_METHOD,
      { query: 'milk', limit: 12 },
      { ...callPolicy, signal: controller.signal },
    )
  })

  it('accepts the legacy string timestamp while keeping search domain fields narrow', async () => {
    const directory = createV4SessionDirectory({
      request: vi.fn().mockResolvedValue({
        sessions: [{
          key: 'agent:main:legacy', title: 'Legacy', effectiveAgentId: null,
          surface: null, updatedAt: null, future: true,
        }],
        messages: [{
          key: 'agent:main:legacy', title: 'Legacy', role: null,
          snippet: 'legacy result', createdAt: '1700000000000',
        }],
        query: 'legacy', ts: 1700000000002,
      }) as SessionDirectoryTransport['request'],
    })

    await expect(directory.search({ query: 'legacy' })).resolves.toEqual({
      sessions: [{ key: 'agent:main:legacy', title: 'Legacy', surface: null }],
      messages: [{
        key: 'agent:main:legacy', title: 'Legacy',
        snippet: 'legacy result', createdAt: 1700000000000,
      }],
    })
  })

  it('maps search Gateway errors and rejects malformed results at the seam', async () => {
    const failure = Object.assign(new Error('not allowed'), { code: 'UNAUTHORIZED' })
    const denied = createV4SessionDirectory({
      request: vi.fn().mockRejectedValue(failure) as SessionDirectoryTransport['request'],
    })
    await expect(denied.search({ query: 'secret' })).rejects.toMatchObject({
      name: 'SessionDirectoryError',
      code: 'forbidden',
    })

    const malformed = createV4SessionDirectory({
      request: vi.fn().mockResolvedValue({ sessions: [], messages: [] }) as
        SessionDirectoryTransport['request'],
    })
    await expect(malformed.search({ query: 'milk' })).rejects.toMatchObject({
      name: 'SessionDirectoryError',
      code: 'unavailable',
    })
  })

  it('maps an unavailable legacy search method to the domain error', async () => {
    const failure = Object.assign(new Error('method missing'), { code: 'METHOD_NOT_FOUND' })
    const directory = createV4SessionDirectory({
      request: vi.fn().mockRejectedValue(failure) as SessionDirectoryTransport['request'],
    })

    await expect(directory.search({ query: 'milk' })).rejects.toMatchObject({
      name: 'SessionDirectoryError',
      code: 'unsupported',
    })
  })

  it('preserves caller cancellation for search requests', async () => {
    const controller = new AbortController()
    const abortError = Object.assign(new Error('cancelled'), { name: 'AbortError' })
    const directory = createV4SessionDirectory({
      request: vi.fn().mockRejectedValue(abortError) as SessionDirectoryTransport['request'],
    })

    await expect(directory.search({
      query: 'milk',
      signal: controller.signal,
    })).rejects.toBe(abortError)
  })

  it('owns its v4 readiness, timeout, and abort policy', async () => {
    const controller = new AbortController()
    const ready = vi.fn().mockResolvedValue(undefined)
    const requestTransport = vi.fn().mockResolvedValue({ sessions: [] })
    const directory = createV4SessionDirectory({
      ready,
      request: requestTransport as SessionDirectoryTransport['request'],
    })

    await directory.listPage({ limit: 5, signal: controller.signal })

    expect(ready).toHaveBeenCalledWith({
      timeoutMs: 10_000,
      signal: controller.signal,
      timeoutAction: 'reject',
      abortAction: 'reject',
    })
    expect(requestTransport).toHaveBeenCalledWith(
      SESSIONS_LIST_METHOD,
      { limit: 5, view: 'session-list-v1' },
      { ...callPolicy, signal: controller.signal },
    )
  })

  it('passes caller cancellation through the count query without reconnect ownership', async () => {
    const controller = new AbortController()
    const ready = vi.fn().mockResolvedValue(undefined)
    const requestTransport = vi.fn().mockResolvedValue({ totalCount: 4 })
    const directory = createV4SessionDirectory({
      ready,
      request: requestTransport as SessionDirectoryTransport['request'],
    })

    await expect(directory.count({ signal: controller.signal })).resolves.toEqual({
      value: 4,
      exact: true,
    })

    expect(ready).toHaveBeenCalledWith({
      timeoutMs: 10_000,
      signal: controller.signal,
      timeoutAction: 'reject',
      abortAction: 'reject',
    })
    expect(requestTransport).toHaveBeenCalledWith(
      SESSIONS_LIST_METHOD,
      { limit: 200, view: 'session-count-v1' },
      { ...callPolicy, signal: controller.signal },
    )
  })

  it('propagates genuine transport failures without rewriting them', async () => {
    const failure = new Error('connection lost')
    const directory = createV4SessionDirectory({
      request: vi.fn().mockRejectedValue(failure) as SessionDirectoryTransport['request'],
    })

    await expect(directory.count()).rejects.toBe(failure)
  })

  it('lets callers replace the Adapter at the SessionDirectory seam', async () => {
    const first = normalizeV4SessionItem({ key: 'agent:main:webchat:one', title: 'One' })
    const second = normalizeV4SessionItem({ key: 'agent:main:webchat:two', title: 'Two' })
    if (!first || !second) throw new Error('invalid fixture')
    const directory: SessionDirectory = {
      listPage: vi.fn().mockResolvedValue({
        items: [first, second],
        hasMore: false,
        nextCursor: null,
      }),
      count: vi.fn().mockResolvedValue({ value: 2, exact: true }),
      resolve: vi.fn().mockResolvedValue({
        key: first.key,
        id: 'session-one',
      }),
      search: vi.fn().mockResolvedValue({ sessions: [], messages: [] }),
    }
    setActivePinia(createPinia())
    const sessions = useSessions(directory)

    await sessions.loadSessions()

    expect(directory.listPage).toHaveBeenCalledWith(expect.objectContaining({
      limit: 200,
      cursor: undefined,
      signal: expect.any(AbortSignal),
    }))
    expect(sessions.sessionsList.value.map(item => item.key)).toEqual([first.key, second.key])
  })
})
