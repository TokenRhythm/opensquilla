import { describe, expect, it } from 'vitest'
import { normalizePageContext, pageAnnotationSnapshots } from './pageContext'

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
})
