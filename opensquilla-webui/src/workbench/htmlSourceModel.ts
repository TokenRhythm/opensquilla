import { parse } from 'parse5'

type Parse5Location = {
  startOffset?: number
  endOffset?: number
}

type Parse5Node = {
  nodeName?: string
  tagName?: string
  attrs?: Array<{ name: string; value: string }>
  childNodes?: Parse5Node[]
  content?: { childNodes?: Parse5Node[] }
  sourceCodeLocation?: Parse5Location | null
}

export interface HtmlSourceElement {
  tagName: string
  label: string
  /** parse5/Monaco offset measured in JavaScript UTF-16 code units. */
  startOffset: number
  endOffset: number
  depth: number
}

export interface SourcePatch {
  /** Wire offset measured in Unicode code points, matching Python string indexes. */
  startOffset: number
  endOffset: number
  replacement: string
}

export const SOURCE_OFFSET_ENCODING = 'unicode-code-point' as const

/** Convert a Monaco/parse5 UTF-16 boundary into the source RPC's code-point index. */
export function codePointOffsetFromUtf16(source: string, utf16Offset: number): number {
  if (!Number.isSafeInteger(utf16Offset) || utf16Offset < 0 || utf16Offset > source.length) {
    throw new RangeError('UTF-16 source offset is out of bounds')
  }
  const previous = utf16Offset > 0 ? source.charCodeAt(utf16Offset - 1) : 0
  const current = utf16Offset < source.length ? source.charCodeAt(utf16Offset) : 0
  if (
    previous >= 0xD800
    && previous <= 0xDBFF
    && current >= 0xDC00
    && current <= 0xDFFF
  ) {
    throw new RangeError('UTF-16 source offset splits a surrogate pair')
  }
  return Array.from(source.slice(0, utf16Offset)).length
}

export function codePointRangeFromUtf16(
  source: string,
  startOffset: number,
  endOffset: number,
): { startOffset: number; endOffset: number } {
  return {
    startOffset: codePointOffsetFromUtf16(source, startOffset),
    endOffset: codePointOffsetFromUtf16(source, endOffset),
  }
}

function elementLabel(node: Parse5Node, tagName: string): string {
  const attrs = Array.isArray(node.attrs) ? node.attrs : []
  const id = attrs.find(attr => attr.name === 'id')?.value.trim()
  const classes = attrs.find(attr => attr.name === 'class')?.value
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .join('.')
  return `${tagName}${id ? `#${id}` : ''}${classes ? `.${classes}` : ''}`
}

/** Parse canonical HTML while retaining source offsets; the DOM is never serialized back. */
export function htmlSourceElements(source: string): HtmlSourceElement[] {
  const root = parse(source, { sourceCodeLocationInfo: true }) as unknown as Parse5Node
  const result: HtmlSourceElement[] = []

  function visit(node: Parse5Node, depth: number) {
    const tagName = typeof node.tagName === 'string' ? node.tagName.toLowerCase() : ''
    const startOffset = node.sourceCodeLocation?.startOffset
    const endOffset = node.sourceCodeLocation?.endOffset
    if (
      tagName
      && typeof startOffset === 'number'
      && typeof endOffset === 'number'
      && endOffset > startOffset
    ) {
      result.push({
        tagName,
        label: elementLabel(node, tagName),
        startOffset,
        endOffset,
        depth,
      })
    }
    for (const child of node.childNodes || []) visit(child, depth + 1)
    for (const child of node.content?.childNodes || []) visit(child, depth + 1)
  }

  visit(root, 0)
  return result
}

export function htmlElementAtOffsets(
  elements: readonly HtmlSourceElement[],
  startOffset: number,
  endOffset = startOffset,
): HtmlSourceElement | null {
  const normalizedEnd = Math.max(startOffset, endOffset)
  const candidates = elements.filter(element =>
    element.startOffset <= startOffset && element.endOffset >= normalizedEnd)
  return candidates.sort((left, right) =>
    (left.endOffset - left.startOffset) - (right.endOffset - right.startOffset))[0] || null
}

/** Return one minimal edit using the source RPC's Unicode-code-point offsets. */
export function minimalSourcePatch(before: string, after: string): SourcePatch | null {
  if (before === after) return null
  const beforeCodePoints = Array.from(before)
  const afterCodePoints = Array.from(after)
  let startOffset = 0
  const sharedLimit = Math.min(beforeCodePoints.length, afterCodePoints.length)
  while (
    startOffset < sharedLimit
    && beforeCodePoints[startOffset] === afterCodePoints[startOffset]
  ) {
    startOffset += 1
  }

  let beforeEnd = beforeCodePoints.length
  let afterEnd = afterCodePoints.length
  while (
    beforeEnd > startOffset
    && afterEnd > startOffset
    && beforeCodePoints[beforeEnd - 1] === afterCodePoints[afterEnd - 1]
  ) {
    beforeEnd -= 1
    afterEnd -= 1
  }
  return {
    startOffset,
    endOffset: beforeEnd,
    replacement: afterCodePoints.slice(startOffset, afterEnd).join(''),
  }
}
