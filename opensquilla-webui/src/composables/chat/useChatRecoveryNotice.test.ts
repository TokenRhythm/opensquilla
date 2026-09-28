import { effectScope, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ChatSessionRecoveryState } from '@/utils/chat/sessionLoadState'
import { useChatRecoveryNotice } from './useChatRecoveryNotice'

afterEach(() => vi.useRealTimers())
describe('quiet automatic recovery notice', () => {
  it('leaves slow runtime startup to the desktop notice but still shows terminal failures', async () => {
    vi.useFakeTimers()
    const scope = effectScope()
    const state = ref<ChatSessionRecoveryState | null>('live-connecting')
    const runtimeStarting = ref(true)
    const visible = scope.run(() => useChatRecoveryNotice(state, runtimeStarting))!
    await vi.advanceTimersByTimeAsync(15_000)
    state.value = 'live-degraded'
    await vi.advanceTimersByTimeAsync(60_000)
    expect(visible.value).toBe(false)

    runtimeStarting.value = false
    await vi.advanceTimersByTimeAsync(2_000)
    expect(visible.value).toBe(true)
    runtimeStarting.value = true
    await nextTick()
    expect(visible.value).toBe(false)
    state.value = null
    runtimeStarting.value = false
    await vi.advanceTimersByTimeAsync(2_000)
    expect(visible.value).toBe(false)
    scope.stop()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('does not flash for a short failure, then keeps one timer through retry phase changes', async () => {
    vi.useFakeTimers()
    const scope = effectScope()
    const state = ref<ChatSessionRecoveryState | null>('live-connecting')
    const visible = scope.run(() => useChatRecoveryNotice(state))!
    await vi.advanceTimersByTimeAsync(1_999)
    expect(visible.value).toBe(false)
    state.value = null
    await nextTick()
    await vi.advanceTimersByTimeAsync(10)
    expect(visible.value).toBe(false)
    state.value = 'live-degraded'
    await nextTick()
    await vi.advanceTimersByTimeAsync(1_000)
    state.value = 'live-connecting'
    await nextTick()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(visible.value).toBe(true)
    state.value = null
    await nextTick()
    expect(visible.value).toBe(false)
    scope.stop()
    expect(vi.getTimerCount()).toBe(0)
  })
})
