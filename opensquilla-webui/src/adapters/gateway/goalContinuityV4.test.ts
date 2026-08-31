import { describe, expect, it, vi } from 'vitest'
import type { RpcCallOptions, RpcEventHandler } from '@/lib/rpc'
import { createV4GoalContinuity } from './goalContinuityV4'

type TestRpc = {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
}

function rpcSource(response: unknown = {
  accepted: true,
  sessionKey: 'agent:demo',
  sessionId: 's1',
  epoch: 1,
  goal: {
    status: 'active',
    goalId: 'g1',
    sessionKey: 'agent:demo',
    sessionId: 's1',
    epoch: 1,
    objective: 'ship',
    stateRevision: 3,
    objectiveRevision: 1,
    progressRevision: 2,
  },
  continuityToken: 'token-1',
}) {
  const requests: Array<{ method: string; params?: Record<string, unknown> }> = []
  const request: TestRpc['request'] = async <T>(
    method: string,
    params?: Record<string, unknown>,
  ): Promise<T> => {
    requests.push({ method, params })
    return response as T
  }
  return {
    requests,
    request: vi.fn(request) as TestRpc['request'],
  }
}

function eventSource() {
  let handler: RpcEventHandler | undefined
  const close = vi.fn()
  const source = {
    subscribe: vi.fn((_event: string, next: RpcEventHandler) => {
      handler = next
      return { close }
    }),
  }
  return { source, close, emit: (payload: unknown, meta?: unknown) => handler?.(payload, meta) }
}

const baseGoal = {
  status: 'active',
  goalId: 'g1',
  sessionKey: 'agent:demo',
  sessionId: 's1',
  epoch: 1,
  stateRevision: 3,
  progressRevision: 2,
  objectiveRevision: 1,
}

describe('createV4GoalContinuity', () => {
  it('reattaches through the generated method and projects the accepted lease', async () => {
    const rpc = rpcSource()
    const continuity = createV4GoalContinuity(rpc)

    await expect(continuity.reattach({
      sessionKey: 'agent:demo',
      sessionId: 's1',
      epoch: 1,
      expectedGoalId: 'g1',
      continuityToken: 'token-1',
      sourceKind: 'web',
    })).resolves.toMatchObject({
      accepted: true,
      sessionKey: 'agent:demo',
      goal: { goalId: 'g1', stateRevision: 3 },
      continuityToken: 'token-1',
    })
    expect(rpc.requests).toEqual([{
      method: 'goals.reattach',
      params: {
        sessionKey: 'agent:demo',
        sessionId: 's1',
        epoch: 1,
        expectedGoalId: 'g1',
        continuityToken: 'token-1',
        sourceKind: 'web',
      },
    }])
  })

  it('allows tokenless takeover and keeps legacy result aliases compatible', async () => {
    const rpc = rpcSource({
      accepted: true,
      session_key: 'agent:demo',
      session_id: 's1',
      epoch: 1,
      goal: {
        status: 'active',
        goal_id: 'g1',
        session_key: 'agent:demo',
        session_id: 's1',
        epoch: 1,
        objective: 'ship',
        state_revision: 3,
        objective_revision: 1,
        progress_revision: 2,
      },
      continuity_token: 'token-2',
    })
    const continuity = createV4GoalContinuity(rpc)
    await expect(continuity.reattach({
      sessionKey: 'agent:demo',
      sessionId: 's1',
      epoch: 1,
      expectedGoalId: 'g1',
      takeover: true,
    })).resolves.toMatchObject({ continuityToken: 'token-2' })
    expect(rpc.requests[0]?.params).toEqual({
      sessionKey: 'agent:demo',
      sessionId: 's1',
      epoch: 1,
      expectedGoalId: 'g1',
      takeover: true,
    })
  })

  it('rejects a sparse or cross-session lease response at the adapter fence', async () => {
    const rpc = rpcSource({
      accepted: true,
      sessionKey: 'agent:other',
      sessionId: 's2',
      epoch: 2,
      goal: { status: 'active' },
      continuityToken: 'token-2',
    })
    const continuity = createV4GoalContinuity(rpc)
    await expect(continuity.reattach({
      sessionKey: 'agent:demo',
      sessionId: 's1',
      epoch: 1,
      expectedGoalId: 'g1',
      continuityToken: 'token-1',
    })).rejects.toMatchObject({ code: 'invalid' })
  })

  it('reports a complete response from another session as a fenced conflict', async () => {
    const rpc = rpcSource({
      accepted: true,
      sessionKey: 'agent:other',
      sessionId: 's2',
      epoch: 2,
      goal: {
        ...baseGoal,
        goalId: 'g1',
        sessionKey: 'agent:other',
        sessionId: 's2',
        epoch: 2,
        objective: 'ship',
      },
      continuityToken: 'token-2',
    })
    const continuity = createV4GoalContinuity(rpc)
    await expect(continuity.reattach({
      sessionKey: 'agent:demo',
      sessionId: 's1',
      epoch: 1,
      expectedGoalId: 'g1',
      continuityToken: 'token-1',
    })).rejects.toMatchObject({ code: 'conflict', retryable: true })
  })

  it('decodes nested canonical, unversioned legacy, flat legacy, and clear events', () => {
    const rpc = rpcSource()
    const events = eventSource()
    const continuity = createV4GoalContinuity(rpc, events.source)
    const received: unknown[] = []
    continuity.subscribe(event => received.push(event))

    events.emit({
      schema_version: 1,
      session_key: 'agent:demo',
      session_id: 's1',
      epoch: 1,
      stream_seq: 7,
      stream_generation: 'generation-a',
      event_type: 'updated',
      goal: { ...baseGoal, stateRevision: 4 },
    })
    events.emit({ ...baseGoal, event_type: 'updated', stream_seq: 8 })
    events.emit({
      sessionKey: 'agent:demo',
      sessionId: 's1',
      epoch: 1,
      streamSeq: 9,
      eventType: 'cleared',
      goal: null,
    })

    expect(received).toHaveLength(3)
    expect(received[0]).toMatchObject({
      eventType: 'updated',
      sessionKey: 'agent:demo',
      streamSeq: 7,
      streamGeneration: 'generation-a',
      goal: { goalId: 'g1', stateRevision: 4 },
    })
    expect(received[1]).toMatchObject({ eventType: 'updated', goal: { goalId: 'g1' } })
    expect(received[2]).toMatchObject({ eventType: 'cleared', goal: null, streamSeq: 9 })
  })

  it('projects sessionEpoch aliases at the adapter boundary', async () => {
    const rpc = rpcSource({
      accepted: true,
      session_key: 'agent:demo',
      session_id: 's1',
      epoch: 1,
      goal: {
        status: 'active',
        goal_id: 'g1',
        session_key: 'agent:demo',
        session_id: 's1',
        session_epoch: 1,
        goal_text: 'ship',
        state_revision: 3,
        objective_revision: 1,
        progress_revision: 2,
      },
      continuity_token: 'token-2',
    })
    const continuity = createV4GoalContinuity(rpc)
    await expect(continuity.reattach({
      sessionKey: 'agent:demo',
      sessionId: 's1',
      epoch: 1,
      expectedGoalId: 'g1',
      continuityToken: 'token-1',
    })).resolves.toMatchObject({ goal: { epoch: 1, objective: 'ship' } })
  })

  it('drops a clear event without a complete session fence', () => {
    const rpc = rpcSource()
    const events = eventSource()
    const warn = vi.fn()
    const continuity = createV4GoalContinuity(rpc, events.source, { warn })
    const listener = vi.fn()
    continuity.subscribe(listener)

    events.emit({ eventType: 'cleared', goal: null })

    expect(listener).not.toHaveBeenCalled()
    expect(warn).toHaveBeenCalledOnce()
  })

  it('drops malformed events and keeps local close separate from the physical source', () => {
    const rpc = rpcSource()
    const events = eventSource()
    const warn = vi.fn()
    const continuity = createV4GoalContinuity(rpc, events.source, { warn })
    const listener = vi.fn()
    const subscription = continuity.subscribe(listener)

    events.emit({ session_key: 'one', sessionKey: 'two', goal: baseGoal })
    expect(listener).not.toHaveBeenCalled()
    expect(warn).toHaveBeenCalledOnce()
    subscription.close()
    expect(events.close).not.toHaveBeenCalled()
    continuity.dispose()
    expect(events.close).toHaveBeenCalledOnce()
  })

  it('drops a nested Goal whose identity disagrees with the event envelope', () => {
    const rpc = rpcSource()
    const events = eventSource()
    const warn = vi.fn()
    const continuity = createV4GoalContinuity(rpc, events.source, { warn })
    const listener = vi.fn()
    continuity.subscribe(listener)

    events.emit({
      sessionKey: 'agent:demo',
      sessionId: 's1',
      epoch: 1,
      eventType: 'updated',
      goal: {
        ...baseGoal,
        sessionId: 's2',
        epoch: 2,
      },
    })

    expect(listener).not.toHaveBeenCalled()
    expect(warn).toHaveBeenCalledOnce()
  })

  it('drops a nested Goal with conflicting identity aliases', () => {
    const rpc = rpcSource()
    const events = eventSource()
    const warn = vi.fn()
    const continuity = createV4GoalContinuity(rpc, events.source, { warn })
    const listener = vi.fn()
    continuity.subscribe(listener)

    events.emit({
      sessionKey: 'agent:demo',
      sessionId: 's1',
      epoch: 1,
      eventType: 'updated',
      goal: {
        ...baseGoal,
        sessionKey: 'agent:demo',
        session_key: 'agent:other',
      },
    })

    expect(listener).not.toHaveBeenCalled()
    expect(warn).toHaveBeenCalledOnce()
  })

  it('keeps duplicate listener registrations independently closable', () => {
    const rpc = rpcSource()
    const events = eventSource()
    const continuity = createV4GoalContinuity(rpc, events.source)
    const listener = vi.fn()
    const first = continuity.subscribe(listener)
    const second = continuity.subscribe(listener)
    first.close()
    events.emit({ ...baseGoal, event_type: 'updated', stream_seq: 1 })
    expect(listener).toHaveBeenCalledOnce()
    second.close()
    events.emit({ ...baseGoal, event_type: 'updated', stream_seq: 2 })
    expect(listener).toHaveBeenCalledOnce()
  })
})
