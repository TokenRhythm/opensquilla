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

  it('persists the opaque screenshot upload without putting File bytes in local JSON', async () => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create(request)
    store.setScreenshot('draft-1', {
      kind: 'staged', local_id: -1, name: 'page-selection.png', mime: 'image/png',
      file_uuid: 'capture-file', file: new File(['png'], 'page-selection.png'),
    })
    const persisted = localStorage.getItem('opensquilla.page-annotation-drafts.v1')!
    expect(persisted).not.toContain('"file":')
    expect(persisted).not.toContain('base64')
    setActivePinia(createPinia())
    const restored = useArtifactPromptAnnotationsStore()
    expect(restored.attachmentsForIds(['draft-1'])).toEqual([
      expect.objectContaining({ file_uuid: 'capture-file', kind: 'staged' }),
    ])
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

  it('does not lose the existing draft when durable local storage fails', async () => {
    const store = useArtifactPromptAnnotationsStore()
    await store.create(request)
    const write = vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('quota') })
    await expect(store.update('draft-1', 'New body')).rejects.toThrow('quota')
    expect(store.annotations['draft-1']?.body).toBe(request.body)
    write.mockRestore()
  })
})
