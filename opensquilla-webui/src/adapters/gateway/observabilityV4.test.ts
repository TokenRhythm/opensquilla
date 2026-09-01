import { describe, expect, it, vi } from 'vitest'
import { createV4Observability } from './observabilityV4'

function transport(call: ReturnType<typeof vi.fn>, supports = true) {
  const markUnsupported = vi.fn()
  return {
    value: {
      request: call,
      ready: vi.fn(async () => {}),
      supports: vi.fn(() => supports),
      markUnsupported,
    },
    markUnsupported,
  }
}

describe('v4 Observability Adapter', () => {
  it('owns Gateway and self-learning status methods', async () => {
    const call = vi.fn(async (method: string) => ({ method, ready: true }))
    const adapter = createV4Observability(
      transport(call).value as Parameters<typeof createV4Observability>[0],
      { requestJson: vi.fn(), requestBinary: vi.fn() },
    )

    await expect(adapter.gatewayStatus()).resolves.toMatchObject({ ready: true })
    await expect(adapter.selfLearningStatus()).resolves.toMatchObject({ ready: true })
    expect(call).toHaveBeenNthCalledWith(1, 'status', {}, expect.any(Object))
    expect(call).toHaveBeenNthCalledWith(2, 'router.selflearning.status', {}, expect.any(Object))
  })

  it('projects usage.query and preserves the semantic range request', async () => {
    const call = vi.fn(async () => ({
      schemaVersion: 1,
      source: 'usage_ledger',
      range: { preset: 'last_7_calendar_days', timezone: 'Asia/Shanghai' },
      totals: { inputTokens: 2, outputTokens: 3, costNanos: 5_000_000 },
      coverage: { status: 'complete' },
    }))
    const rpc = transport(call)
    const adapter = createV4Observability(
      rpc.value as Parameters<typeof createV4Observability>[0], {
      requestJson: vi.fn(),
      requestBinary: vi.fn(),
      },
    )

    const snapshot = await adapter.usage('7', { timezone: 'Asia/Shanghai' })

    expect(call).toHaveBeenCalledWith('usage.query', {
      schemaVersion: 1,
      range: { preset: 'last_7_calendar_days' },
      timezone: 'Asia/Shanghai',
      include: { days: true, models: true, sessions: true },
    }, expect.any(Object))
    expect(snapshot.totals).toMatchObject({ input: 2, output: 3, cost: 0.005 })
  })

  it('falls back only when the optional query capability is absent', async () => {
    const missing = Object.assign(new Error('Method not found'), { code: 'METHOD_NOT_FOUND' })
    const call = vi.fn(async (method: string) => {
      if (method === 'usage.query') throw missing
      return { totalSessions: 4, totalTokens: 9, totalCostUsd: 0.25, sessions: [] }
    })
    const rpc = transport(call)
    const adapter = createV4Observability(
      rpc.value as Parameters<typeof createV4Observability>[0], {
      requestJson: vi.fn(),
      requestBinary: vi.fn(),
      },
    )

    const snapshot = await adapter.usage('all', { timezone: 'UTC' })

    expect(rpc.markUnsupported).toHaveBeenCalledWith('usage.query')
    expect(snapshot.mode).toBe('session_approximation')
    expect(snapshot.totals).toMatchObject({ sessions: 4, totalTokens: 9, cost: 0.25 })
  })

  it('owns readiness, log, update, and support-bundle transport details', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'doctor.status') return { status: 'ready', ready: true }
      if (method === 'logs.status') return { gateway_file_log: { enabled: true } }
      if (method === 'logs.tail') return { lines: ['ready'], cursor: 4 }
      throw new Error(`unexpected method ${method}`)
    })
    const http = {
      requestJson: vi.fn(async () => ({
        current: '1.0.0',
        latest: '1.1.0',
        available: true,
        url: 'https://example.test/release',
        checkedAt: '2026-09-01T00:00:00Z',
      })),
      requestBinary: vi.fn(async () => ({
        metadata: { filename: 'support.zip' },
        blob: async () => new Blob(['bundle']),
      })),
    }
    const adapter = createV4Observability(
      transport(call).value as Parameters<typeof createV4Observability>[0],
      http as Parameters<typeof createV4Observability>[1],
    )

    await expect(adapter.readiness({ deep: true })).resolves.toMatchObject({ ready: true })
    await expect(adapter.logStatus()).resolves.toMatchObject({ gateway_file_log: { enabled: true } })
    await expect(adapter.tailLogs({ cursor: 0 })).resolves.toEqual({ entries: ['ready'], cursor: 4 })
    await expect(adapter.updateNotice()).resolves.toMatchObject({ latest: '1.1.0' })
    const bundle = await adapter.downloadSupportBundle({ includeContent: false })

    expect(http.requestJson).toHaveBeenCalledWith('/api/system/update', expect.objectContaining({ method: 'GET' }))
    expect(http.requestBinary).toHaveBeenCalledWith('/api/v1/diagnostics/bundle', expect.objectContaining({
      method: 'POST',
      json: { include_content: false, days: 1 },
    }))
    expect(bundle.filename).toBe('support.zip')
  })
})
