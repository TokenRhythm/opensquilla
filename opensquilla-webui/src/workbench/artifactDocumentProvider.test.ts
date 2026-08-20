import { describe, expect, it, vi } from 'vitest'

import type { ArtifactPayload } from '@/types/rpc'
import {
  ARTIFACT_DOCUMENT_RPC_METHODS,
  createLegacyArtifactWorkspace,
  createRpcArtifactDocumentProvider,
  isOfficeArtifact,
  normalizeArtifactChangeSet,
  normalizeArtifactDocument,
  normalizeArtifactEditCapabilities,
  normalizeArtifactEditSession,
  normalizeArtifactRevision,
} from './artifactDocumentProvider'

type GenericRpcCall = <T = unknown>(
  method: string,
  params?: Record<string, unknown>,
  options?: unknown,
) => Promise<T>

const officeArtifact: ArtifactPayload = {
  id: 'artifact-office',
  name: 'quarterly-plan.pptx',
  mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  download_url: '/api/v1/artifacts/artifact-office',
}

describe('artifact document provider', () => {
  it('does not promote a preview-only format into selection or editing', () => {
    const capabilities = normalizeArtifactEditCapabilities({
      formats: {
        docx: { preview: true, publish: false },
        xlsx: { preview: false, publish: false },
        pptx: { preview: false, publish: false },
        html: { preview: true, manualEdit: true, agentEdit: true },
      },
    })

    expect(capabilities.office).toMatchObject({
      preview: true,
      selectionContext: false,
      manualEdit: false,
      agentEdit: false,
      edit: false,
      publish: false,
    })
  })

  it('preserves explicit false edit axes over legacy edit and enabled summaries', () => {
    const capabilities = normalizeArtifactEditCapabilities({
      formats: {
        html: {
          enabled: true,
          preview: true,
          edit: true,
          manualEdit: false,
          agentEdit: false,
          publish: false,
        },
      },
    })
    const document = normalizeArtifactDocument({
      id: 'doc-explicit-false',
      sessionKey: 'session-a',
      name: 'page.html',
      format: 'html',
      headRevisionId: 'rev-explicit-false',
      capabilities: {
        preview: true,
        edit: true,
        manualEdit: false,
        agentEdit: false,
        publish: false,
      },
    }, capabilities)

    expect(capabilities.html).toMatchObject({
      manualEdit: false,
      agentEdit: false,
      edit: false,
      publish: false,
    })
    expect(document?.capabilities).toMatchObject({
      manualEdit: false,
      agentEdit: false,
      edit: false,
      publish: false,
    })
  })

  it('normalizes the stable document, revision, and change-set contracts', () => {
    const capabilities = normalizeArtifactEditCapabilities({
      available: true,
      revisions: true,
      changeSets: true,
      comments: true,
      office: { enabled: false, reason: 'office-disabled' },
      html: { enabled: true, source: true },
    })
    const document = normalizeArtifactDocument({
      documentId: 'doc-1',
      sessionKey: 'session-a',
      name: 'page.html',
      kind: 'html',
      headRevisionId: 'rev-2',
      generation: 2,
      stateRevision: 4,
      createdAt: 1,
      updatedAt: 2,
      schemaVersion: 1,
    }, capabilities)
    const revision = normalizeArtifactRevision({
      revisionId: 'rev-2',
      documentId: 'doc-1',
      parentRevisionId: 'rev-1',
      generation: 2,
      artifact: {
        artifactId: 'artifact-head',
        sha256: 'a'.repeat(64),
        filename: 'page.html',
        mediaType: 'text/html',
        byteSize: 42,
        downloadUrl: '/api/v1/artifacts/artifact-head',
      },
      source: 'agent',
      actorKind: 'agent',
      actorId: 'main',
      createdAt: 2,
    })
    const changeSet = normalizeArtifactChangeSet({
      id: 'change-1',
      documentId: 'doc-1',
      baseRevisionId: 'rev-1',
      turnId: 'turn-1',
      summary: 'Replace the title',
      state: 'ready',
      operations: [{ op: 'replace', path: '/title' }],
      candidateArtifact: {
        id: 'artifact-candidate',
        sha256: 'c'.repeat(64),
        name: 'page.html',
        mime: 'text/html',
        size: 84,
      },
      stateRevision: 1,
      createdByKind: 'agent',
      createdById: 'main',
    })
    expect(document).toMatchObject({
      documentId: 'doc-1',
      capabilities: {
        preview: true,
        edit: true,
        source: true,
      },
    })
    expect(revision).toMatchObject({
      artifactId: 'artifact-head',
      downloadUrl: '/api/v1/artifacts/artifact-head',
    })
    expect(changeSet).toMatchObject({
      turnId: 'turn-1',
      summary: 'Replace the title',
      operations: [{ op: 'replace', path: '/title' }],
      candidateArtifact: { id: 'artifact-candidate', sha256: 'c'.repeat(64) },
    })
  })

  it('lets a preview-only bundle document override global single-file HTML editing', () => {
    const capabilities = normalizeArtifactEditCapabilities({
      formats: {
        html: {
          preview: true,
          manualEdit: true,
          agentEdit: true,
          sourceEdit: true,
          comments: true,
        },
      },
    })
    const document = normalizeArtifactDocument({
      id: 'doc-bundle',
      sessionKey: 'session-a',
      name: 'site.html',
      format: 'html',
      headRevisionId: 'rev-bundle',
      generation: 1,
      stateRevision: 1,
      capabilities: {
        download: true,
        preview: true,
        versionHistory: true,
        comments: true,
        manualEdit: false,
        agentEdit: false,
        sourceEdit: false,
        promptAnnotations: false,
        selection: false,
        unavailableReason: 'html_bundle_edit_not_supported',
      },
    }, capabilities)

    expect(capabilities.html).toMatchObject({ preview: true, edit: true, source: true })
    expect(document?.capabilities).toEqual({
      download: true,
      preview: true,
      selectionContext: false,
      manualEdit: false,
      agentEdit: false,
      publish: false,
      edit: false,
      revisions: true,
      changeSets: false,
      comments: true,
      source: false,
      promptAnnotations: false,
      reason: 'html_bundle_edit_not_supported',
    })
  })

  it('loads the current head plus version and change side data', async () => {
    const supported = new Set<string>(
      Object.values(ARTIFACT_DOCUMENT_RPC_METHODS).filter(
        method => method !== ARTIFACT_DOCUMENT_RPC_METHODS.documentsOpen,
      ),
    )
    const call = vi.fn(async (method: string) => {
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.capabilities) {
        return {
          formats: {
            docx: { preview: false, manualEdit: false, comments: true },
            xlsx: { preview: false, manualEdit: false, comments: true },
            pptx: { preview: false, manualEdit: false, comments: true },
            html: {
              preview: true,
              manualEdit: true,
              sourceEdit: true,
              comments: true,
            },
          },
        }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.documentsGet) {
        return {
          document: {
            id: 'doc-1',
            name: 'page.html',
            format: 'html',
            headRevisionId: 'rev-2',
            generation: 2,
            stateRevision: 2,
            capabilities: {
              download: true,
              preview: true,
              versionHistory: true,
              comments: true,
              manualEdit: true,
              agentEdit: true,
              sourceEdit: true,
            },
          },
        }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.revisionsList) {
        return {
          revisions: [
            {
              id: 'rev-2',
              documentId: 'doc-1',
              parentRevisionId: 'rev-1',
              generation: 2,
              sha256: 'b'.repeat(64),
              name: 'page.html',
              mime: 'text/html',
              size: 84,
              downloadUrl: '/api/v1/artifacts/artifact-head',
              source: 'agent',
              actorKind: 'agent',
              actorId: 'main',
            },
          ],
        }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.changesList) {
        return { changeSets: [] }
      }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const provider = createRpcArtifactDocumentProvider({
      call: call as unknown as GenericRpcCall,
      supportsMethod: method => supported.has(method),
      markMethodUnavailable: vi.fn(),
    })

    const workspace = await provider.loadWorkspace({
      id: 'artifact-original',
      documentId: 'doc-1',
      name: 'page.html',
      mime: 'text/html',
    }, 'session-a')

    expect(workspace.source).toBe('document-api')
    expect(workspace.document.documentId).toBe('doc-1')
    expect(workspace.headArtifact).toMatchObject({
      id: 'rev-2',
      download_url: '/api/v1/artifact-documents/doc-1',
    })
    expect(call).toHaveBeenCalledWith(
      ARTIFACT_DOCUMENT_RPC_METHODS.documentsGet,
      { documentId: 'doc-1', sessionKey: 'session-a' },
      expect.objectContaining({ timeoutMs: 10_000 }),
    )
    expect(call).toHaveBeenCalledWith(
      ARTIFACT_DOCUMENT_RPC_METHODS.revisionsList,
      { documentId: 'doc-1', sessionKey: 'session-a' },
      expect.any(Object),
    )
  })

  it('keeps immutable artifact previews read-only without adopting or matching by name', async () => {
    const supported = new Set<string>([
      ARTIFACT_DOCUMENT_RPC_METHODS.capabilities,
      ARTIFACT_DOCUMENT_RPC_METHODS.documentsList,
      ARTIFACT_DOCUMENT_RPC_METHODS.documentsOpen,
      ARTIFACT_DOCUMENT_RPC_METHODS.revisionsList,
      ARTIFACT_DOCUMENT_RPC_METHODS.changesList,
    ])
    const call = vi.fn(async (method: string) => {
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.capabilities) {
        return { formats: { html: { preview: true, sourceEdit: true } } }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.documentsList) {
        return {
          documents: [{
            id: 'doc-old',
            name: 'page.html',
            format: 'html',
            headRevisionId: 'rev-old',
          }],
        }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.documentsOpen) {
        return {
          document: {
            id: 'doc-new',
            name: 'page.html',
            format: 'html',
            headRevisionId: 'rev-new',
          },
        }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.revisionsList) {
        return {
          revisions: [{
            id: 'rev-new',
            documentId: 'doc-new',
            name: 'page.html',
            mime: 'text/html',
            downloadUrl: '/api/v1/artifact-documents/doc-new?revisionId=rev-new',
          }],
        }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.changesList) return { changeSets: [] }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const provider = createRpcArtifactDocumentProvider({
      call: call as unknown as GenericRpcCall,
      supportsMethod: method => supported.has(method),
      markMethodUnavailable: vi.fn(),
    })

    const artifact = {
      id: 'artifact-new',
      name: 'page.html',
      mime: 'text/html',
    }
    const workspace = await provider.loadWorkspace(artifact, 'session-a')

    expect(workspace.source).toBe('legacy-artifact')
    expect(workspace.headArtifact).toMatchObject(artifact)
    expect(call).not.toHaveBeenCalledWith(
      ARTIFACT_DOCUMENT_RPC_METHODS.documentsOpen,
      expect.anything(),
      expect.anything(),
    )
    expect(call).not.toHaveBeenCalledWith(
      ARTIFACT_DOCUMENT_RPC_METHODS.documentsList,
      expect.anything(),
      expect.anything(),
    )
  })

  it('preserves source CAS metadata returned by read and patch RPCs', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.sourceRead) {
        return {
          source: {
            documentId: 'doc-html',
            revisionId: 'rev-1',
            language: 'html',
            text: '<h1>Before</h1>',
            sha256: 'a'.repeat(64),
            offsetEncoding: 'unicode-code-point',
            stateRevision: 3,
          },
        }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.sourcePatch) {
        return {
          source: {
            documentId: 'doc-html',
            revisionId: 'rev-2',
            sha256: 'b'.repeat(64),
            offsetEncoding: 'unicode-code-point',
            patchCount: 1,
            stateRevision: 4,
          },
        }
      }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const provider = createRpcArtifactDocumentProvider({
      call: call as unknown as GenericRpcCall,
      supportsMethod: () => true,
      markMethodUnavailable: vi.fn(),
    })

    const source = await provider.readSource({
      sessionKey: 'session-a',
      documentId: 'doc-html',
    })
    const patched = await provider.patchSource({
      sessionKey: 'session-a',
      documentId: 'doc-html',
      expectedHeadRevisionId: 'rev-1',
      expectedStateRevision: 3,
      expectedSourceSha256: 'a'.repeat(64),
      offsetEncoding: 'unicode-code-point',
      patches: [{ startOffset: 4, endOffset: 10, replacement: 'After' }],
    })

    expect(source).toMatchObject({
      revisionId: 'rev-1',
      content: '<h1>Before</h1>',
      sha256: 'a'.repeat(64),
      patchCount: null,
      offsetEncoding: 'unicode-code-point',
    })
    expect(patched).toMatchObject({
      revisionId: 'rev-2',
      sha256: 'b'.repeat(64),
      patchCount: 1,
      offsetEncoding: 'unicode-code-point',
    })
  })

  it('normalizes the product-only mutation resolution wire', async () => {
    const call = vi.fn(async (method: string) => {
      expect(method).toBe(ARTIFACT_DOCUMENT_RPC_METHODS.mutationResolve)
      return {
        status: 'applied',
        retryAfterMs: null,
        result: {
          documentId: 'doc-html',
          revisionId: 'rev-2',
          sha256: 'b'.repeat(64),
          stateRevision: 4,
        },
        receipt: { attemptId: 'must-be-ignored' },
      }
    })
    const provider = createRpcArtifactDocumentProvider({
      call: call as unknown as GenericRpcCall,
      supportsMethod: () => true,
      markMethodUnavailable: vi.fn(),
    })

    await expect(provider.resolveMutation?.({
      sessionKey: 'session-a',
      operation: 'source.patch',
      requestId: 'request-a',
      documentId: 'doc-html',
    })).resolves.toEqual({
      status: 'applied',
      retryAfterMs: null,
      result: {
        documentId: 'doc-html',
        revisionId: 'rev-2',
        sha256: 'b'.repeat(64),
        stateRevision: 4,
      },
    })
  })

  it('normalizes and advances explicit edit sessions while preserving legacy fallback', async () => {
    let sessionStateRevision = 1
    let lastSavedRevisionId = 'rev-1'
    const editSessionPayload = (status = 'active') => ({
      id: 'edit-session-1',
      documentId: 'doc-html',
      baseRevisionId: 'rev-1',
      lastSavedRevisionId,
      mode: 'edit',
      status,
      stateRevision: sessionStateRevision,
      expiresAt: Date.now() + 60_000,
    })
    const call = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.editSessionStart) {
        return { editSession: editSessionPayload() }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.editSessionHeartbeat) {
        expect(params).toMatchObject({
          editSessionId: 'edit-session-1',
          expectedStateRevision: 1,
        })
        sessionStateRevision = 2
        return { editSession: editSessionPayload() }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.sourcePatch) {
        expect(params).toMatchObject({
          editSessionId: 'edit-session-1',
          expectedEditSessionStateRevision: 2,
          expectedLastSavedRevisionId: 'rev-1',
        })
        sessionStateRevision = 3
        lastSavedRevisionId = 'rev-2'
        return {
          source: {
            documentId: 'doc-html',
            revisionId: 'rev-2',
            sha256: 'b'.repeat(64),
            stateRevision: 2,
          },
          editSession: editSessionPayload(),
        }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.editSessionClose) {
        expect(params).toMatchObject({
          editSessionId: 'edit-session-1',
          expectedStateRevision: 3,
        })
        sessionStateRevision = 4
        return { editSession: editSessionPayload('closed') }
      }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const provider = createRpcArtifactDocumentProvider({
      call: call as unknown as GenericRpcCall,
      supportsMethod: () => true,
      markMethodUnavailable: vi.fn(),
    })

    const started = await provider.startEditSession?.({
      sessionKey: 'session-a',
      documentId: 'doc-html',
      mode: 'edit',
      clientRequestId: 'request-1',
    })
    expect(started).toMatchObject({
      editSessionId: 'edit-session-1',
      stateRevision: 1,
      lastSavedRevisionId: 'rev-1',
    })
    const heartbeat = await provider.heartbeatEditSession?.({
      sessionKey: 'session-a',
      editSessionId: started!.editSessionId,
      expectedStateRevision: started!.stateRevision,
    })
    const saved = await provider.patchSource({
      sessionKey: 'session-a',
      documentId: 'doc-html',
      editSessionId: heartbeat!.editSessionId,
      expectedEditSessionStateRevision: heartbeat!.stateRevision,
      expectedLastSavedRevisionId: heartbeat!.lastSavedRevisionId,
    })
    const closed = await provider.closeEditSession?.({
      sessionKey: 'session-a',
      editSessionId: saved!.editSession!.editSessionId,
      expectedStateRevision: saved!.editSession!.stateRevision,
    })

    expect(saved).toMatchObject({
      revisionId: 'rev-2',
      editSession: {
        editSessionId: 'edit-session-1',
        lastSavedRevisionId: 'rev-2',
        stateRevision: 3,
      },
    })
    expect(closed).toMatchObject({ status: 'closed', stateRevision: 4 })
    expect(normalizeArtifactEditSession({
      editSessionId: 'camel-id',
      documentId: 'doc-html',
      mode: 'edit',
    })?.editSessionId).toBe('camel-id')

    const markMethodUnavailable = vi.fn()
    const legacy = createRpcArtifactDocumentProvider({
      call: vi.fn().mockRejectedValue(Object.assign(new Error('Method not found'), {
        code: 'METHOD_NOT_FOUND',
      })),
      supportsMethod: () => true,
      markMethodUnavailable,
    })
    await expect(legacy.startEditSession?.({
      sessionKey: 'session-a',
      documentId: 'doc-html',
      mode: 'edit',
    })).resolves.toBeNull()
    expect(markMethodUnavailable).toHaveBeenCalledWith(
      ARTIFACT_DOCUMENT_RPC_METHODS.editSessionStart,
    )
  })

  it('uses artifacts.get as a compatibility fallback and keeps Office download-only', async () => {
    const call = vi.fn(async (method: string) => {
      if (method !== ARTIFACT_DOCUMENT_RPC_METHODS.legacyGet) {
        throw new Error(`Unexpected RPC: ${method}`)
      }
      return {
        artifact: {
          ...officeArtifact,
          download_url: '/api/v1/artifacts/artifact-office?latest=1',
          size: 1234,
        },
      }
    })
    const provider = createRpcArtifactDocumentProvider({
      call: call as unknown as GenericRpcCall,
      supportsMethod: method => method === ARTIFACT_DOCUMENT_RPC_METHODS.legacyGet,
      markMethodUnavailable: vi.fn(),
    })

    const workspace = await provider.loadWorkspace(officeArtifact, 'session-a')

    expect(isOfficeArtifact(officeArtifact)).toBe(true)
    expect(workspace.source).toBe('legacy-artifact')
    expect(workspace.document.kind).toBe('presentation')
    expect(workspace.document.capabilities).toMatchObject({
      download: true,
      preview: false,
      edit: false,
      comments: false,
    })
    expect(workspace.headArtifact.download_url).toContain('latest=1')
    expect(call).toHaveBeenCalledWith(
      ARTIFACT_DOCUMENT_RPC_METHODS.legacyGet,
      { artifactId: 'artifact-office', sessionKey: 'session-a' },
      expect.any(Object),
    )
  })

  it('does not adopt an immutable Office artifact while previewing it', async () => {
    const supported = new Set<string>([
      ARTIFACT_DOCUMENT_RPC_METHODS.capabilities,
      ARTIFACT_DOCUMENT_RPC_METHODS.documentsList,
      ARTIFACT_DOCUMENT_RPC_METHODS.documentsOpen,
      ARTIFACT_DOCUMENT_RPC_METHODS.revisionsList,
      ARTIFACT_DOCUMENT_RPC_METHODS.changesList,
    ])
    const call = vi.fn(async (method: string) => {
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.capabilities) {
        return {
          formats: {
            docx: { preview: false, comments: true },
            xlsx: { preview: false, comments: true },
            pptx: { preview: false, comments: true },
            html: { preview: true, manualEdit: true, comments: true },
          },
        }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.documentsList) {
        return { documents: [] }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.documentsOpen) {
        return {
          document: {
            id: 'doc-office',
            name: 'quarterly-plan.pptx',
            format: 'pptx',
            headRevisionId: 'rev-office',
            generation: 1,
            stateRevision: 1,
            capabilities: {
              download: true,
              preview: false,
              versionHistory: true,
              comments: true,
              manualEdit: false,
              agentEdit: false,
              sourceEdit: false,
              unavailableReason: 'office_sidecar_not_configured',
            },
          },
        }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.revisionsList) {
        return {
          revisions: [{
            id: 'rev-office',
            documentId: 'doc-office',
            generation: 1,
            sha256: 'c'.repeat(64),
            name: 'quarterly-plan.pptx',
            mime: officeArtifact.mime,
            size: 2048,
            source: 'initial',
            downloadUrl: '/api/v1/artifact-documents/doc-office?revisionId=rev-office',
          }],
        }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.changesList) return { changeSets: [] }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const provider = createRpcArtifactDocumentProvider({
      call: call as unknown as GenericRpcCall,
      supportsMethod: method => supported.has(method),
      markMethodUnavailable: vi.fn(),
    })

    const workspace = await provider.loadWorkspace(officeArtifact, 'session-a')

    expect(call).not.toHaveBeenCalledWith(
      ARTIFACT_DOCUMENT_RPC_METHODS.documentsOpen,
      expect.anything(),
      expect.anything(),
    )
    expect(workspace.source).toBe('legacy-artifact')
    expect(workspace.document.capabilities).toMatchObject({
      download: true,
      preview: false,
      edit: false,
      revisions: false,
      comments: false,
      source: false,
    })
    expect(workspace.headArtifact).toMatchObject(officeArtifact)
  })

  it('propagates transient document RPC failures instead of returning a legacy head', async () => {
    const supported = new Set<string>([
      ARTIFACT_DOCUMENT_RPC_METHODS.capabilities,
      ARTIFACT_DOCUMENT_RPC_METHODS.documentsGet,
      ARTIFACT_DOCUMENT_RPC_METHODS.revisionsList,
      ARTIFACT_DOCUMENT_RPC_METHODS.changesList,
      ARTIFACT_DOCUMENT_RPC_METHODS.legacyGet,
    ])
    const call = vi.fn(async (method: string) => {
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.capabilities) {
        return { formats: { html: { preview: true, sourceEdit: true } } }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.documentsGet) {
        return {
          document: {
            id: 'doc-html',
            name: 'page.html',
            format: 'html',
            headRevisionId: 'revision-head',
          },
        }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.revisionsList) {
        throw new Error('revision service timed out')
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.changesList) return { changeSets: [] }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.legacyGet) {
        return { artifact: { ...officeArtifact, download_url: '/old-original' } }
      }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const provider = createRpcArtifactDocumentProvider({
      call: call as unknown as GenericRpcCall,
      supportsMethod: method => supported.has(method),
      markMethodUnavailable: vi.fn(),
    })

    await expect(provider.loadWorkspace({
      id: 'artifact-html',
      documentId: 'doc-html',
      name: 'page.html',
      mime: 'text/html',
      download_url: '/api/v1/artifacts/artifact-html',
    }, 'session-a')).rejects.toThrow('revision service timed out')
    expect(call).not.toHaveBeenCalledWith(
      ARTIFACT_DOCUMENT_RPC_METHODS.legacyGet,
      expect.anything(),
      expect.anything(),
    )
  })

  it('uses the stable latest-head endpoint when revision metadata is unavailable', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.capabilities) {
        return { formats: { html: { preview: true, sourceEdit: true } } }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.documentsGet) {
        return {
          document: {
            id: 'doc-html',
            name: 'page.html',
            format: 'html',
            headRevisionId: 'revision-head',
          },
        }
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.revisionsList) return { revisions: [] }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.changesList) return { changeSets: [] }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const provider = createRpcArtifactDocumentProvider({
      call: call as unknown as GenericRpcCall,
      supportsMethod: () => true,
      markMethodUnavailable: vi.fn(),
    })

    const workspace = await provider.loadWorkspace({
      id: 'artifact-html',
      documentId: 'doc-html',
      name: 'page.html',
      mime: 'text/html',
      download_url: '/api/v1/artifacts/artifact-html',
    }, 'session-a')

    expect(workspace.source).toBe('document-api')
    expect(workspace.headArtifact).toMatchObject({
      id: 'revision-head',
      download_url: '/api/v1/artifact-documents/doc-html',
    })
    expect(workspace.headArtifact.download_url).not.toContain('/api/v1/artifacts/')
  })

  it('builds a read-only legacy workspace without claiming unsupported editing', () => {
    const workspace = createLegacyArtifactWorkspace(officeArtifact, 'session-a')

    expect(workspace.revisions).toHaveLength(1)
    expect(workspace.changeSets).toEqual([])
    expect(workspace.document.capabilities.reason).toBe('office-editor-unavailable')
  })
})
