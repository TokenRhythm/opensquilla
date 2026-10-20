// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useArtifactPromptAnnotationsStore } from './artifactPromptAnnotations'
import { pageContextForAnnotations } from '@/types/pageContext'

const request = {
  annotationId: 'draft-1', sessionKey: 'session-1', documentId: 'page-1',
  documentName: 'page.html', resourceId: 'document:page-1', body: 'Enlarge the heading',
  revisionId: 'revision-1', sha256: 'a'.repeat(64),
  selection: {
    selectionId: 'selection-1', targetRef: 'target-1', tagName: 'h1', elementPath: 'h1',
    selectionText: 'Welcome', locatorHint: 'h1',
  },
}

beforeEach(() => { localStorage.clear(); setActivePinia(createPinia()) })

describe('local page annotation drafts', () => {
  it('copies recorded selection evidence without projecting or truncating it again', async () => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create(request)
    const selection = { format: 'docx' as const, kind: 'element' as const, truncated: false,
      paragraphIds: Array(65).fill('paragraph'), source: { story: 'header', storyInstance: 'header-1', path: [1] },
      bounds: { x: 12, width: 24 } }
    const restored = { ...store.snapshotsForIds(['draft-1'])[0], selection }
    const [id] = store.restoreSnapshots(request.sessionKey, [restored], ['draft-1'])
    const [snapshot] = store.snapshotsForIds([id])
    expect(snapshot.selection).toEqual(selection)
    expect(snapshot.selection).not.toBe(selection)
    expect(snapshot.selection?.paragraphIds).not.toBe(selection.paragraphIds)
    expect(() => pageContextForAnnotations([snapshot])).toThrow('Office selection.paragraphIds')
    selection.paragraphIds[0] = 'changed after capture'
    expect(snapshot.selection?.paragraphIds?.[0]).toBe('paragraph')
  })


  it('records into the composer without sending and scopes HTML drafts to the active page', async () => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create({ ...request, annotationId: 'first', pagePath: 'first.html' })
    await store.create({ ...request, annotationId: 'second', pagePath: 'second.html' })
    store.beginOverlayEdit('second', request.sessionKey)
    expect(store.sendableDraftsForSession(request.sessionKey)).toEqual([])
    store.completeOverlayEdit('second')
    expect(store.sendableDraftsForSession(request.sessionKey).map(item => item.annotationId)).toEqual(['second'])
    expect(store.draftsForSession(request.sessionKey)).toHaveLength(2)
    store.setActivePage(request.sessionKey, 'first.html')
    expect(store.sendableDraftsForSession(request.sessionKey).map(item => item.annotationId)).toEqual(['first'])
  })

  it('reselects atomically at the draft limit and an old ACK cannot remove the replacement', async () => {
    const store = useArtifactPromptAnnotationsStore()
    for (let i = 0; i < 16; i++) await store.create({ ...request, annotationId: `draft-${i}` })
    const sent = store.snapshotsForIds(['draft-1'])
    await store.replaceSelection('draft-1', { ...request, annotationId: 'reselected', selection: { ...request.selection, locatorHint: 'h2' } })
    store.acknowledgeSent(sent)
    expect(store.drafts).toHaveLength(16)
    expect(store.annotations.reselected).toMatchObject({ body: request.body, locatorHint: 'h2' })
  })

  it('applies the limit per HTML page and preserves stale draft text for reselection', async () => {
    const store = useArtifactPromptAnnotationsStore()
    for (let i = 0; i < 16; i++) await store.create({ ...request, annotationId: `old-${i}`, pagePath: 'a.html' })
    await store.create({ ...request, annotationId: 'another-page', pagePath: 'b.html' })
    store.validateDocumentVersion(request.sessionKey, request.documentId, 'revision-2', 'b'.repeat(64))
    expect(store.sendBlockedReason(request.sessionKey)).toBe('stale')
    expect(store.sendableDraftsForSession(request.sessionKey)).toEqual([])
    expect(store.annotations['another-page'].body).toBe(request.body)
    await store.replaceSelection('another-page', { ...request, annotationId: 'fresh', pagePath: 'b.html',
      revisionId: 'revision-2', sha256: 'b'.repeat(64) })
    expect(store.sendableDraftsForSession(request.sessionKey).map(item => item.annotationId)).toEqual(['fresh'])
  })

  it('does not erase a changed HTML target even when body and version remain equal', async () => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create(request)
    const sent = store.snapshotsForIds(['draft-1'])
    await store.create({ ...request, selection: { ...request.selection, locatorHint: 'h2', selectionText: 'Another heading' } })
    expect(store.acknowledgeSent(sent)).toEqual([])
    expect(store.annotations['draft-1'].locatorHint).toBe('h2')
  })

  it('protects queued drafts and restores cancelled snapshots with new identities', async () => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create(request)
    const snapshots = store.snapshotsForIds(['draft-1'])
    store.setQueuedDraftIds(request.sessionKey, ['draft-1'])
    await expect(store.update('draft-1', 'changed')).rejects.toThrow('queued')
    expect(store.sendableDraftsForSession(request.sessionKey)).toEqual([])
    const ids = store.restoreSnapshots(request.sessionKey, snapshots, ['draft-1'])
    expect(ids[0]).not.toBe('draft-1')
    expect(store.annotations[ids[0]].body).toBe(request.body)
  })

  it('shows the restored HTML page when another page in the same document became active', async () => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create({ ...request, annotationId: 'queued-A', pagePath: 'a.html' })
    const snapshots = store.snapshotsForIds(['queued-A'])
    store.setQueuedDraftIds(request.sessionKey, ['queued-A'])
    await store.create({ ...request, annotationId: 'new-B', pagePath: 'b.html', body: 'Keep page B' })

    const ids = store.restoreSnapshots(request.sessionKey, snapshots, ['queued-A'])
    store.setQueuedDraftIds(request.sessionKey, [])

    expect(store.activeDraftsForSession(request.sessionKey).map(item => item.annotationId)).toEqual(ids)
    expect(store.sendableDraftsForSession(request.sessionKey).map(item => item.annotationId)).toEqual(ids)
    expect(store.annotations['new-B'].body).toBe('Keep page B')
    expect(store.annotations['queued-A']).toBeUndefined()
  })

  it('persists ordinary annotation input across store recreation without a gateway', async () => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create(request)
    setActivePinia(createPinia())
    const restored = useArtifactPromptAnnotationsStore()
    expect(restored.snapshotsForIds(['draft-1'])).toEqual([
      expect.objectContaining({ body: request.body, targetRef: 'target-1', resourceId: 'document:page-1' }),
    ])
    expect(restored.snapshotsForIds(['draft-1'])[0]).not.toHaveProperty('stateRevision')
  })

  it('persists a subpage hint and does not acknowledge a replacement on another page', async () => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create({ ...request, pagePath: 'layouts/editorial.html' })
    const sent = store.snapshotsForIds(['draft-1'])
    expect(sent[0].pagePath).toBe('layouts/editorial.html')
    setActivePinia(createPinia())
    const restored = useArtifactPromptAnnotationsStore()
    expect(restored.snapshotsForIds(['draft-1'])[0].pagePath).toBe('layouts/editorial.html')
    await restored.create({ ...request, pagePath: 'layouts/dashboard.html' })
    restored.acknowledgeSent(sent)
    expect(restored.annotations['draft-1'].pagePath).toBe('layouts/dashboard.html')
  })

  it('restores legacy drafts without retaining their automatic screenshot uploads', async () => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create(request)
    localStorage.setItem('opensquilla.page-annotation-drafts.v1', JSON.stringify([{
      ...store.annotations['draft-1'],
      screenshotAttachment: {
        kind: 'staged', local_id: -1, name: 'page-selection.png', mime: 'image/png',
        file_uuid: 'expired-capture-file',
      },
    }]))
    setActivePinia(createPinia())
    const restored = useArtifactPromptAnnotationsStore()
    expect(restored.annotations['draft-1']).not.toHaveProperty('screenshotAttachment')
    expect(restored.sendBlockedReason('session-1')).toBeNull()
    expect(await restored.prepareForSend(['draft-1'])).toBe(true)
    expect(restored.snapshotsForIds(['draft-1'])[0]).toMatchObject({
      body: request.body, targetRef: 'target-1', locatorHint: 'h1',
    })
    await restored.update('draft-1', 'Another instruction')
    expect(localStorage.getItem('opensquilla.page-annotation-drafts.v1'))
      .not.toContain('screenshotAttachment')
  })

  it('clears only unchanged drafts after an accepted send', async () => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create(request)
    const sent = store.snapshotsForIds(['draft-1'])
    await store.update('draft-1', 'A newer instruction')
    store.acknowledgeSent(sent)
    expect(store.annotations['draft-1']?.body).toBe('A newer instruction')
    store.acknowledgeSent(store.snapshotsForIds(['draft-1']))
    expect(store.annotations['draft-1']).toBeUndefined()
  })

  it('keeps native overlay ownership separate from a ready-to-send draft', async () => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create(request)
    store.beginOverlayEdit('draft-1', 'session-1')
    expect(store.sendBlockedReason('session-1')).toBe('editing')
    expect(store.sendableDraftsForSession('session-1')).toEqual([])
    store.completeOverlayEdit('draft-1')
    expect(store.sendableDraftsForSession('session-1')).toHaveLength(1)
  })

  it.each(['', ' \n\t '])('keeps an empty draft editable without blocking ready instructions', async body => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create({ ...request, annotationId: 'empty-draft', body })
    expect(store.sendBlockedReason('session-1')).toBeNull()
    expect(store.sendableDraftsForSession('session-1')).toEqual([])
    expect(await store.prepareForSend(['empty-draft'])).toBe(false)

    await store.create(request)
    expect(store.sendBlockedReason('session-1')).toBeNull()
    expect(store.sendableDraftsForSession('session-1').map(item => item.annotationId))
      .toEqual(['draft-1'])
    store.acknowledgeSent(store.snapshotsForIds(['draft-1']))
    expect(store.activeDraftsForSession('session-1')).toEqual([
      expect.objectContaining({ annotationId: 'empty-draft', body }),
    ])
  })

  it('does not lose the existing draft when durable local storage fails', async () => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create(request)
    const write = vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('quota') })
    await expect(store.update('draft-1', 'New body')).rejects.toThrow('quota')
    expect(store.annotations['draft-1']?.body).toBe(request.body)
    write.mockRestore()
  })
})
