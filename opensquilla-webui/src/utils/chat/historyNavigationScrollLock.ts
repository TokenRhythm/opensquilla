import type { Ref } from 'vue'

/**
 * Keeps live-edge following disabled while a minimap-triggered smooth scroll is
 * crossing the bottom threshold. Intermediate scroll frames must not be
 * mistaken for the reader returning to the live edge.
 */
export function createHistoryNavigationScrollLock(autoScroll: Ref<boolean>) {
  let locked = false
  let interrupted = false

  return {
    start() {
      locked = true
      interrupted = false
      autoScroll.value = false
    },
    interrupt() {
      if (!locked) return false
      const firstInterruption = !interrupted
      interrupted = true
      autoScroll.value = false
      return firstInterruption
    },
    finish() {
      const wasInterrupted = interrupted
      locked = false
      interrupted = false
      return wasInterrupted
    },
    updateFromScroll(bottomGap: number) {
      if (!locked) autoScroll.value = bottomGap < 60
    },
    get locked() {
      return locked
    },
  }
}
