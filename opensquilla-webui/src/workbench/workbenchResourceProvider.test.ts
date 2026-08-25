import { describe, expect, it, vi } from 'vitest'

import {
  createRpcWorkbenchResourceProvider,
  WORKBENCH_RESOURCE_RPC_METHODS,
} from './workbenchResourceProvider'

describe('workbench resource provider', () => {
  it('keeps selection and Agent editing independent from preview and legacy edit', async () => {
    const call = vi.fn(async () => ({
      resources: [{
        resource: { type: 'attachment', attachmentId: 'att-legacy' },
        name: 'legacy.html',
        mime: 'text/html',
        capabilities: { preview: true, download: true, edit: true, publish: false },
        relations: {},
      }, {
        resource: { type: 'document', documentId: 'doc-current' },
        name: 'current.html',
        mime: 'text/html',
        capabilities: {
          preview: true,
          download: true,
          selectionContext: true,
          manualEdit: true,
          agentEdit: true,
          edit: true,
          publish: true,
        },
        relations: {},
      }],
      totalCount: 2,
    }))
    const provider = createRpcWorkbenchResourceProvider({ call, supportsMethod: () => true })

    const result = await provider.list('session-a')

    expect(result.resources[0]?.capabilities).toMatchObject({
      preview: true,
      manualEdit: true,
      agentEdit: false,
      selectionContext: false,
      publish: false,
    })
    expect(result.resources[1]?.capabilities).toMatchObject({
      preview: true,
      manualEdit: true,
      agentEdit: true,
      selectionContext: true,
      publish: true,
    })
  })

  it('normalizes separate attachment, document and deliverable identities', async () => {
    const call = vi.fn(async () => ({
      resources: [
        {
          resource: { type: 'attachment', attachmentId: 'att-a' },
          name: 'same.html',
          mime: 'text/html',
          sha256: 'a'.repeat(64),
          capabilities: { preview: true, download: true, edit: true, publish: false },
          relations: {},
        },
        {
          resource: { type: 'document', id: 'doc-a' },
          name: 'same.html',
          mime: 'text/html',
          capabilities: { preview: true, download: true, edit: true, publish: true },
          relations: {
            documentId: 'doc-a',
            headRevisionId: 'rev-a',
            source: { type: 'attachment', attachmentId: 'att-source' },
          },
        },
        {
          resource: { type: 'deliverable', artifactId: 'art-a', id: 'art-a' },
          name: 'same.html',
          mime: 'text/html',
          capabilities: { preview: true, download: true, edit: true, publish: false },
          relations: { publishedRevisionId: 'rev-a' },
        },
      ],
      totalCount: 3,
    }))
    const provider = createRpcWorkbenchResourceProvider({ call, supportsMethod: () => true })

    const result = await provider.list('session-a')

    expect(result.resources.map(item => item.resource)).toEqual([
      { type: 'attachment', attachmentId: 'att-a', id: 'att-a' },
      { type: 'document', documentId: 'doc-a', id: 'doc-a' },
      { type: 'deliverable', artifactId: 'art-a', id: 'art-a' },
    ])
    expect(result.resources[1]?.relations.source).toEqual({
      type: 'attachment',
      attachmentId: 'att-source',
      id: 'att-source',
    })
    expect(result.totalCount).toBe(3)
    expect(call).toHaveBeenCalledWith(
      WORKBENCH_RESOURCE_RPC_METHODS.list,
      { sessionKey: 'session-a' },
      expect.objectContaining({ timeoutMs: 15_000 }),
    )
  })

  it('walks every resource page and accepts the inert url protocol identity', async () => {
    const call = vi.fn(async (_method: string, params?: Record<string, unknown>) => {
      if (params?.cursor === 'next-page') {
        return {
          resources: [{
            resource: { type: 'url', urlId: 'url-a' },
            name: 'Reference',
            mime: 'text/uri-list',
            capabilities: {
              preview: false,
              download: false,
              edit: false,
              publish: false,
              editReasonCode: 'url_resources_not_enabled',
            },
            relations: {},
          }],
          totalCount: 2,
          hasMore: false,
        }
      }
      return {
        resources: [{
          resource: { type: 'attachment', id: 'att-a' },
          name: 'page.html',
          mime: 'text/html',
          capabilities: { preview: true, download: true, edit: true, publish: false },
          relations: {},
        }],
        totalCount: 2,
        hasMore: true,
        nextCursor: 'next-page',
      }
    })
    const provider = createRpcWorkbenchResourceProvider({ call, supportsMethod: () => true })

    const result = await provider.list('session-a', { limit: 1 })

    expect(result.resources.map(item => item.resource)).toEqual([
      { type: 'attachment', attachmentId: 'att-a', id: 'att-a' },
      { type: 'url', urlId: 'url-a', id: 'url-a' },
    ])
    expect(result.resources[1]?.capabilities).toMatchObject({
      reasonCode: 'url_resources_not_enabled',
      previewReasonCode: 'url_resources_not_enabled',
      editReasonCode: 'url_resources_not_enabled',
    })
    expect(call).toHaveBeenNthCalledWith(
      2,
      WORKBENCH_RESOURCE_RPC_METHODS.list,
      { sessionKey: 'session-a', limit: 1, cursor: 'next-page' },
      expect.any(Object),
    )
  })

  it('imports a source by explicit user action and preserves the receipt', async () => {
    const call = vi.fn(async () => ({
      document: {
        id: 'doc-a',
        sessionKey: 'session-a',
        name: 'page.html',
        format: 'html',
        headRevisionId: 'rev-a',
        capabilities: { preview: true, sourceEdit: true, versionHistory: true },
      },
      revision: {
        id: 'rev-a',
        documentId: 'doc-a',
        artifactId: 'internal-a',
        sha256: 'a'.repeat(64),
        name: 'page.html',
        mime: 'text/html',
      },
      binding: {
        // Initial V036 gateways used the repository-native alias. The client
        // must accept it during mixed-version upgrades.
        id: 'bind-a',
        documentId: 'doc-a',
        source: { type: 'attachment', attachmentId: 'att-a' },
        sourceSha256: 'a'.repeat(64),
      },
      receipt: {
        attemptId: 'attempt-a',
        idempotencyKey: 'request-a',
        status: 'applied',
        replayed: false,
      },
    }))
    const provider = createRpcWorkbenchResourceProvider({ call, supportsMethod: () => true })

    const result = await provider.importDocument({
      sessionKey: 'session-a',
      source: { type: 'attachment', attachmentId: 'att-a' },
      expectedSha256: 'a'.repeat(64),
      idempotencyKey: 'request-a',
    })

    expect(result.document.documentId).toBe('doc-a')
    expect(result.binding?.source).toEqual({
      type: 'attachment',
      attachmentId: 'att-a',
      id: 'att-a',
    })
    expect(result.receipt).toMatchObject({
      requestId: 'request-a',
      status: 'applied',
      replayed: false,
    })
    expect(call).toHaveBeenCalledWith(
      WORKBENCH_RESOURCE_RPC_METHODS.importDocument,
      {
        sessionKey: 'session-a',
        source: { type: 'attachment', attachmentId: 'att-a', id: 'att-a' },
        mode: 'copy',
        expectedSha256: 'a'.repeat(64),
        clientRequestId: 'request-a',
        idempotencyKey: 'request-a',
      },
      expect.any(Object),
    )
  })

  it('gets a resource through the canonical resourceRef field', async () => {
    const call = vi.fn(async () => ({
      resource: {
        resource: { type: 'attachment', id: 'att-a' },
        name: 'page.html',
        mime: 'text/html',
        capabilities: { preview: true, download: true, edit: true, publish: false },
        relations: {},
      },
    }))
    const provider = createRpcWorkbenchResourceProvider({ call, supportsMethod: () => true })

    await expect(provider.get('session-a', { type: 'attachment', attachmentId: 'att-a' }))
      .resolves.toMatchObject({ name: 'page.html' })
    expect(call).toHaveBeenCalledWith(
      WORKBENCH_RESOURCE_RPC_METHODS.get,
      {
        sessionKey: 'session-a',
        resourceRef: { type: 'attachment', attachmentId: 'att-a', id: 'att-a' },
      },
      expect.any(Object),
    )
  })

  it('opens the current Document head through the canonical resourceRef field', async () => {
    const call = vi.fn(async () => ({
      disposition: 'document',
      resolution: { status: 'materialized' },
      resource: {
        resource: { type: 'document', documentId: 'doc-a' },
        name: 'page.html',
        mime: 'text/html',
        capabilities: {
          preview: true,
          download: true,
          manualEdit: true,
          edit: true,
          publish: true,
        },
        relations: {
          documentId: 'doc-a',
          headRevisionId: 'rev-a',
          headArtifactId: 'internal-a',
        },
      },
      document: {
        documentId: 'doc-a',
        sessionKey: 'session-a',
        name: 'page.html',
        format: 'html',
        headRevisionId: 'rev-a',
        capabilities: { preview: true, sourceEdit: true, versionHistory: true },
      },
      revision: {
        revisionId: 'rev-a',
        documentId: 'doc-a',
        artifactId: 'internal-a',
        sha256: 'a'.repeat(64),
        name: 'page.html',
        mime: 'text/html',
      },
      materialized: true,
    }))
    const provider = createRpcWorkbenchResourceProvider({ call, supportsMethod: () => true })

    await expect(provider.open?.(
      'session-a',
      { type: 'attachment', attachmentId: 'att-a' },
      {
        intent: 'edit-current',
        expectedSha256: 'b'.repeat(64),
        idempotencyKey: 'open-att-a',
      },
    )).resolves.toMatchObject({
      disposition: 'document',
      resolution: { status: 'materialized' },
      materialized: true,
      document: { documentId: 'doc-a', headRevisionId: 'rev-a' },
      revision: { documentId: 'doc-a', revisionId: 'rev-a' },
    })
    expect(call).toHaveBeenCalledWith(
      WORKBENCH_RESOURCE_RPC_METHODS.open,
      {
        sessionKey: 'session-a',
        resourceRef: { type: 'attachment', attachmentId: 'att-a', id: 'att-a' },
        intent: 'edit-current',
        expectedSha256: 'b'.repeat(64),
        idempotencyKey: 'open-att-a',
      },
      expect.any(Object),
    )
  })

  it('returns a stable read-only disposition instead of manufacturing a Document', async () => {
    const call = vi.fn(async () => ({
      disposition: 'readonly',
      resolution: { status: 'readonly' },
      reasonCode: 'format_edit_not_supported',
      materialized: false,
      resource: {
        resource: { type: 'attachment', id: 'att-pdf' },
        name: 'report.pdf',
        mime: 'application/pdf',
        capabilities: { preview: true, download: true, edit: true, publish: false },
        relations: {},
      },
    }))
    const provider = createRpcWorkbenchResourceProvider({ call, supportsMethod: () => true })

    const result = await provider.open?.(
      'session-a',
      { type: 'attachment', id: 'att-pdf' },
      { intent: 'edit-current', idempotencyKey: 'open-att-pdf' },
    )

    expect(result).toMatchObject({
      disposition: 'readonly',
      resolution: { status: 'readonly' },
      reasonCode: 'format_edit_not_supported',
      materialized: false,
      resource: {
        capabilities: {
          manualEdit: false,
          reasonCode: 'format_edit_not_supported',
        },
      },
    })
  })

  it('keeps old Gateways on the compatibility path when resource open is unavailable', async () => {
    const unavailable = Object.assign(new Error('Method not found'), {
      code: 'METHOD_NOT_FOUND',
    })
    const call = vi.fn(async () => { throw unavailable })
    const markMethodUnavailable = vi.fn()
    const provider = createRpcWorkbenchResourceProvider({
      call,
      markMethodUnavailable,
      supportsMethod: () => true,
    })

    await expect(provider.open?.(
      'session-a',
      { type: 'deliverable', artifactId: 'artifact-a' },
      { intent: 'edit-current', idempotencyKey: 'open-artifact-a' },
    )).resolves.toBeNull()
    expect(markMethodUnavailable).toHaveBeenCalledWith(
      WORKBENCH_RESOURCE_RPC_METHODS.open,
    )
  })

  it('rejects a conflicting canonical and legacy resource identity before RPC', async () => {
    const call = vi.fn()
    const provider = createRpcWorkbenchResourceProvider({ call, supportsMethod: () => true })

    await expect(provider.get('session-a', {
      type: 'attachment',
      attachmentId: 'att-canonical',
      id: 'att-legacy',
    })).rejects.toThrow('identity is invalid')
    expect(call).not.toHaveBeenCalled()
  })

  it('accepts only an offline preview descriptor bound to the requested resource', async () => {
    const call = vi.fn(async () => ({
      resource: {
        resource: { type: 'attachment', id: 'att-a' },
        name: 'page.html',
        mime: 'text/html',
        sha256: 'a'.repeat(64),
        downloadUrl: '/api/v1/attachments/a',
        capabilities: { preview: true, download: true, edit: true, publish: false },
        relations: {},
      },
      preview: {
        protocolVersion: 1,
        mode: 'isolated',
        resource: { type: 'attachment', id: 'att-a' },
        launchUrl: '/api/v1/attachments/a',
        sandboxProfile: 'opaque-offline',
        network: false,
        adapter: { adapterId: 'html' },
      },
    }))
    const provider = createRpcWorkbenchResourceProvider({ call, supportsMethod: () => true })

    const result = await provider.createPreview?.(
      'session-a',
      { type: 'attachment', id: 'att-a' },
    )

    expect(result?.preview).toMatchObject({
      mode: 'isolated',
      sandboxProfile: 'opaque-offline',
      network: false,
    })
    expect(call).toHaveBeenCalledWith(
      WORKBENCH_RESOURCE_RPC_METHODS.createPreview,
      {
        sessionKey: 'session-a',
        resourceRef: { type: 'attachment', attachmentId: 'att-a', id: 'att-a' },
        mode: 'isolated',
      },
      expect.any(Object),
    )
  })

  it('publishes one immutable revision', async () => {
    const call = vi.fn(async () => ({
      deliverable: {
        id: 'artifact-a',
        name: 'page.html',
        mime: 'text/html',
        sha256: 'a'.repeat(64),
      },
      publication: {
        // Match the initial V036 gateway wire shape as an upgrade fixture.
        id: 'publication-a',
        documentId: 'doc-a',
        revisionId: 'rev-a',
        deliverableId: 'artifact-a',
      },
      receipt: {
        attemptId: 'attempt-a',
        idempotencyKey: 'publish-a',
        status: 'applied',
        replayed: false,
      },
    }))
    const provider = createRpcWorkbenchResourceProvider({ call, supportsMethod: () => true })

    const result = await provider.publishDocument({
      sessionKey: 'session-a',
      documentId: 'doc-a',
      revisionId: 'rev-a',
      idempotencyKey: 'publish-a',
    })

    expect(result.publication).toMatchObject({
      revisionId: 'rev-a',
      artifactId: 'artifact-a',
    })
    expect(call).toHaveBeenCalledWith(
      WORKBENCH_RESOURCE_RPC_METHODS.publishDocument,
      {
        sessionKey: 'session-a',
        documentId: 'doc-a',
        revisionId: 'rev-a',
        clientRequestId: 'publish-a',
        idempotencyKey: 'publish-a',
      },
      expect.any(Object),
    )
  })

  it('fails closed when the v2 API is unavailable', async () => {
    const call = vi.fn()
    const provider = createRpcWorkbenchResourceProvider({
      call,
      supportsMethod: () => false,
    })

    await expect(provider.list('session-a')).resolves.toEqual({
      resources: [],
      totalCount: 0,
    })
    await expect(provider.importDocument({
      sessionKey: 'session-a',
      source: { type: 'attachment', id: 'att-a' },
      expectedSha256: 'a'.repeat(64),
      idempotencyKey: 'request-a',
    })).rejects.toThrow('unavailable')
    expect(call).not.toHaveBeenCalled()
  })
})
