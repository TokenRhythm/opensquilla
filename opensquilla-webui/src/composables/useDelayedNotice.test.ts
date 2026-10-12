import { computed, effectScope, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CONNECTION_NOTICE_DELAY_MS, RECOVERY_NOTICE_DELAY_MS, useDelayedNotice } from './useDelayedNotice'

const scopes: ReturnType<typeof effectScope>[] = []
function setup(delayMs = RECOVERY_NOTICE_DELAY_MS) {
  const scope = effectScope()
  scopes.push(scope)
  const phase = ref('healthy')
  const immediate = ref(false)
  const suppressed = ref(false)
  const visible = scope.run(() => useDelayedNotice(
    computed(() => phase.value !== 'healthy'), immediate, suppressed, delayMs,
  ))!
  return { scope, phase, immediate, suppressed, visible }
}
beforeEach(() => vi.useFakeTimers())
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()); vi.useRealTimers() })

describe('passive recovery feedback', () => {
  it.each([1_000, 3_000, 4_999])('keeps a %s ms connection recovery quiet', async duration => {
    const test = setup(CONNECTION_NOTICE_DELAY_MS)
    test.phase.value = 'reconnecting'
    await vi.advanceTimersByTimeAsync(duration)
    expect(test.visible.value).toBe(false)
    test.phase.value = 'healthy'
    await vi.advanceTimersByTimeAsync(10_000)
    expect(test.visible.value).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('shows connection feedback at 5s across phase churn and resets after recovery', async () => {
    const test = setup(CONNECTION_NOTICE_DELAY_MS)
    for (const phase of ['checking', 'suspect', 'disconnected', 'reconnecting']) {
      test.phase.value = phase
      await vi.advanceTimersByTimeAsync(1_000)
      expect(test.visible.value).toBe(false)
    }
    await vi.advanceTimersByTimeAsync(999)
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(test.visible.value).toBe(true)
    test.phase.value = 'healthy'
    expect(test.visible.value).toBe(false)
    test.phase.value = 'reconnecting'
    await vi.advanceTimersByTimeAsync(4_999)
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(test.visible.value).toBe(true)
  })

  it.each([500, 1_500, RECOVERY_NOTICE_DELAY_MS - 1])('is completely silent for a %s ms recovery', async duration => {
    const test = setup()
    test.phase.value = 'reconnecting'
    await vi.advanceTimersByTimeAsync(duration)
    expect(test.visible.value).toBe(false)
    test.phase.value = 'healthy'
    await vi.advanceTimersByTimeAsync(10_000)
    expect(test.visible.value).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('does not postpone feedback when an uninterrupted recovery changes phase', async () => {
    const test = setup()
    for (const phase of ['checking', 'suspect', 'disconnected', 'reconnecting']) {
      test.phase.value = phase
      await vi.advanceTimersByTimeAsync(500)
    }
    expect(test.visible.value).toBe(true)
    test.phase.value = 'disconnected'
    expect(test.visible.value).toBe(true)
    test.phase.value = 'healthy'
    expect(test.visible.value).toBe(false)
    test.phase.value = 'reconnecting'
    expect(test.visible.value).toBe(false)
  })

  it('shows a confirmed blocker immediately without carrying it across recovery', async () => {
    const test = setup(CONNECTION_NOTICE_DELAY_MS)
    test.phase.value = 'disconnected'
    test.immediate.value = true
    expect(test.visible.value).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
    test.immediate.value = false
    test.phase.value = 'reconnecting'
    expect(test.visible.value).toBe(true)
    test.phase.value = 'healthy'
    expect(test.visible.value).toBe(false)
  })

  it('leaves runtime startup feedback to its owner and starts a fresh grace period afterwards', async () => {
    const test = setup()
    test.phase.value = 'checking'
    await vi.advanceTimersByTimeAsync(1_500)
    test.suppressed.value = true
    test.immediate.value = true
    await vi.advanceTimersByTimeAsync(60_000)
    expect(test.visible.value).toBe(false)
    test.immediate.value = false
    test.suppressed.value = false
    await vi.advanceTimersByTimeAsync(1_999)
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(test.visible.value).toBe(true)
  })

  it('cancels feedback when its owner unmounts', async () => {
    const test = setup()
    test.phase.value = 'checking'
    test.scope.stop()
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(test.visible.value).toBe(false)
  })
})
