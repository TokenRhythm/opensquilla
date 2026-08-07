import { ref, watch, onScopeDispose, type Ref } from 'vue'

// The live answer's on-screen reveal is gated to a [MIN, MAX] window after a
// turn starts streaming, so the model-router panel above it gets a brief
// "decide first" moment before the answer appears — without dragging on slow
// turns. The answer text keeps streaming/accumulating regardless; only its
// display waits, so no streamed content is ever lost.
// The window is deliberately short: the answer reveals the instant the router
// decision lands (typically well before MAX), so the gate gives the panel a
// glance of lead time without ever holding the real answer back for decoration.
// A brief beat so the router panel registers its "deciding" moment before the
// answer appears — kept short so decoration never makes the product feel slow.
// In practice the answer reveals the instant the router decision lands (warm
// routing is well under MAX); these only bound the wait, they do not pad it.
export const ANSWER_REVEAL_MIN_MS = 400
// Backstop kept short: if the router decision never lands for a turn, the answer
// area would otherwise sit blank for the whole window and read as a hang. 600ms
// still gives the router panel a brief lead without a visible stall.
export const ANSWER_REVEAL_MAX_MS = 600
// Optional live-tuning override: localStorage 'opensquilla.chat.answerReveal' = "min,max" (ms).
const ANSWER_REVEAL_KEY = 'opensquilla.chat.answerReveal'

export interface UseChatAnswerRevealOptions {
  isStreaming: Ref<boolean>
  routerEnabled: Ref<boolean>
  routerVisualEffectsEnabled: Ref<boolean>
  /** Truthy once the live turn's router decision has arrived (router locked). */
  routerDecided: () => unknown
}

export function useChatAnswerReveal(options: UseChatAnswerRevealOptions) {
  const answerRevealOpen = ref(false)
  let minTimer: ReturnType<typeof setTimeout> | null = null
  let maxTimer: ReturnType<typeof setTimeout> | null = null
  let startedAt = 0
  let windowMin = ANSWER_REVEAL_MIN_MS
  let windowMax = ANSWER_REVEAL_MAX_MS

  function readWindow(): { min: number; max: number } {
    try {
      const raw = localStorage.getItem(ANSWER_REVEAL_KEY)
      if (raw) {
        const [min, max] = raw.split(',').map(part => Number(part.trim()))
        if (Number.isFinite(min) && Number.isFinite(max) && min >= 0 && max >= min) {
          return { min, max }
        }
      }
    } catch {}
    return { min: ANSWER_REVEAL_MIN_MS, max: ANSWER_REVEAL_MAX_MS }
  }

  function clearTimers() {
    if (minTimer) { clearTimeout(minTimer); minTimer = null }
    if (maxTimer) { clearTimeout(maxTimer); maxTimer = null }
  }

  function open() {
    clearTimers()
    answerRevealOpen.value = true
  }

  function routingActive(): boolean {
    return options.routerEnabled.value && options.routerVisualEffectsEnabled.value
  }

  // Turn started: with no router panel to lead, reveal immediately (unchanged
  // behavior); otherwise hold the answer and arm the MAX backstop.
  function onStreamStart() {
    clearTimers()
    if (!routingActive()) {
      answerRevealOpen.value = true
      return
    }
    answerRevealOpen.value = false
    startedAt = Date.now()
    const w = readWindow()
    windowMin = w.min
    windowMax = w.max
    maxTimer = setTimeout(open, windowMax)
  }

  // Router decision arrived: reveal as soon as MIN has elapsed (immediately if
  // it already has), capped implicitly under MAX since MIN <= MAX.
  function onRouterLocked() {
    if (answerRevealOpen.value || !routingActive()) return
    const remaining = Math.max(0, windowMin - (Date.now() - startedAt))
    clearTimers()
    if (remaining <= 0) {
      open()
      return
    }
    minTimer = setTimeout(open, remaining)
  }

  function reset() {
    clearTimers()
    answerRevealOpen.value = false
  }

  watch(options.isStreaming, (streaming) => {
    if (streaming) onStreamStart()
    else reset()
  })

  watch(options.routerDecided, (decided, prev) => {
    if (!prev && decided) onRouterLocked()
  })

  function cleanup() {
    clearTimers()
  }

  onScopeDispose(cleanup)

  // `revealNow` lets callers force the reveal open immediately (e.g. a
  // user-blocking interrupt must not wait behind the router-lead window).
  return { answerRevealOpen, revealNow: open, cleanup }
}
