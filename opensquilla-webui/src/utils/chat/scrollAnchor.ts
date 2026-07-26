export interface MessageScrollAnchor {
  container: HTMLElement
  element: HTMLElement
  messageId: string
  offsetTop: number
  expectedScrollTop: number
  cancelled: boolean
}

interface StabilizeMessageAnchorOptions {
  isCurrent?: () => boolean
  timeoutMs?: number
}

function messageElements(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('[data-message-id]'))
    .filter(element => Boolean(element.dataset.messageId))
}

function currentAnchorElement(anchor: MessageScrollAnchor): HTMLElement | null {
  if (anchor.container.contains(anchor.element)) return anchor.element
  return messageElements(anchor.container)
    .find(element => element.dataset.messageId === anchor.messageId) ?? null
}

/** Capture the first visible durable message rather than the container height. */
export function captureVisibleMessageAnchor(container: HTMLElement | null): MessageScrollAnchor | null {
  if (!container) return null
  const containerRect = container.getBoundingClientRect()
  const elements = messageElements(container)
  const element = elements.find(candidate => {
    const rect = candidate.getBoundingClientRect()
    return rect.bottom > containerRect.top && rect.top < containerRect.bottom
  })
  if (!element) return null
  const messageId = element.dataset.messageId
  if (!messageId) return null
  return {
    container,
    element,
    messageId,
    offsetTop: element.getBoundingClientRect().top - containerRect.top,
    expectedScrollTop: container.scrollTop,
    cancelled: false,
  }
}

/** Restore one message to the same viewport position after rows are prepended. */
export function restoreMessageAnchor(anchor: MessageScrollAnchor | null): boolean {
  if (!anchor || anchor.cancelled || !anchor.container.isConnected) return false
  const element = currentAnchorElement(anchor)
  if (!element) return false
  const nextOffset = element.getBoundingClientRect().top
    - anchor.container.getBoundingClientRect().top
  const delta = nextOffset - anchor.offsetTop
  if (delta) anchor.container.scrollTop += delta
  anchor.expectedScrollTop = anchor.container.scrollTop
  anchor.element = element
  return true
}

/**
 * Keep the anchor stable while images above it finish decoding. User scroll
 * intent cancels delayed corrections so late media never pulls the reader back.
 */
export function stabilizeMessageAnchor(
  anchor: MessageScrollAnchor | null,
  options: StabilizeMessageAnchorOptions = {},
): () => void {
  if (!anchor || anchor.cancelled) return () => {}
  const container = anchor.container
  const pending = new Set<HTMLImageElement>(
    Array.from(container.querySelectorAll<HTMLImageElement>('img')).filter(image => !image.complete),
  )
  if (pending.size === 0) return () => {}

  const isCurrent = options.isCurrent ?? (() => true)
  const intentEvents = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const
  let timeout: ReturnType<typeof setTimeout> | null = null
  let cleaned = false

  const cleanup = () => {
    if (cleaned) return
    cleaned = true
    if (timeout) clearTimeout(timeout)
    timeout = null
    for (const eventName of intentEvents) container.removeEventListener(eventName, cancel)
    container.removeEventListener('scroll', onScroll)
    for (const image of pending) {
      image.removeEventListener('load', settle)
      image.removeEventListener('error', settle)
    }
    pending.clear()
  }
  const cancel = () => {
    anchor.cancelled = true
    cleanup()
  }
  const onScroll = () => {
    // Programmatic navigation (minimap/latest controls) happens outside the
    // scroll container and therefore emits none of the intent events above.
    // A position other than our own last correction means the reader moved on.
    if (Math.abs(container.scrollTop - anchor.expectedScrollTop) > 1) cancel()
  }
  const settle = (event: Event) => {
    const image = event.currentTarget as HTMLImageElement
    image.removeEventListener('load', settle)
    image.removeEventListener('error', settle)
    pending.delete(image)
    if (!isCurrent() || anchor.cancelled) {
      cleanup()
      return
    }
    queueMicrotask(() => {
      if (isCurrent() && !anchor.cancelled) restoreMessageAnchor(anchor)
    })
    if (pending.size === 0) cleanup()
  }

  for (const eventName of intentEvents) container.addEventListener(eventName, cancel, { passive: true })
  container.addEventListener('scroll', onScroll, { passive: true })
  for (const image of pending) {
    image.addEventListener('load', settle)
    image.addEventListener('error', settle)
  }
  timeout = setTimeout(cleanup, options.timeoutMs ?? 15_000)
  return cancel
}
