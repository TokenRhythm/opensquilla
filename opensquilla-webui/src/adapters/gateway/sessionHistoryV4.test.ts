import { describe, expect, it, vi } from 'vitest'
import { CHAT_HISTORY_METHOD, type ChatHistoryResult } from '@/contracts/generated/v4/chatHistory'
import type { RpcCallOptions } from '@/lib/rpc'
import { SessionReadSessionMissingError } from '@/modules/sessionReadLifecycle'
import {
  requestV4SessionHistory,
  type SessionHistoryV4Transport,
} from './sessionHistoryV4'

async function readRoutingSnapshot(
  routerDecision: Record<string, unknown>,
  field: 'router_decision' | 'routerDecision' = 'router_decision',
) {
  const result: ChatHistoryResult = {
    messages: [{ id: 'router-1', role: 'router', text: '', [field]: routerDecision }],
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
  const transport: SessionHistoryV4Transport = {
    async request<T>(): Promise<T> { return result as T },
  }
  const page = await requestV4SessionHistory(
    transport,
    'session-1',
    { direction: 'latest', limit: 100, signal: new AbortController().signal },
    {
      includeSummaries: true,
      policy: { concurrentHistoryReads: () => true },
      contractError: message => new Error(message),
    },
  )
  return page.messages[0]?.routerDecision
}

describe('v4 SessionHistory Adapter', () => {
  it.each([
    {
      name: 'canonical routing facts',
      wire: { accepted_routing_mode: 'ensemble', source: 'squilla_router' },
      expected: { accepted_routing_mode: 'ensemble', source: 'squilla_router' },
    },
    {
      name: 'legacy routing facts',
      wire: { acceptedRoutingMode: 'llm_ensemble', routing_source: 'llm_ensemble' },
      expected: { accepted_routing_mode: 'llm_ensemble', source: 'llm_ensemble' },
    },
    {
      name: 'conflicting nonempty spellings',
      wire: {
        accepted_routing_mode: 'direct', acceptedRoutingMode: 'ensemble',
        source: 'squilla_router', routing_source: 'llm_ensemble',
      },
      expected: { accepted_routing_mode: 'direct', source: 'squilla_router' },
    },
    {
      name: 'empty canonical spellings',
      wire: {
        accepted_routing_mode: '', acceptedRoutingMode: 'ensemble',
        source: '', routing_source: 'llm_ensemble',
      },
      expected: { accepted_routing_mode: 'ensemble', source: 'llm_ensemble' },
    },
    {
      name: 'null canonical spellings',
      wire: {
        accepted_routing_mode: null, acceptedRoutingMode: 'ensemble',
        source: null, routing_source: 'llm_ensemble',
      },
      expected: { accepted_routing_mode: 'ensemble', source: 'llm_ensemble' },
    },
  ])('projects $name into an envelope-free routing snapshot', async ({ wire, expected }) => {
    const snapshot = await readRoutingSnapshot({
      tier: 'c1', model: 'provider/model', ...wire,
      key: 'other-session', turn_id: 'other-turn', stream_seq: 71,
      future_protocol_field: { diagnostic: true },
    })
    expect(snapshot).toEqual({ tier: 'c1', model: 'provider/model', ...expected })
  })

  it.each(['router_decision', 'routerDecision'] as const)(
    'deeply freezes named routing content from %s without changing nested keys',
    async field => {
      const decision = { selected_model: { model_id: 'provider/model', available: false } }
      const tiers = { c1: { model_id: 'provider/canonical' } }
      const snapshot = await readRoutingSnapshot({
        tier: 'c1', model: 'provider/model',
        baseline_model: '', baselineModel: 'provider/baseline',
        decision,
        router_tier_snapshot: tiers,
        routerTierSnapshot: { c1: { model_id: 'provider/legacy' } },
      }, field)

      expect(snapshot).toEqual({
        tier: 'c1', model: 'provider/model', baseline_model: 'provider/baseline',
        decision: { selected_model: { model_id: 'provider/model', available: false } },
        router_tier_snapshot: { c1: { model_id: 'provider/canonical' } },
      })
      expect(Object.isFrozen(snapshot)).toBe(true)
      expect(Object.isFrozen(snapshot?.decision)).toBe(true)
      expect(Object.isFrozen(snapshot?.router_tier_snapshot)).toBe(true)
      expect(Object.isFrozen(Object.values(snapshot?.decision ?? {})[0])).toBe(true)
      expect(Object.isFrozen(Object.values(snapshot?.router_tier_snapshot ?? {})[0])).toBe(true)
      expect(snapshot?.decision).not.toBe(decision)
      expect(snapshot?.router_tier_snapshot).not.toBe(tiers)
      decision.selected_model.model_id = 'provider/later'
      tiers.c1.model_id = 'provider/later'
      expect(snapshot?.decision).toEqual({
        selected_model: { model_id: 'provider/model', available: false },
      })
      expect(snapshot?.router_tier_snapshot).toEqual({ c1: { model_id: 'provider/canonical' } })
    },
  )

  it('uses the legacy tier snapshot when the canonical snapshot is null', async () => {
    expect(await readRoutingSnapshot({
      tier: 'c1', router_tier_snapshot: null,
      routerTierSnapshot: { c1: { model_id: 'provider/legacy' } },
    })).toEqual({ tier: 'c1', router_tier_snapshot: { c1: { model_id: 'provider/legacy' } } })
  })

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
