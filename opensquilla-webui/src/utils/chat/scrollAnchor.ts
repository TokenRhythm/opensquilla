import { applyProgrammaticScroll } from './scrollMutation'

export interface MessageScrollAnchor {
  container: HTMLElement
  element: HTMLElement
  messageId: string
  offsetTop: number
  expectedScrollTop: number
  cancelled: boolean
}

export interface ElementScrollAnchor {
  container: HTMLElement
  offsetTop: number
}

export interface TextScrollAnchor extends ElementScrollAnchor {
  token: string
  occurrence: number
}

export interface ScrollHandoffGuard {
  acceptCurrentPosition: () => void
  cancel: () => void
  dispose: () => void
  isCancelled: () => boolean
  positionChangedBeyondTolerance: () => boolean
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
  if (delta) {
    applyProgrammaticScroll(anchor.container, () => {
      anchor.container.scrollTop += delta
    })
  }
  anchor.expectedScrollTop = anchor.container.scrollTop
  anchor.element = element
  return true
}

/**
 * Capture an element's viewport position for a one-way DOM handoff. Unlike a
 * durable message anchor, the source element is expected to be replaced (for
 * example, the live answer becoming its canonical Markdown row).
 */
export function captureElementScrollAnchor(
  container: HTMLElement | null,
  element: HTMLElement | null,
): ElementScrollAnchor | null {
  if (!container || !element || !container.contains(element)) return null
  return {
    container,
    offsetTop: element.getBoundingClientRect().top
      - container.getBoundingClientRect().top,
  }
}

/** Restore a replacement element to the source element's viewport position. */
export function restoreElementScrollAnchor(
  anchor: ElementScrollAnchor | null,
  replacement: HTMLElement | null,
): boolean {
  if (
    !anchor
    || !anchor.container.isConnected
    || !replacement
    || !anchor.container.contains(replacement)
  ) return false
  const nextOffset = replacement.getBoundingClientRect().top
    - anchor.container.getBoundingClientRect().top
  const delta = nextOffset - anchor.offsetTop
  if (Math.abs(delta) >= 0.5) {
    applyProgrammaticScroll(anchor.container, () => {
      anchor.container.scrollTop += delta
    })
  }
  return true
}

function occurrenceBefore(text: string, token: string, offset: number): number {
  let occurrence = 0
  let cursor = text.indexOf(token)
  while (cursor >= 0 && cursor < offset) {
    occurrence += 1
    cursor = text.indexOf(token, cursor + token.length)
  }
  return occurrence
}

function occurrenceOffset(text: string, token: string, occurrence: number): number {
  let cursor = -token.length
  for (let index = 0; index <= occurrence; index += 1) {
    cursor = text.indexOf(token, cursor + token.length)
    if (cursor < 0) return -1
  }
  return cursor
}

function textBoundary(root: HTMLElement, offset: number): [Text, number] | null {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let remaining = offset
  let node = walker.nextNode()
  while (node) {
    const text = node as Text
    const length = text.data.length
    if (remaining <= length) return [text, remaining]
    remaining -= length
    node = walker.nextNode()
  }
  return null
}

function caretHitAtPoint(x: number, y: number): [Node, number] | null {
  const caretDocument = document as Document & {
    caretPositionFromPoint?: (x: number, y: number) => {
      offsetNode: Node
      offset: number
    } | null
    caretRangeFromPoint?: (x: number, y: number) => Range | null
  }
  const position = caretDocument.caretPositionFromPoint?.(x, y)
  if (position) return [position.offsetNode, position.offset]
  const range = caretDocument.caretRangeFromPoint?.(x, y)
  return range ? [range.startContainer, range.startOffset] : null
}

function textNodeOffset(root: HTMLElement, target: Text): number {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let offset = 0
  let node = walker.nextNode()
  while (node) {
    if (node === target) return offset
    offset += (node as Text).data.length
    node = walker.nextNode()
  }
  return -1
}

function tokenAroundOffset(text: string, offset: number): { token: string; start: number } | null {
  let nearest: RegExpMatchArray | null = null
  let nearestDistance = Number.POSITIVE_INFINITY
  for (const match of text.matchAll(/[A-Za-z0-9_\u00c0-\uffff-]{4,}/g)) {
    const start = match.index ?? -1
    if (start < 0) continue
    const end = start + match[0].length
    const distance = offset < start ? start - offset : offset > end ? offset - end : 0
    if (distance < nearestDistance) {
      nearest = match
      nearestDistance = distance
    }
    if (distance === 0) break
  }
  if (!nearest || nearestDistance > 64) return null
  const matchStart = nearest.index ?? 0
  const matchText = nearest[0]
  const relativeOffset = Math.max(0, Math.min(matchText.length, offset - matchStart))
  const chunkStart = matchText.length <= 32
    ? 0
    : Math.max(0, Math.min(matchText.length - 32, relativeOffset - 16))
  return {
    token: matchText.slice(chunkStart, chunkStart + 32),
    start: matchStart + chunkStart,
  }
}

/** Capture a visible text token so a semantic DOM replacement can preserve Y. */
export function captureVisibleTextScrollAnchor(
  container: HTMLElement | null,
  root: HTMLElement | null,
): TextScrollAnchor | null {
  if (!container || !root || !container.contains(root)) return null
  const containerRect = container.getBoundingClientRect()
  const rootRect = root.getBoundingClientRect()
  const visibleTop = Math.max(containerRect.top + 8, rootRect.top + 1)
  const visibleBottom = Math.min(containerRect.bottom - 8, rootRect.bottom - 1)
  const visibleLeft = Math.max(containerRect.left + 8, rootRect.left + 1)
  const visibleRight = Math.min(containerRect.right - 8, rootRect.right - 1)
  if (visibleBottom <= visibleTop || visibleRight <= visibleLeft) return null

  // Sample at most 15 viewport points and ask Chromium for the caret directly.
  // This avoids thousands of Range#getBoundingClientRect calls when the reader
  // is deep inside a 128 KiB answer; the remaining occurrence scan is pure
  // string/tree work and does not trigger layout.
  const xFractions = [0.18, 0.5, 0.82]
  const yFractions = [0.12, 0.31, 0.5, 0.69, 0.88]
  const fullText = root.textContent || ''
  for (const yFraction of yFractions) {
    const y = visibleTop + (visibleBottom - visibleTop) * yFraction
    for (const xFraction of xFractions) {
      const x = visibleLeft + (visibleRight - visibleLeft) * xFraction
      const hit = caretHitAtPoint(x, y)
      if (!hit || !(hit[0] instanceof Text) || !root.contains(hit[0])) continue
      const textNode = hit[0]
      const candidate = tokenAroundOffset(textNode.data, hit[1])
      if (!candidate) continue
      const nodeOffset = textNodeOffset(root, textNode)
      if (nodeOffset < 0) continue
      const range = document.createRange()
      range.setStart(textNode, candidate.start)
      range.setEnd(textNode, candidate.start + candidate.token.length)
      const rect = range.getBoundingClientRect()
      if (rect.bottom > containerRect.top && rect.top < containerRect.bottom) {
        const absoluteOffset = nodeOffset + candidate.start
        return {
          container,
          token: candidate.token,
          occurrence: occurrenceBefore(fullText, candidate.token, absoluteOffset),
          offsetTop: rect.top - containerRect.top,
        }
      }
    }
  }
  return null
}

/** Restore the captured text token inside a replacement answer subtree. */
export function restoreTextScrollAnchor(
  anchor: TextScrollAnchor | null,
  replacement: HTMLElement | null,
): boolean {
  if (
    !anchor
    || !anchor.container.isConnected
    || !replacement
    || !anchor.container.contains(replacement)
  ) return false
  const text = replacement.textContent || ''
  const startOffset = occurrenceOffset(text, anchor.token, anchor.occurrence)
  if (startOffset < 0) return false
  const start = textBoundary(replacement, startOffset)
  const end = textBoundary(replacement, startOffset + anchor.token.length)
  if (!start || !end) return false
  const range = document.createRange()
  range.setStart(...start)
  range.setEnd(...end)
  const nextOffset = range.getBoundingClientRect().top
    - anchor.container.getBoundingClientRect().top
  const delta = nextOffset - anchor.offsetTop
  if (Math.abs(delta) >= 0.5) {
    applyProgrammaticScroll(anchor.container, () => {
      anchor.container.scrollTop += delta
    })
  }
  return true
}

/**
 * Cancel a short DOM handoff when fresh reader input arrives. The position
 * baseline also catches scrollbar/programmatic navigation that emits no key or
 * wheel event; callers accept their own corrections after applying them.
 */
export function createScrollHandoffGuard(
  container: HTMLElement,
  tolerancePx = 8,
): ScrollHandoffGuard {
  let cancelled = false
  let expectedScrollTop = container.scrollTop
  const intentEvents = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const
  const cancel = () => { cancelled = true }
  for (const eventName of intentEvents) container.addEventListener(eventName, cancel, { passive: true })
  const dispose = () => {
    for (const eventName of intentEvents) container.removeEventListener(eventName, cancel)
  }
  return {
    acceptCurrentPosition: () => { expectedScrollTop = container.scrollTop },
    cancel,
    dispose,
    isCancelled: () => cancelled,
    positionChangedBeyondTolerance: () => (
      Math.abs(container.scrollTop - expectedScrollTop) > tolerancePx
    ),
  }
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
