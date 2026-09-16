import { describe, expect, it, vi } from 'vitest'
import type { RpcCallOptions } from '@/lib/rpc'

import {
  decodeSessionDirectoryChange,
  createV4SessionDirectoryChanges,
} from './sessionDirectoryChangesV4'
import {
  SESSIONS_SUBSCRIBE_METHOD,
} from '@/contracts/generated/v4/sessionsSubscribe'
import { SESSIONS_UNSUBSCRIBE_METHOD } from '@/contracts/generated/v4/sessionsUnsubscribe'

type Handler = (payload: unknown) => void

function makeHarness(initialGeneration = 1) {
  let generation = initialGeneration
  const handlers = new Map<string, Set<Handler>>()
  const calls: Array<{
    method: string
    params?: Record<string, unknown>
    options?: RpcCallOptions
  }> = []
  const request = vi.fn(async (
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<never> => {
    calls.push({ method, params, options })
    return undefined as never
  })
  const rpc = {
    request,
    ready: vi.fn(async () => undefined),
    get generation() { return generation },
    markUnsupported: vi.fn(),
  }
  const events = {
    subscribe: vi.fn((event: string, handler: Handler) => {
      const set = handlers.get(event) || new Set<Handler>()
      set.add(handler)
      handlers.set(event, set)
      return { close: () => set.delete(handler) }
    }),
  }
  return {
    rpc,
    events,
    calls,
    emit(event: string, payload: unknown) {
      handlers.get(event)?.forEach(handler => handler(payload))
    },
    setGeneration(value: number) { generation = value },
    listenerCount(event: string) { return handlers.get(event)?.size || 0 },
  }
}

async function flushAsyncWork() {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}

describe('v4 SessionDirectoryChanges Adapter', () => {
  it('projects canonical and legacy payloads without exposing wire aliases', () => {
    expect(decodeSessionDirectoryChange({
      schema_version: 1,
      key: 'agent:main:task',
      reason: 'task_terminal',
      run_status: 'idle',
      last_task: { task_id: 'task-1', status: 'succeeded' },
      future: { retained: true },
    })).toEqual({
      key: 'agent:main:task',
      reason: 'taskTerminal',
      runStatus: 'idle',
      lastTask: { id: 'task-1', status: 'succeeded' },
    })

    expect(decodeSessionDirectoryChange({
      key: 'cron:reminder:run:1',
      reason: 'cron_static_message',
      taskId: 'cron:reminder:run:1',
      status: 'succeeded',
    })).toEqual({
      key: 'cron:reminder:run:1',
      reason: 'cronStaticMessage',
      lastTask: { id: 'cron:reminder:run:1', status: 'succeeded' },
    })
  })

  it('drops malformed and unknown-version payloads', () => {
    expect(decodeSessionDirectoryChange({ key: 'k', reason: 7 })).toBeNull()
    expect(decodeSessionDirectoryChange({
      schema_version: 2,
      key: 'k',
      reason: 'created',
    })).toBeNull()
    expect(decodeSessionDirectoryChange({
      key: 'k',
      reason: 'future_reason',
    })).toEqual({ key: 'k', reason: 'unknown' })
  })

  it('keeps legacy identity-only and snake/camel identity events as invalidations', () => {
    // Older fork/invalidation paths sent only the key.  They have no task
    // meaning, but the directory still needs to refresh.
    expect(decodeSessionDirectoryChange({ key: 'legacy-only' }))
      .toEqual({ key: 'legacy-only', reason: 'unknown' })
    expect(decodeSessionDirectoryChange({ session_key: 'legacy-snake' }))
      .toEqual({ key: 'legacy-snake', reason: 'unknown' })
    expect(decodeSessionDirectoryChange({ sessionKey: 'legacy-camel' }))
      .toEqual({ key: 'legacy-camel', reason: 'unknown' })
    // A present malformed reason remains rejected; only an absent legacy
    // reason is projected to the neutral invalidation reason.
    expect(decodeSessionDirectoryChange({ key: 'k', reason: 7 })).toBeNull()
  })

  it('maps known generic invalidations to updated while preserving unknown reasons', () => {
    for (const reason of ['turn_complete', 'cron_result', 'cron_system_event', 'updated']) {
      expect(decodeSessionDirectoryChange({ key: 'k', reason }))
        .toEqual({ key: 'k', reason: 'updated' })
    }
    expect(decodeSessionDirectoryChange({ key: 'k', reason: 'future_reason_v2' }))
      .toEqual({ key: 'k', reason: 'unknown' })
  })

  it('retains a legacy top-level terminal status beside a task projection', () => {
    expect(decodeSessionDirectoryChange({
      key: 'agent:main:legacy-status',
      reason: 'task_terminal',
      status: 'succeeded',
      last_task: { task_id: 'task-legacy' },
    })).toEqual({
      key: 'agent:main:legacy-status',
      reason: 'taskTerminal',
      lastTask: { id: 'task-legacy', status: 'succeeded' },
    })
  })

  it('fans out many local listeners through one raw event listener', async () => {
    const harness = makeHarness()
    const changes = createV4SessionDirectoryChanges(harness.rpc, harness.events)
    const first = vi.fn()
    const second = vi.fn()
    const firstSubscription = changes.subscribe(first)
    const secondSubscription = changes.subscribe(second)

    await changes.resume()
    expect(harness.calls).toHaveLength(1)
    expect(harness.calls[0].method).toBe(SESSIONS_SUBSCRIBE_METHOD)
    expect(harness.calls[0].params).toEqual({})
    expect(harness.calls[0].options).toMatchObject({ expectedGeneration: 1, abortAction: 'reject' })
    expect(harness.events.subscribe).toHaveBeenCalledTimes(2)
    expect(Math.max(...harness.events.subscribe.mock.invocationCallOrder))
      .toBeLessThan(Math.min(...harness.rpc.request.mock.invocationCallOrder))

    harness.emit('sessions.changed', {
      schema_version: 1,
      key: 'agent:main:new',
      reason: 'created',
    })
    expect(first).toHaveBeenCalledWith({ key: 'agent:main:new', reason: 'created' })
    expect(second).toHaveBeenCalledWith({ key: 'agent:main:new', reason: 'created' })

    firstSubscription.close()
    secondSubscription.close()
    await flushAsyncWork()
    expect(harness.calls.filter(call => call.method === SESSIONS_UNSUBSCRIBE_METHOD)).toHaveLength(1)
  })

  it('rebinds once after a new generation and fences unsubscribe', async () => {
    const harness = makeHarness()
    const changes = createV4SessionDirectoryChanges(harness.rpc, harness.events)
    const subscription = changes.subscribe(vi.fn())
    await changes.resume()

    harness.setGeneration(2)
    harness.emit('_state', 'disconnected')
    subscription.close()
    await flushAsyncWork()
    // The old lease must not send unsubscribe to generation 2.
    expect(harness.calls.filter(call => call.method === SESSIONS_UNSUBSCRIBE_METHOD)).toHaveLength(0)

    const replacement = changes.subscribe(vi.fn())
    await changes.resume()
    expect(harness.calls.filter(call => call.method === SESSIONS_SUBSCRIBE_METHOD)).toHaveLength(2)
    replacement.close()
    await flushAsyncWork()
    expect(harness.calls.filter(call => call.method === SESSIONS_UNSUBSCRIBE_METHOD)).toHaveLength(1)
    expect(harness.calls[harness.calls.length - 1]?.options).toMatchObject({ expectedGeneration: 2 })
  })

  it('automatically rebinds an active lease when transport reconnects', async () => {
    const harness = makeHarness()
    const changes = createV4SessionDirectoryChanges(harness.rpc, harness.events)
    const subscription = changes.subscribe(vi.fn())
    await changes.resume()

    harness.setGeneration(2)
    harness.emit('_state', 'disconnected')
    harness.emit('_state', 'connected')
    await flushAsyncWork()

    expect(harness.calls.filter(call => call.method === SESSIONS_SUBSCRIBE_METHOD))
      .toHaveLength(2)
    subscription.close()
  })

  it('serializes cleanup behind a pending subscribe and never recycles transport', async () => {
    const harness = makeHarness()
    let resolveSubscribe!: () => void
    harness.rpc.request.mockImplementation(async (
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ): Promise<never> => {
      harness.calls.push({ method, params, options })
      if (method === SESSIONS_SUBSCRIBE_METHOD) {
        await new Promise<void>(resolve => { resolveSubscribe = resolve })
      }
      return undefined as never
    })
    const changes = createV4SessionDirectoryChanges(harness.rpc, harness.events)
    const subscription = changes.subscribe(vi.fn())
    const resume = changes.resume()
    await flushAsyncWork()
    subscription.close()
    resolveSubscribe()
    await resume
    await flushAsyncWork()
    expect(harness.calls.map(call => call.method)).toEqual([
      SESSIONS_SUBSCRIBE_METHOD,
      SESSIONS_UNSUBSCRIBE_METHOD,
    ])
    expect(harness.calls[harness.calls.length - 1]?.options).toMatchObject({ expectedGeneration: 1 })
  })

  it('orders a new subscribe after an in-flight unsubscribe', async () => {
    const harness = makeHarness()
    let resolveUnsubscribe!: () => void
    harness.rpc.request.mockImplementation(async (
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ): Promise<never> => {
      harness.calls.push({ method, params, options })
      if (method === SESSIONS_UNSUBSCRIBE_METHOD) {
        await new Promise<void>(resolve => { resolveUnsubscribe = resolve })
      }
      return undefined as never
    })
    const changes = createV4SessionDirectoryChanges(harness.rpc, harness.events)
    const first = changes.subscribe(vi.fn())
    await changes.resume()
    first.close()
    await flushAsyncWork()

    const second = changes.subscribe(vi.fn())
    const resume = changes.resume()
    await flushAsyncWork()
    expect(harness.calls.map(call => call.method)).toEqual([
      SESSIONS_SUBSCRIBE_METHOD,
      SESSIONS_UNSUBSCRIBE_METHOD,
    ])

    resolveUnsubscribe()
    await resume
    expect(harness.calls.map(call => call.method)).toEqual([
      SESSIONS_SUBSCRIBE_METHOD,
      SESSIONS_UNSUBSCRIBE_METHOD,
      SESSIONS_SUBSCRIBE_METHOD,
    ])
    second.close()
  })

  it('does not retry forbidden subscription endlessly and releases transport listeners', async () => {
    const harness = makeHarness()
    const error = Object.assign(new Error('guest denied'), { code: 'UNAUTHORIZED' })
    harness.rpc.request.mockImplementation(async (
      method: string,
      params?: Record<string, unknown>,
      options?: RpcCallOptions,
    ): Promise<never> => {
      harness.calls.push({ method, params, options })
      throw error
    })
    const warn = vi.fn()
    const changes = createV4SessionDirectoryChanges(harness.rpc, harness.events, { warn })
    const subscription = changes.subscribe(vi.fn())
    await changes.resume()
    await changes.resume()
    expect(harness.calls.filter(call => call.method === SESSIONS_SUBSCRIBE_METHOD)).toHaveLength(1)
    expect(warn).not.toHaveBeenCalled()
    subscription.close()
    changes.dispose()
    expect(harness.listenerCount('sessions.changed')).toBe(0)
    expect(harness.listenerCount('_state')).toBe(0)
  })
})
