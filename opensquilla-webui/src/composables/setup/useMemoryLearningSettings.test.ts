import { beforeEach, describe, expect, it, vi } from 'vitest'

const rpcCall = vi.fn()
const waitForConnection = vi.fn().mockResolvedValue(undefined)

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({ call: rpcCall, waitForConnection }),
}))

import { useMemoryLearningSettings } from './useMemoryLearningSettings'

describe('useMemoryLearningSettings', () => {
  beforeEach(() => {
    rpcCall.mockReset()
  })

  it('loads toggle state from config.get', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'config.get') {
        return {
          memory: {
            dream: { enabled: true, auto_schedule: true },
            experimental: {
              constraint_annotation: true,
              constraint_routing: false,
              sufficiency_check: true,
              usage_tracking: true,
            },
          },
          compaction: { anchor_enabled: true },
          squilla_router: { self_learning: { enabled: false } },
        }
      }
      return {}
    })
    const ml = useMemoryLearningSettings()
    await ml.load()

    expect(ml.dreamEnabled.value).toBe(true)
    expect(ml.selfLearningEnabled.value).toBe(false)
    expect(ml.trainingPaused.value).toBe(false)
    expect(ml.constraintAnnotation.value).toBe(true)
    expect(ml.constraintRouting.value).toBe(false)
    expect(ml.sufficiencyCheck.value).toBe(true)
    expect(ml.usageTracking.value).toBe(true)
    expect(ml.compactionAnchors.value).toBe(true)
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
    const ml = useMemoryLearningSettings()

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
    const ml = useMemoryLearningSettings()

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
    const ml = useMemoryLearningSettings()
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
    const ml = useMemoryLearningSettings()

    const ok = await ml.setSelfLearning(true)

    expect(ok).toBe(false)
    expect(ml.selfLearningEnabled.value).toBe(false)
  })

  it('persists experimental gates and reports boot-bound memory changes', async () => {
    rpcCall.mockResolvedValue({ restartRequired: true })
    const ml = useMemoryLearningSettings()

    expect(await ml.setConstraintRouting(true)).toBe(true)

    expect(rpcCall).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'memory.experimental.constraint_routing': true },
    })
    expect(ml.constraintRouting.value).toBe(true)
    expect(ml.restartRequired.value).toBe(true)
  })

  it('persists compaction anchors without inventing a restart', async () => {
    rpcCall.mockResolvedValue({ restartRequired: false })
    const ml = useMemoryLearningSettings()

    expect(await ml.setCompactionAnchors(true)).toBe(true)

    expect(rpcCall).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'compaction.anchor_enabled': true },
    })
    expect(ml.compactionAnchors.value).toBe(true)
    expect(ml.restartRequired.value).toBe(false)
  })
})
