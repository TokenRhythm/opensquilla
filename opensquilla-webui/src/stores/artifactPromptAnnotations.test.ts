import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { PromptAnnotation } from '@/types/promptAnnotations'
import {
  PROMPT_ANNOTATION_MAX_COUNT,
  promptAnnotationBodyByteLength,
} from '@/types/promptAnnotations'
import type { ArtifactPromptAnnotationProvider } from '@/workbench/artifactPromptAnnotationProvider'
import {
  PROMPT_ANNOTATION_CREATE_AMBIGUOUS,
  useArtifactPromptAnnotationsStore,
} from './artifactPromptAnnotations'

function annotation(id: string, overrides: Partial<PromptAnnotation> = {}): PromptAnnotation {
  return {
    annotationId: id,
    sessionKey: 'session-a',
    sessionId: null,
    sessionEpoch: null,
    documentId: 'document-a',
    documentName: 'page.html',
    revisionId: 'revision-a',
    generation: 1,
    anchorId: `anchor-${id}`,
    body: `Change ${id}`,
    status: 'draft',
    freshness: 'fresh',
    staleReason: null,
    stateRevision: 1,
    tagName: 'button',
    locator: { start_offset: 1 },
    quote: '<button>',
    sourceExcerpt: '<button>Before</button>',
    sentMessageId: null,
    sentTurnId: null,
    sentOrder: null,
    createdAt: id,
    updatedAt: id,
    schemaVersion: 1,
    ...overrides,
  }
}

function provider(items: PromptAnnotation[] = []): ArtifactPromptAnnotationProvider {
  return {
    list: vi.fn().mockResolvedValue(items),
    create: vi.fn(async request => annotation(request.annotationId, {
      sessionKey: request.sessionKey,
      documentId: request.documentId,
      revisionId: request.revisionId,
      body: '',
    })),
    update: vi.fn(async request => annotation(request.annotationId, {
      body: request.body,
      stateRevision: request.expectedStateRevision + 1,
    })),
    discard: vi.fn(async request => annotation(request.annotationId, {
      status: 'discarded',
      stateRevision: request.expectedStateRevision + 1,
    })),
    focus: vi.fn(async request => ({
      focused: true as const,
      annotationId: request.annotationId,
      documentId: 'document-a',
    })),
  }
}

beforeEach(() => setActivePinia(createPinia()))

describe('artifact prompt annotations store', () => {
  it('does not let a stale in-flight list erase a newly created composer draft', async () => {
    let finishList!: (items: PromptAnnotation[]) => void
    const rpc = provider()
    vi.mocked(rpc.list).mockImplementationOnce(() => new Promise(resolve => {
      finishList = resolve
    }))
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(rpc)

    const loading = store.load('session-a')
    const created = await store.create({
      annotationId: 'annotation-created-while-loading',
      sessionKey: 'session-a',
      documentId: 'document-a',
      revisionId: 'revision-a',
      selection: {
        selectionId: 'selection-created-while-loading',
        tagName: 'button',
        elementPath: '[["","button",1]]',
        elementProofSha256: 'a'.repeat(64),
      },
      body: 'Keep this visible.',
    })
    finishList([])
    await loading

    expect(store.activeDraftsForSession('session-a')).toEqual([created])
    expect(store.loadedSessions['session-a']).toBe(true)
  })

  it('recovers durable drafts after reconnect without duplicating a cached load', async () => {
    const rpc = provider([annotation('annotation-1')])
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(rpc)

    await store.load('session-a')
    await store.load('session-a')
    expect(rpc.list).toHaveBeenCalledOnce()
    expect(store.activeDraftsForSession('session-a')).toHaveLength(1)

    await store.load('session-a', { force: true })
    expect(rpc.list).toHaveBeenCalledTimes(2)
  })

  it('keeps the selected document batch ordered and clears only server-accepted IDs', async () => {
    const first = annotation('annotation-1', { createdAt: 1 })
    const second = annotation('annotation-2', { createdAt: 2 })
    const other = annotation('annotation-3', { documentId: 'document-b', createdAt: 3 })
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(provider([first, second, other]))
    await store.load('session-a')
    store.setActiveDocument('session-a', 'document-a')

    expect(store.sendableDraftsForSession('session-a').map(item => item.annotationId))
      .toEqual(['annotation-1', 'annotation-2'])
    store.acknowledgeAccepted(
      ['annotation-1', 'annotation-2'],
      ['annotation-2', 'not-requested'],
    )
    expect(store.draftsForSession('session-a').map(item => item.annotationId))
      .toEqual(['annotation-1', 'annotation-3'])
  })

  it('allows compatibility stale drafts while still blocking empty instructions', async () => {
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(provider([
      annotation('annotation-stale', { freshness: 'stale' }),
      annotation('annotation-empty', { body: '' }),
    ]))
    await store.load('session-a')

    expect(store.sendableDraftsForSession('session-a').map(item => item.annotationId))
      .toEqual(['annotation-stale'])
    expect(store.sendBlockedReason('session-a')).toBe('empty')
    expect(store.draftsForSession('session-a')).toHaveLength(2)
  })

  it('enforces the 16 KiB limit in UTF-8 bytes for multibyte instructions', async () => {
    const overLimit = '😀'.repeat(4097)
    expect(overLimit.length).toBeLessThan(16 * 1024)
    expect(promptAnnotationBodyByteLength(overLimit)).toBeGreaterThan(16 * 1024)
    const rpc = provider([annotation('annotation-over-bytes', { body: overLimit })])
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(rpc)
    await store.load('session-a')

    expect(store.sendableDraftsForSession('session-a')).toEqual([])
    expect(store.sendBlockedReason('session-a')).toBe('too-long')
    await expect(store.update('annotation-over-bytes', overLimit)).rejects.toThrow('too long')
    expect(rpc.update).not.toHaveBeenCalled()
  })

  it('enforces the per-document draft cap before calling the Gateway', async () => {
    const items = Array.from({ length: PROMPT_ANNOTATION_MAX_COUNT }, (_, index) => (
      annotation(`annotation-${index}`)
    ))
    const rpc = provider(items)
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(rpc)
    await store.load('session-a')

    await expect(store.create({
      annotationId: 'annotation-over-limit',
      sessionKey: 'session-a',
      documentId: 'document-a',
      revisionId: 'revision-a',
      selection: {
        selectionId: 'selection-over-limit',
        tagName: 'p',
        elementPath: '[["","p",1]]',
        elementProofSha256: 'a'.repeat(64),
      },
    })).rejects.toThrow('limit')
    expect(rpc.create).not.toHaveBeenCalled()
  })

  it('adopts a durable create when the committed RPC response is lost', async () => {
    const durable: PromptAnnotation[] = []
    const rpc = provider()
    vi.mocked(rpc.create).mockImplementationOnce(async (request) => {
      durable.push(annotation(request.annotationId, {
        sessionKey: request.sessionKey,
        documentId: request.documentId,
        revisionId: request.revisionId,
        body: request.body || '',
      }))
      throw Object.assign(new Error('response lost after commit'), { code: 'RPC_TIMEOUT' })
    })
    vi.mocked(rpc.list).mockImplementation(async () => [...durable])
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(rpc)

    const created = await store.create({
      annotationId: 'annotation-committed',
      sessionKey: 'session-a',
      documentId: 'document-a',
      revisionId: 'revision-a',
      selection: {
        selectionId: 'selection-committed',
        tagName: 'p',
        elementPath: '[["","p",1]]',
        elementProofSha256: 'a'.repeat(64),
      },
    })

    expect(created.annotationId).toBe('annotation-committed')
    expect(store.annotations['annotation-committed']).toEqual(created)
    expect(store.activeDraftsForSession('session-a')).toEqual([created])
    expect(rpc.create).toHaveBeenCalledOnce()
    expect(rpc.list).toHaveBeenCalledOnce()
    expect(durable.map(item => item.annotationId)).toEqual(['annotation-committed'])
  })

  it('replays only the same id and reports ambiguity when create and refetch stay unavailable', async () => {
    const rpc = provider()
    vi.mocked(rpc.create).mockRejectedValue(
      Object.assign(new Error('response timed out'), { code: 'RPC_TIMEOUT' }),
    )
    vi.mocked(rpc.list).mockRejectedValue(new Error('connection unavailable'))
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(rpc)
    const request = {
      annotationId: 'annotation-uncertain',
      sessionKey: 'session-a',
      documentId: 'document-a',
      revisionId: 'revision-a',
      selection: {
        selectionId: 'selection-uncertain',
        tagName: 'p',
        elementPath: '[["","p",1]]',
        elementProofSha256: 'b'.repeat(64),
      },
    }

    await expect(store.create(request)).rejects.toMatchObject({
      code: PROMPT_ANNOTATION_CREATE_AMBIGUOUS,
    })
    expect(rpc.create).toHaveBeenCalledTimes(2)
    expect(rpc.create).toHaveBeenNthCalledWith(1, request)
    expect(rpc.create).toHaveBeenNthCalledWith(2, request)
    expect(rpc.list).toHaveBeenCalledTimes(2)
    expect(store.draftsForSession('session-a')).toEqual([])
  })

  it('preserves a definitive create rejection after an authoritative empty refetch', async () => {
    const rejection = Object.assign(new Error('selected element changed'), {
      code: 'ARTIFACT_ELEMENT_CHANGED',
      accepted: false,
    })
    const rpc = provider()
    vi.mocked(rpc.create).mockRejectedValueOnce(rejection)
    vi.mocked(rpc.list).mockResolvedValueOnce([])
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(rpc)

    await expect(store.create({
      annotationId: 'annotation-rejected',
      sessionKey: 'session-a',
      documentId: 'document-a',
      revisionId: 'revision-a',
      selection: {
        selectionId: 'selection-rejected',
        tagName: 'p',
        elementPath: '[["","p",1]]',
        elementProofSha256: 'c'.repeat(64),
      },
    })).rejects.toBe(rejection)
    expect(rpc.create).toHaveBeenCalledOnce()
    expect(rpc.list).toHaveBeenCalledOnce()
    expect(store.draftsForSession('session-a')).toEqual([])
  })

  it('serializes autosaves so each update uses the latest CAS revision', async () => {
    let finishFirst!: () => void
    const rpc = provider([annotation('annotation-1')])
    vi.mocked(rpc.update)
      .mockImplementationOnce(request => new Promise(resolve => {
        finishFirst = () => resolve(annotation(request.annotationId, {
          body: request.body,
          stateRevision: 2,
        }))
      }))
      .mockImplementationOnce(async request => annotation(request.annotationId, {
        body: request.body,
        stateRevision: 3,
      }))
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(rpc)
    await store.load('session-a')

    const first = store.update('annotation-1', 'first body')
    const second = store.update('annotation-1', 'second body')
    await vi.waitFor(() => expect(rpc.update).toHaveBeenCalledOnce())
    finishFirst()
    await Promise.all([first, second])

    expect(rpc.update).toHaveBeenNthCalledWith(1, expect.objectContaining({
      expectedStateRevision: 1,
    }))
    expect(rpc.update).toHaveBeenNthCalledWith(2, expect.objectContaining({
      body: 'second body',
      expectedStateRevision: 2,
    }))
    expect(store.annotations['annotation-1']?.body).toBe('second body')
  })

  it('waits for in-flight autosaves before a send snapshots the batch', async () => {
    let finishUpdate!: () => void
    const rpc = provider([annotation('annotation-1')])
    vi.mocked(rpc.update).mockImplementationOnce(request => new Promise(resolve => {
      finishUpdate = () => resolve(annotation(request.annotationId, {
        body: request.body,
        stateRevision: 2,
      }))
    }))
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(rpc)
    await store.load('session-a')

    const update = store.update('annotation-1', 'latest body')
    const prepared = store.prepareForSend(['annotation-1'])
    let settled = false
    void prepared.then(() => { settled = true })
    await vi.waitFor(() => expect(rpc.update).toHaveBeenCalledOnce())
    expect(settled).toBe(false)
    finishUpdate()

    await expect(prepared).resolves.toBe(true)
    await update
    expect(store.snapshotsForIds(['annotation-1'])[0]?.body).toBe('latest body')
  })

  it('keeps an overlay-owned autosaved draft out of the composer and send batch until confirmed', async () => {
    const rpc = provider([
      annotation('annotation-overlay', { body: '' }),
      annotation('annotation-ready', {
        documentId: 'document-b',
        body: 'Already ready to send.',
      }),
    ])
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(rpc)
    await store.load('session-a')
    store.setActiveDocument('session-a', 'document-a')

    store.beginOverlayEdit('annotation-overlay', 'session-a')
    await store.update('annotation-overlay', 'Autosaved while the trusted editor is open.')

    expect(store.draftsForSession('session-a').map(item => item.annotationId))
      .toEqual(['annotation-overlay', 'annotation-ready'])
    expect(store.activeDraftsForSession('session-a')).toEqual([])
    expect(store.sendableDraftsForSession('session-a')).toEqual([])
    expect(store.sendBlockedReason('session-a')).toBe('editing')
    await expect(store.prepareForSend(['annotation-overlay'])).resolves.toBe(false)
    expect(store.snapshotsForIds(['annotation-overlay'])).toEqual([])

    // An already-confirmed instruction remains visible, but cannot be sent
    // while another trusted editor is still open in this session.
    store.setActiveDocument('session-a', 'document-b')
    expect(store.activeDraftsForSession('session-a').map(item => item.annotationId))
      .toEqual(['annotation-ready'])
    expect(store.sendableDraftsForSession('session-a').map(item => item.annotationId))
      .toEqual(['annotation-ready'])
    expect(store.sendBlockedReason('session-a')).toBe('editing')

    store.completeOverlayEdit('annotation-overlay')
    expect(store.sendBlockedReason('session-a')).toBeNull()
    store.setActiveDocument('session-a', 'document-a')

    expect(store.activeDraftsForSession('session-a').map(item => item.annotationId))
      .toEqual(['annotation-overlay'])
    expect(store.sendableDraftsForSession('session-a').map(item => item.annotationId))
      .toEqual(['annotation-overlay'])
    expect(store.snapshotsForIds(['annotation-overlay'])[0]?.body)
      .toBe('Autosaved while the trusted editor is open.')
  })

  it('recovers an unfinished autosaved draft when its trusted overlay is unexpectedly released', async () => {
    const rpc = provider([annotation('annotation-overlay', { body: 'Recovered body.' })])
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(rpc)
    await store.load('session-a')

    store.beginOverlayEdit('annotation-overlay', 'session-a')
    expect(store.activeDraftsForSession('session-a')).toEqual([])
    expect(store.sendBlockedReason('session-a')).toBe('editing')

    // A forced list while the overlay still owns the draft must not expose it.
    await store.load('session-a', { force: true })
    expect(store.activeDraftsForSession('session-a')).toEqual([])

    store.releaseOverlayEdit('annotation-overlay')
    expect(store.sendBlockedReason('session-a')).toBeNull()
    expect(store.activeDraftsForSession('session-a').map(item => item.annotationId))
      .toEqual(['annotation-overlay'])
  })

  it('fails closed when an overlay owner carries a mismatched session value', async () => {
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(provider([annotation('annotation-overlay')]))
    await store.load('session-a')

    store.beginOverlayEdit('annotation-overlay', 'unexpected-session')

    expect(store.activeDraftsForSession('session-a')).toEqual([])
    expect(store.sendableDraftsForSession('session-a')).toEqual([])
    expect(store.sendBlockedReason('session-a')).toBe('editing')
    expect(store.snapshotsForIds(['annotation-overlay'])).toEqual([])
  })

  it('lets the Gateway resolve both current and compatibility stale drafts', async () => {
    const rpc = provider([
      annotation('annotation-fresh'),
      annotation('annotation-stale', { freshness: 'stale' }),
    ])
    const store = useArtifactPromptAnnotationsStore()
    store.setProvider(rpc)
    await store.load('session-a')

    await expect(store.focus('annotation-fresh')).resolves.toMatchObject({
      focused: true,
      annotationId: 'annotation-fresh',
      documentId: 'document-a',
    })
    expect(rpc.focus).toHaveBeenCalledWith({
      sessionKey: 'session-a',
      annotationId: 'annotation-fresh',
    })

    await expect(store.focus('annotation-stale')).resolves.toMatchObject({
      focused: true,
      annotationId: 'annotation-stale',
      documentId: 'document-a',
    })
    expect(rpc.focus).toHaveBeenCalledTimes(2)
  })
})
