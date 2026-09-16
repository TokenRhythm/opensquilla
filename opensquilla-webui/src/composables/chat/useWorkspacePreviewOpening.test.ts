import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref, type EffectScope } from 'vue'
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
function setup() {
  const scope = effectScope()
  scopes.push(scope)
  const options = {
    sessionKey: ref('session-A'), currentEpoch: ref(0), enabled: ref(true),
    resolve: vi.fn<() => Promise<WorkbenchResource | null>>().mockResolvedValue(resource),
    openCurrent: vi.fn<() => Promise<WorkbenchResourceOpenResponse | null>>().mockResolvedValue(current),
    show: vi.fn(), onError: vi.fn(),
  }
  return { ...options, scope, api: scope.run(() => useWorkspacePreviewOpening(options))! }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
afterEach(() => scopes.splice(0).forEach(scope => scope.stop()))

describe('workspace preview canonical opening', () => {
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
