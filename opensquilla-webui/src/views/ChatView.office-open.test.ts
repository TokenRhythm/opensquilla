import { describe, expect, it, vi } from 'vitest'
import { transpileModule, ScriptTarget } from '@typescript/typescript6'
import type { ArtifactPayload } from '@/types/artifacts'
import { artifactCategory, isInlineMediaArtifact, isOfficeArtifact } from '@/utils/chat/artifacts'
import { artifactWorkbenchPreviewKind, isOfficePreviewFormat } from '@/utils/workbench/artifactPreview'
import { artifactPayloadFromRevision } from '@/workbench/artifactDocumentProvider'
import { createWorkbenchResourceRef } from '@/types/workbenchResources'
import { workbenchResourceKey, artifactPayloadFromWorkbenchResource, resourceFromPreparedPreview } from '@/workbench/workbenchResourceItems'
import chatViewSource from './ChatView.vue?raw'

// Run the actual two entry functions with controlled boundary services. This
// catches routing and async-session regressions without mounting the whole chat.
const resourceStart = chatViewSource.indexOf('async function openDeliverableWorkbenchResource(')
const resourceEnd = chatViewSource.indexOf('\nasync function resolveWorkspacePreviewResource(', resourceStart)
const openStart = chatViewSource.indexOf('function openArtifact(')
const openEnd = chatViewSource.indexOf('\nfunction closeDeliverables', openStart)
const entrySource = transpileModule(`${chatViewSource.slice(resourceStart, resourceEnd)}\n${chatViewSource.slice(openStart, openEnd)}`, {
  compilerOptions: { target: ScriptTarget.ES2022 },
}).outputText

function harness() {
  const sessionKey = { value: 'session-a' }, currentEpoch = { value: 1 }
  const attachmentWorkbenchPreviewEnabled = { value: true }, workbenchEnabled = { value: true }
  const artifact = { id: 'published-ppt', name: '计划.pptx', sha256: 'a'.repeat(64) }
  const resource = { resource: createWorkbenchResourceRef('deliverable', artifact.id),
    name: artifact.name, sha256: artifact.sha256, capabilities: { preview: true, edit: true }, relations: {} }
  const revision = { revisionId: 'rev-current', documentId: 'doc-current', artifactId: 'art-current',
    artifactSha256: 'b'.repeat(64), filename: artifact.name,
    mediaType: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    byteSize: 456, downloadUrl: '/documents/doc-current?revisionId=rev-current' }
  const document = { documentId: 'doc-current', headRevisionId: revision.revisionId }
  const current = { disposition: 'document', document, revision,
    resource: { ...resource, resource: createWorkbenchResourceRef('document', document.documentId) } }
  const services = {
    resolve: vi.fn(async (): Promise<unknown> => resource),
    openCurrent: vi.fn(async (): Promise<unknown> => current),
    importDocument: vi.fn(async () => ({ document, revision })),
    preview: vi.fn(async () => ({ resource, preview: { resource: resource.resource, launchUrl: '/original', mode: 'local-renderer' } })),
  }
  const openItem = vi.fn(() => true), legacy = vi.fn(() => true), pushToast = vi.fn()
  const workspacePreviewOpening = { open: vi.fn() }
  const catalog = { get: vi.fn<(key: string, id: string) => Promise<ArtifactPayload | null>>(async () => null) }
  const dependencies = {
    sessionKey, currentEpoch, attachmentWorkbenchPreviewEnabled, workbenchEnabled,
    sessionArtifacts: { value: [artifact] }, workbenchResourcesStore: services,
    artifactWorkbench: { artifacts: catalog },
    workbenchStore: { openItem }, openLegacyArtifactWorkbench: legacy,
    artifactPreviewItemForExplicitOpen: (value: unknown) => value,
    platform: { capabilities: {}, workbench: {} }, workspacePreviewOpening,
    artifactImageLightbox: { open: vi.fn() }, focusInlineDeliverable: vi.fn(() => true),
    artifactCategory, isInlineMediaArtifact, isOfficeArtifact, artifactWorkbenchPreviewKind, isOfficePreviewFormat,
    artifactPayloadFromRevision, createWorkbenchResourceRef, workbenchResourceKey,
    artifactPayloadFromWorkbenchResource, resourceFromPreparedPreview,
    classifyArtifactProductError: () => ({ messageKey: 'failure', fallbackMessage: 'Cannot open' }),
    workbenchResourceUnavailableReasonKey: () => 'unsupported', workbenchResourceActionReasonCode: () => 'unsupported',
    t: (key: string) => key, pushToast,
  }
  const api = new Function(...Object.keys(dependencies), `${entrySource}\nreturn { openArtifact, openDeliverableWorkbenchResource };`)(...Object.values(dependencies)) as {
    openArtifact(artifact: ArtifactPayload): boolean
    openDeliverableWorkbenchResource(artifact: ArtifactPayload): Promise<void>
  }
  return { api, artifact, resource, current, revision, services, openItem, legacy, pushToast, workspacePreviewOpening, catalog,
    sessionKey, currentEpoch, attachmentWorkbenchPreviewEnabled, workbenchEnabled }
}

describe('chat Office card document opening', () => {
  it('routes an older internal HTML artifact into the canonical opener before any metadata await', () => {
    const h = harness()
    const artifact: ArtifactPayload = {
      id: h.artifact.id, kind: 'artifact_ref', name: '公司介绍.html', mime: 'text/html',
      size: 23367, session_id: 'persisted-session-id', source: 'workspace-preview',
      download_url: `/api/v1/artifacts/${h.artifact.id}`,
    }
    expect(h.api.openArtifact(artifact)).toBe(true)
    // The helper owns metadata lookup and request ordering together. The
    // persisted card has neither documentId nor the transient action session.
    expect(h.workspacePreviewOpening.open).toHaveBeenCalledOnce()
    expect(h.workspacePreviewOpening.open).toHaveBeenCalledWith({ artifactId: artifact.id }, 'session-a', undefined)
    expect(h.catalog.get).not.toHaveBeenCalled()
    expect(h.services.resolve).not.toHaveBeenCalled()
    expect(h.services.openCurrent).not.toHaveBeenCalled()
    expect(h.openItem).not.toHaveBeenCalled()
    expect(h.legacy).not.toHaveBeenCalled()
  })

  it('uses an explicit document identity without metadata lookup or a legacy internal-artifact fallback', async () => {
    const h = harness()
    const artifact = { ...h.artifact, name: '公司介绍.html', mime: 'text/html', source: 'workspace-preview',
      documentId: 'doc_current', previewPagePath: 'pages/company.html' }
    await h.api.openDeliverableWorkbenchResource(artifact)
    expect(h.catalog.get).not.toHaveBeenCalled()
    expect(h.workspacePreviewOpening.open).toHaveBeenCalledWith('doc_current', 'session-a', 'pages/company.html')
    expect(h.services.resolve).not.toHaveBeenCalled()
    expect(h.services.openCurrent).not.toHaveBeenCalled()
    expect(h.openItem).not.toHaveBeenCalled()
    expect(h.legacy).not.toHaveBeenCalled()
  })

  it.each(['session-a', 'session-b'])('preserves the original workspace action session %s and child page', expectedSessionKey => {
    const h = harness()
    expect(h.api.openArtifact({
      source: 'workspace-preview', documentId: 'doc_site', session_key: expectedSessionKey,
      name: '公司介绍.html', mime: 'text/html', previewPagePath: 'pages/company.html',
    })).toBe(true)
    // A stale action must reach the existing helper with its original session,
    // so that helper can reject it instead of opening in the current session.
    expect(h.workspacePreviewOpening.open).toHaveBeenCalledWith('doc_site', expectedSessionKey, 'pages/company.html')
    expect(h.services.resolve).not.toHaveBeenCalled()
    expect(h.openItem).not.toHaveBeenCalled()
    expect(h.legacy).not.toHaveBeenCalled()
  })

  it.each(['docx', 'pptx', 'xlsx', 'html'])('opens %s through the canonical document and retains revision/hash', async extension => {
    const h = harness(), artifact = { ...h.artifact, name: `计划.${extension}` }
    expect(h.api.openArtifact(artifact)).toBe(true)
    await vi.waitFor(() => expect(h.openItem).toHaveBeenCalledTimes(1))
    expect(h.services.resolve).toHaveBeenCalledWith('session-a', createWorkbenchResourceRef('deliverable', artifact.id))
    expect(h.services.openCurrent).toHaveBeenCalledWith('session-a', h.resource)
    expect(h.openItem).toHaveBeenCalledWith(expect.objectContaining({
      sessionKey: 'session-a', resourceIdentity: 'document:doc-current', initialSection: 'preview',
      artifact: expect.objectContaining({ documentId: 'doc-current', revisionId: 'rev-current', sha256: 'b'.repeat(64),
        revision_download_url: h.revision.downloadUrl }),
    }))
    expect(h.legacy).not.toHaveBeenCalled()
    expect(h.services.importDocument).not.toHaveBeenCalled()
  })

  it.each(['pdf', 'doc', 'ppt', 'xls'])('leaves %s on its existing preview route', extension => {
    const h = harness()
    h.api.openArtifact({ ...h.artifact, name: `文件.${extension}` })
    expect(h.services.resolve).not.toHaveBeenCalled()
    expect(h.services.openCurrent).not.toHaveBeenCalled()
    expect(h.legacy).toHaveBeenCalledTimes(1)
  })

  it('preserves the resource and workbench feature gates', () => {
    const h = harness(); h.attachmentWorkbenchPreviewEnabled.value = false
    h.api.openArtifact(h.artifact)
    expect(h.legacy).toHaveBeenCalledTimes(1)
    expect(h.services.resolve).not.toHaveBeenCalled()
    h.attachmentWorkbenchPreviewEnabled.value = true; h.workbenchEnabled.value = false
    expect(h.api.openArtifact(h.artifact)).toBe(false)
    expect(h.services.resolve).not.toHaveBeenCalled()
  })

  it('preserves explicit document identity and the old API import fallback', async () => {
    const h = harness()
    await h.api.openDeliverableWorkbenchResource({ ...h.artifact, documentId: 'doc-current' })
    expect(h.services.resolve).toHaveBeenCalledWith('session-a', createWorkbenchResourceRef('document', 'doc-current'))
    h.services.openCurrent.mockResolvedValue(null); h.openItem.mockClear()
    await h.api.openDeliverableWorkbenchResource(h.artifact)
    expect(h.services.importDocument).toHaveBeenCalledWith('session-a', h.resource)
    expect(h.openItem).toHaveBeenCalledWith(expect.objectContaining({ artifact: expect.objectContaining({ documentId: 'doc-current' }) }))
  })

  it('keeps an authoritative readonly response as preview and never imports it', async () => {
    const h = harness()
    h.services.openCurrent.mockResolvedValue({ disposition: 'readonly', resource: h.resource })
    await h.api.openDeliverableWorkbenchResource(h.artifact)
    expect(h.services.importDocument).not.toHaveBeenCalled()
    expect(h.services.preview).toHaveBeenCalledWith('session-a', h.resource.resource)
    expect(h.openItem).toHaveBeenCalledWith(expect.objectContaining({ preparedPreview: expect.any(Object), previewLeaseEligible: false }))
  })

  it.each(['session', 'epoch'] as const)('discards a completed open after the %s changes', async change => {
    const h = harness()
    let resolve!: (value: unknown) => void
    h.services.openCurrent.mockImplementationOnce(() => new Promise(done => { resolve = done }))
    const pending = h.api.openDeliverableWorkbenchResource(h.artifact)
    await vi.waitFor(() => expect(h.services.openCurrent).toHaveBeenCalled())
    if (change === 'session') h.sessionKey.value = 'session-b'
    else h.currentEpoch.value += 1
    resolve(h.current); await pending
    expect(h.openItem).not.toHaveBeenCalled()
    expect(h.legacy).not.toHaveBeenCalled()
    expect(h.pushToast).not.toHaveBeenCalled()
    expect(h.services.importDocument).not.toHaveBeenCalled()
  })

  it('does not continue resolution or surface a stale error in a different session', async () => {
    const h = harness()
    let reject!: (reason: unknown) => void
    h.services.resolve.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
    const pending = h.api.openDeliverableWorkbenchResource(h.artifact)
    h.sessionKey.value = 'session-b'; reject(new Error('Late failure')); await pending
    expect(h.services.openCurrent).not.toHaveBeenCalled()
    expect(h.pushToast).not.toHaveBeenCalled()
    expect(h.legacy).not.toHaveBeenCalled()
  })
})
