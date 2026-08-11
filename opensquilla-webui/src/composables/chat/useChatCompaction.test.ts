import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import { useChatCompaction } from './useChatCompaction'

function createHarness() {
  const schedulePendingDrainAfterTerminal = vi.fn()
  const popAllPendingIntoComposer = vi.fn(() => true)
  const scope = effectScope()
  const api = scope.run(() => useChatCompaction({
    sessionKey: ref('agent:main:test'),
    schedulePendingDrainAfterTerminal,
    popAllPendingIntoComposer,
  }))!

  return {
    api,
    schedulePendingDrainAfterTerminal,
    popAllPendingIntoComposer,
    stop: () => scope.stop(),
  }
}

describe('useChatCompaction replay compatibility', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('ignores replayed progress so it cannot resurrect a stale busy indicator', () => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({ status: 'started', source: 'manual' }, { replayed: true })

      expect(h.api.compactStatus.value.visible).toBe(false)
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('accepts a replayed terminal event and settles the current compaction', () => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({ status: 'started', source: 'manual', key: 'agent:main:test' })
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(true)

      h.api.showCompactionToast(
        { status: 'completed', source: 'manual', key: 'agent:main:test' },
        { replayed: true },
      )

      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
      expect(h.api.compactStatus.value).toMatchObject({
        visible: true,
        status: 'completed',
        tone: 'ok',
        isBusy: false,
      })
      expect(h.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('distinguishes within-budget skips from compaction vetoes', () => {
    const benign = createHarness()
    const vetoed = createHarness()
    try {
      benign.api.showCompactionToast({
        status: 'skipped',
        source: 'manual',
        reason: 'within_compaction_budget',
      })
      vetoed.api.showCompactionToast({
        status: 'skipped',
        source: 'manual',
        reason: 'no_safe_turn_boundary',
      })

      expect(benign.api.compactStatus.value).toMatchObject({
        status: 'skipped',
        tone: 'info',
        message: 'No organization needed; context has enough space',
        reason: 'within_compaction_budget',
      })
      expect(vetoed.api.compactStatus.value).toMatchObject({
        status: 'skipped',
        tone: 'warn',
        message: 'Context organization was not applied',
        reason: 'no_safe_turn_boundary',
      })
    } finally {
      benign.api.cleanup()
      benign.stop()
      vetoed.api.cleanup()
      vetoed.stop()
    }
  })

  it('settles optimistic busy from a replayed terminal before the id acknowledgement', () => {
    const h = createHarness()
    try {
      h.api.setCompactInFlight(true, 'agent:main:test')
      h.api.showCompactStatus('started', 'Compacting…')

      h.api.showCompactionToast({
        status: 'completed',
        source: 'manual',
        key: 'agent:main:test',
        compaction_id: 'cmp-reconnected',
        sequence: 2,
      }, { replayed: true })

      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
      expect(h.api.compactStatus.value).toMatchObject({
        status: 'completed',
        isBusy: false,
      })
      expect(h.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()

      h.api.showCompactionToast({
        status: 'started',
        source: 'manual',
        key: 'agent:main:test',
        compaction_id: 'cmp-reconnected',
      })
      expect(h.api.compactStatus.value.status).toBe('completed')
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('treats a replayed stale outcome as terminal and resumes the pending drain', () => {
    const h = createHarness()
    try {
      h.api.setCompactInFlight(true, 'agent:main:test')
      h.api.showCompactStatus('started', 'Compacting…')

      h.api.showCompactionToast({
        status: 'stale',
        source: 'manual',
        key: 'agent:main:test',
        compaction_id: 'cmp-stale',
        sequence: 2,
        reason: 'stale_preimage',
      }, { replayed: true })

      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
      expect(h.api.compactStatus.value).toMatchObject({
        visible: true,
        status: 'stale',
        tone: 'warn',
        detail: 'stale_preimage',
        isBusy: false,
      })
      expect(h.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
      expect(h.popAllPendingIntoComposer).not.toHaveBeenCalled()

      h.api.showCompactionToast({
        status: 'started',
        source: 'manual',
        key: 'agent:main:test',
        compaction_id: 'cmp-stale',
      })
      expect(h.api.compactStatus.value.status).toBe('stale')
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('keeps unknown replayed legacy terminals invisible until an owner is known', () => {
    const h = createHarness()
    try {
      expect(h.api.showCompactionToast({ compacted: false }, { replayed: true })).toBe(false)

      expect(h.api.compactStatus.value.visible).toBe(false)
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('treats timed_out as terminal and recovers pending input when safe', () => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({ status: 'started', source: 'manual' })
      h.api.showCompactionToast({ status: 'timed_out', source: 'manual' }, { replayed: true })

      expect(h.api.compactStatus.value).toMatchObject({
        visible: true,
        status: 'timed_out',
        tone: 'warn',
        isBusy: false,
      })
      expect(h.popAllPendingIntoComposer).toHaveBeenCalledOnce()
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('settles emergency_ephemeral without claiming durable completion in state', () => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({ status: 'started', source: 'manual' })
      h.api.showCompactionToast({
        status: 'emergency_ephemeral',
        source: 'automatic',
      })

      expect(h.api.compactStatus.value).toMatchObject({
        visible: true,
        status: 'emergency_ephemeral',
        tone: 'warn',
        detail: 'Request-scoped; session history was not rewritten',
        isBusy: false,
      })
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
      expect(h.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('does not let an older replayed terminal settle a newer compaction', () => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({
        status: 'started',
        source: 'manual',
        compaction_id: 'cmp-new',
        sequence: 1,
      })
      h.api.showCompactionToast({
        status: 'completed',
        source: 'manual',
        compaction_id: 'cmp-old',
        sequence: 2,
      }, { replayed: true })

      expect(h.api.isCompactInFlightForCurrentSession()).toBe(true)
      expect(h.api.compactStatus.value.status).toBe('started')
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('drops duplicate or out-of-order events for the same operation', () => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({
        status: 'started',
        source: 'manual',
        compaction_id: 'cmp-sequenced',
        sequence: 2,
      })
      h.api.showCompactionToast({
        status: 'cancelled',
        source: 'manual',
        compaction_id: 'cmp-sequenced',
        sequence: 1,
      })

      expect(h.api.isCompactInFlightForCurrentSession()).toBe(true)
      expect(h.api.compactStatus.value.status).toBe('started')
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('does not let a delayed wait:false acknowledgement resurrect a terminal operation', () => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({
        status: 'started',
        source: 'manual',
        compaction_id: 'cmp-fast',
        sequence: 1,
      })
      h.api.showCompactionToast({
        status: 'completed',
        source: 'manual',
        compaction_id: 'cmp-fast',
        sequence: 2,
      })
      h.api.showCompactionToast({
        status: 'started',
        source: 'manual',
        compaction_id: 'cmp-fast',
      })

      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
      expect(h.api.compactStatus.value).toMatchObject({
        status: 'completed',
        isBusy: false,
      })
      expect(h.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('keeps an activity compaction on the activity surface through terminal delivery', () => {
    const h = createHarness()
    try {
      expect(h.api.showCompactionToast({
        status: 'started',
        source: 'automatic',
        compaction_id: 'cmp-activity',
        sequence: 1,
      }, { placement: 'activity' })).toBe('activity')

      expect(h.api.showCompactionToast({
        status: 'completed',
        source: 'automatic',
        compaction_id: 'cmp-activity',
        sequence: 2,
        user_visible: false,
      }, { placement: 'standalone' })).toBe('activity')
      expect(h.api.compactStatus.value.visible).toBe(false)
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('settles a standalone compaction in place when streaming starts meanwhile', () => {
    const h = createHarness()
    try {
      expect(h.api.showCompactionToast({
        status: 'started',
        source: 'automatic',
        compaction_id: 'cmp-standalone',
        sequence: 1,
      }, { placement: 'standalone' })).toBe('standalone')

      expect(h.api.showCompactionToast({
        status: 'completed',
        source: 'automatic',
        compaction_id: 'cmp-standalone',
        sequence: 2,
        user_visible: false,
      }, { placement: 'activity' })).toBe('standalone')
      expect(h.api.compactStatus.value).toMatchObject({
        visible: true,
        status: 'completed',
        isBusy: false,
        compactionId: 'cmp-standalone',
      })
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('does not create feedback for an untracked hidden terminal', () => {
    const h = createHarness()
    try {
      expect(h.api.showCompactionToast({
        status: 'completed',
        source: 'automatic',
        compaction_id: 'cmp-hidden',
        sequence: 1,
        user_visible: false,
      }, { placement: 'standalone' })).toBe(false)

      expect(h.api.getCompactionPlacement('cmp-hidden')).toBeNull()
      expect(h.api.compactStatus.value.visible).toBe(false)
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('promotes a provisional replay owner on an authoritative duplicate terminal', () => {
    const h = createHarness()
    try {
      const terminal = {
        status: 'completed',
        source: 'automatic',
        compaction_id: 'cmp-replay-owner',
        sequence: 2,
      }

      expect(h.api.showCompactionToast(terminal, {
        replayed: true,
        placement: 'standalone',
      })).toBe(false)
      expect(h.api.getCompactionPlacement('cmp-replay-owner')).toBe('standalone')
      expect(h.api.compactStatus.value.visible).toBe(false)

      expect(h.api.showCompactionToast(terminal, {
        authoritativeLive: true,
        placement: 'activity',
      })).toBe('activity')
      expect(h.api.getCompactionPlacement('cmp-replay-owner')).toBe('activity')
      expect(h.api.compactStatus.value.visible).toBe(false)
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })
})
