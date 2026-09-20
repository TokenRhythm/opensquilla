// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useArtifactPromptAnnotationsStore } from './artifactPromptAnnotations'

const request = {
  annotationId: 'draft-1', sessionKey: 'session-1', documentId: 'page-1',
  documentName: 'page.html', resourceId: 'document:page-1', body: 'Enlarge the heading',
  selection: {
    selectionId: 'selection-1', targetRef: 'target-1', tagName: 'h1', elementPath: 'h1',
    selectionText: 'Welcome', locatorHint: 'h1',
  },
}

beforeEach(() => { localStorage.clear(); setActivePinia(createPinia()) })

describe('local page annotation drafts', () => {
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
