import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  flushActiveWorkbenchDocumentContext,
  useWorkbenchDocumentContextStore,
} from './workbenchDocumentContext'

const context = {
  activeItemId: 'artifact-preview-a',
  documentId: 'document-a',
  headRevisionId: 'revision-a',
  sessionKey: 'session-a',
}

describe('workbench document context bridge', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('exposes the active Document only to its exact session and clears on close', () => {
    const store = useWorkbenchDocumentContextStore()
    const controller = store.attachController(vi.fn())
    controller.setActive(context)

    expect(store.currentDocumentContext('session-a')).toEqual({
      documentId: 'document-a',
      headRevisionId: 'revision-a',
    })
    expect(store.currentDocumentContext('session-b')).toBeNull()

    controller.clear()
    expect(store.currentDocumentContext('session-a')).toBeNull()
  })

  it('blocks send when the dirty editor flush fails', async () => {
    const readLatest = vi.fn(async () => ({
      documentId: 'document-a',
      headRevisionId: 'revision-b',
    }))

    await expect(flushActiveWorkbenchDocumentContext({
      beforeClose: vi.fn(async () => false),
      isCurrent: () => true,
      readLatest,
    })).resolves.toBe(false)
    expect(readLatest).not.toHaveBeenCalled()
  })

  it('publishes the head reread after a successful editor flush', async () => {
    const store = useWorkbenchDocumentContextStore()
    const prepare = vi.fn(async () => ({
      documentId: 'document-a',
      headRevisionId: 'revision-b',
    }))
    const controller = store.attachController(prepare)
    controller.setActive(context)

    await expect(store.prepareDocumentContextForSend('session-a')).resolves.toEqual({
      documentId: 'document-a',
      headRevisionId: 'revision-b',
    })
    expect(store.currentDocumentContext('session-a')).toEqual({
      documentId: 'document-a',
      headRevisionId: 'revision-b',
    })
  })

  it('flushes a requested hidden document without rebinding the active item', async () => {
    const store = useWorkbenchDocumentContextStore()
    const prepareActive = vi.fn()
    const prepareDocument = vi.fn(async request => ({
      documentId: request.documentId,
      headRevisionId: 'revision-hidden-saved',
    }))
    const controller = store.attachController(prepareActive, prepareDocument)
    controller.setActive({
      ...context,
      activeItemId: 'artifact-preview-b',
      documentId: 'document-b',
    })

    await expect(store.prepareDocumentForSend('session-a', 'document-a')).resolves.toEqual({
      documentId: 'document-a',
      headRevisionId: 'revision-hidden-saved',
    })
    expect(prepareActive).not.toHaveBeenCalled()
    expect(prepareDocument).toHaveBeenCalledWith({
      documentId: 'document-a',
      sessionKey: 'session-a',
    })
    expect(store.currentDocumentContext('session-a')).toEqual({
      documentId: 'document-b',
      headRevisionId: 'revision-a',
    })
  })

  it('rejects a preparation whose active item changes while flushing', async () => {
    let complete!: (value: { documentId: string; headRevisionId: string }) => void
    const prepare = vi.fn(() => new Promise<{
      documentId: string
      headRevisionId: string
    }>((resolve) => {
      complete = resolve
    }))
    const store = useWorkbenchDocumentContextStore()
    const controller = store.attachController(prepare)
    controller.setActive(context)

    const pending = store.prepareDocumentContextForSend('session-a')
    controller.setActive({
      ...context,
      activeItemId: 'artifact-preview-b',
      documentId: 'document-b',
    })
    complete({ documentId: 'document-a', headRevisionId: 'revision-b' })

    await expect(pending).resolves.toBe(false)
    expect(store.currentDocumentContext('session-a')).toEqual({
      documentId: 'document-b',
      headRevisionId: 'revision-a',
    })
  })

  it('does not leak an in-flight context across a session switch', async () => {
    let complete!: (value: { documentId: string; headRevisionId: string }) => void
    const store = useWorkbenchDocumentContextStore()
    const controller = store.attachController(() => new Promise(resolve => {
      complete = resolve
    }))
    controller.setActive(context)

    const pending = store.prepareDocumentContextForSend('session-a')
    controller.setActive({
      ...context,
      activeItemId: 'artifact-preview-b',
      documentId: 'document-b',
      sessionKey: 'session-b',
    })
    complete({ documentId: 'document-a', headRevisionId: 'revision-b' })

    await expect(pending).resolves.toBe(false)
    expect(store.currentDocumentContext('session-a')).toBeNull()
    expect(store.currentDocumentContext('session-b')).toEqual({
      documentId: 'document-b',
      headRevisionId: 'revision-a',
    })
  })

  it('rejects stale cleanup from a replaced controller', () => {
    const store = useWorkbenchDocumentContextStore()
    const stale = store.attachController(vi.fn())
    stale.setActive(context)
    const current = store.attachController(vi.fn())
    current.setActive({ ...context, headRevisionId: 'revision-b' })

    stale.detach()
    expect(store.currentDocumentContext('session-a')?.headRevisionId).toBe('revision-b')
  })
})
