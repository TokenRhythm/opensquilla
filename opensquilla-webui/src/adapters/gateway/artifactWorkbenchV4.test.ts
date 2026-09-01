import { describe, expect, it, vi } from 'vitest'

import { createV4ArtifactWorkbench } from './artifactWorkbenchV4'

function artifact(id: string) {
  return {
    id,
    name: `${id}.txt`,
    mime: 'text/plain',
    size: 4,
    download_url: `/api/v1/artifacts/${id}`,
  }
}

function harness(responses: unknown[], supported = true) {
  const request = vi.fn(async <T = unknown>() => responses.shift() as T)
  const ready = vi.fn(async () => undefined)
  const markUnsupported = vi.fn()
  const handlers = new Map<string, (payload: unknown) => void>()
  const close = vi.fn()
  const workbench = createV4ArtifactWorkbench({
    request: request as unknown as Parameters<typeof createV4ArtifactWorkbench>[0]['request'],
    ready,
    supports: vi.fn(() => supported),
    markUnsupported,
  }, {
    subscribe: vi.fn((event, handler) => {
      handlers.set(event, handler)
      return { close }
    }),
  }, {
    requestJson: vi.fn(async () => ({})) as unknown as Parameters<
      typeof createV4ArtifactWorkbench
    >[2]['requestJson'],
  })
  return { close, handlers, markUnsupported, ready, request, workbench }
}

describe('ArtifactWorkbench v4 Adapter', () => {
  it('validates canonical artifact pages and preserves oldest-first session order', async () => {
    const { ready, request, workbench } = harness([
      {
        artifacts: [artifact('two'), artifact('three')],
        has_more: true,
        oldest_cursor: 'two',
        newest_cursor: 'three',
        total_count: 3,
        page_size: 2,
      },
      {
        artifacts: [artifact('one')],
        has_more: false,
        oldest_cursor: 'one',
        newest_cursor: 'one',
        total_count: 3,
        page_size: 2,
      },
    ])

    await expect(workbench.artifacts.listSession('agent:main:webchat:test', {
      limit: 2,
    })).resolves.toMatchObject([
      { id: 'one' },
      { id: 'two' },
      { id: 'three' },
    ])
    expect(ready).toHaveBeenCalledWith(expect.objectContaining({ timeoutAction: 'reject' }))
    expect(request).toHaveBeenNthCalledWith(
      2,
      'artifacts.list',
      expect.objectContaining({ before: 'two', limit: 2 }),
      expect.objectContaining({ timeoutAction: 'reconnect' }),
    )
  })

  it('decodes the published legacy camel-case pagination aliases', async () => {
    const { workbench } = harness([{
      artifacts: [artifact('legacy')],
      hasMore: false,
      oldestCursor: 'legacy',
    }])

    await expect(workbench.artifacts.listSession('agent:main:webchat:test'))
      .resolves.toMatchObject([{ id: 'legacy' }])
  })

  it('returns an explicit optional-capability absence without issuing a call', async () => {
    const { request, workbench } = harness([], false)

    await expect(workbench.artifacts.listSession('agent:main:webchat:test'))
      .resolves.toBeNull()
    expect(request).not.toHaveBeenCalled()
  })

  it('shares one event lease and dedupes canonical/legacy document changes by sequence', () => {
    const { close, handlers, workbench } = harness([])
    const listener = vi.fn()
    const subscription = workbench.subscribeDocumentChanges(listener)

    handlers.get('session.event.artifact_state')?.({
      document_id: 'document-1',
      artifact_event_seq: 4,
    })
    handlers.get('document.state_changed')?.({
      documentId: 'document-1',
      artifactEventSeq: 4,
    })
    handlers.get('document.state_changed')?.({
      documentId: 'document-1',
      artifactEventSeq: 5,
    })

    expect(listener).toHaveBeenCalledTimes(2)
    expect(listener).toHaveBeenLastCalledWith({ documentId: 'document-1' })
    subscription.close()
    expect(close).toHaveBeenCalledTimes(2)
  })
})
