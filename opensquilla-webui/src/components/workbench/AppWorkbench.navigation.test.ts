// @vitest-environment happy-dom
import { createApp, nextTick, type App } from 'vue'
import { createPinia } from 'pinia'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import en from '@/locales/en.json'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { useWorkbenchStore } from '@/workbench/store'
import { WorkbenchRuntimeManager } from '@/workbench/runtime'
import { useWorkbenchResourcesStore } from '@/stores/workbenchResources'
import { artifactDocumentWorkspaceKey, useArtifactDocumentsStore } from '@/stores/artifactDocuments'
import { useArtifactPromptAnnotationsStore } from '@/stores/artifactPromptAnnotations'
import { artifactFromWorkbenchItem, artifactWorkbenchItemId, createArtifactPreviewWorkbenchItem } from '@/workbench/artifactItems'
import { artifactPayloadFromRevision, createLegacyArtifactWorkspace } from '@/workbench/artifactDocumentProvider'
import { createResourceCollectionWorkbenchItem } from '@/workbench/workbenchResourceItems'
import { artifactProductClientError } from '@/utils/artifactProductErrors'
import * as platformApi from '@/platform'
import { createWebPlatform } from '@/platform/web'
import type { ArtifactPayload } from '@/types/artifacts'
import { createWorkbenchResourceRef, type WorkbenchResource, type WorkbenchResourceOpenResponse } from '@/types/workbenchResources'
import AppWorkbench from './AppWorkbench.vue'

const { pushToast, previewHarness } = vi.hoisted(() => ({ pushToast: vi.fn(), previewHarness: { enabled: false, runtime: false } }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast }) }))
vi.mock('@/workbench/runtime', async importOriginal => {
  const original = await importOriginal<typeof import('@/workbench/runtime')>()
  return {
    ...original,
    attachWorkbenchRuntime: (...args: Parameters<typeof original.attachWorkbenchRuntime>) =>
      previewHarness.runtime ? original.attachWorkbenchRuntime(...args) : async () => {},
  }
})
// Keep the real collection runtime and event routing; preview-engine I/O is
// outside this host integration test (the document panel is stubbed below).
vi.mock('./artifactWorkbenchProvider', async importOriginal => {
  const original = await importOriginal<typeof import('./artifactWorkbenchProvider')>()
  return { ...original, createArtifactWorkbenchDefinitions: (options: Parameters<typeof original.createArtifactWorkbenchDefinitions>[0]) =>
    original.createArtifactWorkbenchDefinitions({ ...options, previewLeasesEnabled: false }),
  }
})
// Exercise AppWorkbench's real actions slot without mounting preview engines.
vi.mock('./WorkbenchHost.vue', async () => {
  const { defineComponent, h } = await import('vue')
  const { useWorkbenchStore } = await import('@/workbench/store')
  return { default: defineComponent({ inheritAttrs: false, props: ['tabInfo'], setup(props, { slots }) {
    const store = useWorkbenchStore()
    return () => h('div', [
      slots.toolbar?.({ item: store.items.find(item => item.id === store.activeItemId) || null }),
      ...(previewHarness.enabled ? store.items.flatMap(item => {
        const info = (props.tabInfo as (item: unknown) => { thumbnailUrl?: string })(item)
        return [
          h('img', { 'data-tab-thumbnail': item.id, src: info.thumbnailUrl }),
          slots.panel?.({ item, active: item.id === store.activeItemId }),
        ]
      }) : []),
    ])
  } }) }
})
vi.mock('./ArtifactDocumentPanel.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ inheritAttrs: false, props: ['artifact', 'documentSnapshot'],
    emits: ['workbench-event'], setup(props, { emit }) {
      return () => h('button', { 'data-capture-thumbnail': props.artifact?.id,
        onClick: () => emit('workbench-event', { type: 'preview-thumbnail', payload: new Blob([
          props.documentSnapshot?.workspace?.headArtifact?.revisionId || props.artifact?.revisionId || '',
        ], { type: 'image/png' }) }),
      }, 'Capture thumbnail')
    },
  }) }
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
    resource: { ...resource(artifact), resource: { type: 'document', documentId: 'canonical-doc' },
      relations: { documentId: 'canonical-doc', headRevisionId: 'revision-head', headArtifactId: 'canonical-head' } },
    document: { ...workspace.document, documentId: 'canonical-doc', headRevisionId: 'revision-head', capabilities: { ...workspace.document.capabilities, ...capabilities } },
    revision: { ...workspace.revisions[0]!, documentId: 'canonical-doc', revisionId: 'revision-head', artifactId: 'canonical-head' },
  }
}
async function flush() { for (let i = 0; i < 8; i += 1) { await Promise.resolve(); await nextTick() } }
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (reason?: unknown) => void; const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail }); return { promise, resolve, reject } }
async function mount(options: { canonicalCurrent?: boolean; currentArtifact?: ArtifactPayload; nativeHtml?: boolean; target?: ArtifactPayload; resourcesEnabled?: boolean; resourceSessionKey?: string } = {}) {
  const currentArtifact = options.currentArtifact || current
  const nextArtifact = options.target || target
  const resourceSessionKey = options.resourceSessionKey || 'session-a'
  const pinia = createPinia()
  const store = useWorkbenchStore(pinia)
  store.setSessionScope('session-a')
  const item = { ...createArtifactPreviewWorkbenchItem({ artifact: currentArtifact, nativeHtml: options.nativeHtml === true, sessionKey: resourceSessionKey,
    navigationArtifacts: [currentArtifact, nextArtifact], resourceIdentity: options.canonicalCurrent
      ? `document:${currentArtifact.documentId || 'current-doc'}` : `deliverable:${currentArtifact.id}` }),
    scope: { type: 'session', id: 'session-a' } as const }
  store.openItem(item)
  const resources = useWorkbenchResourcesStore(pinia)
  const resolve = vi.spyOn(resources, 'resolveFresh').mockResolvedValue(resource(nextArtifact))
  const resolveLegacy = vi.spyOn(resources, 'resolve').mockResolvedValue(resource(nextArtifact))
  const openCurrent = vi.spyOn(resources, 'openCurrent').mockResolvedValue(opened(nextArtifact))
  const prepareAnnotation = vi.spyOn(resources, 'prepareAnnotation').mockResolvedValue(opened(nextArtifact))
  const host = document.createElement('div'); document.body.append(host)
  const fetchArtifact = vi.fn().mockResolvedValue({ ok: true, blob: new Blob(['download']) })
  const app = createApp(AppWorkbench, { sessionId: 'session-a', routeActive: true,
    promptAnnotationsEnabled: options.nativeHtml === true,
    officePromptAnnotationsEnabled: true, workbenchResourcesEnabled: options.resourcesEnabled ?? true })
  app.use(pinia)
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.provide(ARTIFACT_WORKBENCH_KEY, {
    documents: null, resources: null, content: { fetchArtifact }, previews: {},
    subscribeDocumentChanges: () => ({ close() {} }),
  } as unknown as ArtifactWorkbench)
  app.mount(host); apps.push(app)
  await flush()
  const select = () => host.querySelector<HTMLSelectElement>('[data-testid="workbench-artifact-switcher"]')
  const choose = async () => {
    const picker = select()!
    picker.value = artifactWorkbenchItemId(resourceSessionKey, nextArtifact, `deliverable:${nextArtifact.id}`)
    picker.dispatchEvent(new Event('change', { bubbles: true }))
    await flush()
  }
  const continueEditing = async () => {
    host.querySelector<HTMLButtonElement>(`[aria-label="${en.workbench.annotationUnified.start}"]`)!.click()
    await flush()
  }
  return { host, store, resources, documents: useArtifactDocumentsStore(pinia), annotations: useArtifactPromptAnnotationsStore(pinia), item, resolve, resolveLegacy,
    openCurrent, prepareAnnotation, select, choose, continueEditing, fetchArtifact }
}
function installPreparedWorkspace(
  h: Awaited<ReturnType<typeof mount>>,
  response = opened(),
  options: { revisionId?: string; sha256?: string; stale?: boolean } = {},
) {
  if (response.disposition !== 'document') throw new Error('Expected document fixture')
  const revision = { ...response.revision,
    ...(options.revisionId ? { revisionId: options.revisionId } : {}),
    ...(options.sha256 ? { artifactSha256: options.sha256 } : {}) }
  const artifact = artifactPayloadFromRevision(revision)
  const workspace = { ...createLegacyArtifactWorkspace(artifact, 'session-a'), source: 'document-api' as const,
    document: { ...response.document, headRevisionId: revision.revisionId }, headArtifact: artifact }
  vi.spyOn(h.documents, 'load').mockImplementation(async (requested, sessionKey) => {
    const key = artifactDocumentWorkspaceKey(requested, sessionKey)
    h.documents.snapshots[key] = { key, loaded: true, loading: false,
      stale: options.stale === true, error: options.stale ? 'Disconnected' : null, workspace }
    return workspace
  })
  return workspace
}

async function mountReadyAnnotationPreview(options: {
  fixed?: boolean
  artifact?: Partial<ArtifactPayload>
  state?: Record<string, unknown>
  prepared?: Partial<Extract<WorkbenchResourceOpenResponse, { disposition: 'document' }>>
} = {}) {
  const prepared = opened(current)
  if (prepared.disposition !== 'document') throw new Error('Expected HTML document fixture')
  prepared.revision.artifactSha256 = 'a'.repeat(64)
  Object.assign(prepared, options.prepared)
  const artifact = { ...artifactPayloadFromRevision(prepared.revision),
    documentId: prepared.document.documentId, ...options.artifact }
  const webPlatform = createWebPlatform()
  vi.spyOn(platformApi, 'usePlatform').mockReturnValue({
    ...webPlatform, id: 'desktop',
    capabilities: { ...webPlatform.capabilities, hasNativeWorkbenchSurfaces: true },
    workbench: { ...webPlatform.workbench, native: {
      onSurfaceEvent: () => () => {},
      createSurface: vi.fn(async () => ({ ok: true })),
      activateSurface: vi.fn(async () => ({ ok: true })),
      setSurfaceRect: vi.fn(async () => ({ ok: true })),
      destroySurface: vi.fn(async () => ({ ok: true })),
    } },
  })
  const state = {
    annotationMode: false, annotationAvailable: true, nativeSurfaceState: 'ready',
    previewWorkingDocumentId: prepared.document.documentId,
    workingFileDocumentId: prepared.document.documentId,
    workingFileEntrypoint: 'index.html', workingFileInitialPage: 'index.html',
    previewLaunchUrl: 'http://preview.test/lease/index.html',
    currentUrl: 'http://preview.test/lease/index.html',
    ...options.state,
  }
  const getRenderState = vi.spyOn(WorkbenchRuntimeManager.prototype, 'getRenderState').mockReturnValue(state)
  previewHarness.runtime = true
  const h = await mount({ canonicalCurrent: !options.fixed, currentArtifact: artifact, nativeHtml: true })
  h.prepareAnnotation.mockResolvedValue(prepared)
  installPreparedWorkspace(h, prepared)
  const runtime = getRenderState.mock.contexts[getRenderState.mock.contexts.length - 1] as WorkbenchRuntimeManager
  runtime.handleSurfaceRect({ itemId: h.item.id, x: 0, y: 0, width: 640, height: 480, visible: true })
  await runtime.flush(h.item.id)
  const openItem = vi.spyOn(h.store, 'openItem')
  const performAction = vi.spyOn(WorkbenchRuntimeManager.prototype, 'performAction')
  return { ...h, prepared, state, runtime, openItem, performAction }
}
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks(); pushToast.mockClear()
  previewHarness.enabled = false
  previewHarness.runtime = false
  localStorage.clear()
})

describe('AppWorkbench artifact navigation', () => {
  it.each([target, { id: 'listed-html', name: 'listed.html', mime: 'text/html' }].flatMap(artifact =>
    ['session-a', 'agent:main:sidechat:child'].map(resourceSessionKey => ({ artifact, name: artifact.name, resourceSessionKey })),
  ))(
    'opens listed $name with $resourceSessionKey authority through the mounted panel', async ({ artifact, resourceSessionKey }) => {
      previewHarness.enabled = true
      previewHarness.runtime = true
      const h = await mount({ target: artifact })
      const listed = resource(artifact)
      // A side collection is visible in the parent, but must never render or
      // read the parent's inventory. Keep different records in both stores.
      h.resources.snapshots['session-a'] = {
        sessionKey: 'session-a', available: true, loading: false, loaded: true,
        error: null, resources: [resource({ id: 'parent-only', name: 'parent-only.html', mime: 'text/html' })], totalCount: 1,
      }
      h.resources.snapshots[resourceSessionKey] = {
        sessionKey: resourceSessionKey, available: true, loading: false, loaded: true,
        error: null, resources: [listed], totalCount: 1,
      }
      const load = vi.spyOn(h.resources, 'load')
      const retain = vi.spyOn(h.resources, 'retain')
      const collection = createResourceCollectionWorkbenchItem({
        sessionKey: resourceSessionKey, title: 'Files', resources: [listed],
      })
      collection.scope = { type: 'session', id: 'session-a' }
      h.store.openItem(collection)
      await flush()
      const entry = h.host.querySelector<HTMLButtonElement>('.resource-collection__open')
      expect(entry?.textContent).toContain(artifact.name)
      expect(entry?.textContent).toContain(en.workbench.resources.publishedVersion)
      expect(h.host.querySelector('.resource-collection')?.textContent).not.toContain('parent-only.html')
      await vi.waitFor(() => expect(load).toHaveBeenCalledWith(resourceSessionKey))
      expect(retain).toHaveBeenCalledWith(resourceSessionKey)
      expect(load.mock.calls.every(([key]) => key === resourceSessionKey)).toBe(true)
      expect(retain.mock.calls.every(([key]) => key === resourceSessionKey)).toBe(true)
      vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:download')
      vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
      h.host.querySelector<HTMLButtonElement>(`[aria-label="Download ${artifact.name}"]`)!.click()
      await flush()
      await vi.waitFor(() => expect(h.fetchArtifact).toHaveBeenCalledWith(expect.objectContaining({ id: artifact.id }), { sessionKey: resourceSessionKey }))
      expect(h.resolveLegacy).toHaveBeenCalledWith(resourceSessionKey, listed.resource)
      expect(h.store.activeItemId).toBe(collection.id)

      const imported = vi.spyOn(h.resources, 'importDocument')
      entry!.click()
      await flush()
      expect(h.openCurrent).not.toHaveBeenCalled()
      expect(imported).not.toHaveBeenCalled()
      expect(h.store.activeItem?.kind).toBe('artifact-preview')
      expect(h.store.activeItem?.scope).toEqual({ type: 'session', id: 'session-a' })
      expect(h.store.activeItem?.payload.sessionKey).toBe(resourceSessionKey)
      expect(h.store.activeItem?.payload.resourceIdentity).toBe(`deliverable:${artifact.id}`)
      expect(artifactFromWorkbenchItem(h.store.activeItem!)).toMatchObject({
        id: artifact.id, sha256: listed.sha256,
      })
    },
  )

  it.each(['document', 'attachment'] as const)('uses read-only discovery and retry to open a listed %s', async type => {
    previewHarness.enabled = true
    previewHarness.runtime = true
    const h = await mount()
    const resolved = opened()
    if (resolved.disposition !== 'document') throw new Error('Expected document fixture')
    const listed: WorkbenchResource = { ...resource(),
      resource: createWorkbenchResourceRef(type, type === 'document' ? 'canonical-doc' : 'listed-current'),
      relations: { documentId: 'canonical-doc' } }
    h.resources.snapshots['session-a'] = {
      sessionKey: 'session-a', available: true, loading: false, loaded: true,
      error: null, resources: [listed], totalCount: 1,
    }
    const load = vi.spyOn(h.resources, 'load')
    const collection = createResourceCollectionWorkbenchItem({ sessionKey: 'session-a', title: 'Files', resources: [listed] })
    h.store.openItem(collection)
    await flush()
    await vi.waitFor(() => expect(load).toHaveBeenCalledWith('session-a'))
    expect(h.host.querySelector('.resource-collection__open')?.textContent).toContain(type === 'document'
      ? en.workbench.resources.currentWorkingFile : target.name)
    const artifact = { ...target, id: 'canonical-head', documentId: 'canonical-doc', revisionId: 'revision-head' }
    const workspace = createLegacyArtifactWorkspace(artifact, 'session-a')
    const key = artifactDocumentWorkspaceKey(artifact, 'session-a')
    h.documents.snapshots[key] = { key, loaded: true, loading: false, stale: false, error: null,
      workspace: { ...workspace, source: 'document-api', document: resolved.document, headArtifact: artifact } }
    h.resolve.mockRejectedValueOnce(artifactProductClientError('PERMISSION_DENIED'))
      .mockImplementation(async (_session, ref) => ref.type === 'document' ? resolved.resource : listed)
    h.host.querySelector<HTMLButtonElement>('.resource-collection__open')!.click()
    await flush()
    await vi.waitFor(() => expect(h.resolve).toHaveBeenCalledWith('session-a', listed.resource, expect.any(Function)))
    expect(h.host.querySelector('[role="alert"]')?.textContent).toContain(en.workbench.artifactErrors.permissionDenied)
    expect(h.store.activeItemId).toBe(collection.id)
    h.host.querySelector<HTMLButtonElement>(`[aria-label="${en.workbench.resources.retry}"]`)!.click()
    await flush()
    await vi.waitFor(() => expect(h.store.activeItem?.payload.resourceIdentity).toBe('document:canonical-doc'))
    expect(h.openCurrent).not.toHaveBeenCalled()
    expect(h.prepareAnnotation).not.toHaveBeenCalled()
    expect(artifactFromWorkbenchItem(h.store.activeItem!)).toMatchObject({
      id: 'canonical-head', documentId: 'canonical-doc', revisionId: 'revision-head',
    })
  })

  it.each([target, { id: 'side-html', name: 'side.html', mime: 'text/html' }])(
    'navigates side $name using child authorization while retaining parent visibility', async artifact => {
      const child = 'agent:main:sidechat:child'
      const h = await mount({ target: artifact, resourceSessionKey: child })
      await h.choose()
      expect(h.resolve).toHaveBeenCalledWith(child, { type: 'deliverable', id: artifact.id, artifactId: artifact.id }, expect.any(Function))
      expect(h.openCurrent).not.toHaveBeenCalled()
      const active = h.store.activeItem!
      expect(active.scope).toEqual({ type: 'session', id: 'session-a' })
      expect(active.payload.sessionKey).toBe(child)
      expect(artifactFromWorkbenchItem(active)).toMatchObject({ id: artifact.id })
      expect(h.store.visibleItems).toContainEqual(active)
      expect(h.store.activeSessionId).toBe('session-a')
    },
  )

  it('keeps child authorization and parent visibility on legacy side preview navigation', async () => {
    const child = 'agent:main:sidechat:child'
    const h = await mount({ resourceSessionKey: child })
    h.resolve.mockResolvedValueOnce(null)
    await h.choose()
    expect(h.store.activeItemId).toBe(artifactWorkbenchItemId(child, target, `deliverable:${target.id}`))
    expect(h.store.activeItem?.scope).toEqual({ type: 'session', id: 'session-a' })
    expect(h.store.activeItem?.payload.sessionKey).toBe(child)
  })

  it('keeps side preview scope when preparing an unlinked delivery for annotation', async () => {
    previewHarness.enabled = true
    previewHarness.runtime = true
    const child = 'agent:main:sidechat:child'
    const h = await mount({ resourceSessionKey: child })
    const result = opened()
    if (result.disposition !== 'document') throw new Error('Expected document fixture')
    const imported = vi.spyOn(h.resources, 'importDocument').mockResolvedValue({
      document: result.document, revision: result.revision,
    } as Awaited<ReturnType<typeof h.resources.importDocument>>)
    await h.choose()
    expect(imported).not.toHaveBeenCalled()
    expect(h.openCurrent).not.toHaveBeenCalled()
    installPreparedWorkspace(h, result)
    await h.continueEditing()
    await vi.waitFor(() => expect(h.prepareAnnotation).toHaveBeenCalledWith(child, {
      resourceRef: createWorkbenchResourceRef('deliverable', String(target.id)),
    }))
    expect(imported).not.toHaveBeenCalled()
    expect(h.store.activeItem?.payload.sessionKey).toBe(child)
    expect(h.store.activeItem?.scope).toEqual({ type: 'session', id: 'session-a' })
  })

  it.each(['close origin', 'change session'] as const)(
    'drops side navigation after %s while the child resource is resolving', async invalidation => {
      const h = await mount({ resourceSessionKey: 'agent:main:sidechat:child' })
      const pending = deferred<WorkbenchResource | null>()
      h.resolve.mockReturnValueOnce(pending.promise)
      await h.choose()
      expect(h.resolve).toHaveBeenCalledTimes(1)
      if (invalidation === 'close origin') h.store.closeItem(h.item.id)
      else h.store.setSessionScope('session-b')
      pending.resolve(resource()); await flush()
      expect(h.openCurrent).not.toHaveBeenCalled()
      expect(h.store.items.map(item => item.id)).toEqual(invalidation === 'close origin' ? [] : [h.item.id])
      expect(h.store.visibleItems).toHaveLength(0)
      expect(pushToast).not.toHaveBeenCalled()
    },
  )

  it.each([false, true])('retires an Office thumbnail when its document head changes (inactive=%s)', async inactive => {
    previewHarness.enabled = true
    vi.spyOn(URL, 'createObjectURL').mockReturnValueOnce('blob:old-office').mockReturnValueOnce('blob:new-office')
    const h = await mount()
    const artifact = { ...target, documentId: 'office-doc', revisionId: 'revision-1', sha256: 'a'.repeat(64) }
    const item = createArtifactPreviewWorkbenchItem({ artifact, nativeHtml: false, sessionKey: 'session-a',
      resourceIdentity: 'document:office-doc' })
    h.store.openItem(item)
    const key = artifactDocumentWorkspaceKey(artifact, 'session-a')
    const workspace = createLegacyArtifactWorkspace(artifact, 'session-a')
    function installHead(revisionId: string, sha256: string) {
      h.documents.snapshots[key] = { key, loaded: true, loading: false, stale: false, error: null,
        workspace: { ...workspace, source: 'document-api',
          document: { ...workspace.document, documentId: 'office-doc', headRevisionId: revisionId },
          headArtifact: { ...artifact, revisionId, sha256 },
        },
      }
    }
    installHead('revision-1', 'a'.repeat(64)); await flush()
    const capture = () => h.host.querySelector<HTMLButtonElement>(`[data-capture-thumbnail="${artifact.id}"]`)!.click()
    const thumbnail = () => h.host.querySelector<HTMLImageElement>(`[data-tab-thumbnail="${item.id}"]`)!.getAttribute('src')
    capture(); await flush()
    expect(thumbnail()).toBe('blob:old-office')
    if (inactive) { h.store.activateItem(h.item.id); await flush() }

    // An old renderer can finish in the microtask window before Vue replaces
    // its props. Those bytes must not be stored under the new head identity.
    const oldRenderFinishes = Promise.resolve().then(capture)
    installHead('revision-2', 'b'.repeat(64))
    await oldRenderFinishes; await flush()
    expect(artifactFromWorkbenchItem(h.store.items.find(candidate => candidate.id === item.id)!)).toMatchObject({ revisionId: 'revision-1' })
    expect(thumbnail()).toBeNull()

    h.store.activateItem(item.id); await flush()
    capture(); await flush()
    expect(thumbnail()).toBe('blob:new-office')
  })

  it('keeps an immutable Office preview thumbnail pinned when the related document head advances', async () => {
    previewHarness.enabled = true
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:immutable-office')
    const h = await mount()
    const artifact = { ...target, documentId: 'office-doc', revisionId: 'revision-1', sha256: 'a'.repeat(64) }
    const item = createArtifactPreviewWorkbenchItem({ artifact, nativeHtml: false, sessionKey: 'session-a',
      preparedPreview: { protocolVersion: 1, mode: 'local-renderer', sandboxProfile: 'none', network: false,
        resource: { type: 'deliverable', id: String(artifact.id) },
        adapter: { id: 'silurus-office', version: '0.88.0', format: 'pptx' },
      },
    })
    h.store.openItem(item); await flush()
    h.host.querySelector<HTMLButtonElement>(`[data-capture-thumbnail="${artifact.id}"]`)!.click(); await flush()
    const key = artifactDocumentWorkspaceKey(artifact, 'session-a')
    const workspace = createLegacyArtifactWorkspace(artifact, 'session-a')
    h.documents.snapshots[key] = { key, loaded: true, loading: false, stale: false, error: null,
      workspace: { ...workspace, source: 'document-api',
        document: { ...workspace.document, documentId: 'office-doc', headRevisionId: 'revision-2' },
        headArtifact: { ...artifact, revisionId: 'revision-2', sha256: 'b'.repeat(64) },
      },
    }
    await flush()
    expect(h.host.querySelector<HTMLImageElement>(`[data-tab-thumbnail="${item.id}"]`)!.getAttribute('src')).toBe('blob:immutable-office')
  })

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
  ])('opens the fixed $name snapshot through canonical resource resolution', async artifact => {
    const h = await mount({ target: artifact })
    await h.choose()
    expect(h.resolve).toHaveBeenCalledWith('session-a', { type: 'deliverable', id: artifact.id, artifactId: artifact.id }, expect.any(Function))
    expect(h.openCurrent).not.toHaveBeenCalled()
    const active = h.store.items.find(item => item.id === h.store.activeItemId)!
    expect(artifactFromWorkbenchItem(active)).toMatchObject({ id: artifact.id })
  })

  it('shows a resolve failure without opening a raw fallback', async () => {
    const h = await mount()
    h.resolve.mockRejectedValueOnce(artifactProductClientError('PERMISSION_DENIED'))
    await h.choose()
    expect(h.store.items).toHaveLength(1)
    expect(h.store.activeItemId).toBe(h.item.id)
    expect(h.select()?.value).toBe(h.item.id)
    expect(pushToast).toHaveBeenCalledWith(en.workbench.artifactErrors.permissionDenied, expect.objectContaining({ tone: 'danger' }))
  })

  it('keeps the fixed snapshot visible when annotation preparation is denied', async () => {
    previewHarness.enabled = true
    previewHarness.runtime = true
    const h = await mount()
    await h.choose()
    const snapshotId = h.store.activeItemId
    h.prepareAnnotation.mockRejectedValueOnce(artifactProductClientError('PERMISSION_DENIED'))
    await h.continueEditing()
    await vi.waitFor(() => expect(pushToast).toHaveBeenCalledWith(en.workbench.artifactErrors.permissionDenied,
      expect.objectContaining({ tone: 'danger' })))
    expect(h.store.activeItemId).toBe(snapshotId)
    expect(artifactFromWorkbenchItem(h.store.activeItem!)).toMatchObject({ id: target.id })
  })

  it('retains the legacy path when resource resolution reports an older Gateway', async () => {
    const h = await mount()
    h.resolve.mockResolvedValueOnce(null)
    await h.choose()
    expect(h.openCurrent).not.toHaveBeenCalled()
    expect(h.store.activeItemId).toBe(artifactWorkbenchItemId('session-a', target, `deliverable:${target.id}`))
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
    expect(h.store.items.map(item => item.id)).toEqual(invalidation === 'close origin' ? [] : [h.item.id])
    expect(h.store.visibleItems).toHaveLength(0)
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('does not reopen after the origin closes during canonical open', async () => {
    previewHarness.enabled = true
    previewHarness.runtime = true
    const h = await mount()
    await h.choose()
    const originId = h.store.activeItemId!
    const pending = deferred<WorkbenchResourceOpenResponse | null>()
    h.prepareAnnotation.mockReturnValueOnce(pending.promise)
    await h.continueEditing()
    await vi.waitFor(() => expect(h.prepareAnnotation).toHaveBeenCalledTimes(1))
    h.store.closeItem(originId)
    pending.resolve(opened()); await flush()
    expect(h.store.items.map(item => item.id)).toEqual([h.item.id])
  })

  it.each([false, true])('does not take focus after switching tabs during annotation preparation (return=%s)', async returns => {
    previewHarness.enabled = true
    previewHarness.runtime = true
    const h = await mount()
    await h.choose()
    const originId = h.store.activeItemId!
    const pending = deferred<WorkbenchResourceOpenResponse | null>()
    h.prepareAnnotation.mockReturnValueOnce(pending.promise)
    await h.continueEditing()
    await vi.waitFor(() => expect(h.prepareAnnotation).toHaveBeenCalledOnce())
    h.store.activateItem(h.item.id)
    if (returns) h.store.activateItem(originId)
    pending.resolve(opened()); await flush()
    expect(h.store.items.map(item => item.id)).toEqual([h.item.id, originId])
    expect(h.store.activeItemId).toBe(returns ? originId : h.item.id)
  })

  it('does not arm an already prepared document after leaving and returning during preview readiness', async () => {
    previewHarness.enabled = true
    previewHarness.runtime = true
    const h = await mount()
    await h.choose()
    installPreparedWorkspace(h)
    const ready = deferred<void>()
    const runtimeFlush = vi.spyOn(WorkbenchRuntimeManager.prototype, 'flush').mockReturnValueOnce(ready.promise)
    const performAction = vi.spyOn(WorkbenchRuntimeManager.prototype, 'performAction')
    await h.continueEditing()
    await vi.waitFor(() => expect(runtimeFlush).toHaveBeenCalled())
    const preparedId = h.store.activeItemId!
    expect(h.store.activeItem?.payload.resourceIdentity).toBe('document:canonical-doc')
    h.store.activateItem(h.item.id)
    h.store.activateItem(preparedId)
    ready.resolve()
    await flush()
    expect(performAction.mock.calls.some(([, action]) => action === 'begin-prepared-annotation')).toBe(false)
  })

  it.each([
    { name: 'entrypoint', pagePath: undefined, currentUrl: 'http://preview.test/lease/index.html#kept' },
    { name: 'authorized child page', pagePath: 'pages/detail.html', currentUrl: 'http://preview.test/lease/pages/detail.html' },
  ])('keeps the ready $name preview while authorizing each annotation enable', async ({ pagePath, currentUrl }) => {
    const h = await mountReadyAnnotationPreview({
      artifact: { previewPagePath: pagePath },
      state: { currentUrl },
      prepared: { pageContext: pagePath ? { pagePath } : undefined },
    })
    const original = h.store.activeItem!
    const originalRequest = original.payload.initialSectionRequestId

    await h.continueEditing()
    await vi.waitFor(() => expect(h.performAction).toHaveBeenCalledWith(original, 'begin-prepared-annotation'))
    await h.continueEditing()
    await vi.waitFor(() => expect(h.performAction.mock.calls
      .filter(([, action]) => action === 'begin-prepared-annotation')).toHaveLength(2))

    expect(h.prepareAnnotation).toHaveBeenCalledTimes(2)
    expect(h.prepareAnnotation).toHaveBeenNthCalledWith(2, 'session-a', {
      resourceRef: createWorkbenchResourceRef('document', h.prepared.document.documentId),
    })
    expect(h.documents.load).toHaveBeenCalledTimes(2)
    expect(h.documents.load).toHaveBeenLastCalledWith(expect.objectContaining({
      revisionId: h.prepared.revision.revisionId, sha256: h.prepared.revision.artifactSha256,
    }), 'session-a', { force: true })
    expect(h.openItem).not.toHaveBeenCalled()
    expect(h.store.activeItem).toBe(original)
    expect(h.store.activeItem?.payload.initialSectionRequestId).toBe(originalRequest)
    expect(h.performAction.mock.calls.filter(([, action]) => action === 'begin-prepared-annotation')).toHaveLength(2)
  })

  it.each([
    { name: 'fixed snapshot', options: { fixed: true } },
    { name: 'Source section', options: { state: { nativeSurfaceState: 'loading', annotationAvailable: false } } },
    { name: 'different page', options: { state: { currentUrl: 'http://preview.test/lease/pages/other.html' } } },
    { name: 'unknown page', options: { state: { workingFilePageUnknown: true } } },
    { name: 'outside the lease', options: { state: { currentUrl: 'http://other.test/index.html' } } },
    { name: 'missing working lease binding', options: { state: { previewWorkingDocumentId: '' } } },
    { name: 'different working lease', options: { state: { previewWorkingDocumentId: 'other-doc' } } },
    { name: 'changed head', options: { artifact: { revisionId: 'revision-previous' } } },
    { name: 'changed bytes', options: { artifact: { sha256: 'b'.repeat(64) } } },
    { name: 'updated baseline', options: { prepared: { baselineUpdated: true } } },
  ])('opens the prepared preview for $name', async ({ options }) => {
    const h = await mountReadyAnnotationPreview(options)
    const originalId = h.store.activeItemId
    const originalRequest = Number(h.store.activeItem?.payload.initialSectionRequestId || 0)

    await h.continueEditing()

    await vi.waitFor(() => expect(h.openItem).toHaveBeenCalledOnce())
    expect(h.prepareAnnotation).toHaveBeenCalledOnce()
    expect(h.store.activeItem?.payload.resourceIdentity).toBe('document:canonical-doc')
    expect(h.store.activeItem?.payload.initialSection).toBe('preview')
    expect(h.store.activeItem?.payload.initialSectionRequestId)
      .toBe(h.store.activeItemId === originalId ? originalRequest + 1 : 1)
  })

  it('requests Preview if Source hides the surface while annotation authorization is pending', async () => {
    const h = await mountReadyAnnotationPreview()
    const originalRequest = Number(h.store.activeItem?.payload.initialSectionRequestId || 0)
    const pending = deferred<WorkbenchResourceOpenResponse | null>()
    h.prepareAnnotation.mockReturnValueOnce(pending.promise)
    await h.continueEditing()
    await vi.waitFor(() => expect(h.prepareAnnotation).toHaveBeenCalledOnce())

    // The runtime queue is still awaiting prepareAnnotation. A Source switch
    // hides native content synchronously before the provider can update ready.
    h.runtime.handleSurfaceRect({ itemId: h.item.id, x: 0, y: 0, width: 640, height: 480, visible: false })
    expect(h.state.nativeSurfaceState).toBe('ready')
    expect(h.state.annotationAvailable).toBe(true)
    pending.resolve(h.prepared)

    await vi.waitFor(() => expect(h.openItem).toHaveBeenCalledOnce())
    expect(h.store.activeItem?.payload.initialSection).toBe('preview')
    expect(h.store.activeItem?.payload.initialSectionRequestId).toBe(originalRequest + 1)
  })

  it('keeps reselection as an explicit navigation to the annotation page', async () => {
    const h = await mountReadyAnnotationPreview()
    await h.documents.load(artifactFromWorkbenchItem(h.item)!, 'session-a', { force: true })
    await h.annotations.create({
      annotationId: 'reselect-existing', sessionKey: 'session-a',
      documentId: h.prepared.document.documentId, documentName: current.name,
      revisionId: h.prepared.revision.revisionId, sha256: h.prepared.revision.artifactSha256,
      body: 'Keep this target', pagePath: 'index.html',
      selection: { selectionId: 'selected', targetRef: 'target', tagName: 'p', elementPath: 'p' },
    })

    h.runtime.handleComponentEvent(h.store.activeItem!, {
      type: 'artifact-annotation-summary', payload: { type: 'reselect', annotationId: 'reselect-existing' },
    })

    await vi.waitFor(() => expect(h.openItem).toHaveBeenCalledOnce())
    expect(h.prepareAnnotation).toHaveBeenCalledOnce()
    expect(artifactFromWorkbenchItem(h.store.activeItem)?.previewPagePath).toBe('index.html')
    expect(h.store.activeItem?.payload.initialSectionRequestId).toBe(1)
  })

  it('does not reuse a ready preview when fresh annotation authorization is denied', async () => {
    const h = await mountReadyAnnotationPreview()
    const original = h.store.activeItem
    h.prepareAnnotation.mockRejectedValueOnce(artifactProductClientError('PERMISSION_DENIED'))

    await h.continueEditing()

    await vi.waitFor(() => expect(pushToast).toHaveBeenCalledWith(en.workbench.artifactErrors.permissionDenied,
      expect.objectContaining({ tone: 'danger' })))
    expect(h.store.activeItem).toBe(original)
    expect(h.openItem).not.toHaveBeenCalled()
    expect(h.documents.load).not.toHaveBeenCalled()
    expect(h.performAction.mock.calls.some(([, action]) => action === 'begin-prepared-annotation')).toBe(false)
  })

  it.each([
    { revisionId: 'revision-next', sha256: 'b'.repeat(64) },
    { revisionId: 'revision-head', sha256: 'b'.repeat(64) },
  ])('keeps drafts on the loaded baseline and stops selection when preparation races with $revisionId/$sha256', async loadedHead => {
    previewHarness.enabled = true
    previewHarness.runtime = true
    const h = await mount()
    await h.choose()
    const originId = h.store.activeItemId
    const prepared = opened()
    if (prepared.disposition !== 'document') throw new Error('Expected document fixture')
    prepared.revision.artifactSha256 = 'a'.repeat(64)
    h.prepareAnnotation.mockResolvedValueOnce(prepared)
    installPreparedWorkspace(h, prepared, loadedHead)
    const draft = { sessionKey: 'session-a', documentId: prepared.document.documentId,
      documentName: target.name, body: 'Keep this requested change',
      selection: { selectionId: 'selected', targetRef: 'target', tagName: 'p', elementPath: 'p' } }
    await h.annotations.create({ ...draft, annotationId: 'prepared-draft',
      revisionId: prepared.revision.revisionId, sha256: prepared.revision.artifactSha256 })
    await h.annotations.create({ ...draft, annotationId: 'current-draft',
      revisionId: loadedHead.revisionId, sha256: loadedHead.sha256 })
    const performAction = vi.spyOn(WorkbenchRuntimeManager.prototype, 'performAction')

    await h.continueEditing()

    await vi.waitFor(() => expect(pushToast).toHaveBeenCalledWith(en.workbench.artifactErrors.documentChanged,
      expect.objectContaining({ tone: 'danger' })))
    expect(h.store.activeItemId).toBe(originId)
    expect(performAction.mock.calls.some(([, action]) => action === 'begin-prepared-annotation')).toBe(false)
    expect(h.annotations.annotations['prepared-draft']).toMatchObject({ freshness: 'stale', body: draft.body })
    expect(h.annotations.annotations['current-draft']).toMatchObject({ freshness: 'fresh', body: draft.body })
    expect(h.annotations.sendableDraftsForSession('session-a').map(item => item.annotationId)).toEqual(['current-draft'])
  })

  it('does not revalidate drafts or start selection using a preserved cache after a failed preview refresh', async () => {
    previewHarness.enabled = true
    previewHarness.runtime = true
    const h = await mount()
    await h.choose()
    const originId = h.store.activeItemId
    installPreparedWorkspace(h, opened(), { stale: true })
    const validateDrafts = vi.spyOn(h.annotations, 'validateDocumentVersion')
    const performAction = vi.spyOn(WorkbenchRuntimeManager.prototype, 'performAction')

    await h.continueEditing()

    await vi.waitFor(() => expect(pushToast).toHaveBeenCalledWith(en.workbench.artifactErrors.documentUnavailable,
      expect.objectContaining({ tone: 'danger' })))
    expect(h.store.activeItemId).toBe(originId)
    expect(validateDrafts).not.toHaveBeenCalled()
    expect(performAction.mock.calls.some(([, action]) => action === 'begin-prepared-annotation')).toBe(false)
  })

  it('reports a delayed annotation preparation error while the originating tab stays active', async () => {
      previewHarness.enabled = true
      previewHarness.runtime = true
      const h = await mount()
      await h.choose()
      const originId = h.store.activeItemId!
      const pending = deferred<never>()
      const operation = h.prepareAnnotation
      operation.mockReturnValueOnce(pending.promise)
      const priorCalls = operation.mock.calls.length
      await h.continueEditing()
      await vi.waitFor(() => expect(operation).toHaveBeenCalledTimes(priorCalls + 1))
      pending.reject(artifactProductClientError('PERMISSION_DENIED'))
      await flush()
      expect(pushToast).toHaveBeenCalledExactlyOnceWith(en.workbench.artifactErrors.permissionDenied,
        expect.objectContaining({ tone: 'danger' }))
      expect(h.store.activeItemId).toBe(originId)
    },
  )

  it.each([false, true])(
    'ignores a delayed annotation preparation error after a tab switch (return=%s)', async returns => {
      previewHarness.enabled = true
      previewHarness.runtime = true
      const h = await mount()
      await h.choose()
      const originId = h.store.activeItemId!
      const pending = deferred<never>()
      const operation = h.prepareAnnotation
      operation.mockReturnValueOnce(pending.promise)
      const priorCalls = operation.mock.calls.length
      await h.continueEditing()
      await vi.waitFor(() => expect(operation).toHaveBeenCalledTimes(priorCalls + 1))
      h.store.activateItem(h.item.id)
      if (returns) h.store.activateItem(originId)
      pending.reject(artifactProductClientError('PERMISSION_DENIED'))
      await flush()
      expect(pushToast).not.toHaveBeenCalled()
      expect(h.store.activeItemId).toBe(returns ? originId : h.item.id)
    },
  )
})
