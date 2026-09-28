import type { ScrollToOptions } from '@tanstack/vue-virtual'

/** Product-facing operations; measurement and range state belong to TanStack. */
export interface ChatMessageListVirtualizer {
  ensureMessageVisible: (index: number) => Promise<HTMLElement | null>
  releaseEnsuredMessage: (index?: number) => void
  messageIndexAtOffset: (offset: number) => number | null
  scrollToMessage: (index: number, options?: ScrollToOptions) => void
  scrollToEnd: (options?: Pick<ScrollToOptions, 'behavior'>) => void
  getDistanceFromEnd: () => number
  hasPendingLayout: () => boolean
  cancelScroll: () => void
  /** Temporarily give a live-to-canonical text handoff ownership of position. */
  beginScrollHandoff: () => () => void
  geometryVersion: () => number
  remeasure: () => void
  isVirtualized: () => boolean
}
