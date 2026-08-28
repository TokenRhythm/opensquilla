import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import {
  createV4SessionDirectory,
  normalizeV4SessionItem,
  type SessionDirectoryRpc,
} from './sessionDirectoryV4'
import { SESSIONS_LIST_METHOD } from '@/contracts/generated/v4/sessionsList'
import type { SessionDirectory } from '@/modules/sessionDirectory'
import { useSessions } from '@/composables/useSessions'

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
  it('uses the pinned legacy Gateway wire without requiring a new envelope', async () => {
    const request = fixtureCase('requests.json', 'request.page-first')
    const response = fixtureCase('responses.json', 'response.empty-page')
    const call = vi.fn().mockResolvedValue(response.payload)
    const directory = createV4SessionDirectory({
      call: call as SessionDirectoryRpc['call'],
    })

    const page = await directory.listPage({
      limit: Number(request.params?.limit),
    })

    expect(call).toHaveBeenCalledWith(request.method, request.params)
    expect(page).toEqual({ items: [], hasMore: false, nextCursor: null })
  })

  it('hides wire method, view, aliases, and extension fields behind the seam', async () => {
    const call = vi.fn().mockResolvedValue({
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
      call: call as SessionDirectoryRpc['call'],
    })

    const page = await directory.listPage({ limit: 25, cursor: 'cursor-1' })

    expect(call).toHaveBeenCalledWith(SESSIONS_LIST_METHOD, {
      limit: 25,
      view: 'session-list-v1',
      cursor: 'cursor-1',
    })
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
    const call = vi.fn().mockResolvedValue(response.payload)
    const directory = createV4SessionDirectory({
      call: call as SessionDirectoryRpc['call'],
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
    const exactCall = vi.fn().mockResolvedValue({ total_count: 42 })
    const exactDirectory = createV4SessionDirectory({
      call: exactCall as SessionDirectoryRpc['call'],
    })
    await expect(exactDirectory.count()).resolves.toEqual({ value: 42, exact: true })

    const legacyCall = vi.fn().mockResolvedValue({
      keys: ['agent:main:webchat:one', 'unknown', 'agent:main:webchat:two'],
    })
    const legacyDirectory = createV4SessionDirectory({
      call: legacyCall as SessionDirectoryRpc['call'],
    })
    await expect(legacyDirectory.count()).resolves.toEqual({ value: 2, exact: false })
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
