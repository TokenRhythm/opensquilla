import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AppSettings, SettingChange } from '@/modules/appSettings'

const rpcCall = vi.fn()
const ready = vi.fn().mockResolvedValue(undefined)

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({ call: rpcCall, ready }),
}))

import { useMemoryLearningSettings } from './useMemoryLearningSettings'

const appSettings = {
  readAll: () => rpcCall('config.get'),
  read: () => Promise.resolve(null),
  readEffective: () => Promise.resolve({ fields: {} }),
  patch: () => Promise.resolve({}),
  patchSafe: (changes: readonly SettingChange[]) => rpcCall('config.patch.safe', {
    patches: Object.fromEntries(changes.map(change => [change.path, change.value])),
  }),
  merge: () => Promise.resolve({}),
} satisfies AppSettings

const observability = {
  selfLearningStatus: () => rpcCall('router.selflearning.status'),
}

describe('useMemoryLearningSettings', () => {
  beforeEach(() => {
    rpcCall.mockReset()
  })

  it('loads toggle state from config.get', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'config.get') {
        return {
          memory: { dream: { enabled: true, auto_schedule: true } },
          squilla_router: { self_learning: { enabled: false } },
        }
      }
      return {}
    })
    const ml = useMemoryLearningSettings(appSettings, observability)
    await ml.load()

    expect(ml.dreamEnabled.value).toBe(true)
    expect(ml.selfLearningEnabled.value).toBe(false)
    expect(ml.trainingPaused.value).toBe(false)
  })

  it('mirrors the backend dream linkage when enabling self-learning', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'config.patch.safe') {
        return {
          linked: ['memory.dream.enabled', 'memory.dream.auto_schedule'],
          linkedLive: true,
        }
      }
      if (method === 'router.selflearning.status') return { enabled: true }
      return {}
    })
    const ml = useMemoryLearningSettings(appSettings, observability)

    const ok = await ml.setSelfLearning(true)

    expect(ok).toBe(true)
    expect(rpcCall).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'squilla_router.self_learning.enabled': true },
    })
    expect(ml.selfLearningEnabled.value).toBe(true)
    expect(ml.dreamEnabled.value).toBe(true)
    expect(ml.dreamAutoSchedule.value).toBe(true)
    expect(ml.dreamLinkedOn.value).toBe(true)
    expect(ml.restartRequired.value).toBe(false)
  })

  it('flags restartRequired when the linkage could not hot-apply', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'config.patch.safe') {
        return { linked: ['memory.dream.enabled'], linkedLive: false, restartRequired: true }
      }
      if (method === 'router.selflearning.status') return { enabled: true }
      return {}
    })
    const ml = useMemoryLearningSettings(appSettings, observability)

    await ml.setSelfLearning(true)

    expect(ml.restartRequired.value).toBe(true)
  })

  it('turning dream off alone pauses training but leaves self-learning on', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'config.get') {
        return {
          memory: { dream: { enabled: true, auto_schedule: true } },
          squilla_router: { self_learning: { enabled: true } },
        }
      }
      if (method === 'router.selflearning.status') return { enabled: true }
      return {}
    })
    const ml = useMemoryLearningSettings(appSettings, observability)
    await ml.load()

    await ml.setDream(false)

    expect(rpcCall).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'memory.dream.enabled': false, 'memory.dream.auto_schedule': false },
    })
    expect(ml.selfLearningEnabled.value).toBe(true)
    expect(ml.trainingPaused.value).toBe(true)
  })

  it('rolls the toggle back when the patch fails', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'config.patch.safe') throw new Error('denied')
      return {}
    })
    const ml = useMemoryLearningSettings(appSettings, observability)

    const ok = await ml.setSelfLearning(true)

    expect(ok).toBe(false)
    expect(ml.selfLearningEnabled.value).toBe(false)
  })
})
