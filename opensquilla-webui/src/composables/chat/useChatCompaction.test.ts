import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import { useChatCompaction } from './useChatCompaction'

function createHarness() {
  const schedulePendingDrainAfterTerminal = vi.fn()
  const popAllPendingIntoComposer = vi.fn(() => true)
  const sessionKey = ref('agent:main:test')
  const scope = effectScope()
  const api = scope.run(() => useChatCompaction({
    sessionKey,
    schedulePendingDrainAfterTerminal,
    popAllPendingIntoComposer,
  }))!

  return {
    api,
    sessionKey,
    schedulePendingDrainAfterTerminal,
    popAllPendingIntoComposer,
    stop: () => scope.stop(),
  }
}

describe('compaction terminal presentation receipts', () => {
  it('reconciles exact duplicate outcomes without repeating terminal settlement', () => {
    const h = createHarness()
    const onTerminal = vi.fn()
    const payload = { key: h.sessionKey.value, source: 'manual', compaction_id: 'receipt-only', status: 'completed', sequence: 2 }
    try {
      h.api.showCompactionToast({ ...payload, status: 'started', sequence: 1 })
      expect(h.api.showCompactionToast(payload, { onTerminal })).toBe('standalone')
      for (const sequence of [2, 3, undefined]) {
        expect(h.api.showCompactionToast({ ...payload, sequence }, { authoritativeLive: true, onTerminal })).toBe(false)
      }
      expect(onTerminal).toHaveBeenCalledTimes(4)
      expect(onTerminal).toHaveBeenLastCalledWith(expect.objectContaining({ compactionId: 'receipt-only', state: 'completed' }))
      expect(h.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
    } finally { h.api.cleanup(); h.stop() }
  })

  it('does not turn wrong-key, stale, conflicting, or progress rejection into a terminal proof', () => {
    const h = createHarness()
    const onTerminal = vi.fn()
    const payload = { key: h.sessionKey.value, source: 'manual', compaction_id: 'receipt-guarded', status: 'completed', sequence: 2 }
    try {
      h.api.showCompactionToast(payload)
      for (const rejected of [
        { ...payload, key: 'other-session' }, { ...payload, sequence: 1 },
        { ...payload, status: 'failed', sequence: 3 }, { ...payload, status: 'observed', sequence: 4 },
      ]) expect(h.api.showCompactionToast(rejected, { authoritativeLive: true, onTerminal })).toBe(false)
      expect(onTerminal).not.toHaveBeenCalled()
      expect(h.api.compactStatus.value.status).toBe('completed')
    } finally { h.api.cleanup(); h.stop() }
  })

  it('cannot use an old terminal proof to settle a newer manual owner', () => {
    const h = createHarness()
    const onTerminal = vi.fn()
    const payload = { key: h.sessionKey.value, source: 'manual', compaction_id: 'old-receipt', status: 'completed', sequence: 2 }
    try {
      h.api.showCompactionToast(payload)
      h.api.beginManualCompactionRequest()
      h.api.showCompactionToast({ ...payload, status: 'started', compaction_id: 'new-owner', sequence: 1 })
      expect(h.api.showCompactionToast({ ...payload, sequence: 9 }, { onTerminal })).toBe(false)
      expect(onTerminal).not.toHaveBeenCalled()
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(true)
      expect(h.api.compactStatus.value.compactionId).toBe('new-owner')
      expect(h.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    } finally { h.api.cleanup(); h.stop() }
  })
})

describe('useChatCompaction replay compatibility', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('releases interrupted maintenance after a Gateway generation replacement', () => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({
        status: 'started', source: 'manual', compaction_id: 'old-process-op', sequence: 1,
      })
      h.api.handleStreamGenerationChange('process-b', 'process-a')

      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
      expect(h.api.compactStatus.value).toMatchObject({
        status: 'failed', isBusy: false, compactionId: 'old-process-op',
        reason: 'gateway_restarted',
      })
      expect(h.popAllPendingIntoComposer).toHaveBeenCalledOnce()
      h.api.showCompactionToast({
        status: 'started', source: 'manual', compaction_id: 'old-process-op', sequence: 2,
      })
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('keeps same-generation reconnect pending until its terminal replay arrives', () => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({
        status: 'started', source: 'manual', compaction_id: 'live-op', sequence: 1,
      })
      h.api.handleStreamGenerationChange('process-a', '')
      h.api.handleStreamGenerationChange('process-a', 'process-a')
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(true)
      h.api.showCompactionToast({
        status: 'failed', source: 'manual', compaction_id: 'live-op', sequence: 2,
        reason: 'compaction_deadline_exceeded',
      }, { replayed: true })
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
      expect(h.popAllPendingIntoComposer).toHaveBeenCalledOnce()
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

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
        reason: 'non_history_envelope_exhausts_budget',
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
        reason: 'non_history_envelope_exhausts_budget',
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

  it.each(['non_history_envelope_exhausts_budget', 'summary_does_not_fit', 'unknown_skip_reason'])(
    'does not label %s as a safe no-op', reason => {
      const h = createHarness()
      try {
        h.api.showCompactionToast({ source: 'manual', status: 'skipped', reason })
        expect(h.api.compactStatus.value).toMatchObject({
          tone: 'warn', message: 'Context organization was not applied',
        })
      } finally { h.api.cleanup(); h.stop() }
    },
  )

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

  it.each(['failed', 'error', 'timed_out'])('recovers pending input after %s unless the server blocks sending', status => {
    const retryable = createHarness()
    const blocked = createHarness()
    try {
      retryable.api.showCompactionToast({ status: 'started', source: 'manual' })
      retryable.api.showCompactionToast({
        status,
        source: 'manual',
        refused: true,
        reason: 'summary_does_not_fit',
      })
      expect(retryable.popAllPendingIntoComposer).toHaveBeenCalledOnce()
      expect(retryable.api.isCompactInFlightForCurrentSession()).toBe(false)
      expect(retryable.api.compactStatus.value).toMatchObject({
        status: status === 'timed_out' ? 'timed_out' : 'failed',
        isBusy: false,
      })

      blocked.api.showCompactionToast({ status: 'started', source: 'manual' })
      blocked.api.showCompactionToast({
        status,
        source: 'manual',
        refused: true,
        safe_to_send: false,
        reason: 'no_safe_turn_boundary',
      })
      expect(blocked.popAllPendingIntoComposer).not.toHaveBeenCalled()
      expect(blocked.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
      expect(blocked.api.isCompactInFlightForCurrentSession()).toBe(false)
    } finally {
      retryable.api.cleanup()
      retryable.stop()
      blocked.api.cleanup()
      blocked.stop()
    }
  })

  it.each([
    ['summary_replay_incomplete', 'The summary did not pass the completeness check', 'The summary was not applied. You can try again later.'],
    ['quality_gate_failed', 'The summary did not pass the completeness check', 'The summary was not applied. You can try again later.'],
    ['summary_does_not_fit', "The summary does not fit the current model's context", 'Shorten the input or choose a model with a larger context window.'],
    ['summary_target_unavailable', 'The summarization model is unavailable', 'Check the model settings and network connection, or choose another model.'],
    ['summary_failed', 'The model could not generate a usable summary', 'Try organizing the context again later, or choose another model.'],
    ['compaction_deadline_exceeded', 'Context organization timed out', 'Try organizing the context again later, or choose another model.'],
    ['unknown_private_provider_error', 'Context organization failed', 'Try organizing the context again later, or choose another model.'],
  ])('explains manual failure %s without exposing raw provider details', (reason, message, detail) => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({ status: 'started', source: 'manual', compaction_id: 'cmp-reason' })
      h.api.showCompactionToast({
        status: 'failed', source: 'manual', compaction_id: 'cmp-reason',
        reason, detail: 'private provider response and prompt data',
      })
      expect(h.api.compactStatus.value.message).toBe(message)
      expect(h.api.compactStatus.value.detail).toBe(detail)
      expect(h.api.compactStatus.value.detail).not.toContain('diagnostic ID')
      expect(h.api.compactStatus.value.detail).not.toContain('private provider')
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it.each(['failed', 'timed_out'])('keeps automatic %s informational in the id-less compatibility surface', status => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({ status: 'started', source: 'automatic' })
      h.api.showCompactionToast({ status, source: 'automatic', reason: 'summary_failed' })
      expect(h.api.compactStatus.value).toMatchObject({
        status, tone: 'info', isBusy: false, detail: '',
        message: 'Context organization did not complete',
      })
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it.each(['manual', 'automatic'])('inherits a %s source only for the same failed operation', source => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({ status: 'started', source, compaction_id: 'cmp-source' })
      h.api.showCompactionToast({ status: 'failed', compaction_id: 'cmp-source', reason: 'summary_failed' })
      expect(h.api.compactStatus.value).toMatchObject({
        source, status: 'failed', isBusy: false,
        tone: source === 'manual' ? 'err' : 'info',
        message: source === 'manual' ? 'The model could not generate a usable summary' : 'Context organization did not complete',
      })
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it.each([false, true])('settles emergency_ephemeral truthfully after replay=%s', replayed => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({ status: 'started', source: 'automatic', compaction_id: 'cmp-temporary' })
      h.api.showCompactionToast({
        status: 'emergency_ephemeral',
        source: 'automatic',
        compaction_id: 'cmp-temporary',
      }, { replayed })

      expect(h.api.compactStatus.value).toMatchObject({
        visible: true,
        status: 'emergency_ephemeral',
        message: 'History temporarily reduced; continuing',
        durability: 'request_scoped',
        tone: 'ok',
        detail: '',
        isBusy: false,
      })
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
      expect(h.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    } finally {
      h.api.cleanup()
      h.stop()
    }
  })

  it('labels completed compaction as organized', () => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({ status: 'started', source: 'manual' })
      h.api.showCompactionToast({ status: 'completed', source: 'manual', durability: 'durable' })

      expect(h.api.compactStatus.value).toMatchObject({
        message: 'Context organized',
        status: 'completed',
        durability: 'durable',
        isBusy: false,
      })
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

  it('keeps metadata for progress within an operation but resets it for a new operation', () => {
    const h = createHarness()
    try {
      h.api.showCompactStatus('started', 'Compacting', {
        source: 'manual',
        compactionId: 'cmp-first',
        durability: 'request_scoped',
        reason: 'first-operation',
      })
      h.api.showCompactStatus('started', 'Compacting', { detail: 'Summarizing' })
      expect(h.api.compactStatus.value).toMatchObject({
        compactionId: 'cmp-first',
        source: 'manual',
        durability: 'request_scoped',
        reason: 'first-operation',
      })

      h.api.showCompactStatus('started', 'Compacting', {
        source: 'automatic',
        compactionId: 'cmp-second',
      })
      expect(h.api.compactStatus.value).toMatchObject({
        compactionId: 'cmp-second',
        source: 'automatic',
        durability: '',
        reason: '',
      })
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

describe('compaction projection recovery', () => {
  it.each(['manual', 'automatic'])('restores an observed-first standalone %s operation from its own identity', source => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({
        status: 'observed', source, compaction_id: 'cmp-observed', sequence: 4,
        stage: 'summarizing', durability: 'none', heartbeat: true,
      }, { authoritativeLive: true })
      expect(h.api.compactStatus.value).toMatchObject({
        visible: true, compactionId: 'cmp-observed', source, isBusy: true, detail: 'summarizing',
      })
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(source === 'manual')
      h.api.showCompactionToast({ status: 'completed', source, compaction_id: 'cmp-observed', sequence: 5 })
      expect(h.schedulePendingDrainAfterTerminal).toHaveBeenCalledTimes(source === 'manual' ? 1 : 0)
    } finally { h.api.cleanup(); h.stop() }
  })

  it('reinstalls the same lifecycle sequence from an authoritative snapshot after presentation reset', () => {
    const h = createHarness()
    const progress = { status: 'observed', source: 'manual', compaction_id: 'cmp-return', sequence: 3 }
    try {
      h.api.showCompactionToast(progress)
      h.api.setCompactInFlight(false)
      h.api.hideCompactStatus()
      expect(h.api.showCompactionToast(progress)).toBe(false)
      expect(h.api.showCompactionToast(progress, { authoritativeLive: true })).toBe('standalone')
      expect(h.api.compactStatus.value.compactionId).toBe('cmp-return')
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(true)
      expect(h.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
      expect(h.popAllPendingIntoComposer).not.toHaveBeenCalled()
    } finally { h.api.cleanup(); h.stop() }
  })

  it.each(['identified', 'optimistic'])('does not let a repeated snapshot take a newer %s manual owner', owner => {
    const h = createHarness()
    const old = { status: 'started', source: 'manual', compaction_id: 'cmp-old-snapshot', sequence: 1 }
    try {
      h.api.showCompactionToast(old)
      h.api.setCompactInFlight(false)
      h.api.hideCompactStatus()
      if (owner === 'identified') {
        h.api.showCompactionToast({ status: 'started', source: 'manual', compaction_id: 'cmp-current', sequence: 1 })
      } else {
        h.api.setCompactInFlight(true)
        h.api.showCompactStatus('started', 'Organizing context', { source: 'manual' })
      }
      expect(h.api.showCompactionToast(old, { authoritativeLive: true })).toBe(false)
      expect(h.api.compactStatus.value.compactionId).toBe(owner === 'identified' ? 'cmp-current' : '')
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(true)
    } finally { h.api.cleanup(); h.stop() }
  })

  it('never borrows another standalone operation identity for an observed update', () => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({ status: 'started', source: 'manual', compaction_id: 'cmp-manual', sequence: 1 })
      h.api.showCompactionToast({ status: 'observed', source: 'automatic', compaction_id: 'cmp-auto', sequence: 1 })
      expect(h.api.compactStatus.value).toMatchObject({ compactionId: 'cmp-auto', source: 'automatic' })
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(true)
      h.api.showCompactionToast({ status: 'completed', source: 'automatic', compaction_id: 'cmp-auto', sequence: 2 })
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(true)
    } finally { h.api.cleanup(); h.stop() }
  })

  it('keeps identity when the first standalone receipt is stale', () => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({ status: 'stale', source: 'manual', compaction_id: 'cmp-stale-first', sequence: 2 })
      expect(h.api.compactStatus.value).toMatchObject({ compactionId: 'cmp-stale-first', source: 'manual', status: 'stale', isBusy: false })
    } finally { h.api.cleanup(); h.stop() }
  })
})

describe('manual compaction request delivery', () => {
  it('keeps an accepted operation busy when its command acknowledgement is lost', () => {
    const h = createHarness()
    try {
      const request = h.api.beginManualCompactionRequest()
      h.api.showCompactionToast({ status: 'observed', source: 'manual', compaction_id: 'cmp-accepted', sequence: 1 })
      expect(request.reject(new Error('Connection closed'))).toBe('unconfirmed')
      expect(h.api.compactStatus.value).toMatchObject({ compactionId: 'cmp-accepted', status: 'started', isBusy: true })
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(true)
      h.api.showCompactionToast({ status: 'completed', source: 'manual', compaction_id: 'cmp-accepted', sequence: 2 })
      expect(h.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
      expect(h.popAllPendingIntoComposer).not.toHaveBeenCalled()
    } finally { h.api.cleanup(); h.stop() }
  })

  it('does not downgrade a confirmed terminal receipt when its command later rejects', () => {
    const h = createHarness()
    try {
      const request = h.api.beginManualCompactionRequest()
      h.api.showCompactionToast({ status: 'completed', source: 'manual', compaction_id: 'cmp-fast-terminal', sequence: 1 })
      expect(request.reject(new Error('Acknowledgement lost'))).toBeNull()
      expect(h.api.compactStatus.value).toMatchObject({ compactionId: 'cmp-fast-terminal', status: 'completed', isBusy: false })
      expect(h.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
    } finally { h.api.cleanup(); h.stop() }
  })

  it('does not use an old manual terminal as acceptance of a new rejected command', () => {
    const h = createHarness()
    try {
      h.api.showCompactionToast({ status: 'completed', source: 'manual', compaction_id: 'cmp-previous', sequence: 1 })
      const request = h.api.beginManualCompactionRequest()
      expect(request.reject({ accepted: false, code: 'INVALID_REQUEST' })).toBeNull()
      expect(h.api.compactStatus.value).toMatchObject({ status: 'failed', compactionId: '', reason: 'request_rejected' })
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
      expect(h.popAllPendingIntoComposer).toHaveBeenCalledOnce()
    } finally { h.api.cleanup(); h.stop() }
  })

  it.each([undefined, 5])('does not let an old terminal with sequence %s retire a newer optimistic command', sequence => {
    const h = createHarness()
    const previous = { status: 'completed', source: 'manual', compaction_id: 'cmp-previous', sequence: 1 }
    try {
      h.api.showCompactionToast(previous)
      const request = h.api.beginManualCompactionRequest()
      expect(h.api.showCompactionToast({ ...previous, sequence }, { authoritativeLive: true })).toBe(false)
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(true)
      expect(request.reject({ accepted: false })).toBeNull()
      expect(h.api.compactStatus.value).toMatchObject({ reason: 'request_rejected', compactionId: '' })
      expect(h.popAllPendingIntoComposer).toHaveBeenCalledOnce()
    } finally { h.api.cleanup(); h.stop() }
  })

  it('retires an unconfirmed optimistic request without leaving a permanent send gate', () => {
    const h = createHarness()
    try {
      const request = h.api.beginManualCompactionRequest()
      expect(request.retire()).toBe('unconfirmed')
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
      expect(h.api.compactStatus.value).toMatchObject({ status: 'failed', reason: 'request_unconfirmed' })
      request.resolve({ status: 'started', compactionId: 'cmp-late' })
      expect(h.api.compactStatus.value.compactionId).toBe('')
      expect(h.popAllPendingIntoComposer).toHaveBeenCalledOnce()
    } finally { h.api.cleanup(); h.stop() }
  })

  it('keeps an identified operation recoverable after request retirement and fences its late callbacks', () => {
    const h = createHarness()
    try {
      const old = h.api.beginManualCompactionRequest()
      h.api.showCompactionToast({ status: 'started', source: 'manual', compaction_id: 'cmp-old', sequence: 1 })
      expect(old.retire()).toBeNull()
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(true)
      h.api.setCompactInFlight(false)
      h.api.hideCompactStatus()
      const current = h.api.beginManualCompactionRequest()
      h.api.showCompactionToast({ status: 'started', source: 'manual', compaction_id: 'cmp-current', sequence: 1 })
      old.resolve({ status: 'started', compactionId: 'cmp-old' })
      old.reject(new Error('late rejection'))
      expect(h.api.compactStatus.value.compactionId).toBe('cmp-current')
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(true)
      current.resolve({ status: 'started', compactionId: 'cmp-current' })
    } finally { h.api.cleanup(); h.stop() }
  })

  it('fences a command callback when the same session incarnation is reset', () => {
    const h = createHarness()
    try {
      const request = h.api.beginManualCompactionRequest()
      h.api.setCompactInFlight(false)
      h.api.hideCompactStatus()
      request.resolve({ status: 'started', compactionId: 'cmp-retired-incarnation' })
      request.reject(new Error('retired incarnation'))
      expect(h.api.compactStatus.value.visible).toBe(false)
      expect(h.api.isCompactInFlightForCurrentSession()).toBe(false)
    } finally { h.api.cleanup(); h.stop() }
  })
})
