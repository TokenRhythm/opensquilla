import { nextTick, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useCronRuns } from './useCronRuns'
import type { CronRun } from '@/types/cron'
import type { CronScheduler } from '@/modules/cronScheduler'

const mocks = vi.hoisted(() => ({
  rpcCall: vi.fn(),
}))

const scheduler = {
  listRuns: async (jobId: string) => {
    const result = await mocks.rpcCall('cron.runs', { id: jobId }) as { runs: CronRun[] }
    return result.runs
  },
} as unknown as CronScheduler

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

function run(summary: string): CronRun {
  return { summary }
}

beforeEach(() => {
  mocks.rpcCall.mockReset()
})

describe('useCronRuns', () => {
  it('keeps a first-load failure distinct from an empty history and supports retry', async () => {
    mocks.rpcCall.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ runs: [run('restored')] })
    const selectedId = ref<string | null>('job-a')
    const state = useCronRuns(scheduler, selectedId)
    await state.loadRuns('job-a')
    expect(state.error.value).toBe('offline')
    expect(state.runs.value).toEqual([])
    expect(state.runsLoading.value).toBe(false)
    await state.loadRuns('job-a')
    expect(state.error.value).toBeNull()
    expect(state.runs.value[0]?.summary).toBe('restored')
  })

  it('keeps last-good history on refresh but clears it when selecting another job', async () => {
    mocks.rpcCall.mockResolvedValueOnce({ runs: [run('old')] }).mockRejectedValue(new Error('offline'))
    const selectedId = ref<string | null>('job-a')
    const state = useCronRuns(scheduler, selectedId)
    await state.loadRuns('job-a')
    await state.loadRuns('job-a')
    expect(state.runs.value[0]?.summary).toBe('old')
    expect(state.error.value).toBe('offline')
    selectedId.value = 'job-b'
    await nextTick()
    expect(state.runs.value).toEqual([])
    await vi.waitFor(() => expect(state.error.value).toBe('offline'))
  })
  it('ignores an older response after the selected job changes', async () => {
    const first = deferred<{ runs: CronRun[] }>()
    const second = deferred<{ runs: CronRun[] }>()
    mocks.rpcCall.mockImplementation((_method: string, params: { id: string }) => (
      params.id === 'job-a' ? first.promise : second.promise
    ))

    const selectedId = ref<string | null>(null)
    const state = useCronRuns(scheduler, selectedId)

    selectedId.value = 'job-a'
    await nextTick()
    selectedId.value = 'job-b'
    await nextTick()

    second.resolve({ runs: [run('run-b')] })
    await second.promise
    first.resolve({ runs: [run('run-a')] })
    await first.promise
    await nextTick()

    expect(state.runs.value.map(item => item.summary)).toEqual(['run-b'])
  })
})
