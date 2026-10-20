import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, reactive, ref, watch, type EffectScope } from 'vue'
import { createPinia } from 'pinia'
import { transpileModule, ScriptTarget } from '@typescript/typescript6'
import { artifactPayloadFromRevision, createLegacyArtifactWorkspace } from '@/workbench/artifactDocumentProvider'
import { useArtifactDocumentsStore } from '@/stores/artifactDocuments'
import { prepareCurrentDocumentPreview } from '@/workbench/currentDocumentOpening'
import type { ArtifactDocumentWorkspace } from '@/types/artifactDocuments'
import type { ArtifactDocumentProvider } from '@/modules/artifactWorkbench'
import { artifactPayloadFromWorkbenchResource, resourceFromPreparedPreview, workbenchResourceKey } from '@/workbench/workbenchResourceItems'
import chatViewSource from './ChatView.vue?raw'

// Execute the production entry points with delayed transport boundaries. This
// verifies session ownership at every await without a whole-app fixture.
const start = chatViewSource.indexOf('let workbenchPreviewRequest = 0')
const end = chatViewSource.indexOf('\nasync function editAttachmentResource(', start)
const explicitStart = chatViewSource.indexOf('function openWorkbenchItemExplicit(')
const explicitEnd = chatViewSource.indexOf('\nfunction artifactPreviewItemForExplicitOpen(', explicitStart)
const entrySource = transpileModule(`${chatViewSource.slice(explicitStart, explicitEnd)}\n${chatViewSource.slice(start, end)}`, {
  compilerOptions: { target: ScriptTarget.ES2022 },
}).outputText

const scopes: EffectScope[] = []
afterEach(() => scopes.splice(0).forEach(scope => scope.stop()))

function harness() {
  const scope = effectScope()
  scopes.push(scope)
  const sessionKey = ref('main-a'), currentEpoch = ref(1), deliveryIdentity = ref('gateway-a')
  const gatewayAccess = reactive({ subscriptionEpoch: 1 })
  const attachment = { attachmentId: 'input-1', name: 'input.html', mime: 'text/html' }
  const resource = { resource: { type: 'attachment', attachmentId: 'input-1', id: 'input-1' }, name: 'input.html', mime: 'text/html',
    sha256: 'a'.repeat(64), capabilities: { preview: true, edit: true }, relations: {} }
  const revision = { ...createLegacyArtifactWorkspace({ id: 'head', name: 'input.html', mime: 'text/html' }, 'main-a').revisions[0]!,
    documentId: 'doc_input', revisionId: 'rev_input', artifactId: 'head', filename: 'input.html',
    mediaType: 'text/html', artifactSha256: 'b'.repeat(64) }
  const document = { documentId: 'doc_input', headRevisionId: revision.revisionId }
  const current = { disposition: 'document', document, revision,
    resource: { ...resource, resource: { type: 'document', documentId: 'doc_input', id: 'doc_input' },
      relations: { documentId: 'doc_input', headRevisionId: revision.revisionId, headArtifactId: revision.artifactId } } }
  const artifactDocumentsStore = useArtifactDocumentsStore(createPinia())
  const workspace = () => {
    const artifact = artifactPayloadFromRevision(current.revision)
    const legacy = createLegacyArtifactWorkspace(artifact, 'main-a')
    return { ...legacy, source: 'document-api', document: { ...legacy.document, ...current.document },
      headArtifact: artifact } as ArtifactDocumentWorkspace
  }
  const loadWorkspace = vi.fn(async (): Promise<ArtifactDocumentWorkspace> => workspace())
  artifactDocumentsStore.setProvider({ loadWorkspace } as unknown as ArtifactDocumentProvider)
  const prepared = { resource, preview: { resource: resource.resource, mode: 'isolated', launchUrl: '/preview' } }
  const services = {
    find: vi.fn(() => resource as typeof resource | null), load: vi.fn(async (): Promise<unknown> => ({})),
    resolve: vi.fn(async (): Promise<unknown> => resource), openCurrent: vi.fn(async (): Promise<unknown> => current),
    importDocument: vi.fn(async (): Promise<unknown> => ({ document, revision })), preview: vi.fn(async (): Promise<unknown> => prepared),
  }
  const openItem = vi.fn(() => true), requestReveal = vi.fn(), pushToast = vi.fn()
  const dependencies = {
    sessionKey, currentEpoch, deliveryIdentity, gatewayAccess, watch, chatViewDisposed: false, chatViewActive: true,
    workbenchResourcesEnabled: { value: true }, workbenchEnabled: { value: true }, workbenchResourcesStore: services,
    workbenchStore: { openItem, requestReveal }, artifactPreviewItemForExplicitOpen: (item: unknown) => item,
    platform: { capabilities: {}, workbench: {} }, artifactPayloadFromRevision, artifactPayloadFromWorkbenchResource,
    prepareCurrentDocumentPreview, artifactDocumentsStore,
    resourceFromPreparedPreview, workbenchResourceKey, t: (key: string) => key, pushToast,
    classifyArtifactProductError: () => ({ messageKey: 'error', fallbackMessage: 'Unavailable' }),
    workbenchResourceUnavailableReasonKey: () => 'unavailable', workbenchResourceActionReasonCode: () => 'unavailable',
  }
  const api = scope.run(() => new Function(...Object.keys(dependencies), `${entrySource}\nreturn { previewAttachmentResource };`)(...Object.values(dependencies))) as {
    previewAttachmentResource(value: typeof attachment): Promise<void>
  }
  return { api, attachment, resource, current, prepared, document, revision, services, openItem, requestReveal, pushToast,
    sessionKey, currentEpoch, deliveryIdentity, gatewayAccess, artifactDocumentsStore, loadWorkspace, workspace }
}

describe('main attachment preview session ownership', () => {
  it('does not reveal the workbench if the panel could not be opened', async () => {
    const h = harness()
    h.openItem.mockReturnValue(false)
    await h.api.previewAttachmentResource(h.attachment)
    expect(h.requestReveal).not.toHaveBeenCalled()
  })

  it('opens the current document with the captured main session and preview lease', async () => {
    const h = harness()
    await h.api.previewAttachmentResource(h.attachment)
    expect(h.services.resolve).toHaveBeenCalledWith('main-a', h.resource.resource)
    expect(h.services.openCurrent).toHaveBeenCalledWith('main-a', h.resource)
    expect(h.openItem).toHaveBeenCalledWith(expect.objectContaining({ sessionKey: 'main-a', previewLeaseEligible: true,
      resourceIdentity: 'document:doc_input', artifact: expect.objectContaining({ documentId: 'doc_input', revisionId: 'rev_input' }) }))
    expect(h.requestReveal).toHaveBeenCalledOnce()
  })

  it('refreshes a cached older attachment Document before showing its latest revision', async () => {
    const h = harness()
    await h.artifactDocumentsStore.load(artifactPayloadFromRevision(h.current.revision), 'main-a')
    h.current.revision = { ...h.current.revision, revisionId: 'revision-two', artifactId: 'head-two' }
    h.current.document = { ...h.current.document, headRevisionId: 'revision-two' }
    h.current.resource.relations = { ...h.current.resource.relations, headRevisionId: 'revision-two', headArtifactId: 'head-two' }
    await h.api.previewAttachmentResource(h.attachment)
    expect(h.loadWorkspace).toHaveBeenCalledTimes(2)
    expect(h.openItem).toHaveBeenCalledWith(expect.objectContaining({
      resourceIdentity: 'document:doc_input',
      artifact: expect.objectContaining({ revisionId: 'revision-two', id: 'head-two' }),
    }))
  })

  it('does not reopen the previous conversation after a delayed document cache refresh', async () => {
    const h = harness()
    let finish!: (workspace: ArtifactDocumentWorkspace) => void
    h.loadWorkspace.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const opening = h.api.previewAttachmentResource(h.attachment)
    await vi.waitFor(() => expect(h.loadWorkspace).toHaveBeenCalledOnce())
    h.sessionKey.value = 'main-b'
    finish(h.workspace())
    await opening
    expect(h.openItem).not.toHaveBeenCalled()
    expect(h.pushToast).not.toHaveBeenCalled()
  })

  it.each(['load', 'resolve', 'openCurrent', 'importDocument', 'preview'] as const)(
    'does not carry a late %s result into another main session', async boundary => {
      const h = harness()
      if (boundary === 'load') h.services.find.mockReturnValue(null)
      if (boundary === 'importDocument') h.services.openCurrent.mockResolvedValue(null)
      if (boundary === 'preview') h.services.openCurrent.mockResolvedValue({ disposition: 'readonly', resource: h.resource })
      let finish!: (value: unknown) => void
      h.services[boundary].mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
      const pending = h.api.previewAttachmentResource(h.attachment)
      await vi.waitFor(() => expect(h.services[boundary]).toHaveBeenCalled())
      h.sessionKey.value = 'main-b'
      finish(boundary === 'openCurrent' ? h.current : boundary === 'importDocument'
        ? { document: h.document, revision: h.revision } : boundary === 'preview' ? h.prepared : h.resource)
      await pending
      expect(h.openItem).not.toHaveBeenCalled()
      expect(h.requestReveal).not.toHaveBeenCalled()
      expect(h.pushToast).not.toHaveBeenCalled()
    },
  )

  it.each(['epoch', 'identity', 'connection'] as const)('drops a preview on %s replacement', async replacement => {
    const h = harness()
    let finish!: (value: unknown) => void
    h.services.openCurrent.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const pending = h.api.previewAttachmentResource(h.attachment)
    await vi.waitFor(() => expect(h.services.openCurrent).toHaveBeenCalled())
    if (replacement === 'epoch') h.currentEpoch.value++
    if (replacement === 'identity') h.deliveryIdentity.value = 'gateway-b'
    if (replacement === 'connection') h.gatewayAccess.subscriptionEpoch++
    finish(h.current); await pending
    expect(h.openItem).not.toHaveBeenCalled()
  })

  it('keeps the newest click active when an older resource resolve finishes later', async () => {
    const h = harness()
    let finish!: (value: unknown) => void
    h.services.resolve.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const older = h.api.previewAttachmentResource(h.attachment)
    await vi.waitFor(() => expect(h.services.resolve).toHaveBeenCalledTimes(1))
    await h.api.previewAttachmentResource(h.attachment)
    finish(h.resource); await older
    expect(h.openItem).toHaveBeenCalledTimes(1)
    expect(h.services.openCurrent).toHaveBeenCalledTimes(1)
  })

  it('does not surface an old session transport error in the replacement conversation', async () => {
    const h = harness()
    let reject!: (error: Error) => void
    h.services.resolve.mockImplementationOnce(() => new Promise((_done, fail) => { reject = fail }))
    const pending = h.api.previewAttachmentResource(h.attachment)
    h.sessionKey.value = 'main-b'; reject(new Error('Late failure')); await pending
    expect(h.pushToast).not.toHaveBeenCalled()
    expect(h.openItem).not.toHaveBeenCalled()
  })
})
