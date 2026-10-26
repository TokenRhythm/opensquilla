// @vitest-environment happy-dom
import { createApp, defineComponent, h, KeepAlive, nextTick, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import type { CronScheduler } from '@/modules/cronScheduler'
import type { CronJob } from '@/types/cron'
import { useCronJobs } from './useCronJobs'

const { pushToast } = vi.hoisted(() => ({ pushToast: vi.fn() }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast }) }))

const cleanups: Array<() => void> = []

beforeEach(() => {
  vi.useFakeTimers()
  pushToast.mockReset()
})

afterEach(() => {
  cleanups.splice(0).forEach(cleanup => cleanup())
  vi.useRealTimers()
})

function deferred() {
  let resolve!: (jobs: readonly CronJob[]) => void
  let reject!: (error: Error) => void
  const promise = new Promise<readonly CronJob[]>((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, resolve, reject }
}

function createScheduler() {
  let listener: Parameters<CronScheduler['subscribe']>[0] | undefined
  const close = vi.fn()
  const scheduler = {
    listJobs: vi.fn<CronScheduler['listJobs']>().mockResolvedValue([]),
    runNow: vi.fn<CronScheduler['runNow']>().mockResolvedValue({}),
    saveJob: vi.fn<CronScheduler['saveJob']>().mockResolvedValue(),
    setEnabled: vi.fn<CronScheduler['setEnabled']>().mockResolvedValue(),
    remove: vi.fn<CronScheduler['remove']>().mockResolvedValue(),
    listRuns: vi.fn<CronScheduler['listRuns']>().mockResolvedValue([]),
    subscribe: vi.fn<CronScheduler['subscribe']>().mockImplementation(callback => {
      listener = callback
      return { close }
    }),
  } satisfies CronScheduler
  return { scheduler, close, finishRun: () => listener?.({ jobId: 'job-a' }) }
}

function mount(scheduler: CronScheduler, keptAlive = false) {
  let state!: ReturnType<typeof useCronJobs>
  const visible = ref(true)
  const View = defineComponent({ setup() {
    state = useCronJobs(scheduler)
    return () => null
  } })
  const app = createApp({ render: () => keptAlive
    ? h(KeepAlive, null, { default: () => visible.value ? h(View) : null })
    : h(View) })
  app.mount(document.createElement('div'))
  let mounted = true
  const unmount = () => {
    if (mounted) { mounted = false; app.unmount() }
  }
  cleanups.push(unmount)
  return { state, visible, unmount }
}

async function settle() {
  for (let i = 0; i < 12; i++) await Promise.resolve()
  await nextTick()
}

// Single-flight ownership, capacity backoff, and activation races are covered
// by useCronJobs.lifecycle.test.ts. These cases retain feedback-specific guards.
describe('cron refresh feedback', () => {
  it('shows toggled and deleted jobs through refreshed data without redundant success toasts', async () => {
    const { scheduler } = createScheduler()
    const job = { id: 'saved', enabled: true }
    scheduler.listJobs.mockResolvedValueOnce([job])
      .mockResolvedValueOnce([{ ...job, enabled: false }])
      .mockResolvedValueOnce([])
    const { state } = mount(scheduler)
    await state.loadData()

    await state.toggleJob(job)
    await settle()
    expect(scheduler.setEnabled).toHaveBeenCalledExactlyOnceWith('saved', false)
    expect(state.jobs.value[0]?.enabled).toBe(false)

    await state.removeJob('saved')
    await settle()
    expect(scheduler.remove).toHaveBeenCalledExactlyOnceWith('saved')
    expect(state.jobs.value).toEqual([])
    expect(scheduler.listJobs).toHaveBeenCalledTimes(3)
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('keeps automatic refresh errors visible without discarding last-good data', async () => {
    const { scheduler, finishRun } = createScheduler()
    scheduler.listJobs.mockResolvedValueOnce([{ id: 'saved' }])
      .mockRejectedValue(new Error('offline'))
    const { state } = mount(scheduler, true)
    await settle()
    finishRun()
    await settle()
    expect(state.jobs.value.map(job => job.id)).toEqual(['saved'])
    expect(state.error.value).toBe(`${i18n.global.t('cronSkills.jobs.errLoad')}: offline`)
    await vi.advanceTimersByTimeAsync(750)
    expect(state.error.value).toBe(`${i18n.global.t('cronSkills.jobs.errLoad')}: offline`)
    expect(state.loading.value).toBe(false)
  })

  it('preserves the warning-toast behavior when the latest recovery fails', async () => {
    const { scheduler } = createScheduler()
    scheduler.listJobs.mockResolvedValueOnce([{ id: 'saved' }]).mockRejectedValueOnce(new Error('offline'))
    scheduler.runNow.mockRejectedValueOnce(new Error('not connected'))
    const { state } = mount(scheduler)
    await state.loadData()
    await state.runJob('job-a')
    expect(state.jobs.value.map(job => job.id)).toEqual(['saved'])
    expect(state.error.value).toContain('offline')
    expect(state.loading.value).toBe(false)
    expect(pushToast).toHaveBeenCalledExactlyOnceWith(expect.any(String), { tone: 'warn' })
    expect(scheduler.runNow).toHaveBeenCalledOnce()
  })

  it('invalidates pending responses and scheduled work when unmounted', async () => {
    const { scheduler, finishRun, close } = createScheduler()
    const pending = deferred()
    scheduler.listJobs.mockResolvedValueOnce([{ id: 'saved' }]).mockReturnValueOnce(pending.promise)
    const { state, unmount } = mount(scheduler, true)
    await settle()
    finishRun()
    await settle()
    unmount()
    pending.resolve([{ id: 'after unmount' }])
    await settle()
    await vi.advanceTimersByTimeAsync(1000)
    await state.loadData()
    finishRun()
    expect(scheduler.listJobs).toHaveBeenCalledTimes(2)
    expect(close).toHaveBeenCalledOnce()
    expect(state.jobs.value.map(job => job.id)).toEqual(['saved'])
    expect(state.loading.value).toBe(false)
    expect(state.error.value).toBeNull()
  })

  it('does not publish recovery failures or toasts after unmount', async () => {
    const { scheduler } = createScheduler()
    const pending = deferred()
    scheduler.listJobs.mockReturnValueOnce(pending.promise)
    scheduler.runNow.mockRejectedValueOnce(new Error('Connection closed'))
    const { state, unmount } = mount(scheduler)
    const run = state.runJob('job-a')
    await settle()
    unmount()
    pending.reject(new Error('late error'))
    await run
    expect(state.hasLoaded.value).toBe(false)
    expect(state.error.value).toBeNull()
    expect(state.loading.value).toBe(false)
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('does not start recovery when a run reports a connection failure after unmount', async () => {
    const { scheduler } = createScheduler()
    let rejectRun!: (error: Error) => void
    scheduler.runNow.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectRun = reject }))
    const { state, unmount } = mount(scheduler)
    const run = state.runJob('job-a')
    unmount()
    rejectRun(new Error('Connection closed'))
    await run
    expect(scheduler.listJobs).not.toHaveBeenCalled()
    expect(state.loading.value).toBe(false)
    expect(pushToast).not.toHaveBeenCalled()
  })
})
