// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, KeepAlive, nextTick, ref } from 'vue'
import { useCronJobs } from './useCronJobs'
import { CronReadUnavailableError, type CronRunFinished, type CronScheduler } from '@/modules/cronScheduler'
import type { CronJob } from '@/types/cron'

vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: vi.fn() }) }))

const apps: Array<ReturnType<typeof createApp>> = []
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); await nextTick() }
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function mount(listJobs = vi.fn().mockResolvedValue([{ id: 'old' }])) {
  const visible = ref(true)
  let finished!: (event: CronRunFinished) => void
  let state!: ReturnType<typeof useCronJobs>
  const runNow = vi.fn().mockResolvedValue({})
  const scheduler = { listJobs, runNow, setEnabled: vi.fn().mockResolvedValue(undefined), remove: vi.fn().mockResolvedValue(undefined), subscribe: vi.fn((listener) => {
    finished = listener
    return { close: vi.fn() }
  }) } as unknown as CronScheduler
  const child = defineComponent({ setup() { state = useCronJobs(scheduler); return () => h('div') } })
  const app = createApp({ setup: () => () => h(KeepAlive, null, { default: () => visible.value ? h(child) : null }) })
  app.mount(document.createElement('div'))
  apps.push(app)
  return { get state() { return state }, visible, listJobs, runNow, finish: () => finished({ jobId: 'old' }) }
}
beforeEach(() => vi.useFakeTimers())
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.useRealTimers() })

describe('Cron list read ownership', () => {
  it('coalesces refresh bursts and retains one follow-up invalidation during a read', async () => {
    const first = deferred<CronJob[]>()
    const list = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue([{ id: 'new' }])
    const view = mount(list)
    await flush()
    const one = view.state.loadData(), two = view.state.loadData()
    expect(one).toBe(two)
    for (let i = 0; i < 20; i++) view.finish()
    expect(list).toHaveBeenCalledTimes(1)
    first.resolve([{ id: 'old' }])
    await one; await flush()
    expect(list).toHaveBeenCalledTimes(2)
    expect(view.state.jobs.value[0]?.id).toBe('new')
    await vi.advanceTimersByTimeAsync(750)
    expect(list).toHaveBeenCalledTimes(2)
  })

  it('retains data and automatically retries only one capacity read at a time', async () => {
    const view = mount()
    await flush()
    view.listJobs.mockRejectedValue(new CronReadUnavailableError('queue full'))
    await view.state.loadData()
    expect(view.state.jobs.value[0]?.id).toBe('old')
    expect(view.state.error.value).toBeNull()
    expect(view.state.waitingForCapacity.value).toBe(true)
    expect(view.state.loading.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1000)
    expect(view.listJobs).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(1999)
    expect(view.listJobs).toHaveBeenCalledTimes(3)
    view.listJobs.mockResolvedValue([{ id: 'recovered' }])
    await vi.advanceTimersByTimeAsync(1)
    expect(view.state.jobs.value[0]?.id).toBe('recovered')
    expect(view.state.waitingForCapacity.value).toBe(false)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(view.listJobs).toHaveBeenCalledTimes(4)
  })

  it('manual retry consumes the pending timer and permanent errors stop retrying', async () => {
    const view = mount(vi.fn().mockRejectedValue(new CronReadUnavailableError('busy')))
    await flush()
    view.listJobs.mockRejectedValue(new Error('Forbidden'))
    await view.state.loadData()
    expect(view.state.error.value).toContain('Forbidden')
    expect(view.state.waitingForCapacity.value).toBe(false)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(view.listJobs).toHaveBeenCalledTimes(2)
  })

  it.each(['success', 'failure'])('ignores old %s after deactivation and reactivation', async outcome => {
    const first = deferred<CronJob[]>()
    const list = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue([{ id: 'new' }])
    const view = mount(list)
    await flush()
    const oldSignal = list.mock.calls[0]![0].signal as AbortSignal
    view.visible.value = false; await flush()
    expect(oldSignal.aborted).toBe(true)
    view.visible.value = true; await flush()
    if (outcome === 'success') first.resolve([{ id: 'old' }])
    else first.reject(new Error('Old error'))
    await flush()
    expect(view.state.jobs.value[0]?.id).toBe('new')
    expect(view.state.error.value).toBeNull()
    expect(view.state.loading.value).toBe(false)
  })

  it('stops capacity retries while cached off-screen and resumes on activation', async () => {
    const view = mount(vi.fn().mockRejectedValue(new CronReadUnavailableError('busy')))
    await flush()
    view.visible.value = false; await flush()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(view.listJobs).toHaveBeenCalledTimes(1)
    view.listJobs.mockResolvedValue([{ id: 'back' }])
    view.visible.value = true; await flush()
    expect(view.listJobs).toHaveBeenCalledTimes(2)
    expect(view.state.jobs.value[0]?.id).toBe('back')
  })

  it('does not replay a failed runNow', async () => {
    const view = mount()
    await flush()
    view.runNow.mockRejectedValue(new CronReadUnavailableError('capacity'))
    await view.state.runJob('old')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(view.runNow).toHaveBeenCalledTimes(1)
  })

  it.each(['toggle', 'remove', 'saved'])('refreshes once after a %s invalidates an in-flight snapshot', async mutation => {
    const first = deferred<CronJob[]>()
    const list = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue([{ id: 'after-mutation' }])
    const view = mount(list)
    await flush()
    if (mutation === 'toggle') await view.state.toggleJob({ id: 'old', enabled: true })
    else if (mutation === 'remove') await view.state.removeJob('old')
    else void view.state.loadData()
    expect(list).toHaveBeenCalledTimes(1)
    first.resolve([{ id: 'before-mutation' }]); await flush()
    expect(list).toHaveBeenCalledTimes(2)
    expect(view.state.jobs.value[0]?.id).toBe('after-mutation')
  })

  it('does not let terminal event bursts bypass capacity backoff', async () => {
    const view = mount(vi.fn().mockRejectedValue(new CronReadUnavailableError('busy')))
    await flush()
    for (let i = 0; i < 20; i++) view.finish()
    await vi.advanceTimersByTimeAsync(999)
    expect(view.listJobs).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(view.listJobs).toHaveBeenCalledTimes(2)
  })
})
