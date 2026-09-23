import { computed, nextTick, type ComputedRef, type Ref } from 'vue'
import { defaultRangeExtractor, observeElementRect, useVirtualizer, type VirtualizerOptions } from '@tanstack/vue-virtual'
import type { SidebarDisplayRow } from '@/utils/sidebarDisplayProjection'
import { readDistanceFromEnd } from '@/utils/virtualizerLayout'

interface SidebarVirtualBlock {
  key: string
  rows: SidebarDisplayRow[]
  collapsed: boolean
}

const observeSidebarRect: VirtualizerOptions<HTMLElement, HTMLElement>['observeElementRect'] =
  (instance, callback) => observeElementRect(instance,
    rect => callback({ ...rect, height: Math.max(1, rect.height) }))

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
      observeElementRect: observeSidebarRect,
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

  function measureElement(element: Element | null) {
    if (virtualized.value) virtualizer.value.measureElement(element as HTMLElement | null)
  }

  async function revealRow(key: string) {
    await nextTick()
    const index = projection.value.rowIndexes.get(key)
    if (virtualized.value && index !== undefined) {
      virtualizer.value.scrollToIndex(index, { align: 'auto', behavior: 'auto' })
    }
  }

  return {
    virtualized, renderedBlocks, measureElement, revealRow,
    hasRow: (key: string) => projection.value.rowIndexes.has(key),
    distanceFromEnd: () => readDistanceFromEnd(container.value, virtualizer.value),
  }
}
