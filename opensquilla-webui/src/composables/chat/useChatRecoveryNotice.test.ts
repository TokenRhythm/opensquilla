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
  const terminal = ref(false)
  const visible = scope.run(() => useChatRecoveryNotice(state, runtimeStarting, connectionBlocked, terminal))!
  return { scope, state, runtimeStarting, connectionBlocked, terminal, visible }
}
beforeEach(() => vi.useFakeTimers())
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()); vi.useRealTimers() })

describe('quiet session recovery notice', () => {
  it.each(['metadata-error', 'history-error', 'live-connecting', 'live-degraded', 'history-loading', 'history-retrying'] as const)(
    'keeps %s quiet after recovery expires without changing recovery state', async initial => {
      const test = setup(initial)
      test.terminal.value = true
      test.connectionBlocked.value = true
      await vi.advanceTimersByTimeAsync(180_000)
      expect(test.visible.value).toBe(false)
      expect(test.state.value).toBe(initial)
      expect(test.terminal.value).toBe(true)
      expect(test.connectionBlocked.value).toBe(true)
      expect(vi.getTimerCount()).toBe(0)
    },
  )

  it('stays quiet through automatic recovery, exhaustion, explicit retry, and success', async () => {
    const test = setup()
    await vi.advanceTimersByTimeAsync(15_000)
    test.state.value = 'live-degraded'
    await vi.advanceTimersByTimeAsync(105_000)
    test.terminal.value = true
    expect(test.visible.value).toBe(false)
    test.state.value = 'metadata-error'
    expect(test.visible.value).toBe(false)
    test.terminal.value = false
    test.state.value = 'history-retrying'
    await vi.advanceTimersByTimeAsync(45_000)
    expect(test.visible.value).toBe(false)
    test.state.value = null
    expect(test.visible.value).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('leaves both runtime startup and transport failure feedback to their owner', async () => {
    const test = setup('live-degraded', true)
    test.terminal.value = true
    test.connectionBlocked.value = true
    await vi.advanceTimersByTimeAsync(120_000)
    expect(test.visible.value).toBe(false)
    test.runtimeStarting.value = false
    expect(test.visible.value).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each(['live-connecting', 'history-retrying', 'metadata-error'] as const)(
    'retains a confirmed missing-session notice while suppressing %s', initial => {
      const test = setup(initial)
      test.state.value = 'session-missing'
      expect(test.visible.value).toBe(true)
      expect(vi.getTimerCount()).toBe(0)
      test.state.value = null
      expect(test.visible.value).toBe(false)
    },
  )

  it('does not claim a session is missing while the local runtime is starting', () => {
    const test = setup('session-missing', true)
    expect(test.visible.value).toBe(false)
    test.runtimeStarting.value = false
    expect(test.visible.value).toBe(true)
  })

  it('allocates no presentation timers to survive unmount', async () => {
    const test = setup('history-error')
    test.scope.stop()
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(test.visible.value).toBe(false)
  })
})
