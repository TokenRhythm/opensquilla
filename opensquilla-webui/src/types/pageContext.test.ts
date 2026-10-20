import { describe, expect, it } from 'vitest'
import { reactive } from 'vue'
import { assertPageContextWithinLimits, pageContextByteLength, normalizePageContext, pageAnnotationSnapshots, pageContextForAnnotations } from './pageContext'

describe('ordinary page context', () => {
  it('does not silently drop the seventeenth annotation before send validation', () => {
    const context = { annotations: [...Array.from({ length: 16 }, () => ({ text: 'change' })), { text: '' }] }
    expect(normalizePageContext(context)?.annotations).toHaveLength(17)
    expect(() => assertPageContextWithinLimits(context)).toThrow('16 annotations')
  })

  it('counts UTF-8 and Python separator spaces, including nested Office context', () => {
    const value = { annotations: [{ text: '你好🙂', selection: { format: 'xlsx', kind: 'cells', sheetIndex: 0 } }] }
    const pythonJson = '{"annotations": [{"text": "你好🙂", "selection": {"format": "xlsx", "kind": "cells", "sheetIndex": 0}}]}'
    expect(pageContextByteLength(value)).toBe(new TextEncoder().encode(pythonJson).byteLength)
  })

  it('accepts exactly 64 KiB and rejects the next byte without changing text', () => {
    const context = { annotations: Array.from({ length: 4 }, () => ({ text: 'a'.repeat(16_384) })) }
    const excess = pageContextByteLength(context) - 65_536
    context.annotations[3].text = context.annotations[3].text.slice(excess)
    expect(pageContextByteLength(context)).toBe(65_536)
    expect(() => assertPageContextWithinLimits(context)).not.toThrow()
    context.annotations[3].text += 'b'
    expect(() => assertPageContextWithinLimits(context)).toThrow('size limit')
    expect(context.annotations[3].text.endsWith('b')).toBe(true)
  })

  it('preserves user words and drops retired authority fields', () => {
    expect(normalizePageContext({
      resourceId: 'document:page', targetRef: 'target-1', expectedSha256: 'hash',
      toolPolicy: 'exclusive', annotations: [{ text: '  Keep this spacing  ', anchorId: 'old' }],
    })).toEqual({
      resourceId: 'document:page', targetRef: 'target-1',
      annotations: [{ text: '  Keep this spacing  ' }],
    })
  })

  it('renders new history without needing live drafts or edit revisions', () => {
    expect(pageAnnotationSnapshots({
      resourceId: 'document:page', annotations: [{ text: 'Change the heading', selectionText: 'Title' }],
    })).toEqual([expect.objectContaining({ documentId: 'page', body: 'Change the heading', quote: 'Title' })])
  })

  it('drops unresolved screenshot references because images use ordinary attachments', () => {
    expect(normalizePageContext({ screenshotRef: 'capture-1' })).toBeNull()
    expect(normalizePageContext({ targetRef: 'page-1', screenshotRef: 'capture-1' }))
      .toEqual({ targetRef: 'page-1' })
  })

  it('preserves the selected subpage across send and history without changing its document', () => {
    const context = { resourceId: 'document:site', targetRef: 'target-1', pagePath: 'layouts/editorial.html',
      annotations: [{ text: 'Change this heading', locatorHint: 'h1' }] }
    expect(normalizePageContext(context)).toEqual(context)
    expect(pageContextForAnnotations(pageAnnotationSnapshots(context))).toEqual(context)
  })

  it('does not combine annotations from different pages of the same native target', () => {
    const snapshots = ['one.html', 'two.html'].flatMap(pagePath => pageAnnotationSnapshots({
      resourceId: 'document:site', targetRef: 'target-1', pagePath,
      annotations: [{ text: 'Change this heading' }],
    }))
    expect(() => pageContextForAnnotations(snapshots)).toThrow('one page at a time')
  })

  it('normalizes identity and empty annotations while preserving actual page names and user text', () => {
    expect(normalizePageContext({
      targetRef: ' target ', resourceId: ' document:site ', revisionId: ' revision ',
      sha256: 'A'.repeat(64), pagePath: ' pages/首页.html ', annotations: [],
    })).toEqual({
      targetRef: 'target', resourceId: 'document:site', revisionId: 'revision',
      sha256: 'a'.repeat(64), pagePath: ' pages/首页.html ',
    })
    expect(normalizePageContext({ annotations: [] })).toBeNull()
    expect(normalizePageContext({ annotations: [{ text: ' \uFEFF ' }] })?.annotations?.[0].text).toBe(' \uFEFF ')
    expect(() => assertPageContextWithinLimits({ annotations: [{ text: '\u0085\u001c' }] })).toThrow('requires text')
  })

  it.each([
    { targetRef: 'x'.repeat(513) },
    { targetRef: ' '.repeat(512) + 'x' },
    { resourceId: '中'.repeat(171) },
    { targetRef: '\uD800' },
    { revisionId: 'r' },
    { sha256: 'a'.repeat(64) },
    { revisionId: 'r', sha256: 'a'.repeat(64) },
    { resourceId: 'document:one', revisionId: 'r', sha256: ` ${'a'.repeat(64)}` },
    { pagePath: 'one.html' },
    ...['/one.html', '../one.html', 'folder/../one.html', 'a\\b.html', 'a:b.html', 'a\0b.html', '中'.repeat(1366)]
      .map(pagePath => ({ resourceId: 'document:one', pagePath })),
  ])('rejects identity/path inputs that the server rejects: %j', value => {
    expect(() => assertPageContextWithinLimits(normalizePageContext(value))).toThrow()
  })

  it.each([
    null, { text: '' }, { text: ' \n ' }, { text: 42 },
    { text: 'a'.repeat(16_385) },
    { text: 'change', selectionText: '🙂'.repeat(4097) },
    { text: 'change', locatorHint: '\uD800' },
    { text: 'change', selection: null },
    { text: 'change', selection: { format: 'xlsx', kind: 'text' } },
  ])('rejects malformed annotations instead of silently omitting them: %j', annotation => {
    const restored = normalizePageContext({ annotations: [annotation] })
    expect(restored?.annotations).toEqual([annotation])
    expect(() => assertPageContextWithinLimits(restored)).toThrow()
  })

  it('validates all selection fields without truncating or silently dropping selected evidence', () => {
    const valid = {
      format: 'docx', kind: 'element', shapeId: '图'.repeat(85), pageIndexes: [0, 100_000],
      source: { story: 'header' }, bounds: { x: 1e-7, width: 30, flipH: false },
    }
    const context = { annotations: [{ text: 'change', selection: valid }] }
    expect(normalizePageContext(context)).toEqual(context)
    expect(pageContextForAnnotations(pageAnnotationSnapshots(context))).toEqual(context)
    for (const invalid of [
      { ...valid, shapeId: '图'.repeat(86) }, { ...valid, text: '\uD800' },
      { ...valid, pageIndex: true }, { ...valid, pageIndexes: [100_001] },
      { ...valid, paragraphIds: Array(65).fill('p') }, { ...valid, ranges: ['x'.repeat(129)] },
      { ...valid, source: { story: 'unknown' } }, { ...valid, source: { story: 'body', path: [0.5] } },
      { ...valid, bounds: { width: Infinity } }, { ...valid, bounds: { flipH: 1 } },
      { ...valid, bounds: { x: 1e9 + 1 } }, { ...valid, truncated: 'false' },
      { ...valid, vendorModel: {} },
    ]) {
      expect(() => assertPageContextWithinLimits({ annotations: [{ text: 'change', selection: invalid }] })).toThrow('Office selection')
    }
  })

  it('includes nested selection text in the complete UTF-8 budget', () => {
    const annotations = Array.from({ length: 4 }, () => ({
      text: 'change', selection: { format: 'docx', kind: 'text', text: '🙂'.repeat(4096) },
    }))
    expect(() => assertPageContextWithinLimits({ annotations })).toThrow('size limit')
  })

  it.each([
    [1e-7, '1e-07'], [1e-5, '1e-05'], [-1e-7, '-1e-07'], [1.2345e-7, '1.2345e-07'],
    [0.00009999999999999999, '9.999999999999999e-05'], [0.0001, '0.0001'], [5e-324, '5e-324'],
  ])('counts Python float serialization for selection geometry %s', (number, pythonNumber) => {
    const value = { bounds: { x: number } }
    const pythonJson = `{"bounds": {"x": ${pythonNumber}}}`
    expect(pageContextByteLength(value)).toBe(new TextEncoder().encode(pythonJson).byteLength)
  })

  it('preserves empty optional strings and selection evidence through history and retry', () => {
    const context = { resourceId: 'document:one', annotations: [{
      text: 'change', selectionText: '', locatorHint: '', selection: { format: 'docx', kind: 'text', text: 'quote' },
    }] }
    const snapshots = pageAnnotationSnapshots(context)
    expect(snapshots[0]).toMatchObject({ targetText: '', quote: '' })
    expect(pageContextForAnnotations(snapshots)).toEqual(context)
  })

  it('does not crash history rendering for a malformed legacy context', () => {
    expect(pageAnnotationSnapshots({ annotations: [{ text: '' }] })).toEqual([])
  })

  it('keeps invalid persisted context recoverable without aborting hydration or dropping user words', () => {
    const invalid = { resourceId: 'document:one', revisionId: 'old', annotations: [
      { text: 'Keep my pending change', selection: { format: 'docx', kind: 'retired' } },
    ] }
    const restored = normalizePageContext(invalid)
    expect(restored).toEqual(invalid)
    expect(() => assertPageContextWithinLimits(restored)).toThrow('both revisionId and sha256')
    expect(normalizePageContext(restored)).toEqual(restored)
    expect(normalizePageContext({ ...invalid, toolPolicy: 'exclusive' })).toEqual(invalid)
    expect(structuredClone(normalizePageContext(reactive(invalid)))).toEqual(invalid)
  })
})
