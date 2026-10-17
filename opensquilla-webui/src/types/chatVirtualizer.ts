import type { ScrollToOptions } from '@tanstack/vue-virtual'

/** Presentation geometry only; no navigation or task authority is restored. */
export interface ChatViewportSnapshot {
  scrollTop: number
  width: number
  followLiveEdge: boolean
  anchor: { key: string; intraOffset: number } | null
  rows: Array<{ key: string; size: number }>
}

/** Product-facing operations; measurement and range state belong to TanStack. */
export interface ChatMessageListVirtualizer {
  /** Unique to this rendered list, including when a main and side coexist. */
  messageAnchorId: (index: number) => string
  ensureMessageVisible: (index: number) => Promise<HTMLElement | null>
  ensureMessageContentVisible?: (index: number) => Promise<HTMLElement | null>
  messageBodyText?: (index: number) => string
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
  captureViewport?: () => ChatViewportSnapshot
  restoreViewport?: (snapshot: ChatViewportSnapshot) => void
}
