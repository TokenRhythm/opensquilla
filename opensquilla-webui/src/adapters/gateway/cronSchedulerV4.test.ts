import { describe, expect, it, vi } from 'vitest'
import { createV4CronScheduler } from './cronSchedulerV4'

describe('CronScheduler v4 Adapter', () => {
  it('projects list and run history shapes without leaking wire envelopes', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'cron.list') return [{ id: 'daily', enabled: true }]
      if (method === 'cron.runs') return [{ summary: 'done' }]
      return {}
    })
    const scheduler = createV4CronScheduler(
      { generation: 1, request: request as never, ready: vi.fn(async () => undefined) },
      { subscribe: vi.fn() },
    )

    await expect(scheduler.listJobs()).resolves.toEqual([{ id: 'daily', enabled: true }])
    await expect(scheduler.listRuns('daily', 3)).resolves.toEqual([{ summary: 'done' }])
    expect(request).toHaveBeenLastCalledWith('cron.runs', { id: 'daily', limit: 3 })
  })

  it('owns one remote event lease for all domain subscribers', async () => {
    const handlers = new Map<string, (payload: unknown) => void>()
    const close = vi.fn()
    const request = vi.fn(async (
      method: string,
      _params?: Record<string, unknown>,
      _options?: unknown,
    ) => (
      method === 'cron.subscribe' || method === 'cron.unsubscribe'
        ? { ok: true, topic: 'cron:*' }
        : {}
    ))
    const scheduler = createV4CronScheduler(
      { generation: 1, request: request as never, ready: vi.fn(async () => undefined) },
      {
        subscribe: vi.fn((event: string, handler: (payload: unknown) => void) => {
          handlers.set(event, handler)
          return { close }
        }),
      },
    )
    const first = vi.fn()
    const second = vi.fn()

    const firstLease = scheduler.subscribe(first)
    const secondLease = scheduler.subscribe(second)
    await Promise.resolve()
    expect(request.mock.calls.filter(([method]) => method === 'cron.subscribe')).toHaveLength(1)
    handlers.get('cron.run.finished')?.({ jobId: 'daily', runId: 'run-1', success: true })
    expect(first).toHaveBeenCalledWith({ jobId: 'daily', runId: 'run-1', success: true })
    expect(second).toHaveBeenCalledWith({ jobId: 'daily', runId: 'run-1', success: true })

    firstLease.close()
    expect(close).not.toHaveBeenCalled()
    secondLease.close()
    await vi.waitFor(() => {
      expect(request.mock.calls.filter(([method]) => method === 'cron.unsubscribe'))
        .toHaveLength(1)
    })
    expect(close).toHaveBeenCalledTimes(2)
    expect(request).toHaveBeenCalledWith(
      'cron.unsubscribe',
      {},
      expect.objectContaining({ expectedGeneration: 1 }),
    )
  })

  it('rebinds an active lease once per transport generation', async () => {
    let generation = 1
    const handlers = new Map<string, (payload: unknown) => void>()
    const request = vi.fn(async (
      method: string,
      _params?: Record<string, unknown>,
      _options?: unknown,
    ) => (
      method === 'cron.subscribe' || method === 'cron.unsubscribe'
        ? { ok: true, topic: 'cron:*' }
        : {}
    ))
    const scheduler = createV4CronScheduler(
      {
        get generation() { return generation },
        request: request as never,
        ready: vi.fn(async () => undefined),
      },
      {
        subscribe: vi.fn((event: string, handler: (payload: unknown) => void) => {
          handlers.set(event, handler)
          return { close: vi.fn() }
        }),
      },
    )
    const lease = scheduler.subscribe(vi.fn())
    await vi.waitFor(() => {
      expect(request.mock.calls.filter(([method]) => method === 'cron.subscribe')).toHaveLength(1)
    })

    generation = 2
    handlers.get('_state')?.('disconnected')
    handlers.get('_state')?.('connected')
    await vi.waitFor(() => {
      expect(request.mock.calls.filter(([method]) => method === 'cron.subscribe')).toHaveLength(2)
    })
    expect(request.mock.calls.filter(([method]) => method === 'cron.subscribe')[1]?.[2])
      .toMatchObject({ expectedGeneration: 2 })

    lease.close()
  })
})
