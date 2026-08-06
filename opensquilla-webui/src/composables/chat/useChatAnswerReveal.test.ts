import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ref, nextTick, effectScope, type EffectScope } from 'vue'
import {
  useChatAnswerReveal,
  ANSWER_REVEAL_MAX_MS as MAX,
} from './useChatAnswerReveal'

function harness(opts: { routerEnabled?: boolean; routerFx?: boolean } = {}) {
  const isStreaming = ref(false)
  const routerEnabled = ref(opts.routerEnabled ?? true)
  const routerVisualEffectsEnabled = ref(opts.routerFx ?? true)
  const decided = ref<unknown>(null)
  const scope: EffectScope = effectScope()
  let api!: ReturnType<typeof useChatAnswerReveal>
  scope.run(() => {
    api = useChatAnswerReveal({
      isStreaming,
      routerEnabled,
      routerVisualEffectsEnabled,
      routerDecided: () => decided.value,
    })
  })
  return { isStreaming, routerEnabled, routerVisualEffectsEnabled, decided, api, scope }
}

describe('useChatAnswerReveal', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('is open from the start: streamed content is never held', () => {
    const h = harness()
    expect(h.api.answerRevealOpen.value).toBe(true)
    h.scope.stop()
  })

  it('stays open when streaming starts, even before any router decision', async () => {
    const h = harness()
    h.isStreaming.value = true
    await nextTick()
    expect(h.api.answerRevealOpen.value).toBe(true)
    h.scope.stop()
  })

  it('ignores router decision timing entirely (no MIN window to wait for)', async () => {
    const h = harness()
    h.isStreaming.value = true
    await nextTick()
    // No decision yet — content is still fully visible.
    expect(h.api.answerRevealOpen.value).toBe(true)
    vi.advanceTimersByTime(MAX + 1000)
    expect(h.api.answerRevealOpen.value).toBe(true)
    h.decided.value = { tier: 'c1' }
    await nextTick()
    expect(h.api.answerRevealOpen.value).toBe(true)
    h.scope.stop()
  })

  it('reveals immediately when routing is not active (no panel to lead)', async () => {
    const h = harness({ routerEnabled: false })
    h.isStreaming.value = true
    await nextTick()
    expect(h.api.answerRevealOpen.value).toBe(true)
    h.scope.stop()
  })

  it('reveals immediately when router visual effects are disabled', async () => {
    const h = harness({ routerFx: false })
    h.isStreaming.value = true
    await nextTick()
    expect(h.api.answerRevealOpen.value).toBe(true)
    h.scope.stop()
  })

  it('resets only after streaming ends, then reopens on the next turn', async () => {
    const h = harness()
    h.isStreaming.value = true
    await nextTick()
    expect(h.api.answerRevealOpen.value).toBe(true)

    h.isStreaming.value = false
    await nextTick()
    expect(h.api.answerRevealOpen.value).toBe(false) // reset for the next turn

    h.isStreaming.value = true
    await nextTick()
    expect(h.api.answerRevealOpen.value).toBe(true) // immediately open again
    h.scope.stop()
  })

  it('revealNow is a safe no-op while open', async () => {
    const h = harness()
    h.isStreaming.value = true
    await nextTick()
    h.api.revealNow()
    expect(h.api.answerRevealOpen.value).toBe(true)
    h.scope.stop()
  })

  it('clears on scope dispose without late reveals (interface stability)', async () => {
    const h = harness()
    h.isStreaming.value = true
    await nextTick()
    h.scope.stop()                      // unmount mid-stream
    vi.advanceTimersByTime(5000)
    expect(h.api.answerRevealOpen.value).toBe(true)
  })
})
