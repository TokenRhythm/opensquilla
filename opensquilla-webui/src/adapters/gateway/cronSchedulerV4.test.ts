import { describe, expect, it, vi } from 'vitest'
import { createV4CronScheduler } from './cronSchedulerV4'
import { CronReadUnavailableError } from '@/modules/cronScheduler'

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
    expect(request).toHaveBeenLastCalledWith('cron.runs', { id: 'daily', limit: 3 }, expect.objectContaining({
      recoveryClass: 'safe-read', expectedGeneration: 1, timeoutMs: 10_000,
      timeoutAction: 'reject', abortAction: 'reject',
    }))
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

  it('classifies explicit capacity once and leaves retries to the read owner', async () => {
    const request = vi.fn().mockRejectedValue(Object.assign(new Error('Connection request queue is full'), {
      code: 'UNAVAILABLE', retryable: true, retry_after_ms: 2000,
    }))
    const scheduler = createV4CronScheduler(
      { generation: 7, request, ready: vi.fn(async () => undefined) }, { subscribe: vi.fn() },
    )
    await expect(scheduler.listJobs()).rejects.toMatchObject({ name: 'CronReadUnavailableError', retryAfterMs: 2000 })
    expect(request).toHaveBeenCalledTimes(1)
  })

  it.each([
    Object.assign(new Error('Forbidden'), { code: 'FORBIDDEN', retryable: true }),
    Object.assign(new Error('Scheduler unavailable'), { code: 'UNAVAILABLE', retryable: true }),
    new Error('Failed to decode data'),
  ])('does not classify a permanent or unrelated failure as capacity: %s', async failure => {
    const scheduler = createV4CronScheduler(
      { generation: 1, request: vi.fn().mockRejectedValue(failure), ready: vi.fn(async () => undefined) },
      { subscribe: vi.fn() },
    )
    await expect(scheduler.listJobs()).rejects.toBe(failure)
    expect(failure).not.toBeInstanceOf(CronReadUnavailableError)
  })

  it('rejects invalid data without retrying', async () => {
    const request = vi.fn().mockResolvedValue([{ id: null }])
    const scheduler = createV4CronScheduler(
      { generation: 1, request, ready: vi.fn(async () => undefined) }, { subscribe: vi.fn() },
    )
    await expect(scheduler.listJobs()).rejects.toThrow('cron.list returned an invalid response')
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('cancels before send and rejects a late result from a replaced generation', async () => {
    const controller = new AbortController()
    let generation = 1
    const request = vi.fn(async () => { generation = 2; return [] })
    const scheduler = createV4CronScheduler(
      { get generation() { return generation }, request: request as never, ready: vi.fn(async () => undefined) },
      { subscribe: vi.fn() },
    )
    controller.abort()
    await expect(scheduler.listJobs({ signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
    expect(request).not.toHaveBeenCalled()
    await expect(scheduler.listJobs()).rejects.toThrow('Cron read connection changed')
    expect(request).toHaveBeenCalledTimes(1)
  })
})
