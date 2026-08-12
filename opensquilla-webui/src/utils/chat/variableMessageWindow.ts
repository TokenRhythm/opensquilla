export const CHAT_HISTORY_VIRTUALIZATION_THRESHOLD = 60
export const CHAT_HISTORY_OVERSCAN_VIEWPORTS = 2
export const CHAT_HISTORY_MAX_MOUNTED_ROWS = 30

export interface VariableWindowRow {
  key: string
  estimatedSize: number
}

export interface VariableWindowEntry {
  index: number
  key: string
  /** Empty height between the preceding mounted row and this row. */
  gapBefore: number
}

export interface VariableWindowLayout {
  entries: VariableWindowEntry[]
  offsets: number[]
  sizes: number[]
  topSpacer: number
  bottomSpacer: number
  totalSize: number
  viewportStartIndex: number
  viewportEndIndex: number
}

export interface VariableWindowOptions {
  rows: readonly VariableWindowRow[]
  measuredSizes: ReadonlyMap<string, number>
  viewportStart: number
  viewportSize: number
  overscanViewports?: number
  maxMountedRows?: number
  forceIndexes?: Iterable<number>
}

export interface ChatMessageListVirtualizer {
  ensureMessageVisible: (index: number) => Promise<HTMLElement | null>
  releaseEnsuredMessage: (index?: number) => void
  messageOffset: (index: number) => number | null
  isVirtualized: () => boolean
}

function finiteNonNegative(value: number, fallback = 0): number {
  return Number.isFinite(value) && value >= 0 ? value : fallback
}

function rowSize(row: VariableWindowRow, measuredSizes: ReadonlyMap<string, number>): number {
  const measured = measuredSizes.get(row.key)
  if (measured !== undefined && Number.isFinite(measured) && measured > 0) return measured
  return Math.max(1, finiteNonNegative(row.estimatedSize, 1))
}

/** Return the row whose interval contains offset, clamped to the row range. */
export function variableWindowIndexAtOffset(offsets: readonly number[], offset: number): number {
  if (offsets.length <= 1) return 0
  const rowCount = offsets.length - 1
  const target = Math.max(0, finiteNonNegative(offset))
  let low = 0
  let high = rowCount - 1
  let result = 0
  while (low <= high) {
    const middle = Math.floor((low + high) / 2)
    if (offsets[middle] <= target) {
      result = middle
      low = middle + 1
    } else {
      high = middle - 1
    }
  }
  return Math.min(rowCount - 1, result)
}

/**
 * Build a variable-height window without coupling it to Vue or DOM geometry.
 * Forced rows may sit outside the viewport range; gap spacers preserve their
 * logical location without mounting every row between them.
 */
export function buildVariableWindow(options: VariableWindowOptions): VariableWindowLayout {
  const rows = options.rows
  if (rows.length === 0) {
    return {
      entries: [],
      offsets: [0],
      sizes: [],
      topSpacer: 0,
      bottomSpacer: 0,
      totalSize: 0,
      viewportStartIndex: 0,
      viewportEndIndex: -1,
    }
  }

  const sizes = rows.map(row => rowSize(row, options.measuredSizes))
  const offsets = new Array<number>(rows.length + 1)
  offsets[0] = 0
  for (let index = 0; index < sizes.length; index += 1) {
    offsets[index + 1] = offsets[index] + sizes[index]
  }

  const totalSize = offsets[offsets.length - 1]
  const viewportSize = Math.max(1, finiteNonNegative(options.viewportSize, 1))
  const viewportStart = Math.min(totalSize, finiteNonNegative(options.viewportStart))
  const overscan = viewportSize * finiteNonNegative(
    options.overscanViewports ?? CHAT_HISTORY_OVERSCAN_VIEWPORTS,
  )
  const renderStart = Math.max(0, viewportStart - overscan)
  const renderEnd = Math.min(totalSize, viewportStart + viewportSize + overscan)
  const startIndex = variableWindowIndexAtOffset(offsets, renderStart)
  const endIndex = Math.max(
    startIndex,
    variableWindowIndexAtOffset(offsets, Math.max(renderStart, renderEnd - 0.001)),
  )
  const viewportStartIndex = variableWindowIndexAtOffset(offsets, viewportStart)
  const viewportEndIndex = variableWindowIndexAtOffset(
    offsets,
    Math.max(viewportStart, Math.min(totalSize, viewportStart + viewportSize) - 0.001),
  )

  const forced = new Set<number>()
  for (const index of options.forceIndexes ?? []) {
    if (Number.isInteger(index) && index >= 0 && index < rows.length) forced.add(index)
  }
  const selected = new Set<number>(forced)
  for (let index = viewportStartIndex; index <= viewportEndIndex; index += 1) {
    selected.add(index)
  }

  // Keep two viewports available when row heights permit it, but do not let a
  // long run of compact messages defeat the DOM ceiling. Visible and forced
  // rows are correctness constraints; the nearest overscan rows consume the
  // remaining budget symmetrically before farther rows.
  const maxMountedRows = Math.max(
    1,
    Math.floor(finiteNonNegative(
      options.maxMountedRows ?? CHAT_HISTORY_MAX_MOUNTED_ROWS,
      CHAT_HISTORY_MAX_MOUNTED_ROWS,
    )),
  )
  const overscanCandidates: number[] = []
  for (let index = startIndex; index <= endIndex; index += 1) {
    if (!selected.has(index)) overscanCandidates.push(index)
  }
  overscanCandidates.sort((left, right) => {
    const distance = (index: number) => index < viewportStartIndex
      ? viewportStartIndex - index
      : index - viewportEndIndex
    return distance(left) - distance(right) || left - right
  })
  const overscanBudget = Math.max(0, maxMountedRows - selected.size)
  for (const index of overscanCandidates.slice(0, overscanBudget)) selected.add(index)
  const indexes = Array.from(selected).sort((left, right) => left - right)

  const entries: VariableWindowEntry[] = []
  let previousEnd = 0
  for (const index of indexes) {
    const start = offsets[index]
    entries.push({
      index,
      key: rows[index].key,
      gapBefore: entries.length === 0 ? 0 : Math.max(0, start - previousEnd),
    })
    previousEnd = offsets[index + 1]
  }

  const firstIndex = indexes[0] ?? 0
  const lastIndex = indexes[indexes.length - 1] ?? -1
  return {
    entries,
    offsets,
    sizes,
    topSpacer: offsets[firstIndex],
    bottomSpacer: lastIndex >= 0 ? Math.max(0, totalSize - offsets[lastIndex + 1]) : totalSize,
    totalSize,
    viewportStartIndex,
    viewportEndIndex,
  }
}
