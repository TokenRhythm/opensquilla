import type { PromptAnnotationSnapshot } from './promptAnnotations'
import type { OfficeSelection } from './officeSelection'

export interface PageAnnotationInput {
  text: string
  selectionText?: string
  locatorHint?: string
  selection?: OfficeSelection
}

/** Ordinary user input. Neither a page reference nor an annotation grants tool access. */
export interface ChatPageContext {
  targetRef?: string
  resourceId?: string
  pagePath?: string
  revisionId?: string
  sha256?: string
  annotations?: PageAnnotationInput[]
}

const encoder = new TextEncoder()
const unpairedSurrogate = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u
// Python str.strip() differs from JavaScript trim() for these Unicode characters.
const surroundingWhitespace = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu

function stripped(value: string): string {
  return value.replace(surroundingWhitespace, '')
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function boundedText(value: unknown, limit: number): value is string {
  return typeof value === 'string' && !unpairedSurrogate.test(value)
    && encoder.encode(value).byteLength <= limit
}

function index(value: unknown): boolean {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 100_000
}

/** Validate the wire selection, without re-projecting or truncating renderer evidence. */
function wireOfficeSelection(value: unknown): OfficeSelection {
  if (!record(value) || !['docx', 'pptx', 'xlsx'].includes(String(value.format))
    || !['text', 'element', 'range'].includes(String(value.kind))
    || (value.format === 'xlsx' ? value.kind === 'text' : value.kind === 'range')) {
    throw new Error('Invalid Office selection format or kind.')
  }
  const strings: Record<string, number> = { text: 16_384, shapeId: 256, name: 256, sheetName: 256, range: 128 }
  const arrays: Record<string, [number, number]> = {
    pageIndexes: [32, 100_000], slideIndexes: [32, 100_000],
    paragraphIds: [64, 256], shapeIds: [64, 256], ranges: [16, 128],
  }
  const enums: Record<string, string[]> = {
    origin: ['master', 'layout', 'slide', 'unknown'],
    elementType: ['chart', 'image', 'picture', 'shape', 'table', 'media', 'group'],
  }
  for (const [key, item] of Object.entries(value)) {
    if (item === undefined) continue // JSON serialization omits undefined object properties.
    let valid: boolean
    if (key === 'format' || key === 'kind') valid = typeof item === 'string'
    else if (Object.prototype.hasOwnProperty.call(strings, key)) valid = boundedText(item, strings[key])
    else if (['pageIndex', 'slideIndex', 'sheetIndex'].includes(key)) valid = index(item)
    else if (Object.prototype.hasOwnProperty.call(arrays, key)) {
      const [count, size] = arrays[key]
      valid = Array.isArray(item) && item.length <= count
        && item.every(entry => key.endsWith('Indexes') ? index(entry) : boundedText(entry, size))
    } else if (Object.prototype.hasOwnProperty.call(enums, key)) valid = typeof item === 'string' && enums[key].includes(item)
    else if (key === 'truncated') valid = typeof item === 'boolean'
    else if (key === 'source') {
      valid = record(item) && Object.keys(item).every(key => ['story', 'storyInstance', 'path'].includes(key))
        && typeof item.story === 'string' && ['body', 'header', 'footer', 'footnote', 'endnote', 'textbox'].includes(item.story)
        && (item.storyInstance === undefined || boundedText(item.storyInstance, 256))
        && (item.path === undefined || Array.isArray(item.path) && item.path.length <= 32 && item.path.every(index))
    } else if (key === 'bounds') {
      valid = record(item) && Object.entries(item).every(([key, value]) => (
        ['flipH', 'flipV'].includes(key) ? typeof value === 'boolean'
          : ['x', 'y', 'width', 'height', 'rotation', 'xPt', 'yPt', 'widthPt', 'heightPt'].includes(key)
            && typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= 1e9
      ))
    } else valid = false
    if (!valid) throw new Error(`Invalid Office selection.${key}.`)
  }
  return JSON.parse(JSON.stringify(value)) as OfficeSelection
}

function validatedPageContext(value: unknown): ChatPageContext | null {
  if (value == null) return null
  if (!record(value)) throw new Error('Invalid page context.')
  const raw = value
  const result: ChatPageContext = {}
  for (const key of ['targetRef', 'resourceId', 'revisionId'] as const) {
    const item = raw[key]
    if (item == null) continue
    if (!boundedText(item, 512) || !stripped(item)) throw new Error(`Invalid pageContext.${key}.`)
    result[key] = stripped(item)
  }
  if (raw.sha256 != null) {
    if (typeof raw.sha256 !== 'string' || !/^[0-9a-fA-F]{64}$/.test(raw.sha256)) {
      throw new Error('Invalid pageContext.sha256.')
    }
    result.sha256 = raw.sha256.toLowerCase()
  }
  if (Boolean(result.revisionId) !== Boolean(result.sha256)) {
    throw new Error('Page context requires both revisionId and sha256.')
  }
  if (result.revisionId && !result.resourceId) throw new Error('Page context version requires a resourceId.')
  if (raw.pagePath != null) {
    const path = raw.pagePath
    if (!boundedText(path, 4096) || !stripped(path) || path.startsWith('/')
      || path.split('/').includes('..') || /[\\:\0]/u.test(path) || !result.resourceId) {
      throw new Error('Invalid pageContext.pagePath.')
    }
    result.pagePath = path
  }
  const annotations = raw.annotations === undefined ? [] : raw.annotations
  if (!Array.isArray(annotations) || annotations.length > 16) {
    throw new Error('Page context supports at most 16 annotations.')
  }
  if (annotations.length) {
    result.annotations = annotations.map((item): PageAnnotationInput => {
      if (!record(item)) throw new Error('Invalid page annotation.')
      const annotation: PageAnnotationInput = { text: '' }
      for (const key of ['text', 'selectionText', 'locatorHint'] as const) {
        if (item[key] == null) continue
        if (!boundedText(item[key], 16_384)) throw new Error('Page annotation exceeds its text limit.')
        annotation[key] = item[key]
      }
      if (!stripped(annotation.text)) throw new Error('Page annotation requires text.')
      if (item.selection !== undefined) annotation.selection = wireOfficeSelection(item.selection)
      return annotation
    })
  }
  if (pageContextByteLength(result) > 65_536) throw new Error('Page context exceeds its size limit.')
  return Object.keys(result).length > 0 ? result : null
}

/**
 * Reading persisted input must not abort queue/history hydration. Keep malformed
 * ordinary fields recoverable; send preflight validates them before any dispatch.
 */
export function normalizePageContext(value: unknown): ChatPageContext | null {
  if (!record(value)) return null
  try { return validatedPageContext(value) } catch {
    const result: Record<string, unknown> = {}
    for (const key of ['targetRef', 'resourceId', 'pagePath', 'revisionId', 'sha256', 'annotations']) {
      if (value[key] !== undefined) result[key] = value[key]
    }
    if (!Object.keys(result).length) return null
    // A recovered record may be Vue-reactive. Persist plain JSON data rather
    // than nested proxies, which IndexedDB's structured clone cannot store.
    try { return JSON.parse(JSON.stringify(result)) as ChatPageContext } catch {
      return result as ChatPageContext
    }
  }
}

export function pageContextForAnnotations(
  snapshots: readonly PromptAnnotationSnapshot[],
): ChatPageContext | null {
  const first = snapshots[0]
  if (!first) return null
  if (snapshots.some(item => item.targetRef !== first.targetRef
    || item.resourceId !== first.resourceId || item.pagePath !== first.pagePath
    || item.revisionId !== first.revisionId || item.sha256 !== first.sha256)) {
    throw new Error('Send annotations for one page at a time.')
  }
  const context: ChatPageContext = {
    ...(first.targetRef ? { targetRef: first.targetRef } : {}),
    ...(first.resourceId ? { resourceId: first.resourceId } : {}),
    ...(first.pagePath ? { pagePath: first.pagePath } : {}),
    ...(first.revisionId ? { revisionId: first.revisionId } : {}),
    ...(first.sha256 ? { sha256: first.sha256 } : {}),
    annotations: snapshots.map(item => ({
      text: item.body,
      ...(item.quote != null ? { selectionText: item.quote } : {}),
      ...(item.locatorHint !== undefined ? { locatorHint: item.locatorHint } : {}),
      ...(item.selection !== undefined ? { selection: item.selection } : {}),
    })),
  }
  return validatedPageContext(context)
}

/**
 * Match Python json.dumps(ensure_ascii=False) after parsing the serialized request.
 * Counting UTF-16 characters or compact JSON bytes underestimates the wire budget.
 */
export function pageContextByteLength(value: unknown): number {
  function adjustments(item: unknown): number {
    if (Array.isArray(item)) {
      return Math.max(0, item.length - 1) + item.reduce<number>((sum, child) => sum + adjustments(child), 0)
    }
    if (item && typeof item === 'object') {
      const entries = Object.values(item).filter(child => child !== undefined)
      return entries.length + Math.max(0, entries.length - 1)
        + entries.reduce<number>((sum, child) => sum + adjustments(child), 0)
    }
    if (typeof item === 'number' && Number.isFinite(item) && item !== 0 && Math.abs(item) < 0.0001) {
      // Python emits these floats in scientific notation with at least two exponent digits.
      const pythonNumber = item.toExponential().replace(/e([+-])(\d)$/u, 'e$10$2')
      return pythonNumber.length - JSON.stringify(item).length
    }
    return 0
  }
  return encoder.encode(JSON.stringify(value)).byteLength + adjustments(value)
}

export function assertPageContextWithinLimits(context: unknown): void {
  validatedPageContext(context)
}

export function pageAnnotationSnapshots(value: unknown): PromptAnnotationSnapshot[] {
  let context: ChatPageContext | null
  try { context = validatedPageContext(value) } catch { return [] }
  if (!context) return []
  const documentId = context.resourceId?.startsWith('document:')
    ? context.resourceId.slice('document:'.length) : context.resourceId || ''
  return (context.annotations || []).map((annotation, index) => ({
    annotationId: `page-annotation-${index}`,
    documentId,
    documentName: '',
    body: annotation.text,
    targetRef: context.targetRef,
    resourceId: context.resourceId,
    revisionId: context.revisionId,
    sha256: context.sha256,
    selection: annotation.selection,
    pagePath: context.pagePath,
    locatorHint: annotation.locatorHint,
    tagName: annotation.selection?.kind || '',
    targetKind: annotation.selection?.kind === 'text' ? 'text' : undefined,
    targetText: annotation.selectionText ?? annotation.selection?.text ?? '',
    quote: annotation.selectionText ?? null,
    sentOrder: index,
  }))
}
