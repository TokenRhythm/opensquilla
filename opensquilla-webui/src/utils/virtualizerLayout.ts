import { nextTick } from 'vue'
import type { VirtualItem, Virtualizer } from '@tanstack/vue-virtual'

/** A native scroll event can reach product handlers before core's observer.
 * Never synchronize core by writing from a geometry read: an intentional
 * pending prepend/seek may own its offset. Use live DOM only for that gap.
 */
export function readDistanceFromEnd(
  container: HTMLElement | null | undefined,
  instance?: Pick<Virtualizer<HTMLElement, HTMLElement>,
    'scrollElement' | 'scrollOffset' | 'getDistanceFromEnd'>,
): number {
  if (!container) return Infinity
  if (instance?.scrollElement === container && instance.scrollOffset === container.scrollTop) {
    return instance.getDistanceFromEnd()
  }
  return Math.max(0, container.scrollHeight - container.scrollTop - container.clientHeight)
}

export interface VirtualizerAnchor {
  key: VirtualItem['key']
  intraOffset: number
}

/** Invalidate offscreen sizes after reflow without losing the reader's row. */
export async function remeasureVirtualizer<TScroll extends Element, TItem extends Element>(
  instance: Virtualizer<TScroll, TItem>,
  options: {
    shouldFollowEnd: () => boolean
    isCurrent: () => boolean
    keepAnchorMounted: (key: VirtualItem['key']) => () => void
    getElement: (index: number) => TItem | null
    applyLayoutChange?: () => void
    anchorOverride?: VirtualizerAnchor | null
  },
): Promise<boolean> {
  const offset = instance.scrollOffset ?? 0
  const item = instance.getVirtualItemForOffset(offset)
  const anchor = options.anchorOverride ?? (item ? { key: item.key, intraOffset: offset - item.start } : null)
  const intraOffset = anchor?.intraOffset ?? 0
  const release = anchor ? options.keepAnchorMounted(anchor.key) : () => {}
  const container = instance.scrollElement
  const intentEvents = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const
  let cancelled = false
  const cancel = () => { cancelled = true }
  const current = () => !cancelled && options.isCurrent()
  for (const event of intentEvents) container?.addEventListener(event, cancel, { passive: true })
  try {
    if (options.applyLayoutChange) options.applyLayoutChange()
    else instance.measure()
    // Let the new spacer geometry and leased row reach the DOM before asking
    // the browser to scroll into a range that may have grown during reflow.
    await nextTick()
    if (!current()) return false
    if (options.shouldFollowEnd()) {
      instance.scrollToEnd()
      return true
    }
    if (!anchor) return false
    for (let pass = 0; pass < 2; pass++) {
      if (!current()) return false
      if (options.shouldFollowEnd()) {
        instance.scrollToEnd()
        return true
      }
      const row = instance.getVirtualItems().find(item => item.key === anchor.key)
      if (!row) return false
      instance.scrollToOffset(row.start + intraOffset, { behavior: 'auto' })
      instance.measureElement(options.getElement(row.index))
      // A large reflow can move to a completely different estimated range.
      // Let native scroll update that range before the second measurement,
      // rather than measuring against the still-stale pre-reflow offset.
      if (pass === 0) {
        const targetWindow = instance.targetWindow
        if (targetWindow) await new Promise<void>(resolve => targetWindow.requestAnimationFrame(() => resolve()))
        await nextTick()
      }
    }
    // Native scroll events update TanStack's offset/range at the next frame,
    // not during Vue's microtasks. Keep the anchor leased until that handoff
    // and one final geometry read complete; do not chase the position in a loop.
    const targetWindow = instance.targetWindow
    if (targetWindow) {
      await new Promise<void>(resolve => targetWindow.requestAnimationFrame(() => resolve()))
      await nextTick()
      if (!current()) return false
      if (options.shouldFollowEnd()) instance.scrollToEnd()
      else {
        const row = instance.getVirtualItems().find(item => item.key === anchor.key)
        if (row) instance.scrollToOffset(row.start + intraOffset, { behavior: 'auto' })
      }
      // Releasing the row can deliver new RO entries. First let core consume
      // this last seek, so those entries cannot compensate from an old offset.
      await new Promise<void>(resolve => targetWindow.requestAnimationFrame(() => resolve()))
      await nextTick()
      if (!current()) return false
    }
    return true
  } finally {
    for (const event of intentEvents) container?.removeEventListener(event, cancel)
    release()
  }
}
