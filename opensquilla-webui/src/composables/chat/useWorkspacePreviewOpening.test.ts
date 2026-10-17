import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref, type EffectScope } from 'vue'
import type { ArtifactPayload } from '@/types/artifacts'
import type { WorkbenchResource, WorkbenchResourceOpenResponse } from '@/types/workbenchResources'
import { useWorkspacePreviewOpening } from './useWorkspacePreviewOpening'

const scopes: EffectScope[] = []
const resource: WorkbenchResource = {
  resource: { type: 'document', documentId: 'doc_weather' }, name: 'beijing-weather.html', mime: 'text/html',
  capabilities: {
    preview: true, download: false, selectionContext: true, manualEdit: false,
    agentEdit: true, edit: true, publish: true,
  },
  relations: { documentId: 'doc_weather' },
}
const current = {
  disposition: 'document', resource, document: { documentId: 'doc_weather' }, revision: {},
} as WorkbenchResourceOpenResponse
const historicalArtifact: ArtifactPayload = {
  id: 'art_historical', kind: 'artifact_ref', name: 'beijing-weather.html', mime: 'text/html',
  session_id: 'persisted-session-id', source: 'workspace-preview', documentId: 'doc_weather',
}
function setup(beginRequest?: () => () => boolean) {
  const scope = effectScope()
  scopes.push(scope)
  const options = {
    sessionKey: ref('session-A'), currentEpoch: ref(0), enabled: ref(true),
    beginRequest,
    getArtifact: vi.fn<(key: string, id: string) => Promise<ArtifactPayload | null>>().mockResolvedValue(historicalArtifact),
    resolve: vi.fn<() => Promise<WorkbenchResource | null>>().mockResolvedValue(resource),
    openCurrent: vi.fn<() => Promise<WorkbenchResourceOpenResponse | null>>().mockResolvedValue(current),
    show: vi.fn(), onError: vi.fn(),
  }
  return { ...options, scope, api: scope.run(() => useWorkspacePreviewOpening(options))! }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
afterEach(() => scopes.splice(0).forEach(scope => scope.stop()))

describe('workspace preview canonical opening', () => {
  it('reports a rejected panel presentation as unsuccessful after resource resolution', async () => {
    const h = setup()
    h.show.mockReturnValue(false)
    expect(await h.api.open('doc_weather')).toBe(false)
    expect(h.show).toHaveBeenCalledOnce()
    expect(h.onError).not.toHaveBeenCalled()
  })

  it.each(['success', 'error'] as const)('does not override a newer public-file click with a late workspace %s', async result => {
    let latest = 0
    const beginRequest = () => {
      const request = ++latest
      return () => request === latest
    }
    const h = setup(beginRequest), pending = deferred<WorkbenchResource | null>()
    h.resolve.mockReturnValueOnce(pending.promise)
    const opening = h.api.open('doc_weather')
    // The enclosing conversation uses this same fence for attachments and
    // public deliverables, so a newer click invalidates this workspace read.
    beginRequest()
    if (result === 'success') pending.resolve(resource)
    else pending.reject(new Error('Late workspace read'))
    await opening
    expect(h.openCurrent).not.toHaveBeenCalled()
    expect(h.show).not.toHaveBeenCalled()
    expect(h.onError).not.toHaveBeenCalled()
  })

  it('maps an internal historical artifact to its document and opens the current revision', async () => {
    const h = setup()
    const latest = { ...current, revision: { revisionId: 'rev_latest', artifactId: 'art_latest' } } as WorkbenchResourceOpenResponse
    h.openCurrent.mockResolvedValue(latest)
    const opened = await h.api.open({ artifactId: 'art_historical' }, 'session-A')
    expect(opened).toBe(true)
    expect(h.getArtifact).toHaveBeenCalledWith('session-A', 'art_historical')
    expect(h.resolve).toHaveBeenCalledWith('session-A', {
      type: 'document', documentId: 'doc_weather', id: 'doc_weather',
    })
    expect(h.openCurrent).toHaveBeenCalledWith('session-A', resource)
    expect(h.show).toHaveBeenCalledWith(latest, 'session-A')
    expect(h.onError).not.toHaveBeenCalled()
  })

  it.each(['missing', 'no-document', 'invalid-document', 'wrong-artifact', 'wrong-source'] as const)(
    'reports %s metadata mapping without opening a fallback', async failure => {
      const h = setup()
      h.getArtifact.mockResolvedValue(failure === 'missing' ? null : {
        ...historicalArtifact, documentId: failure === 'no-document' ? undefined
          : failure === 'invalid-document' ? '../doc_weather' : 'doc_weather',
        ...(failure === 'wrong-artifact' ? { id: 'art_unrelated' } : {}),
        ...(failure === 'wrong-source' ? { source: 'upload' } : {}),
      })
      const opened = await h.api.open({ artifactId: 'art_historical' }, 'session-A')
      expect(opened).not.toBe(true)
      expect(h.onError).toHaveBeenCalledOnce()
      expect(h.resolve).not.toHaveBeenCalled()
      expect(h.openCurrent).not.toHaveBeenCalled()
      expect(h.show).not.toHaveBeenCalled()
    },
  )

  it.each(['resolve', 'openCurrent'] as const)('refuses an unavailable %s after successful metadata mapping', async stage => {
    const h = setup()
    h[stage].mockResolvedValue(null)
    await h.api.open({ artifactId: 'art_historical' }, 'session-A')
    expect(h.getArtifact).toHaveBeenCalledOnce()
    expect(h.show).not.toHaveBeenCalled()
    expect(h.onError).toHaveBeenCalledOnce()
    if (stage === 'resolve') expect(h.openCurrent).not.toHaveBeenCalled()
  })

  it('reports a current metadata lookup failure', async () => {
    const h = setup()
    const error = new Error('ACCESS_DENIED')
    h.getArtifact.mockRejectedValue(error)
    await h.api.open({ artifactId: 'art_historical' }, 'session-A')
    expect(h.onError).toHaveBeenCalledWith(error)
    expect(h.resolve).not.toHaveBeenCalled()
    expect(h.show).not.toHaveBeenCalled()
  })

  it('does not resolve a document when the capability is disabled during metadata lookup', async () => {
    const h = setup(), pending = deferred<ArtifactPayload | null>()
    h.getArtifact.mockReturnValue(pending.promise)
    const opening = h.api.open({ artifactId: 'art_historical' }, 'session-A')
    expect(h.getArtifact).toHaveBeenCalledOnce()
    h.enabled.value = false
    pending.resolve(historicalArtifact)
    await opening
    expect(h.resolve).not.toHaveBeenCalled()
    expect(h.openCurrent).not.toHaveBeenCalled()
    expect(h.show).not.toHaveBeenCalled()
    expect(h.onError).toHaveBeenCalledOnce()
  })

  it('keeps a newer direct document click when an earlier artifact lookup finishes late', async () => {
    const h = setup(), pending = deferred<ArtifactPayload | null>()
    h.getArtifact.mockReturnValueOnce(pending.promise)
    const earlier = h.api.open({ artifactId: 'art_historical' }, 'session-A')
    const secondResource = { ...resource, resource: { type: 'document' as const, documentId: 'doc_second' } }
    const secondCurrent = { ...current, resource: secondResource, document: { documentId: 'doc_second' } } as WorkbenchResourceOpenResponse
    h.resolve.mockResolvedValue(secondResource)
    h.openCurrent.mockResolvedValue(secondCurrent)
    await h.api.open('doc_second', 'session-A')
    pending.resolve(historicalArtifact)
    await earlier
    expect(h.getArtifact).toHaveBeenCalledOnce()
    expect(h.resolve).toHaveBeenCalledOnce()
    expect(h.resolve).toHaveBeenCalledWith('session-A', { type: 'document', documentId: 'doc_second', id: 'doc_second' })
    expect(h.show).toHaveBeenCalledOnce()
    expect(h.show).toHaveBeenCalledWith(secondCurrent, 'session-A')
    expect(h.onError).not.toHaveBeenCalled()
  })

  it.each(['resolve', 'reject'] as const)('keeps the latest artifact click when an older metadata request completes with %s', async finish => {
    const h = setup(), earlierMetadata = deferred<ArtifactPayload | null>()
    h.getArtifact.mockReturnValueOnce(earlierMetadata.promise)
      .mockResolvedValueOnce({ ...historicalArtifact, id: 'art_second' })
    const earlier = h.api.open({ artifactId: 'art_historical' }, 'session-A')
    await h.api.open({ artifactId: 'art_second' }, 'session-A')
    if (finish === 'resolve') earlierMetadata.resolve(historicalArtifact)
    else earlierMetadata.reject(new Error('Late metadata failure'))
    await earlier
    expect(h.getArtifact.mock.calls).toEqual([['session-A', 'art_historical'], ['session-A', 'art_second']])
    expect(h.resolve).toHaveBeenCalledOnce()
    expect(h.openCurrent).toHaveBeenCalledOnce()
    expect(h.show).toHaveBeenCalledOnce()
    expect(h.show).toHaveBeenCalledWith(current, 'session-A')
    expect(h.onError).not.toHaveBeenCalled()
  })

  it.each(['switch', 'switch-back', 'reset', 'dispose'] as const)('invalidates pending metadata on %s', async change => {
    const h = setup(), pending = deferred<ArtifactPayload | null>()
    h.getArtifact.mockReturnValue(pending.promise)
    const opening = h.api.open({ artifactId: 'art_historical' }, 'session-A')
    expect(h.getArtifact).toHaveBeenCalledOnce()
    if (change === 'switch' || change === 'switch-back') {
      h.sessionKey.value = 'session-B'
      if (change === 'switch-back') h.sessionKey.value = 'session-A'
    } else if (change === 'reset') h.currentEpoch.value += 1
    else h.scope.stop()
    pending.resolve(historicalArtifact)
    await opening
    expect(h.resolve).not.toHaveBeenCalled()
    expect(h.openCurrent).not.toHaveBeenCalled()
    expect(h.show).not.toHaveBeenCalled()
    expect(h.onError).not.toHaveBeenCalled()
  })

  it('rejects a stale artifact action before metadata lookup', async () => {
    const h = setup()
    await h.api.open({ artifactId: 'art_historical' }, 'session-B')
    expect(h.getArtifact).not.toHaveBeenCalled()
    expect(h.resolve).not.toHaveBeenCalled()
    expect(h.show).not.toHaveBeenCalled()
    expect(h.onError).not.toHaveBeenCalled()
  })

  it('opens the resolved current Document and deduplicates the same live tool id', async () => {
    const h = setup()
    const payload = {
      key: 'session-A', epoch: 0, task_id: 'turn-1', id: 'tool-1', name: 'open_workspace_preview',
      result: JSON.stringify({ documentId: 'doc_weather', resourceId: 'document:doc_weather',
        entrypoint: '/task/beijing-weather.html', previewStatus: 'ready' }),
    }
    h.api.acceptLiveResult({ ...payload, is_error: true })
    h.api.acceptLiveResult({ ...payload, key: 'session-B' })
    expect(h.resolve).not.toHaveBeenCalled()
    h.api.acceptLiveResult(payload)
    h.api.acceptLiveResult(payload)
    await vi.waitFor(() => expect(h.show).toHaveBeenCalledTimes(1))
    expect(h.resolve).toHaveBeenCalledWith('session-A', {
      type: 'document', documentId: 'doc_weather', id: 'doc_weather',
    })
    expect(h.openCurrent).toHaveBeenCalledWith('session-A', resource)
    expect(h.show).toHaveBeenCalledWith(current, 'session-A')
    await h.api.open('doc_weather')
    expect(h.show).toHaveBeenCalledTimes(2)
  })

  it.each(['resolve', 'openCurrent'] as const)('refuses unavailable %s without a fallback', async stage => {
    const h = setup()
    h[stage].mockResolvedValue(null)
    await h.api.open('doc_weather')
    expect(h.show).not.toHaveBeenCalled()
    expect(h.onError).toHaveBeenCalledOnce()
    if (stage === 'resolve') expect(h.openCurrent).not.toHaveBeenCalled()
  })

  it('surfaces permission failures without opening any preview', async () => {
    const h = setup()
    h.openCurrent.mockRejectedValue(new Error('ACCESS_DENIED'))
    await h.api.open('doc_weather')
    expect(h.show).not.toHaveBeenCalled()
    expect(h.onError).toHaveBeenCalledOnce()
  })

  it.each(['resolve', 'openCurrent'] as const)('respects preview capability at %s', async stage => {
    const h = setup()
    const unavailable = { ...resource, capabilities: { ...resource.capabilities, preview: false } }
    if (stage === 'resolve') h.resolve.mockResolvedValue(unavailable)
    else h.openCurrent.mockResolvedValue({ ...current, resource: unavailable })
    await h.api.open('doc_weather')
    expect(h.show).not.toHaveBeenCalled()
    expect(h.onError).toHaveBeenCalledOnce()
    if (stage === 'resolve') expect(h.openCurrent).not.toHaveBeenCalled()
  })

  it.each(['switch-back', 'reset', 'dispose'] as const)('invalidates pending resolve on %s', async change => {
    const h = setup()
    const pending = deferred<WorkbenchResource | null>()
    h.resolve.mockReturnValue(pending.promise)
    const opening = h.api.open('doc_weather')
    if (change === 'switch-back') {
      h.sessionKey.value = 'session-B'
      h.sessionKey.value = 'session-A'
    } else if (change === 'reset') h.currentEpoch.value += 1
    else h.scope.stop()
    pending.resolve(resource)
    await opening
    expect(h.openCurrent).not.toHaveBeenCalled()
    expect(h.show).not.toHaveBeenCalled()
    expect(h.onError).not.toHaveBeenCalled()
  })

  it('does not steal focus when the task changes while openCurrent is pending', async () => {
    const h = setup()
    const pending = deferred<WorkbenchResourceOpenResponse | null>()
    h.openCurrent.mockReturnValue(pending.promise)
    const opening = h.api.open('doc_weather')
    await vi.waitFor(() => expect(h.openCurrent).toHaveBeenCalledOnce())
    h.sessionKey.value = 'session-B'
    pending.resolve(current)
    await opening
    expect(h.show).not.toHaveBeenCalled()
  })

  it('rejects a stale clicked action and unsupported capability explicitly', async () => {
    const h = setup()
    await h.api.open('doc_weather', 'session-B')
    expect(h.resolve).not.toHaveBeenCalled()
    h.enabled.value = false
    await h.api.open('doc_weather')
    expect(h.onError).toHaveBeenCalledOnce()
    expect(h.resolve).not.toHaveBeenCalled()
  })

  it('keeps a child page on the same Document and forwards its validated logical path', async () => {
    const h = setup()
    const pages = { ...resource, previewPages: ['index.html', 'pages/culture.html'] }
    h.resolve.mockResolvedValue(pages)
    const opened = { ...current, resource: pages }
    h.openCurrent.mockResolvedValue(opened)
    await h.api.open('doc_weather', 'session-A', 'pages/culture.html')
    expect(h.openCurrent).toHaveBeenCalledWith('session-A', pages)
    expect(h.show).toHaveBeenCalledWith(opened, 'session-A', 'pages/culture.html')
    await h.api.open('doc_weather', 'session-A')
    expect(h.show).toHaveBeenLastCalledWith(opened, 'session-A')
  })

  it.each(['../secret.html', '/outside.html', 'style.css', 'bad%2fname.html', 'page.html?x=1', ''])
  ('rejects unsafe page target %s before resource resolution', async page => {
    const h = setup()
    await h.api.open('doc_weather', 'session-A', page)
    expect(h.resolve).not.toHaveBeenCalled()
    expect(h.show).not.toHaveBeenCalled()
    expect(h.onError).toHaveBeenCalledOnce()
  })

  it.each(['resolve', 'openCurrent'] as const)('revalidates page membership at %s', async stage => {
    const h = setup()
    const pages = { ...resource, previewPages: ['culture.html'] }
    h.resolve.mockResolvedValue(pages)
    h.openCurrent.mockResolvedValue({ ...current, resource: pages })
    if (stage === 'resolve') h.resolve.mockResolvedValue({ ...resource, previewPages: [] })
    else h.openCurrent.mockResolvedValue({ ...current, resource: { ...resource, previewPages: [] } })
    await h.api.open('doc_weather', 'session-A', 'culture.html')
    expect(h.show).not.toHaveBeenCalled()
    expect(h.onError).toHaveBeenCalledOnce()
    if (stage === 'resolve') expect(h.openCurrent).not.toHaveBeenCalled()
  })

  it('keeps the most recently clicked page when two opens finish out of order', async () => {
    const h = setup()
    const pages = { ...resource, previewPages: ['culture.html', 'food.html'] }
    h.resolve.mockResolvedValue(pages)
    const first = deferred<WorkbenchResourceOpenResponse | null>()
    h.openCurrent.mockReturnValueOnce(first.promise).mockResolvedValue({ ...current, resource: pages })
    const earlier = h.api.open('doc_weather', 'session-A', 'culture.html')
    await vi.waitFor(() => expect(h.openCurrent).toHaveBeenCalledOnce())
    await h.api.open('doc_weather', 'session-A', 'food.html')
    first.resolve({ ...current, resource: pages })
    await earlier
    expect(h.show).toHaveBeenCalledOnce()
    expect(h.show).toHaveBeenCalledWith(expect.anything(), 'session-A', 'food.html')
  })

  it('does not display a requested child page after the session resets during its open', async () => {
    const h = setup()
    const pages = { ...resource, previewPages: ['culture.html'] }
    h.resolve.mockResolvedValue(pages)
    const pending = deferred<WorkbenchResourceOpenResponse | null>()
    h.openCurrent.mockReturnValue(pending.promise)
    const opening = h.api.open('doc_weather', 'session-A', 'culture.html')
    await vi.waitFor(() => expect(h.openCurrent).toHaveBeenCalledOnce())
    h.currentEpoch.value += 1
    pending.resolve({ ...current, resource: pages })
    await opening
    expect(h.show).not.toHaveBeenCalled()
    expect(h.onError).not.toHaveBeenCalled()
  })
})
