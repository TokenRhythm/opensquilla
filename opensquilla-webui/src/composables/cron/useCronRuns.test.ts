import { nextTick, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useCronRuns } from './useCronRuns'
import type { CronRun } from '@/types/cron'

const mocks = vi.hoisted(() => ({
  rpcCall: vi.fn(),
}))

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({ call: mocks.rpcCall }),
}))

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
  it('ignores an older response after the selected job changes', async () => {
    const first = deferred<{ runs: CronRun[] }>()
    const second = deferred<{ runs: CronRun[] }>()
    mocks.rpcCall.mockImplementation((_method: string, params: { id: string }) => (
      params.id === 'job-a' ? first.promise : second.promise
    ))

    const selectedId = ref<string | null>(null)
    const state = useCronRuns(selectedId)

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
