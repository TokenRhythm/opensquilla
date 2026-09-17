import { effectScope, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ChatSessionRecoveryState } from '@/utils/chat/sessionLoadState'
import { useChatRecoveryNotice } from './useChatRecoveryNotice'

afterEach(() => vi.useRealTimers())
describe('quiet automatic recovery notice', () => {
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
