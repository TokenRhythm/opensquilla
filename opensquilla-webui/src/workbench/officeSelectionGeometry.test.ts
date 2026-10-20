// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { cellArea, docxParagraphHit, selectionAnchor } from './officeSelectionGeometry'

describe('transient Office geometry', () => {
  it('anchors to the visible selected fragments and preserves all final rectangles', () => {
    const viewport = { left: 10, top: 20, width: 200, height: 100 }
    const rects = [{ left: 5, top: 5, width: 100, height: 30 }, { left: 30, top: 60, width: 120, height: 20 }]
    expect(selectionAnchor(rects, viewport)).toEqual({ viewport, rects,
      rect: { left: 10, top: 20, width: 140, height: 60 }, visible: true })
    expect(selectionAnchor([{ left: 500, top: 200, width: 30, height: 20 }], viewport)?.visible).toBe(false)
    expect(selectionAnchor([{ left: NaN, top: 0, width: 20, height: 20 }], viewport)).toBeNull()
  })
  it('parses final ranges without expanding a large selection into individual cells', () => {
    expect(cellArea('B2:D1000000')).toEqual({ top: 2, left: 2, bottom: 1000000, right: 4 })
    expect(cellArea('XFD1048576')).toEqual({ top: 1048576, bottom: 1048576, left: 16384, right: 16384 })
    for (const value of ['A0', 'XFE1', 'A1:A1048577', 'B2:A1', '1:4', '*', '../A1']) expect(cellArea(value)).toBeNull()
  })
  it('selects the clicked paragraph by source across wrapped runs, not duplicate text or paragraph IDs', () => {
    const canvas = { left: 50, top: 100, width: 600, height: 800 }
    const source = { story: 'body', storyInstance: '', path: [3] }
    const runs = [
      { text: 'Repeated ', x: 10, y: 20, w: 80, h: 20, paragraphId: 'reused', source },
      { text: 'paragraph', x: 10, y: 40, w: 90, h: 20, paragraphId: 'reused', source },
      { text: 'Repeated paragraph', x: 10, y: 80, w: 100, h: 20, paragraphId: 'reused', source: { ...source, path: [7] } },
      { text: 'Repeated paragraph', x: 10, y: 120, w: 100, h: 20, paragraphId: 'reused', source: { ...source, story: 'header' } },
    ]
    expect(docxParagraphHit(runs, 20, 45, canvas, 2)).toEqual({
      kind: 'text', rect: { left: 60, top: 120, width: 90, height: 40 },
      rects: [{ left: 60, top: 120, width: 80, height: 20 }, { left: 60, top: 140, width: 90, height: 20 }],
      selection: { format: 'docx', kind: 'text', text: 'Repeated paragraph', pageIndex: 2, pageIndexes: [2],
        paragraphIds: ['reused'], source, truncated: false },
    })
    expect(docxParagraphHit(runs, 20, 85, canvas, 2)?.selection?.source?.path).toEqual([7])
    expect(docxParagraphHit([{ text: 'ambiguous', x: 0, y: 0, w: 80, h: 20 }], 5, 5, canvas, 0)).toBeNull()
    expect(docxParagraphHit(runs, 500, 500, canvas, 0)).toBeNull()
  })
})
