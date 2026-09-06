import { describe, expect, it, vi } from 'vitest'

import { createV4UsageReporting } from './usageReportingV4'

function adapterWith(responses: Record<string, unknown>) {
  const request = vi.fn(async (method: string): Promise<unknown> => {
    if (!(method in responses)) throw new Error(`unexpected method: ${method}`)
    return responses[method]
  })
  return {
    adapter: createV4UsageReporting({
      request: async <T = unknown>(method: string) => await request(method) as T,
      ready: vi.fn(async () => {}),
      supports: vi.fn(() => true),
      markUnsupported: vi.fn(),
    }),
    request,
  }
}

describe('v4 UsageReporting Adapter', () => {
  it('projects legacy status aliases into one canonical session shape', async () => {
    const { adapter } = adapterWith({
      'usage.status': {
        total_sessions: 1,
        total_tokens: 7,
        total_cost_usd: 0.25,
        sessions: [{
          session: 'agent:main:webchat:test',
          input_tokens: 3,
          output_tokens: 4,
          cache_read_tokens: 2,
          cache_write_tokens: 1,
          cost_usd: 0.25,
          cost_source: 'provider_billed',
          context_status: {
            context_tokens: 600,
            context_window_tokens: 1000,
            pressure: 0.6,
            warning_ratio: 0.85,
          },
        }],
      },
    })

    const result = await adapter.status('agent:main:webchat:test')

    expect(result).toEqual({
      totalSessions: 1,
      activeSessions: 0,
      totalInputTokens: 3,
      totalOutputTokens: 4,
      totalTokens: 7,
      totalCostUsd: 0.25,
      totalCacheReadTokens: 2,
      totalCacheWriteTokens: 1,
      sessions: [{
        session: 'agent:main:webchat:test',
        sessionKey: 'agent:main:webchat:test',
        sessionId: '',
        inputTokens: 3,
        outputTokens: 4,
        cacheReadTokens: 2,
        cacheWriteTokens: 1,
        costUsd: 0.25,
        billedCostUsd: null,
        estimatedCostUsd: null,
        estimatedEventCount: null,
        missingCostEntries: null,
        costSource: 'provider_billed',
        costEphemeral: false,
        estimateBasis: '',
        model: '',
        modelBreakdown: [],
        costSourceCounts: {},
        nativeBilledByCurrency: {},
        pendingBillingReceiptCount: 0,
        nativeBillingExpectedReceiptCount: 0,
        nativeBillingMissingConfirmedReceiptCount: 0,
        contextStatus: {
          contextTokens: 600,
          contextWindowTokens: 1000,
          pressure: 0.6,
          warningRatio: 0.85,
        },
      }],
    })
  })

  it('projects usage.cost without exposing its wire breakdown rows', async () => {
    const { adapter } = adapterWith({
      'usage.cost': {
        totalCostUsd: 0.5,
        breakdown: [{
          session_key: 'agent:main:webchat:cost',
          model: 'example/model',
          input_tokens: 8,
          output_tokens: 2,
          cost_usd: 0.5,
          cost_source: 'opensquilla_estimate',
        }],
      },
    })

    const result = await adapter.costBreakdown()

    expect(result.totalCostUsd).toBe(0.5)
    expect(result.sessions).toHaveLength(1)
    expect(result.sessions[0]).toMatchObject({
      sessionKey: 'agent:main:webchat:cost',
      model: 'example/model',
      inputTokens: 8,
      outputTokens: 2,
      costUsd: 0.5,
      costSource: 'opensquilla_estimate',
    })
    expect(result.sessions[0]).not.toHaveProperty('input_tokens')
    expect(result.sessions[0]).not.toHaveProperty('cost_usd')
  })
})
