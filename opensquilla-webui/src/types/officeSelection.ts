import type { OfficePreviewFormat } from '@/utils/workbench/artifactPreview'

/** Context for an agent to re-read and verify; never an executable edit operation. */
export interface OfficeSelection {
  format: OfficePreviewFormat
  kind: 'text' | 'element' | 'range'
  text?: string
  pageIndex?: number
  slideIndex?: number
  sheetIndex?: number
  pageIndexes?: number[]
  slideIndexes?: number[]
  paragraphIds?: string[]
  shapeIds?: string[]
  shapeId?: string
  name?: string
  sheetName?: string
  origin?: 'master' | 'layout' | 'slide' | 'unknown'
  elementType?: 'chart' | 'image' | 'picture' | 'shape' | 'table' | 'media' | 'group'
  range?: string
  ranges?: string[]
  source?: { story: string; storyInstance: string; path: number[] }
  truncated: boolean
}

const encoder = new TextEncoder()
function boundedText(value: unknown, limit: number): string | undefined {
  if (typeof value !== 'string') return undefined
  if (encoder.encode(value).length <= limit) return value
  let result = '', bytes = 0
  for (const character of value) {
    bytes += encoder.encode(character).length
    if (bytes > limit) break
    result += character
  }
  return result
}
function object(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}
function index(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0 && Number(value) <= 100_000
}

/** Allow-list projection also keeps vendor models, binary data and comments out of chat. */
export function normalizeOfficeSelection(value: unknown): OfficeSelection | null {
  const raw = object(value)
  if (!raw || !['docx', 'pptx', 'xlsx'].includes(String(raw.format))
    || !['text', 'element', 'range'].includes(String(raw.kind))
    || (raw.format === 'xlsx' ? raw.kind === 'text' : raw.kind === 'range')) return null
  const result: OfficeSelection = {
    format: raw.format as OfficePreviewFormat,
    kind: raw.kind as OfficeSelection['kind'],
    truncated: raw.truncated === true,
  }
  const text = boundedText(raw.text, 16_384)
  if (text !== undefined) {
    result.text = text
    if (text !== raw.text) result.truncated = true
  }
  for (const key of ['pageIndex', 'slideIndex', 'sheetIndex'] as const) {
    if (index(raw[key])) result[key] = raw[key]
  }
  for (const key of ['pageIndexes', 'slideIndexes'] as const) {
    if (Array.isArray(raw[key])) {
      result[key] = raw[key].filter(index).slice(0, 32)
      if (result[key].length !== raw[key].length) result.truncated = true
    }
  }
  for (const key of ['paragraphIds', 'shapeIds'] as const) {
    if (Array.isArray(raw[key])) result[key] = raw[key].flatMap(item => {
      const text = boundedText(item, 256)
      return text === undefined ? [] : [text]
    }).slice(0, 64)
    if (Array.isArray(raw[key]) && JSON.stringify(result[key]) !== JSON.stringify(raw[key])) result.truncated = true
  }
  for (const key of ['shapeId', 'name', 'sheetName'] as const) {
    const text = boundedText(raw[key], 256)
    if (text !== undefined) { result[key] = text; if (text !== raw[key]) result.truncated = true }
  }
  if (['master', 'layout', 'slide', 'unknown'].includes(String(raw.origin))) {
    result.origin = raw.origin as OfficeSelection['origin']
  }
  if (['chart', 'image', 'picture', 'shape', 'table', 'media', 'group'].includes(String(raw.elementType))) {
    result.elementType = raw.elementType as OfficeSelection['elementType']
  }
  const range = boundedText(raw.range, 128)
  if (range !== undefined) result.range = range
  if (Array.isArray(raw.ranges)) result.ranges = raw.ranges.flatMap(item => {
    const text = boundedText(item, 128)
    return text === undefined ? [] : [text]
  }).slice(0, 16)
  if (Array.isArray(raw.ranges) && JSON.stringify(result.ranges) !== JSON.stringify(raw.ranges)) result.truncated = true
  const source = object(raw.source)
  if (source && ['body', 'header', 'footer', 'footnote', 'endnote', 'textbox'].includes(String(source.story))
    && typeof source.storyInstance === 'string' && Array.isArray(source.path)
    && source.path.length <= 32 && source.path.every(index)) {
    result.source = { story: String(source.story), storyInstance: boundedText(source.storyInstance, 256)!, path: [...source.path] }
  }
  return result
}

function columnName(column: number): string {
  let name = ''
  for (let value = column; value > 0; value = Math.floor((value - 1) / 26)) {
    name = String.fromCharCode(65 + (value - 1) % 26) + name
  }
  return name
}

/** Silurus XLSX uses one-based cell coordinates. Only bounded human-readable ranges leave the adapter. */
export function projectOfficeSelection(value: unknown): OfficeSelection | null {
  const raw = object(value)
  if (!raw) return null
  if (raw.format === 'docx' && raw.kind === 'text' && !raw.source && Array.isArray(raw.runs)) {
    const sources = raw.runs.flatMap(run => { const source = object(object(run)?.source); return source ? [source] : [] })
    // A single source paragraph is meaningful. Do not pretend a multi-paragraph range has one source.
    if (sources.length && sources.every(source => JSON.stringify(source) === JSON.stringify(sources[0]))) {
      return normalizeOfficeSelection({ ...raw, source: sources[0] })
    }
  }
  if (raw.format !== 'xlsx' || raw.kind !== 'range') return normalizeOfficeSelection(raw)
  const state = object(raw.selection)
  const ranges = (Array.isArray(state?.areas) ? state.areas : []).flatMap(area => {
    const item = object(area)
    if (!item) return []
    const coordinate = (value: unknown, limit: number): value is number => Number.isInteger(value) && Number(value) > 0 && Number(value) <= limit
    if (item.kind === 'cells' && coordinate(item.top, 1_048_576) && coordinate(item.bottom, 1_048_576)
      && coordinate(item.left, 16_384) && coordinate(item.right, 16_384)) {
      return [`${columnName(Number(item.left))}${item.top}:${columnName(Number(item.right))}${item.bottom}`]
    }
    if (item.kind === 'rows' && coordinate(item.firstRow, 1_048_576) && coordinate(item.lastRow, 1_048_576)) return [`${item.firstRow}:${item.lastRow}`]
    if (item.kind === 'columns' && coordinate(item.firstColumn, 16_384) && coordinate(item.lastColumn, 16_384)) return [`${columnName(item.firstColumn)}:${columnName(item.lastColumn)}`]
    return item.kind === 'sheet' ? ['*'] : []
  })
  const cells = Array.isArray(raw.cells) ? raw.cells : []
  const text = cells.slice(0, 64).flatMap(cell => {
    const item = object(cell), address = object(item?.address)
    return item && address && Number.isInteger(address.row) && Number.isInteger(address.col)
      && Number(address.row) > 0 && Number(address.row) <= 1_048_576 && Number(address.col) > 0 && Number(address.col) <= 16_384
      ? [`${columnName(Number(address.col))}${address.row}: ${String(item.displayText || '').slice(0, 512)}${typeof item.formula === 'string' ? ` [formula: ${item.formula.slice(0, 512)}]` : ''}`] : []
  }).join('\n')
  return normalizeOfficeSelection({ ...raw, ranges, range: ranges[0], text,
    truncated: raw.truncated === true || cells.length > 64 || ranges.length > 16 })
}
