import { describe, expect, it, vi } from 'vitest'
import type { RpcCallOptions } from '@/lib/rpc'

import {
  createV4SessionLifecycle,
} from './sessionLifecycleV4'
import {
  SESSIONS_CREATE_METHOD,
} from '@/contracts/generated/v4/sessionsCreate'
import {
  SESSIONS_RENAME_METHOD,
} from '@/contracts/generated/v4/sessionsRename'
import {
  SESSIONS_DELETE_METHOD,
} from '@/contracts/generated/v4/sessionsDelete'

type LifecycleTransport = Parameters<typeof createV4SessionLifecycle>[0]

function makeTransport(responses: Record<string, unknown>) {
  const request = vi.fn(async (
    method: string,
    _params?: Record<string, unknown>,
    _options?: RpcCallOptions,
  ) => responses[method])
  return { request, transport: { request } as LifecycleTransport }
}

describe('v4 SessionLifecycle Adapter', () => {
  it('maps the domain create input to the legacy wire names', async () => {
    const { request, transport } = makeTransport({
      [SESSIONS_CREATE_METHOD]: {
        key: 'agent:main:webchat:abc123',
        sessionId: 'abc123',
        seededMessage: true,
        future: { retained: true },
      },
    })
    const lifecycle = createV4SessionLifecycle(transport)

    await expect(lifecycle.create({
      agentId: 'main',
      kind: 'webchat',
      workspaceId: 'project-a',
      title: 'Draft',
      message: 'hello',
      model: 'openai/gpt-test',
    })).resolves.toEqual({
      key: 'agent:main:webchat:abc123',
      sessionId: 'abc123',
      seededMessage: true,
    })
    expect(request).toHaveBeenCalledWith(
      SESSIONS_CREATE_METHOD,
      {
        agentId: 'main',
        kind: 'webchat',
        workspaceId: 'project-a',
        displayName: 'Draft',
        message: 'hello',
        model: 'openai/gpt-test',
      },
      undefined,
    )
  })

  it('keeps the no-manager compatibility note and omits unknown wire fields', async () => {
    const { transport } = makeTransport({
      [SESSIONS_CREATE_METHOD]: {
        key: 'agent:main:abc123',
        sessionId: 'abc123',
        note: 'session manager not available',
        extra: 'future',
      },
    })
    await expect(createV4SessionLifecycle(transport).create()).resolves.toEqual({
      key: 'agent:main:abc123',
      sessionId: 'abc123',
      note: 'session manager not available',
    })
  })

  it('maps the semantic title to displayName for rename and narrows the result', async () => {
    const { request, transport } = makeTransport({
      [SESSIONS_RENAME_METHOD]: {
        key: 'agent:main:webchat:abc123',
        updated: ['displayName'],
        extension: true,
      },
    })
    await expect(createV4SessionLifecycle(transport).rename({
      key: 'agent:main:webchat:abc123',
      title: 'Renamed',
    })).resolves.toEqual({
      key: 'agent:main:webchat:abc123',
      updatedFields: ['displayName'],
    })
    expect(request).toHaveBeenCalledWith(
      SESSIONS_RENAME_METHOD,
      { key: 'agent:main:webchat:abc123', displayName: 'Renamed' },
      undefined,
    )
  })

  it('preserves partial delete success and original key spelling', async () => {
    const { request, transport } = makeTransport({
      [SESSIONS_DELETE_METHOD]: {
        deleted: ['webchat:one'],
        errors: ["webchat:missing: 'Session not found: webchat:missing'"],
      },
    })
    await expect(createV4SessionLifecycle(transport).remove([
      'webchat:one',
      'webchat:missing',
    ])).resolves.toEqual({
      deleted: ['webchat:one'],
      errors: ["webchat:missing: 'Session not found: webchat:missing'"],
    })
    expect(request).toHaveBeenCalledWith(
      SESSIONS_DELETE_METHOD,
      { keys: ['webchat:one', 'webchat:missing'] },
      undefined,
    )
  })

  it('maps legacy RPC errors to domain errors without changing abort identity', async () => {
    const failure = Object.assign(new Error('guest denied'), { code: 'OWNER_REQUIRED' })
    const request = vi.fn().mockRejectedValue(failure)
    const lifecycle = createV4SessionLifecycle({ request } as LifecycleTransport)
    await expect(lifecycle.create()).rejects.toMatchObject({
      name: 'SessionLifecycleError',
      code: 'forbidden',
    })

    const abort = Object.assign(new Error('cancelled'), { name: 'AbortError' })
    const abortRequest = vi.fn().mockRejectedValue(abort)
    const controller = new AbortController()
    const abortedLifecycle = createV4SessionLifecycle({ request: abortRequest } as LifecycleTransport)
    await expect(abortedLifecycle.rename({
      key: 'k',
      title: 't',
      signal: controller.signal,
    })).rejects.toBe(abort)
  })

  it.each(['AGENT_NOT_FOUND', 'agent.not_found', 'WORKSPACE_NOT_FOUND'])(
    'maps %s compatibility errors to not-found',
    async code => {
      const failure = Object.assign(new Error('missing'), { code })
      const request = vi.fn().mockRejectedValue(failure)
      const lifecycle = createV4SessionLifecycle({ request } as LifecycleTransport)

      await expect(lifecycle.create()).rejects.toMatchObject({
        name: 'SessionLifecycleError',
        code: 'not-found',
      })
    },
  )

  it('rejects malformed success payloads at the adapter boundary', async () => {
    const { transport } = makeTransport({
      [SESSIONS_CREATE_METHOD]: { key: 'missing-session-id' },
      [SESSIONS_RENAME_METHOD]: { key: 'k' },
      [SESSIONS_DELETE_METHOD]: { deleted: [] },
    })
    const lifecycle = createV4SessionLifecycle(transport)
    await expect(lifecycle.create()).rejects.toMatchObject({ code: 'unavailable' })
    await expect(lifecycle.rename({ key: 'k', title: 't' })).rejects.toMatchObject({ code: 'unavailable' })
    await expect(lifecycle.remove(['k'])).rejects.toMatchObject({ code: 'unavailable' })
  })
})
