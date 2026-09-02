import { describe, expect, it, vi } from 'vitest'
import { createV4CronScheduler } from './cronSchedulerV4'

describe('CronScheduler v4 Adapter', () => {
  it('projects list and run history shapes without leaking wire envelopes', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'cron.list') return { jobs: [{ id: 'daily', enabled: true }] }
      if (method === 'cron.runs') return { runs: [{ summary: 'done' }] }
      return {}
    })
    const scheduler = createV4CronScheduler(
      { request: request as never, ready: vi.fn(async () => undefined) },
      { subscribe: vi.fn() },
    )

    await expect(scheduler.listJobs()).resolves.toEqual([{ id: 'daily', enabled: true }])
    await expect(scheduler.listRuns('daily', 3)).resolves.toEqual([{ summary: 'done' }])
    expect(request).toHaveBeenLastCalledWith('cron.runs', { id: 'daily', limit: 3 })
  })

  it('owns one remote event lease for all domain subscribers', async () => {
    let emit: (payload: unknown) => void = () => undefined
    const close = vi.fn()
    const request = vi.fn(async (_method: string) => ({}))
    const scheduler = createV4CronScheduler(
      { request: request as never, ready: vi.fn(async () => undefined) },
      { subscribe: vi.fn((_event: string, handler: (payload: unknown) => void) => { emit = handler; return { close } }) },
    )
    const first = vi.fn()
    const second = vi.fn()

    const firstLease = scheduler.subscribe(first)
    const secondLease = scheduler.subscribe(second)
    await Promise.resolve()
    expect(request.mock.calls.filter(([method]) => method === 'cron.subscribe')).toHaveLength(1)
    emit({ runId: 'run-1', success: true })
    expect(first).toHaveBeenCalledWith({ runId: 'run-1', success: true })
    expect(second).toHaveBeenCalledWith({ runId: 'run-1', success: true })

    firstLease.close()
    expect(close).not.toHaveBeenCalled()
    secondLease.close()
    await Promise.resolve()
    expect(close).toHaveBeenCalledOnce()
    expect(request).toHaveBeenCalledWith('cron.unsubscribe')
  })
})
