import { describe, expect, it } from 'vitest'
import { normalizePageContext, pageAnnotationSnapshots, pageContextForAnnotations } from './pageContext'

describe('ordinary page context', () => {
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
})
