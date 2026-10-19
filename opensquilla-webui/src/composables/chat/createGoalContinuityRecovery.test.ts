import { afterEach, describe, expect, it, vi } from 'vitest'
import { GoalCenterError } from '@/modules/goalCenter'
import { createGoalContinuityRecovery, type GoalContinuityOwner } from './createGoalContinuityRecovery'

const owner: GoalContinuityOwner = {
  sessionKey: 'session-a', sessionId: 'durable-a', epoch: 1, goalId: 'goal-a', token: 'token-a', authority: 'authority-a',
}

afterEach(() => vi.useRealTimers())

describe('bounded Goal continuity recovery', () => {
  it('renews only an exhausted incident and retains Retry-After while duplicate manual clicks join', async () => {
    vi.useFakeTimers()
    const attempt = vi.fn(async (_owner: GoalContinuityOwner, _signal: AbortSignal, _admission: { explicit: boolean }) => true)
      .mockRejectedValueOnce(new GoalCenterError('unavailable', 'busy', {
        retryable: true, recoveryAction: 'retry', retryAfterMs: 180_000,
      }))
    const recovery = createGoalContinuityRecovery({ isCurrent: () => true, attempt, onWorking() {}, onTerminal() {} })
    try {
      void recovery.recover(owner)
      await vi.advanceTimersByTimeAsync(120_001)
      expect(recovery.decision(owner)?.reason).toBe('budget-exhausted')
      expect(recovery.canRetry(owner)).toBe(true)
      const manual = recovery.recover(owner, { explicit: true })
      expect(recovery.recover(owner, { explicit: true })).toBe(manual)
      expect(recovery.recover(owner)).toBe(manual)
      await vi.advanceTimersByTimeAsync(59_998)
      expect(attempt).toHaveBeenCalledOnce()
      await vi.advanceTimersByTimeAsync(1)
      await manual
      expect(attempt).toHaveBeenCalledTimes(2)
      expect(attempt.mock.calls.map(call => call[2])).toEqual([{ explicit: false }, { explicit: true }])
      expect(attempt.mock.calls.every(call => call[0].token === owner.token)).toBe(true)
      expect(recovery.decision(owner)).toBeNull()
      expect(recovery.canRetry(owner)).toBe(false)
    } finally { recovery.dispose() }
  })

  it('bounds the renewed episode and consumes explicit subscription admission only once across reconnects', async () => {
    vi.useFakeTimers()
    const attempt = vi.fn(async (_owner: GoalContinuityOwner, _signal: AbortSignal, _admission: { explicit: boolean }) => {
      throw new GoalCenterError('unavailable', 'busy', { retryable: true, recoveryAction: 'retry', retryAfterMs: 5_000 })
    })
    const recovery = createGoalContinuityRecovery({ isCurrent: () => true, attempt, onWorking() {}, onTerminal() {} })
    try {
      void recovery.recover(owner)
      await vi.advanceTimersByTimeAsync(120_001)
      void recovery.recover(owner, { explicit: true })
      await vi.advanceTimersByTimeAsync(5_000)
      for (let index = 0; index < 4; index++) {
        recovery.cancel()
        void recovery.recover(owner)
        void recovery.recover(owner, { explicit: true })
        await vi.advanceTimersByTimeAsync(10_000)
      }
      await vi.advanceTimersByTimeAsync(75_000)
      expect(recovery.decision(owner)?.reason).toBe('budget-exhausted')
      const count = attempt.mock.calls.length
      expect(attempt.mock.calls.filter(call => call[2].explicit)).toHaveLength(1)
      recovery.cancel()
      await recovery.recover(owner)
      await vi.advanceTimersByTimeAsync(120_000)
      expect(attempt).toHaveBeenCalledTimes(count)
    } finally { recovery.dispose() }
  })

  it.each(['require-takeover', 'owner-attached', 'refresh-goal', 'stop', 'retry'] as const)(
    'never renews authoritative retryable:false rejection %s as a manual continuity retry', async recoveryAction => {
      vi.useFakeTimers()
      const error = new GoalCenterError('unavailable', 'rejected', { retryable: false, recoveryAction })
      const attempt = vi.fn(async () => { throw error })
      const recovery = createGoalContinuityRecovery({ isCurrent: () => true, attempt, onWorking() {}, onTerminal() {} })
      try {
        await recovery.recover(owner)
        await vi.advanceTimersByTimeAsync(121_000)
        expect(recovery.decision(owner)).toMatchObject({ reason: 'rejected', error, action: recoveryAction })
        expect(recovery.canRetry(owner)).toBe(false)
        await recovery.recover(owner, { explicit: true })
        expect(attempt).toHaveBeenCalledOnce()
      } finally { recovery.dispose() }
    },
  )

  it.each(['authority', 'sessionKey', 'sessionId', 'goalId', 'token', 'epoch'] as const)(
    'does not share a terminal decision across a changed %s', async field => {
      const recovery = createGoalContinuityRecovery({ isCurrent: () => true,
        attempt: async () => { throw new GoalCenterError('conflict', 'take over', { retryable: false, recoveryAction: 'require-takeover' }) },
        onWorking() {}, onTerminal() {},
      })
      try {
        await recovery.recover(owner)
        const next = { ...owner, [field]: field === 'epoch' ? 2 : 'replacement' }
        expect(recovery.decision(next)).toBeNull()
        expect(recovery.canRetry(next)).toBe(false)
        expect(recovery.decision(owner)?.action).toBe('require-takeover')
      } finally { recovery.dispose() }
    },
  )

  it('retains deadline and Retry-After across repeated hydration and connection replacement', async () => {
    vi.useFakeTimers()
    const attempt = vi.fn(async () => { throw new GoalCenterError('unavailable', 'busy', {
      retryable: true, recoveryAction: 'retry', retryAfterMs: 5_000,
    }) })
    const terminal = vi.fn()
    const recovery = createGoalContinuityRecovery({ isCurrent: () => true, attempt, onWorking() {}, onTerminal: terminal })
    try {
      void recovery.recover(owner)
      await vi.advanceTimersByTimeAsync(4_999)
      expect(attempt).toHaveBeenCalledTimes(1)
      recovery.cancel()
      void recovery.recover(owner)
      await vi.advanceTimersByTimeAsync(1)
      expect(attempt).toHaveBeenCalledTimes(2)
      for (let count = 0; count < 5; count++) void recovery.recover(owner)
      await vi.advanceTimersByTimeAsync(115_001)
      const boundedCalls = attempt.mock.calls.length
      expect(terminal).toHaveBeenCalledOnce()
      recovery.cancel()
      await recovery.recover(owner)
      await vi.advanceTimersByTimeAsync(300_000)
      expect(attempt).toHaveBeenCalledTimes(boundedCalls)
      expect(vi.getTimerCount()).toBe(0)
    } finally { recovery.dispose() }
  })

  it('bounds a hung physical attempt and cancels late ownership without applying a successor', async () => {
    vi.useFakeTimers()
    const signals: AbortSignal[] = []
    let current = true
    const attempt = vi.fn(async (_owner: GoalContinuityOwner, signal: AbortSignal) => {
      signals.push(signal)
      return await new Promise<boolean>(() => {})
    })
    const terminal = vi.fn()
    const recovery = createGoalContinuityRecovery({ isCurrent: () => current, attempt, onWorking() {}, onTerminal: terminal })
    try {
      void recovery.recover(owner)
      await vi.advanceTimersByTimeAsync(7_000)
      expect(signals[0]?.aborted).toBe(true)
      expect(attempt).toHaveBeenCalledTimes(1)
      current = false
      recovery.cancel()
      current = true
      void recovery.recover(owner)
      await vi.advanceTimersByTimeAsync(113_001)
      expect(terminal).toHaveBeenCalledOnce()
      expect(vi.getTimerCount()).toBe(0)
    } finally { recovery.dispose() }
  })

  it('closes a successful incident even when success changes the detached predicate', async () => {
    vi.useFakeTimers()
    let detached = true
    const attempt = vi.fn(async () => { detached = false; return true })
    const recovery = createGoalContinuityRecovery({ isCurrent: () => detached, attempt, onWorking() {}, onTerminal() {} })
    try {
      await recovery.recover(owner)
      await vi.advanceTimersByTimeAsync(121_000)
      detached = true
      await recovery.recover(owner)
      expect(attempt).toHaveBeenCalledTimes(2)
    } finally { recovery.dispose() }
  })

  it('does not treat programming errors as transient transport failures', async () => {
    const attempt = vi.fn(async () => { throw new TypeError('cannot read property') })
    const terminal = vi.fn()
    const recovery = createGoalContinuityRecovery({ isCurrent: () => true, attempt, onWorking() {}, onTerminal: terminal })
    try {
      await recovery.recover(owner)
      await recovery.recover(owner)
      expect(attempt).toHaveBeenCalledOnce()
      expect(terminal).toHaveBeenCalledTimes(2)
    } finally { recovery.dispose() }
  })

  it('retains an abandoned attempt incident and binds first authority without renewing its deadline', async () => {
    vi.useFakeTimers()
    const attempt = vi.fn(async () => false)
    const terminal = vi.fn()
    const recovery = createGoalContinuityRecovery({ isCurrent: () => true, attempt, onWorking() {}, onTerminal: terminal })
    try {
      await recovery.recover({ ...owner, authority: null })
      await vi.advanceTimersByTimeAsync(119_000)
      recovery.bindAuthority(owner)
      await recovery.recover(owner)
      await vi.advanceTimersByTimeAsync(1_001)
      await recovery.recover(owner)
      expect(attempt).toHaveBeenCalledTimes(2)
      expect(terminal).toHaveBeenCalledOnce()
      expect(vi.getTimerCount()).toBe(0)
    } finally { recovery.dispose() }
  })

  it('does not renew an old incident after more than 64 different Goal identities', async () => {
    vi.useFakeTimers()
    const attempt = vi.fn(async () => false)
    const terminal = vi.fn()
    const recovery = createGoalContinuityRecovery({ isCurrent: () => true, attempt, onWorking() {}, onTerminal: terminal })
    try {
      await recovery.recover(owner)
      for (let index = 0; index < 65; index++) {
        await recovery.recover({ ...owner, goalId: `other-goal-${index}`, token: `other-token-${index}` })
      }
      await vi.advanceTimersByTimeAsync(120_001)
      await recovery.recover(owner)
      expect(attempt).toHaveBeenCalledTimes(66)
      expect(terminal).toHaveBeenCalledOnce()
    } finally { recovery.dispose() }
  })

  it('bounds a terminal refresh and does not restart it on repeated hydration', async () => {
    vi.useFakeTimers()
    const attempt = vi.fn(async () => { throw new GoalCenterError('conflict', 'changed', { retryable: false, recoveryAction: 'refresh-goal' }) })
    const refresh = vi.fn(async (_owner: GoalContinuityOwner, _signal: AbortSignal) => { await new Promise(() => {}) })
    const recovery = createGoalContinuityRecovery({ isCurrent: () => true, attempt, onWorking() {}, onTerminal() {}, refresh })
    try {
      const pending = recovery.recover(owner)
      await vi.advanceTimersByTimeAsync(7_000)
      await pending
      await recovery.recover(owner)
      expect(attempt).toHaveBeenCalledOnce()
      expect(refresh).toHaveBeenCalledOnce()
      expect(refresh.mock.calls[0]![1].aborted).toBe(true)
      expect(vi.getTimerCount()).toBe(0)
    } finally { recovery.dispose() }
  })

  it.each(['require-takeover', 'owner-attached', 'refresh-goal', 'stop'] as const)('does not automatically repeat %s even when legacy retryability is true', async recoveryAction => {
    vi.useFakeTimers()
    const attempt = vi.fn(async () => { throw new GoalCenterError('conflict', 'state rejected', { retryable: true, recoveryAction }) })
    const terminal = vi.fn()
    const recovery = createGoalContinuityRecovery({ isCurrent: () => true, attempt, onWorking() {}, onTerminal: terminal })
    try {
      await recovery.recover(owner)
      await vi.advanceTimersByTimeAsync(121_000)
      expect(attempt).toHaveBeenCalledOnce()
      expect(terminal).toHaveBeenCalledWith(expect.any(GoalCenterError), recoveryAction)
    } finally { recovery.dispose() }
  })
})
