import { ref, watch, onScopeDispose, type Ref } from 'vue'

// The live answer's on-screen reveal used to be gated to a [MIN, MAX] window so
// the model-router panel could "decide first" before the answer appeared. The
// product contract is now "streamed content is the answer": non-reasoning text
// must appear as it streams, with no hold window and no router-led reveal.
// This composable keeps its historical interface (answerRevealOpen, revealNow,
// cleanup) so callers and tests keep working, but the gate is always open —
// streamed content is never held back for decoration.
export const ANSWER_REVEAL_MIN_MS = 0
// Backstop kept for interface stability; the gate is always open, so no timer
// ever needs to fire.
export const ANSWER_REVEAL_MAX_MS = 0

export interface UseChatAnswerRevealOptions {
  isStreaming: Ref<boolean>
  routerEnabled: Ref<boolean>
  routerVisualEffectsEnabled: Ref<boolean>
  /** Truthy once the live turn's router decision has arrived (router locked). */
  routerDecided: () => unknown
}

export function useChatAnswerReveal(options: UseChatAnswerRevealOptions) {
  // Always open: streamed content reveals immediately, every turn, regardless
  // of router panel state or decision timing.
  const answerRevealOpen = ref(true)

  function open() {
    answerRevealOpen.value = true
  }

  // Turn started: nothing to gate, reveal stays open.
  function onStreamStart() {
    open()
  }

  // Router decision arrived: no-op — the answer was never held.
  function onRouterLocked() {
    // no-op
  }

  // Streaming ended: reset to the closed position so the next turn's
  // stream-start transition (false -> true) still re-pins the live edge.
  function reset() {
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
    // no timers to clear; retained for interface stability
  }

  onScopeDispose(cleanup)

  // `revealNow` lets callers force the reveal open immediately (e.g. a
  // user-blocking interrupt). The gate is always open, so this is a no-op that
  // still satisfies the interface.
  return { answerRevealOpen, revealNow: open, cleanup }
}
