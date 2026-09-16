import { describe, expect, it } from 'vitest'
import { createV4UsageReporting } from './usageReportingV4'

function reporting(response: unknown) {
  return createV4UsageReporting({
    request: async <T = unknown>() => response as T,
  })
}

describe('Usage wire compatibility projection', () => {
  it('uses first usage for startedAt without replacing creation time', async () => {
    const result = await reporting({
      sessions: [{
        session_key: 'active-session',
        first_usage_at_ms: 1200,
        last_usage_at_ms: 2400,
        created_at: 1000,
        started_at: 1100,
        totals: {},
      }],
    }).snapshot('all')
    expect(result.sessions[0]).toMatchObject({
      createdAt: 1000, startedAt: 1200, updatedAt: 2400,
    })
    expect(result.sessions[0]).not.toHaveProperty('first_usage_at_ms')
  })

  it('honors a legacy aggregate token total while keeping an explicit zero', async () => {
    const legacy = await reporting({ totals: { tokens: 42 }, sessions: [] }).status()
    expect(legacy.totalTokens).toBe(42)
    const zero = await reporting({
      totalTokens: 0, totals: { tokens: 42 }, sessions: [],
    }).status()
    expect(zero.totalTokens).toBe(0)
  })

  it('distinguishes unavailable session measurements from recorded zeroes', async () => {
    const result = await reporting({
      sessions: [
        { session: 'missing' },
        { session: 'null', input_tokens: null, cost_usd: null },
        {
          session: 'zero', input_tokens: 0, output_tokens: '0',
          cache_read_tokens: 0, cache_write_tokens: 0, cost_usd: 0,
          billed_cost_usd: 0, estimated_cost_usd: 0, estimated_event_count: 0,
          missing_cost_entries: 0,
        },
      ],
    }).status()
    expect(result.sessions[0]).toMatchObject({
      inputTokens: null, outputTokens: null, cacheReadTokens: null,
      cacheWriteTokens: null, costUsd: null, billedCostUsd: null,
      estimatedCostUsd: null, estimatedEventCount: null, missingCostEntries: null,
    })
    expect(result.sessions[1]).toMatchObject({ inputTokens: null, costUsd: null })
    expect(result.sessions[2]).toMatchObject({
      inputTokens: 0, outputTokens: 0, cacheReadTokens: 0,
      cacheWriteTokens: 0, costUsd: 0, billedCostUsd: 0,
      estimatedCostUsd: 0, estimatedEventCount: 0, missingCostEntries: 0,
    })
    expect(result.totalTokens).toBe(0)
  })

  it('projects fallback cost provenance and identities without retaining wire aliases', async () => {
    const result = await reporting({
      sessions: [
        {
          session_id: 'deleted-session', session_key: null,
          model: 'provider/model', cost_usd: 4,
          cost_source: 'opensquilla_estimate', estimate_basis: 'cache_blind',
        },
        { key: 'active-session', cost_source: 'provider_billed', cost_usd: 5 },
      ],
    }).snapshot('all')
    expect(result.sessions[0]).toMatchObject({
      session: 'deleted-session', sessionId: 'deleted-session', sessionKey: '',
      costUsd: 4, costSource: 'opensquilla_estimate', estimateBasis: 'cache_blind',
    })
    expect(result.sessions[1]).toMatchObject({
      sessionKey: 'active-session', costSource: 'provider_billed', costUsd: 5,
    })
    expect(result.sessions[0]).not.toHaveProperty('estimate_basis')
    expect(result.sessions[0]).not.toHaveProperty('session_id')
  })

  it('preserves ledger zero defaults for absent, null and recorded-zero totals', async () => {
    const result = await reporting({
      sessions: [
        { session_key: 'absent' },
        { session_key: 'empty', totals: {} },
        { session_key: 'null', totals: { inputTokens: null, costUsd: null } },
        {
          session_key: 'zero',
          totals: {
            inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0,
            costUsd: 0, billedCostUsd: 0, estimatedCostUsd: 0,
            estimatedEventCount: 0, missingCostEntries: 0,
          },
        },
      ],
    }).snapshot('all')
    expect(result.source).toBe('usage_ledger')
    expect(result.sessions).toHaveLength(4)
    for (const session of result.sessions) {
      expect(session).toMatchObject({
        inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0,
        costUsd: 0, billedCostUsd: 0, estimatedCostUsd: 0,
        estimatedEventCount: 0, missingCostEntries: 0,
      })
    }
  })

  it('does not replace legacy row measurements with nested ledger totals', async () => {
    const result = await reporting({
      sessions: [
        { session: 'missing', totals: { inputTokens: 9, costUsd: 4 } },
        { session: 'null', input_tokens: null, cost_usd: null, totals: {} },
        {
          session: 'zero', input_tokens: 0, cost_usd: 0,
          totals: { inputTokens: 9, costUsd: 4 },
        },
      ],
    }).status()
    expect(result.sessions[0]).toMatchObject({ inputTokens: null, costUsd: null })
    expect(result.sessions[1]).toMatchObject({ inputTokens: null, costUsd: null })
    expect(result.sessions[2]).toMatchObject({ inputTokens: 0, costUsd: 0 })
    expect(result.totalTokens).toBe(0)
    expect(result.totalCostUsd).toBe(0)
  })
})
