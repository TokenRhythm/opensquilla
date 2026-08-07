import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ref, nextTick, effectScope, type EffectScope } from 'vue'
import {
  useChatAnswerReveal,
  ANSWER_REVEAL_MIN_MS as MIN,
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

  it('holds the answer until MIN when the router decides quickly', async () => {
    const h = harness()
    h.isStreaming.value = true
    await nextTick()
    expect(h.api.answerRevealOpen.value).toBe(false)

    vi.advanceTimersByTime(50)          // decision arrives well before MIN
    h.decided.value = { tier: 'c1' }
    await nextTick()
    expect(h.api.answerRevealOpen.value).toBe(false) // still held — MIN not reached

    vi.advanceTimersByTime(MIN - 50 - 1) // just before MIN
    expect(h.api.answerRevealOpen.value).toBe(false)
    vi.advanceTimersByTime(1)            // == MIN
    expect(h.api.answerRevealOpen.value).toBe(true)
    h.scope.stop()
  })

  it('reveals as soon as the decision lands if MIN has already passed', async () => {
    const h = harness()
    h.isStreaming.value = true
    await nextTick()
    const past = Math.floor((MIN + MAX) / 2) // past MIN, before MAX
    vi.advanceTimersByTime(past)
    expect(h.api.answerRevealOpen.value).toBe(false) // no decision yet → still held
    h.decided.value = { tier: 'c2' }
    await nextTick()
    expect(h.api.answerRevealOpen.value).toBe(true)  // decision after MIN → reveal now
    h.scope.stop()
  })

  it('falls back to MAX when the decision never arrives', async () => {
    const h = harness()
    h.isStreaming.value = true
    await nextTick()
    vi.advanceTimersByTime(MAX - 1)
    expect(h.api.answerRevealOpen.value).toBe(false)
    vi.advanceTimersByTime(1)            // == MAX
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

  it('resets the gate and clears timers when streaming ends', async () => {
    const h = harness()
    h.isStreaming.value = true
    await nextTick()
    vi.advanceTimersByTime(4000)
    expect(h.api.answerRevealOpen.value).toBe(true)

    h.isStreaming.value = false
    await nextTick()
    expect(h.api.answerRevealOpen.value).toBe(false) // reset for the next turn
    h.scope.stop()
  })

  it('clears pending timers on scope dispose (no late reveal)', async () => {
    const h = harness()
    h.isStreaming.value = true
    await nextTick()
    h.scope.stop()                      // unmount mid-window
    vi.advanceTimersByTime(5000)
    expect(h.api.answerRevealOpen.value).toBe(false)
  })
})
