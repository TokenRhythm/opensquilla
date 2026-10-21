// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, KeepAlive, nextTick, ref } from 'vue'
import { useCronRuns } from './useCronRuns'
import { CronReadUnavailableError, type CronScheduler } from '@/modules/cronScheduler'
import type { CronRun } from '@/types/cron'

const apps: Array<ReturnType<typeof createApp>> = []
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); await nextTick() }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
function mount(listRuns = vi.fn().mockResolvedValue([{ summary: 'A history' }])) {
  const selectedId = ref<string | null>('A'), visible = ref(true)
  let state!: ReturnType<typeof useCronRuns>
  const child = defineComponent({ setup() {
    state = useCronRuns({ listRuns } as unknown as CronScheduler, selectedId)
    return () => h('div')
  } })
  const app = createApp({ setup: () => () => h(KeepAlive, null, { default: () => visible.value ? h(child) : null }) })
  app.mount(document.createElement('div')); apps.push(app)
  return { get state() { return state }, selectedId, visible, listRuns }
}
beforeEach(() => vi.useFakeTimers())
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.useRealTimers() })

describe('Cron history read ownership', () => {
  it('retains same-job history while capacity is unavailable and recovers automatically', async () => {
    const view = mount(); await flush()
    view.listRuns.mockRejectedValue(new CronReadUnavailableError('busy'))
    await view.state.loadRuns('A')
    expect(view.state.runs.value[0]?.summary).toBe('A history')
    expect(view.state.waitingForCapacity.value).toBe(true)
    expect(view.state.runsLoading.value).toBe(false)
    view.listRuns.mockResolvedValue([{ summary: 'new history' }])
    await vi.advanceTimersByTimeAsync(1000)
    expect(view.state.runs.value[0]?.summary).toBe('new history')
    expect(view.state.waitingForCapacity.value).toBe(false)
  })

  it.each(['close', 'deactivate'])('stops capacity retries on %s', async action => {
    const view = mount(vi.fn().mockRejectedValue(new CronReadUnavailableError('busy'))); await flush()
    if (action === 'close') view.selectedId.value = null
    else view.visible.value = false
    await flush(); await vi.advanceTimersByTimeAsync(30_000)
    expect(view.listRuns).toHaveBeenCalledTimes(1)
  })

  it('aborts an old selection and never shows its history for a replacement', async () => {
    const old = deferred<CronRun[]>()
    const view = mount(vi.fn().mockReturnValueOnce(old.promise).mockRejectedValue(new CronReadUnavailableError('busy')))
    await flush()
    const signal = view.listRuns.mock.calls[0]![2].signal as AbortSignal
    view.selectedId.value = 'B'; await flush()
    expect(signal.aborted).toBe(true)
    old.resolve([{ summary: 'A late' }]); await flush()
    expect(view.state.runs.value).toEqual([])
    expect(view.state.waitingForCapacity.value).toBe(true)
  })

  it('coalesces a run completion behind an old history snapshot into a fresh read', async () => {
    const first = deferred<CronRun[]>()
    const view = mount(vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue([{ summary: 'completed run' }]))
    await flush()
    void view.state.loadRuns('A')
    first.resolve([{ summary: 'before run' }]); await flush()
    expect(view.listRuns).toHaveBeenCalledTimes(2)
    expect(view.state.runs.value[0]?.summary).toBe('completed run')
  })

  it('reports permanent errors and stops retrying', async () => {
    const view = mount(vi.fn().mockRejectedValue(new Error('Forbidden'))); await flush()
    expect(view.state.error.value).toBe('Forbidden')
    expect(view.state.waitingForCapacity.value).toBe(false)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(view.listRuns).toHaveBeenCalledTimes(1)
  })
})
