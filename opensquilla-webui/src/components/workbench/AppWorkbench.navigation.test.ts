// @vitest-environment happy-dom
import { createApp, nextTick, type App } from 'vue'
import { createPinia } from 'pinia'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import en from '@/locales/en.json'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { useWorkbenchStore } from '@/workbench/store'
import { useWorkbenchResourcesStore } from '@/stores/workbenchResources'
import { artifactFromWorkbenchItem, artifactWorkbenchItemId, createArtifactPreviewWorkbenchItem } from '@/workbench/artifactItems'
import { createLegacyArtifactWorkspace } from '@/workbench/artifactDocumentProvider'
import { artifactProductClientError } from '@/utils/artifactProductErrors'
import type { ArtifactPayload } from '@/types/artifacts'
import type { WorkbenchResource, WorkbenchResourceOpenResponse } from '@/types/workbenchResources'
import AppWorkbench from './AppWorkbench.vue'

const { pushToast } = vi.hoisted(() => ({ pushToast: vi.fn() }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast }) }))
vi.mock('@/workbench/runtime', async importOriginal => ({
  ...await importOriginal<typeof import('@/workbench/runtime')>(),
  attachWorkbenchRuntime: () => async () => {},
}))
// Exercise AppWorkbench's real actions slot without mounting preview engines.
vi.mock('./WorkbenchHost.vue', async () => {
  const { defineComponent, h } = await import('vue')
  const { useWorkbenchStore } = await import('@/workbench/store')
  return { default: defineComponent({ inheritAttrs: false, setup(_, { slots }) {
    const store = useWorkbenchStore()
    return () => h('div', slots.toolbar?.({ item: store.items.find(item => item.id === store.activeItemId) || null }))
  } }) }
})
vi.mock('@/components/ResourceActionsMenu.vue', () => ({ default: { render: () => null } }))

const apps: App[] = []
const current: ArtifactPayload = { id: 'current-html', name: 'current.html', mime: 'text/html' }
const target: ArtifactPayload = { id: 'raw-slides', name: 'slides.pptx', mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' }
const capabilities = { preview: true, download: true, selectionContext: true, manualEdit: false, agentEdit: true, edit: true, publish: true }
function resource(artifact = target): WorkbenchResource {
  return { resource: { type: 'deliverable', id: String(artifact.id) }, name: artifact.name!, mime: artifact.mime!,
    sha256: 'a'.repeat(64), capabilities, relations: {} }
}
function opened(artifact = target): WorkbenchResourceOpenResponse {
  const workspace = createLegacyArtifactWorkspace(artifact, 'session-a')
  return {
    disposition: 'document', resolution: { status: 'current' }, materialized: false,
    resource: { ...resource(artifact), resource: { type: 'document', documentId: 'canonical-doc' } },
    document: { ...workspace.document, documentId: 'canonical-doc', headRevisionId: 'revision-head', capabilities: { ...workspace.document.capabilities, ...capabilities } },
    revision: { ...workspace.revisions[0]!, documentId: 'canonical-doc', revisionId: 'revision-head', artifactId: 'canonical-head' },
  }
}
async function flush() { for (let i = 0; i < 8; i += 1) { await Promise.resolve(); await nextTick() } }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
async function mount(options: { canonicalCurrent?: boolean; target?: ArtifactPayload; resourcesEnabled?: boolean } = {}) {
  const nextArtifact = options.target || target
  const pinia = createPinia()
  const store = useWorkbenchStore(pinia)
  store.setSessionScope('session-a')
  const item = createArtifactPreviewWorkbenchItem({ artifact: current, nativeHtml: false, sessionKey: 'session-a',
    navigationArtifacts: [current, nextArtifact], ...(options.canonicalCurrent ? { resourceIdentity: 'document:current-doc' } : {}) })
  store.openItem(item)
  const resources = useWorkbenchResourcesStore(pinia)
  const resolve = vi.spyOn(resources, 'resolve').mockResolvedValue(resource(nextArtifact))
  const openCurrent = vi.spyOn(resources, 'openCurrent').mockResolvedValue(opened(nextArtifact))
  const host = document.createElement('div'); document.body.append(host)
  const app = createApp(AppWorkbench, { sessionId: 'session-a', routeActive: true, workbenchResourcesEnabled: options.resourcesEnabled ?? true })
  app.use(pinia)
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.provide(ARTIFACT_WORKBENCH_KEY, {
    documents: null, resources: null, content: {}, previews: {},
    subscribeDocumentChanges: () => ({ close() {} }),
  } as unknown as ArtifactWorkbench)
  app.mount(host); apps.push(app)
  await flush()
  const select = () => host.querySelector<HTMLSelectElement>('[data-testid="workbench-artifact-switcher"]')
  const choose = async () => {
    const picker = select()!
    picker.value = artifactWorkbenchItemId('session-a', nextArtifact)
    picker.dispatchEvent(new Event('change', { bubbles: true }))
    await flush()
  }
  return { host, store, resources, item, resolve, openCurrent, select, choose }
}
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks(); pushToast.mockClear()
})

describe('AppWorkbench artifact navigation', () => {
  it('hides the switcher when the current canonical identity has no matching option', async () => {
    const h = await mount({ canonicalCurrent: true })
    expect(h.select()).toBeNull()
    expect(h.resolve).not.toHaveBeenCalled()
  })

  it('shows a matched legacy item with the current option selected', async () => {
    const h = await mount()
    expect(h.select()?.options.length).toBe(2)
    expect(h.select()?.value).toBe(h.item.id)
  })

  it.each([
    target,
    { id: 'raw-html', name: 'other.html', mime: 'text/html', documentId: 'known-doc' },
  ])('opens $name through canonical resource resolution', async artifact => {
    const h = await mount({ target: artifact })
    await h.choose()
    expect(h.resolve).toHaveBeenCalledWith('session-a', artifact.documentId
      ? { type: 'document', id: artifact.documentId, documentId: artifact.documentId }
      : { type: 'deliverable', id: artifact.id, artifactId: artifact.id })
    expect(h.openCurrent).toHaveBeenCalledWith('session-a', resource(artifact))
    const active = h.store.items.find(item => item.id === h.store.activeItemId)!
    expect(artifactFromWorkbenchItem(active)).toMatchObject({ id: 'canonical-head', documentId: 'canonical-doc', revisionId: 'revision-head' })
    expect(h.store.items.some(item => item.id === artifactWorkbenchItemId('session-a', artifact))).toBe(false)
  })

  it.each(['resolve', 'openCurrent'] as const)('shows %s failure without opening a raw fallback', async phase => {
    const h = await mount()
    h[phase].mockRejectedValueOnce(artifactProductClientError('PERMISSION_DENIED'))
    await h.choose()
    expect(h.store.items).toHaveLength(1)
    expect(h.store.activeItemId).toBe(h.item.id)
    expect(h.select()?.value).toBe(h.item.id)
    expect(pushToast).toHaveBeenCalledWith(en.workbench.artifactErrors.permissionDenied, expect.objectContaining({ tone: 'danger' }))
  })

  it('retains the legacy path when resource resolution reports an older Gateway', async () => {
    const h = await mount()
    h.resolve.mockResolvedValueOnce(null)
    await h.choose()
    expect(h.openCurrent).not.toHaveBeenCalled()
    expect(h.store.activeItemId).toBe(artifactWorkbenchItemId('session-a', target))
  })

  it.each(['close origin', 'change session'] as const)('does not reopen after %s during resolution', async invalidation => {
    const h = await mount()
    const pending = deferred<WorkbenchResource | null>()
    h.resolve.mockReturnValueOnce(pending.promise)
    await h.choose()
    if (invalidation === 'close origin') h.store.closeItem(h.item.id)
    else h.store.setSessionScope('session-b')
    pending.resolve(resource()); await flush()
    expect(h.openCurrent).not.toHaveBeenCalled()
    expect(h.store.items).toHaveLength(0)
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('does not reopen after the origin closes during canonical open', async () => {
    const h = await mount()
    const pending = deferred<WorkbenchResourceOpenResponse | null>()
    h.openCurrent.mockReturnValueOnce(pending.promise)
    await h.choose()
    expect(h.openCurrent).toHaveBeenCalledTimes(1)
    h.store.closeItem(h.item.id)
    pending.resolve(opened()); await flush()
    expect(h.store.items).toHaveLength(0)
  })
})
