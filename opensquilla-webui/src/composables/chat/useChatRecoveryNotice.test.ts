import { effectScope, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChatSessionRecoveryState } from '@/utils/chat/sessionLoadState'
import { useChatRecoveryNotice } from './useChatRecoveryNotice'

const scopes: ReturnType<typeof effectScope>[] = []
function setup(initial: ChatSessionRecoveryState | null = 'live-connecting', starting = false) {
  const scope = effectScope()
  scopes.push(scope)
  const state = ref<ChatSessionRecoveryState | null>(initial)
  const runtimeStarting = ref(starting)
  const connectionBlocked = ref(false)
  const visible = scope.run(() => useChatRecoveryNotice(state, runtimeStarting, connectionBlocked))!
  return { scope, state, runtimeStarting, connectionBlocked, visible }
}
beforeEach(() => vi.useFakeTimers())
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()); vi.useRealTimers() })

describe('quiet automatic recovery notice', () => {
  it('leaves slow runtime startup to the desktop notice and starts a fresh connection grace period', async () => {
    const test = setup('live-connecting', true)
    await vi.advanceTimersByTimeAsync(15_000)
    test.state.value = 'live-degraded'
    await vi.advanceTimersByTimeAsync(60_000)
    expect(test.visible.value).toBe(false)

    test.runtimeStarting.value = false
    await vi.advanceTimersByTimeAsync(4_999)
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(test.visible.value).toBe(true)
    test.runtimeStarting.value = true
    expect(test.visible.value).toBe(false)
    test.state.value = null
    test.runtimeStarting.value = false
    await vi.advanceTimersByTimeAsync(5_000)
    expect(test.visible.value).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each([1_000, 3_000, 4_999])('does not flash for a %s ms live recovery', async duration => {
    const test = setup()
    await vi.advanceTimersByTimeAsync(duration)
    expect(test.visible.value).toBe(false)
    test.state.value = null
    await vi.advanceTimersByTimeAsync(10_000)
    expect(test.visible.value).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('keeps one 5s timer through live phase changes, removes feedback and resets on recovery', async () => {
    const test = setup('live-degraded')
    await vi.advanceTimersByTimeAsync(3_000)
    test.state.value = 'live-connecting'
    await vi.advanceTimersByTimeAsync(1_999)
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(test.visible.value).toBe(true)
    test.state.value = 'live-degraded'
    expect(test.visible.value).toBe(true)
    test.state.value = null
    expect(test.visible.value).toBe(false)
    test.state.value = 'live-connecting'
    await vi.advanceTimersByTimeAsync(4_999)
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(test.visible.value).toBe(true)
  })

  it.each(['history-retrying', 'history-error'] as const)('keeps %s on its existing 2s delay', async initial => {
    const test = setup(initial)
    await vi.advanceTimersByTimeAsync(1_999)
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(test.visible.value).toBe(true)
    test.state.value = null
    expect(test.visible.value).toBe(false)
  })

  it('does not restart history feedback when retrying changes to an error', async () => {
    const test = setup('history-retrying')
    await vi.advanceTimersByTimeAsync(1_000)
    test.state.value = 'history-error'
    await vi.advanceTimersByTimeAsync(999)
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(test.visible.value).toBe(true)
  })

  it.each([1_500, 2_500])('starts a full connection grace period after %s ms of history recovery', async historyTime => {
    const test = setup('history-retrying')
    await vi.advanceTimersByTimeAsync(historyTime)
    expect(test.visible.value).toBe(historyTime >= 2_000)
    test.state.value = 'live-connecting'
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(4_999)
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(test.visible.value).toBe(true)
  })

  it('does not carry a live recovery delay or visible state into a history error', async () => {
    const test = setup()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(test.visible.value).toBe(true)
    test.state.value = 'history-error'
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1_999)
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(test.visible.value).toBe(true)
  })

  it.each(['live-connecting', 'history-retrying'] as const)('shows a confirmed missing session immediately during %s', initial => {
    const test = setup(initial)
    expect(test.visible.value).toBe(false)
    test.state.value = 'session-missing'
    expect(test.visible.value).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
    test.state.value = null
    expect(test.visible.value).toBe(false)
  })

  it('shows a confirmed blocked connection immediately without accelerating history feedback', async () => {
    const test = setup()
    test.connectionBlocked.value = true
    expect(test.visible.value).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
    test.state.value = 'history-error'
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1_999)
    expect(test.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(test.visible.value).toBe(true)
    test.state.value = null
    test.connectionBlocked.value = false
    expect(test.visible.value).toBe(false)
  })

  it('cancels both notice owners when the chat unmounts', async () => {
    const test = setup('history-retrying')
    test.state.value = 'live-connecting'
    test.scope.stop()
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(test.visible.value).toBe(false)
  })
})
