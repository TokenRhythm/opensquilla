import { normalizeOfficeSelection, type OfficeSelection } from '@/types/officeSelection'
import type { OfficeHitTarget, OfficeSelectionAnchor, OfficeViewportRect } from './officeSelectionInteraction'

export function viewportRect(rect: OfficeViewportRect): OfficeViewportRect {
  return { left: rect.left, top: rect.top, width: rect.width, height: rect.height }
}
function valid(rect: OfficeViewportRect) {
  return Object.values(rect).every(Number.isFinite) && rect.width > 0 && rect.height > 0
}
function union(rects: OfficeViewportRect[]): OfficeViewportRect {
  const left = Math.min(...rects.map(rect => rect.left)), top = Math.min(...rects.map(rect => rect.top))
  return { left, top, width: Math.max(...rects.map(rect => rect.left + rect.width)) - left,
    height: Math.max(...rects.map(rect => rect.top + rect.height)) - top }
}
export function selectionAnchor(rects: readonly OfficeViewportRect[], viewport: OfficeViewportRect): OfficeSelectionAnchor | null {
  const all = rects.filter(valid).map(viewportRect)
  if (!all.length || !valid(viewport)) return null
  const visible = all.map(rect => {
    const left = Math.max(rect.left, viewport.left), top = Math.max(rect.top, viewport.top)
    return { left, top, width: Math.min(rect.left + rect.width, viewport.left + viewport.width) - left,
      height: Math.min(rect.top + rect.height, viewport.top + viewport.height) - top }
  }).filter(valid)
  return { rect: union(visible.length ? visible : all), rects: all, viewport: viewportRect(viewport), visible: visible.length > 0 }
}

/** Final DOM ranges, including partial runs, are more precise than a pointer-down hit. */
export function nativeTextRects(container: HTMLElement): OfficeViewportRect[] {
  const selection = container.ownerDocument.getSelection()
  if (!selection || selection.isCollapsed) return []
  const rects: OfficeViewportRect[] = []
  for (let index = 0; index < selection.rangeCount; index++) {
    const range = selection.getRangeAt(index)
    if (!container.contains(range.startContainer) || !container.contains(range.endContainer)) return []
    if (typeof range.getClientRects === 'function') rects.push(...Array.from(range.getClientRects(), viewportRect))
  }
  return rects.filter(valid)
}

interface ParagraphRun {
  text: string; x: number; y: number; w: number; h: number
  paragraphId?: string
  source?: { story: string; storyInstance: string; path: readonly number[] }
}
/**
 * A click targets its exact structural paragraph, never matching words elsewhere.
 * Text/geometry describe that paragraph's visible page fragment; source retains
 * the complete paragraph identity when the paragraph continues onto another page.
 */
export function docxParagraphHit(runs: readonly ParagraphRun[], x: number, y: number,
  canvas: OfficeViewportRect, pageIndex: number): OfficeHitTarget | null {
  const hit = runs.find(run => x >= run.x && x <= run.x + run.w && y >= run.y && y <= run.y + run.h)
  if (!hit || (!hit.source && !hit.paragraphId)) return null
  const sameSource = (run: ParagraphRun) => hit.source
    ? run.source?.story === hit.source.story && run.source.storyInstance === hit.source.storyInstance
      && JSON.stringify(run.source.path) === JSON.stringify(hit.source.path)
    : !run.source && run.paragraphId === hit.paragraphId
  const paragraph = runs.filter(sameSource)
  const text = paragraph.map(run => run.text).join('')
  if (!text.trim()) return null
  const selection = normalizeOfficeSelection({ format: 'docx', kind: 'text', text, pageIndex,
    pageIndexes: [pageIndex], paragraphIds: [...new Set(paragraph.flatMap(run => run.paragraphId ? [run.paragraphId] : []))],
    source: hit.source, truncated: false })
  const rects = paragraph.map(run => ({ left: canvas.left + run.x, top: canvas.top + run.y, width: run.w, height: run.h })).filter(valid)
  return selection && rects.length ? { kind: 'text', rect: union(rects), rects, selection } : null
}

/**
 * Resolve a saved quote only when it is unique within its semantic run scope.
 * The vendor's selectable run attributes are isolated here; no source model is mutated.
 */
export function locateTextRects(container: HTMLElement, selection: OfficeSelection): OfficeViewportRect[] {
  if (!selection.text || selection.truncated) return []
  const nodes = [...container.querySelectorAll<HTMLElement>('[data-ooxml-selection-run]')].filter(node => {
    if (node.dataset.ooxmlSelectionRun !== selection.format) return false
    if (selection.source && (node.dataset.sourceStory !== selection.source.story
      || node.dataset.sourceStoryInstance !== selection.source.storyInstance
      || node.dataset.sourcePath !== JSON.stringify(selection.source.path))) return false
    if (selection.paragraphIds?.length && !selection.paragraphIds.includes(node.dataset.paragraphId || '')) return false
    if (selection.shapeIds?.length && !selection.shapeIds.includes(node.dataset.shapeId || '')) return false
    return true
  })
  const textNodes: Text[] = []
  for (const node of nodes) {
    const walker = container.ownerDocument.createTreeWalker(node, NodeFilter.SHOW_TEXT)
    while (walker.nextNode()) textNodes.push(walker.currentNode as Text)
  }
  const text = textNodes.map(node => node.data).join('')
  const start = text.indexOf(selection.text)
  if (start < 0 || text.indexOf(selection.text, start + 1) !== -1) return []
  const end = start + selection.text.length
  let offset = 0
  const rects: OfficeViewportRect[] = []
  for (const node of textNodes) {
    const length = node.data.length
    if (offset < end && offset + length > start) {
      const range = container.ownerDocument.createRange()
      range.setStart(node, Math.max(0, start - offset))
      range.setEnd(node, Math.min(length, end - offset))
      if (typeof range.getClientRects === 'function') rects.push(...Array.from(range.getClientRects(), viewportRect))
    }
    offset += length
  }
  return rects.filter(valid)
}

export interface CellArea { top: number; left: number; bottom: number; right: number }
function column(value: string) {
  return [...value].reduce((total, char) => total * 26 + char.charCodeAt(0) - 64, 0)
}
/** One-based OOXML ranges; reject malformed/out-of-bounds coordinates before using renderer APIs. */
export function cellArea(value: string): CellArea | null {
  const match = /^([A-Z]+)([1-9]\d*)(?::([A-Z]+)([1-9]\d*))?$/i.exec(value)
  if (!match) return null
  const left = column(match[1]!.toUpperCase()), top = Number(match[2])
  const right = column((match[3] || match[1]!).toUpperCase()), bottom = Number(match[4] || match[2])
  return top <= bottom && left <= right && bottom <= 1_048_576 && right <= 16_384 ? { left, top, right, bottom } : null
}
