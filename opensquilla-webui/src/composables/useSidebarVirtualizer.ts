import { computed, nextTick, onScopeDispose, type ComputedRef, type Ref } from 'vue'
import { defaultRangeExtractor, observeElementRect, useVirtualizer, type VirtualizerOptions } from '@tanstack/vue-virtual'
import type { SidebarDisplayRow } from '@/utils/sidebarDisplayProjection'
import { readDistanceFromEnd } from '@/utils/virtualizerLayout'

interface SidebarVirtualBlock {
  key: string
  rows: SidebarDisplayRow[]
  collapsed: boolean
}

function createSidebarRectObserver() {
  let cancelPendingRestore: (() => void) | undefined
  const observeSidebarRect: VirtualizerOptions<HTMLElement, HTMLElement>['observeElementRect'] =
    (instance, callback) => {
      let pendingAnchor: { key: string | number | bigint; offset: number } | undefined
      let disposed = false
      let restoreFrame: number | undefined
      const view = instance.targetWindow
      const element = instance.scrollElement
      const cancelRestore = () => {
        pendingAnchor = undefined
        if (restoreFrame !== undefined) view?.cancelAnimationFrame(restoreFrame)
        restoreFrame = undefined
      }
      cancelPendingRestore = cancelRestore
      const inputEvents = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const
      for (const event of inputEvents) element?.addEventListener(event, cancelRestore, { passive: true })
      const cleanup = observeElementRect(instance, rect => {
        const previousWidth = instance.scrollRect?.width
        let restoreAfterCommit = false
        if (!pendingAnchor && previousWidth && rect.width && previousWidth !== rect.width) {
          const offset = instance.scrollOffset ?? 0
          const item = instance.getVirtualItemForOffset(offset)
          if (item) {
            pendingAnchor = { key: item.key, offset: offset - item.start }
            restoreAfterCommit = true
          }
        }
        callback({ ...rect, height: Math.max(1, rect.height) })
        if (!restoreAfterCommit) return
        // Drawer layouts change several measured row heights at once. Keep
        // ordinary backward-scroll behavior. Container and row observers may
        // deliver in separate microtasks; wait for the frame's entire batch and
        // Vue's DOM commit, unless a new user gesture takes over the position.
        restoreFrame = view?.requestAnimationFrame(() => {
          restoreFrame = undefined
          void nextTick(() => {
            const anchor = pendingAnchor
            pendingAnchor = undefined
            if (disposed || !anchor) return
            const current = instance.getVirtualItems().find(item => item.key === anchor.key)
            if (!current) return
            const target = current.start + anchor.offset
            if (Math.abs(target - (instance.scrollOffset ?? 0)) > 0.5) {
              instance.scrollToOffset(target, { behavior: 'auto' })
            }
          })
        })
      })
      return () => {
        disposed = true
        cancelRestore()
        if (cancelPendingRestore === cancelRestore) cancelPendingRestore = undefined
        for (const event of inputEvents) element?.removeEventListener(event, cancelRestore)
        cleanup?.()
      }
    }
  return { observe: observeSidebarRect, cancel: () => cancelPendingRestore?.() }
}

/** One geometry owner across Pinned, Projects and Recents. Headers stay mounted
 * for navigation/accessibility; only expanded rows enter the virtual sequence.
 * Spacers preserve normal flow, including pointer-capture and teleported menus.
 */
export function useSidebarVirtualizer<T extends SidebarVirtualBlock>(
  container: Ref<HTMLElement | null>,
  blocks: ComputedRef<T[]>,
  retainedRows: ComputedRef<string[]>,
  focusedItem: Ref<string>,
) {
  const projection = computed(() => {
    const entries: Array<{ key: string; row?: SidebarDisplayRow }> = []
    const rowIndexes = new Map<string, number>()
    const sections = blocks.value.map(block => {
      const headingIndex = entries.length
      entries.push({ key: `heading:${block.key}` })
      if (!block.collapsed) for (const row of block.rows) {
        rowIndexes.set(row.key, entries.length)
        entries.push({ key: `row:${row.key}`, row })
      }
      return { block, headingIndex }
    })
    return {
      entries, rowIndexes, sections,
      indexes: new Map(entries.map((entry, index) => [entry.key, index])),
    }
  })
  const virtualized = computed(() => projection.value.rowIndexes.size >= 100)
  const layoutObserver = createSidebarRectObserver()
  const virtualizer = useVirtualizer<HTMLElement, HTMLElement>(computed(() => {
    const { entries, indexes, rowIndexes, sections } = projection.value
    const retained = new Set(sections.map(section => section.headingIndex))
    for (const key of retainedRows.value) {
      const index = rowIndexes.get(key)
      if (index !== undefined) retained.add(index)
    }
    const focused = indexes.get(focusedItem.value)
    if (focused !== undefined) {
      // Adjacent rows must exist before the browser's next native Tab/Shift+Tab,
      // even when focus is outside the viewport (e.g. returning from a menu).
      retained.add(focused)
      for (const step of [-1, 1]) {
        for (let index = focused + step; index >= 0 && index < entries.length; index += step) {
          retained.add(index)
          if (entries[index]?.row) break
        }
      }
    }
    return {
      enabled: virtualized.value,
      count: entries.length,
      getScrollElement: () => container.value,
      getItemKey: (index: number) => entries[index]!.key,
      estimateSize: () => 44,
      initialRect: { width: 260, height: 600 },
      // A closed mobile drawer has zero height. Keep a minimal virtual range
      // so its retained headings still describe the correct scroll extent.
      observeElementRect: layoutObserver.observe,
      overscan: 5,
      anchorTo: 'end' as const,
      followOnAppend: false,
      scrollEndThreshold: -1,
      rangeExtractor: (range: Parameters<typeof defaultRangeExtractor>[0]) =>
        [...new Set([...defaultRangeExtractor(range), ...retained])].sort((a, b) => a - b),
    }
  }))

  const renderedBlocks = computed(() => {
    const { entries, sections } = projection.value
    if (!virtualized.value) return sections.map(({ block, headingIndex }) => ({
      block, headingIndex, gapAfter: 0,
      rows: block.collapsed ? [] : block.rows.map((row, index) => ({ row, index: headingIndex + index + 1, gapBefore: 0 })),
    }))
    const items = virtualizer.value.getVirtualItems()
    const measured = new Map(items.map(item => [item.index, item]))
    const total = virtualizer.value.getTotalSize()
    return sections.map(({ block, headingIndex }, sectionIndex) => {
      const nextHeading = sections[sectionIndex + 1]?.headingIndex ?? entries.length
      let cursor = measured.get(headingIndex)?.end ?? 0
      const rows = items.filter(item => item.index > headingIndex && item.index < nextHeading)
        .map(item => {
          const gapBefore = Math.max(0, item.start - cursor)
          cursor = item.end
          return { row: entries[item.index]!.row!, index: item.index, gapBefore }
        })
      const end = measured.get(nextHeading)?.start ?? total
      return { block, headingIndex, rows, gapAfter: Math.max(0, end - cursor) }
    })
  })

  const pendingMeasurements = new Set<HTMLElement>()
  let measurementQueued = false
  let disposed = false
  onScopeDispose(() => {
    disposed = true
    pendingMeasurements.clear()
  })
  function measureElement(element: Element | null) {
    if (!virtualized.value) return
    if (element) pendingMeasurements.add(element as HTMLElement)
    if (measurementQueued) return
    measurementQueued = true
    // Vue calls function refs during its patch. A layout read after an old
    // keyed spacer is removed can clamp scrollTop before its replacement is
    // inserted. Measure the complete window after the DOM commit instead.
    void nextTick(() => {
      measurementQueued = false
      const elements = [...pendingMeasurements]
      pendingMeasurements.clear()
      if (disposed || !virtualized.value) return
      virtualizer.value.measureElement(null)
      for (const item of elements) {
        if (item.isConnected) virtualizer.value.measureElement(item)
      }
    })
  }

  async function revealRow(key: string) {
    await nextTick()
    const index = projection.value.rowIndexes.get(key)
    if (virtualized.value && index !== undefined) {
      // Explicit navigation takes precedence over the previous reading anchor.
      layoutObserver.cancel()
      virtualizer.value.scrollToIndex(index, { align: 'auto', behavior: 'auto' })
    }
  }

  return {
    virtualized, renderedBlocks, measureElement, revealRow,
    hasRow: (key: string) => projection.value.rowIndexes.has(key),
    distanceFromEnd: () => readDistanceFromEnd(container.value, virtualizer.value),
  }
}
