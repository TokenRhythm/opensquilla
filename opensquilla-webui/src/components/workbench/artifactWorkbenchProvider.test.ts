import { afterEach, describe, expect, it, vi } from 'vitest'
import type {
  NativeWorkbenchApi,
  NativeWorkbenchSurfaceResult,
  Platform,
} from '@/platform/types'
import type { ArtifactPayload } from '@/types/rpc'
import { createLegacyArtifactWorkspace } from '@/workbench/artifactDocumentProvider'
import {
  createArtifactCollectionWorkbenchItem,
  createArtifactPreviewWorkbenchItem,
} from '@/workbench/artifactItems'
import {
  artifactPayloadFromWorkbenchResource,
  resourceFromPreparedPreview,
} from '@/workbench/workbenchResourceItems'
import { normalizeWorkbenchResource } from '@/workbench/workbenchResourceProvider'
import type {
  WorkbenchPanelRenderState,
  WorkbenchRuntimeContext,
} from '@/workbench/types'
import { createArtifactWorkbenchDefinitions } from './artifactWorkbenchProvider'

const artifact: ArtifactPayload = {
  id: 'artifact-1',
  name: 'preview.html',
  mime: 'text/html',
  size: 128,
  download_url: '/api/v1/artifacts/artifact-1',
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function nativeResource() {
  return {
    artifact,
    data: new TextEncoder().encode('<p>preview</p>').buffer,
    hasRelativeResources: false,
    mime: 'text/html',
    relativeResourceCount: 0,
    sessionKey: 'session-a',
  }
}

async function createNativeRuntimeHarness(
  nativeApi: NativeWorkbenchApi,
  confirmRemoteResources = vi.fn(async () => true),
) {
  const renderState: Record<string, unknown> = {}
  const pushToast = vi.fn()
  const reportError = vi.fn()
  const context: WorkbenchRuntimeContext = {
    nativeWorkbenchApi: nativeApi,
    getRenderState: () => renderState,
    updateRenderState: patch => Object.assign(renderState, patch),
    isItemOpen: () => true,
    setExpanded: vi.fn(),
    reportError,
  }
  const item = createArtifactPreviewWorkbenchItem({
    artifact,
    nativeHtml: true,
    sessionKey: 'session-a',
  })
  const definition = createArtifactWorkbenchDefinitions({
    authToken: () => '',
    baseOrigin: 'http://localhost',
    confirmRemoteResources,
    currentSessionId: () => 'session-a',
    openArtifact: vi.fn(),
    platform: {
      capabilities: { canOpenArtifactsNatively: false },
      files: {},
    } as unknown as Platform,
    pushToast,
    t: key => key,
  }).find(candidate => candidate.kind === 'artifact-preview')!
  const runtime = await definition.createRuntime!(item, context)
  return {
    confirmRemoteResources,
    definition,
    item,
    pushToast,
    renderState,
    reportError,
    runtime,
  }
}

describe('artifact Workbench provider', () => {
  it('does not expose a Desktop native-open diagnostic in the toast', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<p>fixture</p>', {
      status: 200,
      headers: { 'content-type': 'text/html' },
    })))
    const diagnostic = 'spawn EACCES /private/operator/report.html'
    const nativeOpen = vi.fn(async () => ({ ok: false, message: diagnostic }))
    const pushToast = vi.fn()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: false,
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      authToken: () => 'synthetic-token',
      baseOrigin: 'http://localhost',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: {
        id: 'desktop',
        capabilities: { canOpenArtifactsNatively: true },
        files: { openArtifact: nativeOpen },
      } as unknown as Platform,
      pushToast,
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(item, {
      getRenderState: () => ({}),
      updateRenderState: vi.fn(),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    })

    await runtime.handleComponentEvent?.({
      type: 'artifact-external-open',
      payload: artifact,
    }, item)

    expect(nativeOpen).toHaveBeenCalledOnce()
    expect(pushToast).toHaveBeenCalledWith('chat.toast.artifactOpenFailed', {
      tone: 'danger',
    })
    expect(JSON.stringify(pushToast.mock.calls)).not.toContain(diagnostic)
    expect(warn).toHaveBeenCalledWith('[artifact] Native open failed:', diagnostic)
  })

  it('routes a validated current-head publish request through the injected store action', async () => {
    const legacy = createLegacyArtifactWorkspace({
      ...artifact,
      documentId: 'document-1',
      revisionId: 'revision-1',
    }, 'session-a')
    const workspace = {
      ...legacy,
      source: 'document-api' as const,
      document: {
        ...legacy.document,
        documentId: 'document-1',
        headRevisionId: 'revision-1',
        name: 'preview.html',
      },
    }
    const publishDocument = vi.fn(async () => undefined)
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: false,
      previewLeaseEligible: false,
      sessionKey: 'session-a',
    })
    const renderState: Record<string, unknown> = {}
    const pushToast = vi.fn()
    const definition = createArtifactWorkbenchDefinitions({
      artifactDocuments: {
        load: vi.fn(async () => undefined),
        snapshot: vi.fn(() => ({
          key: 'fixture', loading: false, loaded: true, stale: false, error: null, workspace,
        })),
        headArtifact: vi.fn(value => value),
      },
      authToken: () => '',
      baseOrigin: 'http://localhost',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: { capabilities: {}, files: {} } as unknown as Platform,
      publishDocument,
      pushToast,
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(item, {
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    })

    await runtime.handleComponentEvent?.({
      type: 'artifact-document-publish',
      payload: { documentId: 'document-1', revisionId: 'revision-1' },
    }, item)

    expect(publishDocument).toHaveBeenCalledWith({
      sessionKey: 'session-a',
      documentId: 'document-1',
      revisionId: 'revision-1',
      name: 'preview.html',
    })
    expect(renderState.documentPublishing).toBe(false)
  })

  it('keeps an attachment preview read-only and never requests an Artifact lease', async () => {
    const createLease = vi.fn()
    const load = vi.fn(async () => undefined)
    const item = createArtifactPreviewWorkbenchItem({
      artifact: {
        name: 'uploaded.html',
        mime: 'text/html',
        download_url: '/api/v1/attachments/fixture',
      },
      nativeHtml: false,
      previewLeaseEligible: false,
      resourceIdentity: 'attachment:att_fixture',
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      artifactDocuments: {
        load,
        snapshot: vi.fn(() => ({
          key: 'fixture', loading: false, loaded: true, stale: false, error: null, workspace: null,
        })),
        headArtifact: vi.fn(value => value),
      },
      authToken: () => 'synthetic-token',
      baseOrigin: 'http://127.0.0.1:18791',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: { capabilities: {}, files: {} } as unknown as Platform,
      previewLeasesEnabled: true,
      pushToast: vi.fn(),
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!

    await definition.createRuntime!(item, {
      nativeWorkbenchApi: {
        createArtifactPreviewLease: createLease,
      } as unknown as NativeWorkbenchApi,
      getRenderState: () => ({}),
      updateRenderState: vi.fn(),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    })

    expect(createLease).not.toHaveBeenCalled()
    expect(load).toHaveBeenCalledOnce()
  })

  it('keeps a bound attachment preview on its immutable wire identity', async () => {
    const resource = normalizeWorkbenchResource({
      resource: { type: 'attachment', attachmentId: 'attachment-rev-1' },
      name: 'uploaded.html',
      mime: 'text/html',
      sha256: '1'.repeat(64),
      downloadUrl: '/api/v1/attachments/attachment-rev-1',
      capabilities: { preview: true, download: true, edit: true, publish: false },
      relations: {
        documentId: 'document-imported',
        headRevisionId: 'revision-2',
        headArtifactId: 'artifact-internal-2',
      },
    })!
    const preview = {
      resource,
      preview: {
        protocolVersion: 1,
        mode: 'isolated' as const,
        resource: resource.resource,
        launchUrl: '/api/v1/workbench/previews/attachment-rev-1',
        sandboxProfile: 'opaque-offline' as const,
        network: false as const,
        adapter: null,
      },
    }
    const preparedResource = resourceFromPreparedPreview(preview)
    const immutableArtifact = artifactPayloadFromWorkbenchResource(preparedResource)
    const load = vi.fn(async () => undefined)
    const snapshot = vi.fn(() => ({
      key: 'mutable-head-that-must-not-be-read',
      loading: false,
      loaded: true,
      stale: false,
      error: null,
      workspace: createLegacyArtifactWorkspace({
        ...artifact,
        id: 'artifact-internal-2',
        sha256: '2'.repeat(64),
      }, 'session-a'),
    }))
    const headArtifact = vi.fn(() => ({
      ...artifact,
      id: 'artifact-internal-2',
      sha256: '2'.repeat(64),
    }))
    const item = createArtifactPreviewWorkbenchItem({
      artifact: immutableArtifact,
      nativeHtml: false,
      preparedPreview: preview.preview,
      previewLeaseEligible: false,
      resourceIdentity: 'attachment:attachment-rev-1',
      sessionKey: 'session-a',
    })
    const renderState: Record<string, unknown> = {}
    const definition = createArtifactWorkbenchDefinitions({
      artifactDocuments: { load, snapshot, headArtifact },
      authToken: () => 'synthetic-token',
      baseOrigin: 'http://127.0.0.1:18791',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: { capabilities: {}, files: {} } as unknown as Platform,
      previewLeasesEnabled: true,
      pushToast: vi.fn(),
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!

    const runtime = await definition.createRuntime!(item, {
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    })
    const props = definition.getProps?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: false,
      runtimeState: renderState,
    }) as Record<string, unknown>

    expect(immutableArtifact).toMatchObject({
      sha256: '1'.repeat(64),
      workbenchResourceType: 'attachment',
    })
    expect(immutableArtifact).not.toHaveProperty('documentId')
    expect(immutableArtifact).not.toHaveProperty('revisionId')
    expect(load).not.toHaveBeenCalled()
    expect(snapshot).not.toHaveBeenCalled()
    expect(headArtifact).not.toHaveBeenCalled()
    expect(props).toMatchObject({
      artifact: expect.objectContaining({ sha256: '1'.repeat(64) }),
      documentFeatures: false,
      previewLaunchUrl: '/api/v1/workbench/previews/attachment-rev-1',
    })
    expect(props.documentSnapshot).toBeUndefined()
    expect(props.documentActions).toBeUndefined()

    const fetchImpl = vi.fn(async () => new Response(null, { status: 404 }))
    vi.stubGlobal('fetch', fetchImpl)
    await runtime.performAction?.('download', item)
    expect(fetchImpl).toHaveBeenCalledWith(
      '/api/v1/workbench/previews/attachment-rev-1',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(headArtifact).not.toHaveBeenCalled()
  })

  it('forwards direct document navigation to the Source section', async () => {
    const item = createArtifactPreviewWorkbenchItem({
      artifact: {
        id: 'artifact-current',
        documentId: 'document-current',
        revisionId: 'revision-current',
        name: 'current.html',
        mime: 'text/html',
      },
      initialSection: 'source',
      nativeHtml: false,
      resourceIdentity: 'document:document-current',
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      authToken: () => '',
      baseOrigin: 'http://localhost',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: { capabilities: {}, files: {} } as unknown as Platform,
      pushToast: vi.fn(),
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!

    const props = definition.getProps?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: false,
      runtimeState: {},
    })
    expect(props).toMatchObject({
      initialSection: 'source',
    })
    expect(props).not.toHaveProperty('editableCopyAvailable')
    expect(props).not.toHaveProperty('editableCopyBusy')
  })

  it('forces a prepared resource preview offline and rejects every network-enabling action', async () => {
    const renderState: Record<string, unknown> = {}
    const savePreviewPreferences = vi.fn(async () => undefined)
    const confirmRemoteResources = vi.fn(async () => true)
    const item = createArtifactPreviewWorkbenchItem({
      artifact: {
        name: 'uploaded.html',
        mime: 'text/html',
        download_url: '/api/v1/workbench/previews/fixture',
        workbenchResourceType: 'attachment',
        workbenchResourceId: 'att_fixture',
      },
      nativeHtml: false,
      preparedPreview: {
        protocolVersion: 1,
        mode: 'isolated',
        resource: { type: 'attachment', id: 'att_fixture' },
        launchUrl: '/api/v1/workbench/previews/fixture',
        sandboxProfile: 'opaque-offline',
        network: false,
        adapter: null,
      },
      previewLeaseEligible: false,
      resourceIdentity: 'attachment:att_fixture',
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      authToken: () => 'synthetic-token',
      baseOrigin: 'http://127.0.0.1:18791',
      confirmRemoteResources,
      currentSessionId: () => 'session-a',
      getPreviewPreferences: async () => ({ mode: 'full', noticeShown: false }),
      openArtifact: vi.fn(),
      platform: { capabilities: {}, files: {} } as unknown as Platform,
      previewLeasesEnabled: true,
      pushToast: vi.fn(),
      savePreviewPreferences,
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(item, {
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    })

    expect(renderState).toMatchObject({
      effectiveMode: 'offline',
      fullModeAvailable: false,
      previewDefaultMode: 'offline',
      previewMode: 'offline',
      previewNetworkAllowed: false,
      previewSandboxProfile: 'opaque-offline',
      remoteResourcesEnabled: false,
    })
    expect(definition.getProps?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: false,
      runtimeState: renderState,
    })).toMatchObject({
      previewMode: 'offline',
      previewNetworkAllowed: false,
      previewSandboxProfile: 'opaque-offline',
    })
    const toolbar = definition.getToolbarItems?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: false,
      runtimeState: {
        ...renderState,
        previewLaunchUrl: 'http://p-fixture.localhost:48721/index.html',
      },
    }) || []
    expect(toolbar.some(control => control.id === 'preview-mode')).toBe(false)
    expect(toolbar.some(control => control.id === 'toggle-remote-resources')).toBe(false)

    await runtime.performAction?.('set-preview-mode-full', item)
    await runtime.performAction?.('toggle-remote-resources', item)
    await runtime.performAction?.('set-default-preview-mode', item)

    expect(renderState.previewMode).toBe('offline')
    expect(renderState.remoteResourcesEnabled).toBe(false)
    expect(confirmRemoteResources).not.toHaveBeenCalled()
    expect(savePreviewPreferences).not.toHaveBeenCalled()
  })

  it('leases the immutable current head while keeping its stable document download URL', async () => {
    let currentHead: ArtifactPayload = {
      ...artifact,
      id: 'artifact-head-1',
      download_url: '/api/v1/artifact-documents/document-1',
    }
    let leaseSequence = 0
    const createLease = vi.fn(async (request: { artifactId: string; mode: string }) => {
      leaseSequence += 1
      const token = `0123456789abcdef0123456789abcde${leaseSequence}`
      return {
        ok: true as const,
        status: 201,
        payload: {
          version: 1 as const,
          lease_id: `apl-${leaseSequence}`,
          effective_mode: request.mode as 'full' | 'offline',
          launch_url: `http://p-${token}.localhost:48721/index.html`,
          entrypoint: 'index.html',
          expires_at: '2099-01-01T00:00:00Z',
          preview_origin: `http://p-${token}.localhost:48721`,
          idle_timeout_seconds: 28_800,
          source: {
            kind: 'single_file' as const,
            collection_status: 'not_applicable' as const,
            file_count: 1,
            total_bytes: 128,
            warning_codes: [],
          },
        },
      }
    })
    const destroySurface = vi.fn(async () => ({ ok: true as const }))
    const nativeApi: NativeWorkbenchApi = {
      getCapabilities: vi.fn(async () => ({
        protocolVersions: [2] as Array<2>,
        modes: ['full', 'offline'] as Array<'full' | 'offline'>,
        maxSurfaces: 8,
      })),
      createArtifactPreviewLease: createLease,
      renewArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 200,
        payload: {
          version: 1 as const,
          lease_id: `apl-${leaseSequence}`,
          expires_at: '2099-01-01T00:00:00Z',
        },
      })),
      revokeArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 204,
        payload: undefined,
      })),
      createSurface: vi.fn(async () => ({ ok: true as const })),
      setSurfaceRect: vi.fn(async () => ({ ok: true as const })),
      activateSurface: vi.fn(async () => ({ ok: true as const })),
      destroySurface,
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const renderState: Record<string, unknown> = {}
    const pushToast = vi.fn()
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const legacy = createLegacyArtifactWorkspace({
      ...artifact,
      documentId: 'document-1',
    }, 'session-a')
    let workspace = {
      ...legacy,
      source: 'document-api' as const,
      document: {
        ...legacy.document,
        documentId: 'document-1',
        headRevisionId: 'revision-1',
      },
    }
    let nextHeadRevisionId = 'revision-2'
    let staleSnapshot = false
    const loadDocument = vi.fn(async (
      _artifact: ArtifactPayload,
      _sessionKey: string,
      options?: { force?: boolean },
    ) => {
      if (options?.force && !staleSnapshot) {
        workspace = {
          ...workspace,
          document: {
            ...workspace.document,
            headRevisionId: nextHeadRevisionId,
          },
        }
      }
      return workspace
    })
    const definition = createArtifactWorkbenchDefinitions({
      artifactDocuments: {
        load: loadDocument,
        snapshot: vi.fn(() => ({
          key: 'session-a\0artifact-1',
          loading: false,
          loaded: true,
          stale: staleSnapshot,
          error: null,
          workspace,
        })),
        headArtifact: vi.fn(() => currentHead),
      },
      authToken: () => 'synthetic-token',
      baseOrigin: 'http://127.0.0.1:18791',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: {
        id: 'desktop',
        capabilities: { canOpenArtifactsNatively: false },
        files: {},
      } as unknown as Platform,
      previewLeasesEnabled: true,
      pushToast,
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(item, {
      nativeWorkbenchApi: nativeApi,
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    })

    expect(createLease).toHaveBeenNthCalledWith(1, expect.objectContaining({
      artifactId: 'artifact-head-1',
    }))
    currentHead = {
      ...currentHead,
      id: 'artifact-head-2',
    }
    await runtime.handleComponentEvent?.({
      type: 'artifact-head-changed',
      payload: { revisionId: 'revision-2' },
    }, item)
    expect(loadDocument).toHaveBeenLastCalledWith(
      artifact,
      'session-a',
      { force: true },
    )
    expect(pushToast).not.toHaveBeenCalled()
    expect(destroySurface).toHaveBeenCalledOnce()
    expect(createLease).toHaveBeenNthCalledWith(2, expect.objectContaining({
      artifactId: 'artifact-head-2',
    }))
    await runtime.handleComponentEvent?.({
      type: 'artifact-head-changed',
      payload: { revisionId: 'revision-2' },
    }, item)
    expect(createLease).toHaveBeenCalledTimes(2)
    expect(destroySurface).toHaveBeenCalledOnce()

    staleSnapshot = true
    await runtime.handleComponentEvent?.({
      type: 'artifact-head-changed',
      payload: { revisionId: 'revision-3' },
    }, item)
    expect(createLease).toHaveBeenCalledTimes(2)
    expect(destroySurface).toHaveBeenCalledTimes(2)
    expect(pushToast).toHaveBeenCalledWith(
      'workbench.artifactDocument.sourceUnavailable',
      { tone: 'danger' },
    )

    staleSnapshot = false
    nextHeadRevisionId = 'revision-3'
    currentHead = {
      ...currentHead,
      id: 'artifact-head-3',
    }
    await runtime.performAction?.('refresh', item)
    expect(createLease).toHaveBeenNthCalledWith(3, expect.objectContaining({
      artifactId: 'artifact-head-3',
    }))
    expect(destroySurface).toHaveBeenCalledTimes(2)
    await runtime.dispose?.('closed')
  })

  it('presents the effective preview mode as one explicit control', () => {
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: false,
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      authToken: () => '',
      baseOrigin: 'http://localhost',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: {
        id: 'web',
        capabilities: { canOpenArtifactsNatively: false },
        files: {},
      } as unknown as Platform,
      pushToast: vi.fn(),
      t: (key, params) => params?.mode ? `${key}:${params.mode}` : key,
    }).find(candidate => candidate.kind === 'artifact-preview')!

    const defaultToolbar = definition.getToolbarItems?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: false,
      runtimeState: {
        previewDefaultMode: 'full',
        previewLaunchUrl: 'http://p-fixture.localhost:48721/index.html',
        previewMode: 'full',
        previewState: 'ready',
      },
    }) || []
    expect(defaultToolbar).toEqual(expect.arrayContaining([
      expect.objectContaining({
        id: 'preview-mode',
        kind: 'select',
        value: 'full',
        options: [
          expect.objectContaining({
            actionId: 'set-preview-mode-full',
            value: 'full',
          }),
          expect.objectContaining({
            actionId: 'set-preview-mode-offline',
            value: 'offline',
          }),
        ],
      }),
    ]))
    expect(defaultToolbar.some(toolbarItem => (
      toolbarItem.id === 'toggle-preview-mode'
      || toolbarItem.id === 'set-default-preview-mode'
    ))).toBe(false)

    const overriddenToolbar = definition.getToolbarItems?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: false,
      runtimeState: {
        previewDefaultMode: 'full',
        previewLaunchUrl: 'http://p-fixture.localhost:48721/index.html',
        previewMode: 'offline',
        previewState: 'ready',
      },
    }) || []
    expect(overriddenToolbar).toEqual(expect.arrayContaining([
      expect.objectContaining({
        id: 'preview-mode',
        kind: 'select',
        value: 'offline',
        actionOptions: [
          expect.objectContaining({ actionId: 'set-default-preview-mode' }),
        ],
      }),
    ]))

    const remoteToolbar = definition.getToolbarItems?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: false,
      runtimeState: {
        fullModeAvailable: false,
        previewDefaultMode: 'full',
        previewLaunchUrl: 'https://gateway.test/api/v1/artifact-preview/token/index.html',
        previewMode: 'offline',
        previewState: 'ready',
      },
    }) || []
    expect(remoteToolbar).toEqual(expect.arrayContaining([
      expect.objectContaining({
        id: 'preview-mode',
        options: expect.arrayContaining([
          expect.objectContaining({
            value: 'full',
            disabled: true,
          }),
        ]),
      }),
    ]))
  })

  it('keeps temporary mode selection separate from the saved default', async () => {
    const requestedModes: string[] = []
    let leaseSequence = 0
    const createLease = vi.fn(async (request: { mode: 'full' | 'offline' }) => {
      requestedModes.push(request.mode)
      leaseSequence += 1
      const token = `${request.mode}-${leaseSequence}`
      return {
        ok: true as const,
        status: 201,
        payload: {
          version: 1 as const,
          lease_id: `apl-${token}`,
          effective_mode: request.mode,
          launch_url: `http://p-${token}.localhost:48721/index.html`,
          entrypoint: 'index.html',
          expires_at: '2099-01-01T00:00:00Z',
          preview_origin: `http://p-${token}.localhost:48721`,
          idle_timeout_seconds: 28_800,
          source: {
            kind: 'bundle' as const,
            collection_status: 'complete' as const,
            file_count: 2,
            total_bytes: 42,
            warning_codes: [],
          },
        },
      }
    })
    const createSurface = vi.fn(async () => ({ ok: true }))
    const nativeApi: NativeWorkbenchApi = {
      getCapabilities: vi.fn(async () => ({
        protocolVersions: [1, 2, 3] as Array<1 | 2 | 3>,
        modes: ['full', 'offline'] as Array<'full' | 'offline'>,
        maxSurfaces: 8,
      })),
      createArtifactPreviewLease: createLease,
      renewArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 200,
        payload: {
          version: 1 as const,
          lease_id: 'apl-fixture',
          expires_at: '2099-01-01T00:00:00Z',
        },
      })),
      revokeArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 204,
        payload: undefined,
      })),
      createSurface,
      setSurfaceRect: vi.fn(async () => ({ ok: true })),
      activateSurface: vi.fn(async () => ({ ok: true })),
      destroySurface: vi.fn(async () => ({ ok: true })),
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const renderState: Record<string, unknown> = {}
    const pushToast = vi.fn()
    const savePreviewPreferences = vi.fn(async () => undefined)
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      authToken: () => '',
      baseOrigin: 'http://127.0.0.1:18791',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      getPreviewPreferences: async () => ({ mode: 'offline', noticeShown: false }),
      openArtifact: vi.fn(),
      platform: {
        id: 'desktop',
        capabilities: { canOpenArtifactsNatively: false },
        files: {},
      } as unknown as Platform,
      previewLeasesEnabled: true,
      pushToast,
      savePreviewPreferences,
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(item, {
      nativeWorkbenchApi: nativeApi,
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    })

    await runtime.performAction?.('set-preview-mode-full', item)
    await runtime.performAction?.('set-preview-mode-full', item)

    expect(requestedModes).toEqual(['offline', 'full'])
    expect(renderState.previewMode).toBe('full')
    expect(createSurface).toHaveBeenLastCalledWith(expect.objectContaining({ version: 3 }))
    expect(renderState.previewDefaultMode).toBe('offline')
    expect(savePreviewPreferences).toHaveBeenCalledOnce()
    expect(savePreviewPreferences).toHaveBeenLastCalledWith({
      mode: 'offline',
      noticeShown: true,
    })

    await runtime.performAction?.('set-default-preview-mode', item)
    await runtime.performAction?.('set-default-preview-mode', item)

    expect(savePreviewPreferences).toHaveBeenCalledTimes(2)
    expect(savePreviewPreferences).toHaveBeenLastCalledWith({
      mode: 'full',
      noticeShown: true,
    })
    expect(pushToast).toHaveBeenCalledOnce()
    expect(renderState.previewDefaultMode).toBe('full')
    await runtime.dispose?.('closed')
  })

  it('silently rebuilds a failed native surface once before showing recovery UI', async () => {
    const createSurface = vi.fn(async () => ({ ok: true }))
    const setSurfaceRect = vi.fn(async () => ({ ok: true }))
    const activateSurface = vi.fn(async () => ({ ok: true }))
    const destroySurface = vi.fn(async () => ({ ok: true }))
    const nativeApi: NativeWorkbenchApi = {
      createSurface,
      setSurfaceRect,
      activateSurface,
      destroySurface,
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const renderState: Record<string, unknown> = {}
    const context: WorkbenchRuntimeContext = {
      nativeWorkbenchApi: nativeApi,
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    }
    const confirmRemoteResources = vi.fn(async () => true)
    const reload = vi.fn(async () => undefined)
    const beforeClose = vi.fn(async () => false)
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const definitions = createArtifactWorkbenchDefinitions({
      authToken: () => '',
      baseOrigin: 'http://localhost',
      confirmRemoteResources,
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: {
        capabilities: { canOpenArtifactsNatively: false },
        files: {},
      } as unknown as Platform,
      pushToast: vi.fn(),
      t: key => key,
    })
    const definition = definitions.find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(item, context)
    await runtime.setComponentHandle?.({ reload, beforeClose })
    await expect(runtime.beforeClose?.()).resolves.toBe(false)
    expect(beforeClose).toHaveBeenCalledOnce()
    const nativeResource = {
      artifact,
      data: new TextEncoder().encode('<img src="./missing.png">').buffer,
      hasRelativeResources: true,
      mime: 'text/html',
      relativeResourceCount: 1,
      sessionKey: 'session-a',
    }
    await runtime.handleComponentEvent?.({
      type: 'native-html-ready',
      payload: nativeResource,
    }, item)
    await runtime.handleSurfaceRect?.({
      itemId: item.id,
      x: 300,
      y: 40,
      width: 600,
      height: 500,
      visible: true,
    }, item)

    expect(createSurface).toHaveBeenCalledWith(expect.objectContaining({
      surfaceId: item.id,
      payload: expect.objectContaining({ allowRemoteResources: false }),
    }))
    expect(activateSurface).toHaveBeenCalledWith(item.id)
    expect(renderState).toMatchObject({
      missingResources: true,
      nativeSurfaceState: 'loading',
      previewState: 'idle',
      remoteResourcesEnabled: false,
    })

    await runtime.performAction?.('toggle-remote-resources', item)
    expect(confirmRemoteResources).toHaveBeenCalledOnce()
    expect(createSurface).toHaveBeenLastCalledWith(expect.objectContaining({
      payload: expect.objectContaining({ allowRemoteResources: true }),
    }))

    const presentation: WorkbenchPanelRenderState = {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: renderState,
    }
    expect(definition.getToolbarItems?.(item, presentation)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: 'missing-resources', kind: 'status' }),
        expect.objectContaining({
          id: 'toggle-remote-resources',
          kind: 'action',
          pressed: true,
        }),
      ]),
    )

    const createsBeforeFailure = createSurface.mock.calls.length
    await runtime.handleNativeSurfaceEvent?.({
      version: 1,
      surfaceId: item.id,
      type: 'error',
    }, item)
    expect(createSurface).toHaveBeenCalledTimes(createsBeforeFailure + 1)
    expect(renderState.nativeSurfaceState).toBe('loading')
    expect(context.reportError).not.toHaveBeenCalled()
    expect(setSurfaceRect).toHaveBeenLastCalledWith(
      expect.objectContaining({ surfaceId: item.id }),
    )

    await runtime.handleNativeSurfaceEvent?.({
      version: 1,
      surfaceId: item.id,
      type: 'crashed',
      detail: { reason: 'unresponsive' },
    }, item)
    expect(renderState.nativeSurfaceState).toBe('crashed')
    expect(destroySurface).toHaveBeenLastCalledWith(item.id)
    expect(context.reportError).toHaveBeenCalledOnce()
    expect(definition.getToolbarItems?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: renderState,
    })?.some(toolbarItem => toolbarItem.id === 'refresh')).toBe(true)

    await runtime.handleComponentEvent?.({
      type: 'native-html-ready',
      payload: nativeResource,
    }, item)
    await runtime.performAction?.('refresh', item)
    expect(reload).toHaveBeenCalledOnce()
    expect(renderState.nativeSurfaceState).toBe('loading')
    expect(destroySurface).toHaveBeenCalledWith(item.id)

    await runtime.dispose?.('closed')
  })

  it('silently discards a pending native create after its item closes', async () => {
    const createControl: {
      resolve: ((result: { ok: boolean }) => void) | null
    } = { resolve: null }
    const createSurface = vi.fn(() => new Promise<{ ok: boolean }>(resolve => {
      createControl.resolve = resolve
    }))
    const destroySurface = vi.fn(async () => ({ ok: true }))
    const nativeApi: NativeWorkbenchApi = {
      createSurface,
      setSurfaceRect: vi.fn(async () => ({ ok: true })),
      activateSurface: vi.fn(async () => ({ ok: true })),
      destroySurface,
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const renderState: Record<string, unknown> = {}
    let itemOpen = true
    const pushToast = vi.fn()
    const context: WorkbenchRuntimeContext = {
      nativeWorkbenchApi: nativeApi,
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => itemOpen,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    }
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      authToken: () => '',
      baseOrigin: 'http://localhost',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: {
        capabilities: { canOpenArtifactsNatively: false },
        files: {},
      } as unknown as Platform,
      pushToast,
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(item, context)
    const creating = runtime.handleComponentEvent?.({
      type: 'native-html-ready',
      payload: {
        artifact,
        data: new TextEncoder().encode('<p>preview</p>').buffer,
        hasRelativeResources: false,
        mime: 'text/html',
        relativeResourceCount: 0,
        sessionKey: 'session-a',
      },
    }, item)
    await vi.waitFor(() => expect(createSurface).toHaveBeenCalledOnce())
    itemOpen = false
    createControl.resolve?.({ ok: true })
    await creating

    expect(destroySurface).toHaveBeenCalledWith(item.id)
    expect(pushToast).not.toHaveBeenCalled()
    expect(renderState.nativeSurfaceState).not.toBe('crashed')
  })

  it('requires confirmation before enabling online resources', async () => {
    const createSurface = vi.fn(async () => ({ ok: true }))
    const nativeApi: NativeWorkbenchApi = {
      createSurface,
      setSurfaceRect: vi.fn(async () => ({ ok: true })),
      activateSurface: vi.fn(async () => ({ ok: true })),
      destroySurface: vi.fn(async () => ({ ok: true })),
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const confirmRemoteResources = vi.fn(async () => false)
    const harness = await createNativeRuntimeHarness(
      nativeApi,
      confirmRemoteResources,
    )
    await harness.runtime.handleComponentEvent?.({
      type: 'native-html-ready',
      payload: nativeResource(),
    }, harness.item)

    await harness.runtime.performAction?.(
      'toggle-remote-resources',
      harness.item,
    )

    expect(confirmRemoteResources).toHaveBeenCalledOnce()
    expect(createSurface).toHaveBeenCalledOnce()
    expect(harness.renderState.remoteResourcesEnabled).toBe(false)
  })

  it('surfaces an offline network block as ready with warnings', async () => {
    const nativeApi: NativeWorkbenchApi = {
      createSurface: vi.fn(async () => ({ ok: true })),
      setSurfaceRect: vi.fn(async () => ({ ok: true })),
      activateSurface: vi.fn(async () => ({ ok: true })),
      destroySurface: vi.fn(async () => ({ ok: true })),
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const harness = await createNativeRuntimeHarness(nativeApi)
    await harness.runtime.handleComponentEvent?.({
      type: 'native-html-ready',
      payload: nativeResource(),
    }, harness.item)

    await harness.runtime.handleNativeSurfaceEvent?.({
      version: 2,
      surfaceId: harness.item.id,
      type: 'blocked-action',
      detail: { action: 'network', reason: 'offline-policy' },
    }, harness.item)

    expect(harness.renderState).toMatchObject({
      networkBlocked: true,
      previewReadiness: 'ready-with-warnings',
    })
    expect(harness.definition.getToolbarItems?.(harness.item, {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: harness.renderState,
    })).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'preview-warnings', kind: 'status' }),
    ]))
  })

  it.each(['create', 'rect', 'activate'] as const)(
    'turns a rejected native %s operation into a recoverable DOM error',
    async failingOperation => {
      const createSurface = failingOperation === 'create'
        ? vi.fn(async () => { throw new Error('create rejected') })
        : vi.fn(async () => ({ ok: true }))
      const setSurfaceRect = failingOperation === 'rect'
        ? vi.fn(async () => { throw new Error('rect rejected') })
        : vi.fn(async () => ({ ok: true }))
      const activateSurface = failingOperation === 'activate'
        ? vi.fn(async () => { throw new Error('activate rejected') })
        : vi.fn(async () => ({ ok: true }))
      const destroySurface = vi.fn(async () => ({ ok: true }))
      const nativeApi: NativeWorkbenchApi = {
        createSurface,
        setSurfaceRect,
        activateSurface,
        destroySurface,
        onSurfaceEvent: vi.fn(() => () => undefined),
      }
      const harness = await createNativeRuntimeHarness(nativeApi)

      await harness.runtime.handleComponentEvent?.({
        type: 'native-html-ready',
        payload: nativeResource(),
      }, harness.item)
      if (failingOperation !== 'create') {
        await harness.runtime.handleSurfaceRect?.({
          itemId: harness.item.id,
          x: 300,
          y: 40,
          width: 600,
          height: 500,
          visible: true,
        }, harness.item)
      }

      expect(harness.renderState.nativeSurfaceState).toBe('error')
      expect(harness.pushToast).toHaveBeenCalledWith(
        'workbench.artifactPreview.failedDetail',
        { tone: 'danger' },
      )
      expect(harness.reportError).toHaveBeenCalledOnce()
      expect(destroySurface).toHaveBeenCalledWith(harness.item.id)
      expect(harness.definition.getProps?.(harness.item, {
        active: true,
        hostAvailable: true,
        nativeSurface: true,
        runtimeState: harness.renderState,
      })).toMatchObject({ nativeSurfaceState: 'error' })
    },
  )

  it('hides the old native surface while a component reloads or fails', async () => {
    const destroySurface = vi.fn(async () => ({ ok: true }))
    const nativeApi: NativeWorkbenchApi = {
      createSurface: vi.fn(async () => ({ ok: true })),
      setSurfaceRect: vi.fn(async () => ({ ok: true })),
      activateSurface: vi.fn(async () => ({ ok: true })),
      destroySurface,
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const harness = await createNativeRuntimeHarness(nativeApi)
    await harness.runtime.handleComponentEvent?.({
      type: 'native-html-ready',
      payload: nativeResource(),
    }, harness.item)

    await harness.runtime.handleComponentEvent?.({
      type: 'preview-state-change',
      payload: 'loading',
    }, harness.item)
    expect(harness.renderState.nativeSurfaceState).toBe('loading')
    expect(harness.renderState.previewState).toBe('loading')
    expect(destroySurface).toHaveBeenCalledWith(harness.item.id)
    expect(harness.definition.getToolbarItems?.(harness.item, {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: harness.renderState,
    })).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'refresh', disabled: true }),
    ]))

    await harness.runtime.handleComponentEvent?.({
      type: 'native-html-ready',
      payload: nativeResource(),
    }, harness.item)
    await harness.runtime.handleComponentEvent?.({
      type: 'preview-state-change',
      payload: 'error',
    }, harness.item)
    expect(harness.renderState.nativeSurfaceState).toBe('loading')
    expect(destroySurface).toHaveBeenCalledTimes(2)

    await harness.runtime.handleComponentEvent?.({
      type: 'preview-state-change',
      payload: 'unsupported',
    }, harness.item)
    expect(harness.renderState.nativeSurfaceState).toBe('error')
    expect(destroySurface).toHaveBeenCalledTimes(3)
    expect(harness.definition.getToolbarItems?.(harness.item, {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: harness.renderState,
    })?.some(item => item.id === 'refresh')).toBe(false)
  })

  it('blocks legacy HTML loading after a non-compatibility lease failure and retries the lease', async () => {
    const lease = {
      version: 1,
      lease_id: 'apl-fixture',
      effective_mode: 'full',
      launch_url:
        'http://p-0123456789abcdef0123456789abcdef.localhost:48721/index.html',
      entrypoint: 'index.html',
      expires_at: '2099-01-01T00:00:00Z',
      preview_origin:
        'http://p-0123456789abcdef0123456789abcdef.localhost:48721',
      idle_timeout_seconds: 28_800,
      source: {
        kind: 'bundle',
        collection_status: 'complete',
        file_count: 2,
        total_bytes: 42,
        warning_codes: [],
      },
    }
    let createResult: Awaited<ReturnType<
      NonNullable<NativeWorkbenchApi['createArtifactPreviewLease']>
    >> = {
      ok: false,
      status: 409,
      code: 'INTEGRITY_ERROR',
      message: 'Artifact integrity check failed.',
    }
    const createLease = vi.fn(async () => createResult)
    const createSurface = vi.fn(async () => ({ ok: true }))
    const nativeApi: NativeWorkbenchApi = {
      getCapabilities: vi.fn(async () => ({
        protocolVersions: [1, 2] as Array<1 | 2>,
        modes: ['full', 'offline'] as Array<'full' | 'offline'>,
        maxSurfaces: 8,
      })),
      createArtifactPreviewLease: createLease,
      renewArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 200,
        payload: {
          version: 1,
          lease_id: lease.lease_id,
          expires_at: lease.expires_at,
        },
      })),
      revokeArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 204,
        payload: undefined,
      })),
      createSurface,
      setSurfaceRect: vi.fn(async () => ({ ok: true })),
      activateSurface: vi.fn(async () => ({ ok: true })),
      destroySurface: vi.fn(async () => ({ ok: true })),
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const renderState: Record<string, unknown> = {}
    const reportError = vi.fn()
    const previewItem = createArtifactPreviewWorkbenchItem({
      artifact: { ...artifact, id: 'art-fixture' },
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      authToken: () => 'synthetic-token',
      baseOrigin: 'http://127.0.0.1:18791',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: {
        id: 'desktop',
        capabilities: { canOpenArtifactsNatively: false },
        files: {},
      } as unknown as Platform,
      previewLeasesEnabled: true,
      pushToast: vi.fn(),
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(previewItem, {
      nativeWorkbenchApi: nativeApi,
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError,
    })

    expect(renderState).toMatchObject({
      previewBlocked: true,
      previewLeaseError: 'The operation could not be completed. Try again.',
      previewLaunchUrl: '',
      previewState: 'error',
    })
    expect(definition.getProps?.(previewItem, {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: renderState,
    })).toMatchObject({
      previewBlocked: true,
      previewErrorMessage: 'The operation could not be completed. Try again.',
      previewLaunchUrl: '',
    })
    expect(createSurface).not.toHaveBeenCalled()

    createResult = { ok: true, status: 201, payload: lease }
    await runtime.performAction?.('refresh', previewItem)

    expect(createLease).toHaveBeenCalledTimes(2)
    expect(createSurface).toHaveBeenCalledWith(expect.objectContaining({
      version: 2,
      kind: 'artifact-preview',
      payload: expect.objectContaining({
        launchUrl: lease.launch_url,
        scopeId: 'session-a',
      }),
    }))
    expect(renderState).toMatchObject({
      previewBlocked: false,
      previewLeaseError: '',
      previewLaunchUrl: lease.launch_url,
    })
    await runtime.dispose?.('closed')
  })

  it('settles a revoked-lease replacement failure without an unhandled renewal rejection', async () => {
    vi.useFakeTimers()
    const privateDiagnostic = 'revoked lease at /private/operator/profile'
    const lease = {
      version: 1 as const,
      lease_id: 'apl-renewal-fixture',
      effective_mode: 'full' as const,
      launch_url: 'http://p-1123456789abcdef0123456789abcdef.localhost:48721/index.html',
      entrypoint: 'index.html',
      expires_at: '2099-01-01T00:00:00Z',
      preview_origin: 'http://p-1123456789abcdef0123456789abcdef.localhost:48721',
      idle_timeout_seconds: 28_800,
      source: {
        kind: 'single_file' as const,
        collection_status: 'not_applicable' as const,
        file_count: 1,
        total_bytes: 42,
        warning_codes: [],
      },
    }
    const destroySurface = vi.fn(async () => ({ ok: false as const }))
    const pushToast = vi.fn()
    const reportError = vi.fn()
    const nativeApi: NativeWorkbenchApi = {
      getCapabilities: vi.fn(async () => ({
        protocolVersions: [2] as Array<2>,
        modes: ['full', 'offline'] as Array<'full' | 'offline'>,
        maxSurfaces: 8,
      })),
      createArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 201,
        payload: lease,
      })),
      renewArtifactPreviewLease: vi.fn(async () => ({
        ok: false as const,
        status: 410,
        code: 'PREVIEW_CAPABILITY_EXPIRED',
        message: privateDiagnostic,
      })),
      revokeArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 204,
        payload: undefined,
      })),
      createSurface: vi.fn(async () => ({ ok: true as const })),
      setSurfaceRect: vi.fn(async () => ({ ok: true as const })),
      activateSurface: vi.fn(async () => ({ ok: true as const })),
      destroySurface,
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const renderState: Record<string, unknown> = {}
    const previewItem = createArtifactPreviewWorkbenchItem({
      artifact: { ...artifact, id: 'artifact-renewal' },
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      authToken: () => 'synthetic-token',
      baseOrigin: 'http://127.0.0.1:18791',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: {
        id: 'desktop',
        capabilities: { canOpenArtifactsNatively: true },
        files: {},
      } as unknown as Platform,
      previewLeasesEnabled: true,
      pushToast,
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(previewItem, {
      nativeWorkbenchApi: nativeApi,
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError,
    })

    try {
      await vi.advanceTimersByTimeAsync(15 * 60 * 1000)
      expect(nativeApi.renewArtifactPreviewLease).toHaveBeenCalledOnce()
      expect(destroySurface).toHaveBeenCalledTimes(2)
      expect(renderState).toMatchObject({
        nativeSurfaceState: 'error',
        previewBlocked: true,
        previewReadiness: 'error',
        previewState: 'error',
      })
      expect(reportError).toHaveBeenCalledOnce()
      expect(pushToast).toHaveBeenCalledWith(
        expect.not.stringContaining(privateDiagnostic),
        { tone: 'danger' },
      )
      expect(renderState.annotationMode).not.toBe(true)
    } finally {
      await runtime.dispose?.('closed')
      vi.useRealTimers()
    }
  })

  it('uses the explicit v1 compatibility path when a v2-era Desktop lacks the lease broker', async () => {
    const nativeApi: NativeWorkbenchApi = {
      getCapabilities: vi.fn(async () => ({
        protocolVersions: [1, 2] as Array<1 | 2>,
        modes: ['full', 'offline'] as Array<'full' | 'offline'>,
        maxSurfaces: 8,
      })),
      createSurface: vi.fn(async () => ({ ok: true })),
      setSurfaceRect: vi.fn(async () => ({ ok: true })),
      activateSurface: vi.fn(async () => ({ ok: true })),
      destroySurface: vi.fn(async () => ({ ok: true })),
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const renderState: Record<string, unknown> = {}
    const previewItem = createArtifactPreviewWorkbenchItem({
      artifact: { ...artifact, id: 'art-fixture' },
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      authToken: () => '',
      baseOrigin: 'http://127.0.0.1:18791',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: {
        id: 'desktop',
        capabilities: { canOpenArtifactsNatively: false },
        files: {},
      } as unknown as Platform,
      previewLeasesEnabled: true,
      pushToast: vi.fn(),
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!
    await definition.createRuntime!(previewItem, {
      nativeWorkbenchApi: nativeApi,
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    })

    expect(renderState).toMatchObject({
      compatibilityFallback: true,
      previewBlocked: false,
      previewMode: 'offline',
    })
  })

  it('clears a Web preview origin before revoking its lease on normal close', async () => {
    const previewOrigin =
      'http://p-0123456789abcdef0123456789abcdef.localhost:48721'
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        version: 1,
        lease_id: 'apl-web-fixture',
        effective_mode: 'full',
        launch_url: `${previewOrigin}/index.html`,
        entrypoint: 'index.html',
        expires_at: '2099-01-01T00:00:00Z',
        preview_origin: previewOrigin,
        idle_timeout_seconds: 28_800,
        source: {
          kind: 'single_file',
          collection_status: 'not_applicable',
          file_count: 1,
          total_bytes: 42,
          warning_codes: [],
        },
      }), {
        status: 201,
        headers: { 'content-type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)
    try {
      const renderState: Record<string, unknown> = {}
      const previewItem = createArtifactPreviewWorkbenchItem({
        artifact: { ...artifact, id: 'art-web-fixture' },
        nativeHtml: false,
        sessionKey: 'session-a',
      })
      const definition = createArtifactWorkbenchDefinitions({
        authToken: () => '',
        baseOrigin: 'http://127.0.0.1:18791',
        confirmRemoteResources: vi.fn(async () => true),
        currentSessionId: () => 'session-a',
        openArtifact: vi.fn(),
        platform: {
          id: 'web',
          capabilities: { canOpenArtifactsNatively: false },
          files: {},
        } as unknown as Platform,
        previewLeasesEnabled: true,
        pushToast: vi.fn(),
        t: key => key,
      }).find(candidate => candidate.kind === 'artifact-preview')!
      const runtime = await definition.createRuntime!(previewItem, {
        getRenderState: () => renderState,
        updateRenderState: patch => Object.assign(renderState, patch),
        isItemOpen: () => true,
        setExpanded: vi.fn(),
        reportError: vi.fn(),
      })

      await runtime.dispose?.('closed')

      expect(String(fetchMock.mock.calls[1]?.[0])).toBe(
        `${previewOrigin}/.opensquilla/clear-site-data`,
      )
      expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({
        credentials: 'omit',
        mode: 'no-cors',
        referrerPolicy: 'no-referrer',
      })
      expect(String(fetchMock.mock.calls[2]?.[0])).toContain(
        '/api/v1/artifact-preview-leases/apl-web-fixture',
      )
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('recovers a missing native surface without resurrecting stale picker state', async () => {
    const legacy = createLegacyArtifactWorkspace(artifact, 'session-a')
    const workspace = {
      ...legacy,
      source: 'document-api' as const,
      document: {
        ...legacy.document,
        documentId: 'document-1',
        headRevisionId: 'revision-1',
        capabilities: {
          ...legacy.document.capabilities,
          preview: true,
          edit: true,
          source: true,
          manualEdit: true,
          agentEdit: true,
          selectionContext: true,
          promptAnnotations: true,
        },
      },
    }
    let leaseSequence = 0
    const createSurface = vi.fn(async () => ({ ok: true as const }))
    let missingSurface = true
    const setSurfaceRect = vi.fn(async (request: { visible: boolean }) => {
      if (missingSurface && request.visible) {
        missingSurface = false
        return {
          ok: false as const,
          message: 'The native Workbench surface no longer exists.',
        }
      }
      return { ok: true as const }
    })
    const nativeApi: NativeWorkbenchApi = {
      getCapabilities: vi.fn(async () => ({
        protocolVersions: [4] as Array<4>,
        modes: ['full', 'offline'] as Array<'full' | 'offline'>,
        maxSurfaces: 8,
      })),
      getArtifactAnnotationCapabilities: vi.fn(async () => ({
        version: 4 as const,
        available: true,
        picker: true,
        trustedOverlay: true,
        overlayCopyVersion: 1 as const,
      })),
      setArtifactAnnotationMode: vi.fn(async () => ({ ok: true as const })),
      showArtifactAnnotationOverlay: vi.fn(async () => ({ ok: true as const })),
      closeArtifactAnnotationOverlay: vi.fn(async () => ({ ok: true as const })),
      createArtifactPreviewLease: vi.fn(async () => {
        leaseSequence += 1
        const token = String(leaseSequence).padStart(32, '0')
        return {
          ok: true as const,
          status: 201,
          payload: {
            version: 1 as const,
            lease_id: `apl-missing-${leaseSequence}`,
            effective_mode: 'full' as const,
            launch_url: `http://p-${token}.localhost:48721/index.html`,
            entrypoint: 'index.html',
            expires_at: '2099-01-01T00:00:00Z',
            preview_origin: `http://p-${token}.localhost:48721`,
            idle_timeout_seconds: 28_800,
            source: {
              kind: 'single_file' as const,
              collection_status: 'not_applicable' as const,
              file_count: 1,
              total_bytes: 128,
              warning_codes: [],
            },
          },
        }
      }),
      renewArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 200,
        payload: {
          version: 1 as const,
          lease_id: `apl-missing-${leaseSequence}`,
          expires_at: '2099-01-01T00:00:00Z',
        },
      })),
      revokeArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 204,
        payload: undefined,
      })),
      createSurface,
      setSurfaceRect,
      activateSurface: vi.fn(async () => ({ ok: true as const })),
      destroySurface: vi.fn(async () => ({ ok: true as const })),
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const renderState: Record<string, unknown> = {}
    const reportError = vi.fn()
    const pushToast = vi.fn()
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      artifactDocuments: {
        load: vi.fn(async () => undefined),
        snapshot: vi.fn(() => ({
          key: 'fixture',
          loading: false,
          loaded: true,
          stale: false,
          error: null,
          workspace,
        })),
        headArtifact: vi.fn(() => artifact),
      },
      promptAnnotations: {
        create: vi.fn(async () => { throw new Error('unused') }),
        update: vi.fn(async () => null),
        discard: vi.fn(async () => true),
        setActiveDocument: vi.fn(),
      },
      authToken: () => 'synthetic-token',
      baseOrigin: 'http://127.0.0.1:18791',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: {
        id: 'desktop',
        capabilities: { canOpenArtifactsNatively: true },
        files: {},
      } as unknown as Platform,
      previewLeasesEnabled: true,
      pushToast,
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(item, {
      nativeWorkbenchApi: nativeApi,
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError,
    })

    await runtime.handleSurfaceRect?.({
      itemId: item.id,
      x: 300,
      y: 40,
      width: 600,
      height: 500,
      visible: true,
    }, item)

    expect(createSurface).toHaveBeenCalledTimes(2)
    expect(renderState.nativeSurfaceState).toBe('ready')
    expect(renderState.annotationAvailable).toBe(true)
    expect(reportError).not.toHaveBeenCalled()
    expect(pushToast).not.toHaveBeenCalled()
    await runtime.performAction?.('toggle-annotation-mode', item)
    expect(renderState.annotationMode).toBe(true)
    await runtime.handleComponentEvent?.({
      type: 'preview-state-change',
      payload: 'loading',
    }, item)
    expect(renderState).toMatchObject({
      annotationAvailable: false,
      annotationMode: false,
      nativeSurfaceState: 'loading',
    })
    await runtime.dispose?.('closed')
  })

  it('refreshes annotation capability after reactivating a replacement surface', async () => {
    const legacy = createLegacyArtifactWorkspace(artifact, 'session-a')
    const workspace = {
      ...legacy,
      source: 'document-api' as const,
      document: {
        ...legacy.document,
        documentId: 'document-1',
        headRevisionId: 'revision-1',
        capabilities: {
          ...legacy.document.capabilities,
          preview: true,
          edit: true,
          source: true,
          promptAnnotations: false,
        },
      },
    }
    let surfaceActive = false
    let leaseSequence = 0
    const getArtifactAnnotationCapabilities = vi.fn(async () => ({
      version: 3 as const,
      available: surfaceActive,
      picker: surfaceActive,
      trustedOverlay: surfaceActive,
    }))
    const createSurface = vi.fn(async () => {
      surfaceActive = false
      return { ok: true as const }
    })
    const setSurfaceRect = vi.fn(async (request: { visible: boolean }) => {
      surfaceActive = request.visible
      return { ok: true as const }
    })
    const activateSurface = vi.fn(async () => {
      surfaceActive = true
      return { ok: true as const }
    })
    const setArtifactAnnotationMode = vi.fn(
      async (): Promise<NativeWorkbenchSurfaceResult> => ({ ok: true }),
    )
    const pushToast = vi.fn()
    const nativeApi: NativeWorkbenchApi = {
      getCapabilities: vi.fn(async () => ({
        protocolVersions: [3] as Array<3>,
        modes: ['full', 'offline'] as Array<'full' | 'offline'>,
        maxSurfaces: 8,
      })),
      getArtifactAnnotationCapabilities,
      setArtifactAnnotationMode,
      showArtifactAnnotationOverlay: vi.fn(async () => ({ ok: true as const })),
      closeArtifactAnnotationOverlay: vi.fn(async () => ({ ok: true as const })),
      createArtifactPreviewLease: vi.fn(async () => {
        leaseSequence += 1
        return {
          ok: true as const,
          status: 201,
          payload: {
            version: 1 as const,
            lease_id: `apl-annotation-${leaseSequence}`,
            effective_mode: 'full' as const,
            launch_url: `http://p-${String(leaseSequence).padStart(32, '0')}.localhost:48721/index.html`,
            entrypoint: 'index.html',
            expires_at: '2099-01-01T00:00:00Z',
            preview_origin: `http://p-${String(leaseSequence).padStart(32, '0')}.localhost:48721`,
            idle_timeout_seconds: 28_800,
            source: {
              kind: 'single_file' as const,
              collection_status: 'not_applicable' as const,
              file_count: 1,
              total_bytes: 128,
              warning_codes: [],
            },
          },
        }
      }),
      renewArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 200,
        payload: {
          version: 1 as const,
          lease_id: `apl-annotation-${leaseSequence}`,
          expires_at: '2099-01-01T00:00:00Z',
        },
      })),
      revokeArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 204,
        payload: undefined,
      })),
      createSurface,
      setSurfaceRect,
      activateSurface,
      destroySurface: vi.fn(async () => ({ ok: true as const })),
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const renderState: Record<string, unknown> = {}
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      artifactDocuments: {
        load: vi.fn(async () => undefined),
        snapshot: vi.fn(() => ({
          key: 'fixture',
          loading: false,
          loaded: true,
          stale: false,
          error: null,
          workspace,
        })),
        headArtifact: vi.fn(() => artifact),
      },
      promptAnnotations: {
        create: vi.fn(async () => { throw new Error('unused') }),
        update: vi.fn(async () => null),
        discard: vi.fn(async () => true),
        setActiveDocument: vi.fn(),
      },
      authToken: () => 'synthetic-token',
      baseOrigin: 'http://127.0.0.1:18791',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: {
        id: 'desktop',
        capabilities: { canOpenArtifactsNatively: true },
        files: {},
      } as unknown as Platform,
      previewLeasesEnabled: true,
      pushToast,
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(item, {
      nativeWorkbenchApi: nativeApi,
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    })

    // The only rect event precedes the replacement. No later resize should be
    // required to discover the active surface's annotation capability.
    await runtime.handleSurfaceRect?.({
      itemId: item.id,
      x: 300,
      y: 40,
      width: 600,
      height: 500,
      visible: true,
    }, item)
    expect(renderState.annotationAvailable).toBe(false)
    workspace.document.capabilities.promptAnnotations = true
    workspace.document.capabilities.manualEdit = true
    workspace.document.capabilities.selectionContext = true
    workspace.document.capabilities.agentEdit = true

    await runtime.handleComponentEvent?.({ type: 'artifact-head-changed' }, item)

    expect(renderState.annotationAvailable).toBe(true)
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'ready',
    }, item)
    const activationOrder = activateSurface.mock.invocationCallOrder
    const capabilityOrder = getArtifactAnnotationCapabilities.mock.invocationCallOrder
    expect(activationOrder[activationOrder.length - 1])
      .toBeLessThan(capabilityOrder[capabilityOrder.length - 1]!)
    expect(definition.getToolbarItems?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: renderState,
    })?.some(toolbarItem => toolbarItem.id === 'toggle-annotation-mode')).toBe(true)

    // Source can replace the native surface while Preview is hidden. The
    // temporary capability miss must preserve the user's picker intent until
    // the replacement surface becomes visible again.
    await runtime.performAction?.('toggle-annotation-mode', item)
    expect(renderState.annotationMode).toBe(true)
    await runtime.handleSurfaceRect?.({
      itemId: item.id,
      x: 300,
      y: 40,
      width: 600,
      height: 500,
      visible: false,
    }, item)
    workspace.document.headRevisionId = 'revision-2'
    await runtime.handleComponentEvent?.({ type: 'artifact-head-changed' }, item)
    expect(renderState.annotationAvailable).toBe(false)
    expect(renderState.annotationMode).toBe(false)

    await runtime.handleSurfaceRect?.({
      itemId: item.id,
      x: 300,
      y: 40,
      width: 600,
      height: 500,
      visible: true,
    }, item)
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'ready',
    }, item)
    expect(renderState.annotationAvailable).toBe(true)
    expect(renderState.annotationMode).toBe(true)
    expect(nativeApi.setArtifactAnnotationMode).toHaveBeenLastCalledWith({
      version: 3,
      surfaceId: item.id,
      enabled: true,
    })

    // A scoped Desktop capability can disappear between two annotations.
    // Rebuild the same resource/head once and replay the bounded picker
    // enable without showing recovery UI or releasing the pressed state.
    await runtime.performAction?.('toggle-annotation-mode', item)
    expect(renderState.annotationMode).toBe(false)
    const createsBeforeCapabilityRecovery = createSurface.mock.calls.length
    setArtifactAnnotationMode.mockResolvedValueOnce({
      ok: false as const,
      code: 'PREVIEW_CAPABILITY_EXPIRED',
      retryable: true,
    })
    await runtime.performAction?.('toggle-annotation-mode', item)
    expect(createSurface).toHaveBeenCalledTimes(createsBeforeCapabilityRecovery + 1)
    expect(renderState.annotationMode).toBe(true)
    expect(renderState.annotationAvailable).toBe(true)
    expect(pushToast).not.toHaveBeenCalled()

    // The same resource/head gets only one silent rebuild. A second failure
    // settles into localized product UI instead of looping surface creation.
    await runtime.performAction?.('toggle-annotation-mode', item)
    const createsBeforeSecondFailure = createSurface.mock.calls.length
    setArtifactAnnotationMode.mockResolvedValueOnce({
      ok: false as const,
      code: 'PREVIEW_CAPABILITY_EXPIRED',
      retryable: true,
    })
    await runtime.performAction?.('toggle-annotation-mode', item)
    expect(createSurface).toHaveBeenCalledTimes(createsBeforeSecondFailure)
    expect(renderState.annotationMode).toBe(false)
    expect(pushToast).toHaveBeenCalledWith(
      'workbench.artifactAnnotation.unavailable',
      { tone: 'danger' },
    )
    await runtime.dispose?.('closed')
  })

  it('ends a submitted annotation picker and aligns retries with native state', async () => {
    const legacy = createLegacyArtifactWorkspace(artifact, 'session-a')
    const workspace = {
      ...legacy,
      source: 'document-api' as const,
      document: {
        ...legacy.document,
        documentId: 'document-1',
        headRevisionId: 'revision-1',
        capabilities: {
          ...legacy.document.capabilities,
          preview: true,
          edit: true,
          source: true,
          promptAnnotations: false,
        },
      },
    }
    let rejectNextCreate: Error | null = null
    let deferNextCreate = false
    let resolveDeferredCreate: (() => void) | null = null
    const finishDeferredCreate = () => {
      const resolve: unknown = resolveDeferredCreate
      if (typeof resolve !== 'function') throw new Error('deferred create is not pending')
      resolve()
      resolveDeferredCreate = null
    }
    const createAnnotation = vi.fn(async (request: {
      annotationId: string
      sessionKey: string
      documentId: string
      revisionId: string
    }) => {
      if (rejectNextCreate) {
        const error = rejectNextCreate
        rejectNextCreate = null
        throw error
      }
      if (deferNextCreate) {
        deferNextCreate = false
        await new Promise<void>((resolve) => {
          resolveDeferredCreate = resolve
        })
      }
      return {
        annotationId: request.annotationId,
        sessionKey: request.sessionKey,
        sessionId: null,
        sessionEpoch: null,
        documentId: request.documentId,
        documentName: 'preview.html',
        revisionId: request.revisionId,
        generation: 1,
        anchorId: 'anchor-1',
        body: '',
        status: 'draft' as const,
        freshness: 'fresh' as const,
        staleReason: null,
        stateRevision: 1,
        tagName: 'button',
        locator: {},
        quote: '<button>',
        sourceExcerpt: null,
        sentMessageId: null,
        sentTurnId: null,
        sentOrder: null,
        createdAt: 1,
        updatedAt: 1,
        schemaVersion: 1,
      }
    })
    let rejectNextUpdate: Error | null = null
    let deferNextUpdate = false
    let resolveDeferredUpdate: (() => void) | null = null
    const finishDeferredUpdate = () => {
      const resolve: unknown = resolveDeferredUpdate
      if (typeof resolve !== 'function') throw new Error('deferred update is not pending')
      resolve()
      resolveDeferredUpdate = null
    }
    const updateAnnotation = vi.fn(async () => {
      if (rejectNextUpdate) {
        const error = rejectNextUpdate
        rejectNextUpdate = null
        throw error
      }
      if (deferNextUpdate) {
        deferNextUpdate = false
        await new Promise<void>((resolve) => {
          resolveDeferredUpdate = resolve
        })
      }
      return null
    })
    let rejectNextDiscard: Error | null = null
    let deferNextDiscard = false
    let resolveDeferredDiscard: (() => void) | null = null
    const finishDeferredDiscard = () => {
      const resolve: unknown = resolveDeferredDiscard
      if (typeof resolve !== 'function') throw new Error('deferred discard is not pending')
      resolve()
      resolveDeferredDiscard = null
    }
    const discardAnnotation = vi.fn(async () => {
      if (rejectNextDiscard) {
        const error = rejectNextDiscard
        rejectNextDiscard = null
        throw error
      }
      if (deferNextDiscard) {
        deferNextDiscard = false
        await new Promise<void>((resolve) => {
          resolveDeferredDiscard = resolve
        })
      }
      return true
    })
    const beginOverlayEdit = vi.fn()
    const completeOverlayEdit = vi.fn()
    const releaseOverlayEdit = vi.fn()
    const setActiveDocument = vi.fn()
    const showOverlay = vi.fn(async (
      _request: Parameters<NonNullable<NativeWorkbenchApi['showArtifactAnnotationOverlay']>>[0],
    ): Promise<NativeWorkbenchSurfaceResult> => ({
      ok: false,
      message: 'overlay-unavailable',
    }))
    let deferNextScreenshot = false
    let resolveDeferredScreenshot: (() => void) | null = null
    const finishDeferredScreenshot = () => {
      const resolve: unknown = resolveDeferredScreenshot
      if (typeof resolve !== 'function') throw new Error('deferred screenshot is not pending')
      resolve()
      resolveDeferredScreenshot = null
    }
    const screenshot = vi.fn(async () => {
      if (deferNextScreenshot) {
        deferNextScreenshot = false
        await new Promise<void>((resolve) => {
          resolveDeferredScreenshot = resolve
        })
      }
      return {
        ok: true as const,
        method: 'screenshot' as const,
        value: {
          mime: 'image/png' as const,
          data: new Uint8Array([137, 80, 78, 71]),
          width: 320,
          height: 180,
        },
      }
    })
    const createScreenshotUrl = vi.spyOn(URL, 'createObjectURL')
      .mockReturnValue('blob:frozen-annotation-preview')
    const revokeScreenshotUrl = vi.spyOn(URL, 'revokeObjectURL')
      .mockImplementation(() => undefined)
    const closeOverlay = vi.fn(async (
      _request: Parameters<NonNullable<NativeWorkbenchApi['closeArtifactAnnotationOverlay']>>[0],
    ): Promise<NativeWorkbenchSurfaceResult> => ({ ok: true }))
    type AnnotationModeResult = NativeWorkbenchSurfaceResult
    let deferNextModeEnable = false
    let resolveDeferredModeEnable: ((result: AnnotationModeResult) => void) | null = null
    const finishDeferredModeEnable = (
      result: AnnotationModeResult = { ok: true },
    ) => {
      const resolve = resolveDeferredModeEnable
      if (!resolve) throw new Error('deferred annotation-mode enable is not pending')
      resolve(result)
      resolveDeferredModeEnable = null
    }
    let deferNextModeDisable = false
    let resolveDeferredModeDisable: ((result: AnnotationModeResult) => void) | null = null
    const finishDeferredModeDisable = (
      result: AnnotationModeResult = { ok: true },
    ) => {
      const resolve = resolveDeferredModeDisable
      if (!resolve) throw new Error('deferred annotation-mode disable is not pending')
      resolve(result)
      resolveDeferredModeDisable = null
    }
    const setMode = vi.fn(async (
      request: Parameters<NonNullable<NativeWorkbenchApi['setArtifactAnnotationMode']>>[0],
    ): Promise<AnnotationModeResult> => {
      if (request.enabled && deferNextModeEnable) {
        deferNextModeEnable = false
        return await new Promise<AnnotationModeResult>((resolve) => {
          resolveDeferredModeEnable = resolve
        })
      }
      if (!request.enabled && deferNextModeDisable) {
        deferNextModeDisable = false
        return await new Promise<AnnotationModeResult>((resolve) => {
          resolveDeferredModeDisable = resolve
        })
      }
      return { ok: true }
    })
    const pushToast = vi.fn()
    const lease = {
      version: 1 as const,
      lease_id: 'apl-annotation',
      effective_mode: 'full' as const,
      launch_url: 'http://p-0123456789abcdef0123456789abcdef.localhost:48721/index.html',
      entrypoint: 'index.html',
      expires_at: '2099-01-01T00:00:00Z',
      preview_origin: 'http://p-0123456789abcdef0123456789abcdef.localhost:48721',
      idle_timeout_seconds: 28_800,
      source: {
        kind: 'single_file' as const,
        collection_status: 'not_applicable' as const,
        file_count: 1,
        total_bytes: 128,
        warning_codes: [],
      },
    }
    const createSurface = vi.fn(async () => ({ ok: true as const }))
    const nativeApi: NativeWorkbenchApi = {
      getCapabilities: vi.fn(async () => ({
        protocolVersions: [3] as Array<3>,
        modes: ['full', 'offline'] as Array<'full' | 'offline'>,
        maxSurfaces: 8,
      })),
      getArtifactAnnotationCapabilities: vi.fn(async () => ({
        version: 3 as const,
        available: true,
        picker: true,
        trustedOverlay: true,
        overlayCopyVersion: 1 as const,
      })),
      setArtifactAnnotationMode: setMode,
      showArtifactAnnotationOverlay: showOverlay,
      closeArtifactAnnotationOverlay: closeOverlay,
      screenshot,
      createArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 201,
        payload: lease,
      })),
      renewArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 200,
        payload: { version: 1 as const, lease_id: lease.lease_id, expires_at: lease.expires_at },
      })),
      revokeArtifactPreviewLease: vi.fn(async () => ({
        ok: true as const,
        status: 204,
        payload: undefined,
      })),
      createSurface,
      setSurfaceRect: vi.fn(async () => ({ ok: true as const })),
      activateSurface: vi.fn(async () => ({ ok: true as const })),
      destroySurface: vi.fn(async () => ({ ok: true as const })),
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const renderState: Record<string, unknown> = {}
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const definition = createArtifactWorkbenchDefinitions({
      artifactDocuments: {
        load: vi.fn(async () => undefined),
        snapshot: vi.fn(() => ({
          key: 'fixture',
          loading: false,
          loaded: true,
          stale: false,
          error: null,
          workspace,
        })),
        headArtifact: vi.fn(() => artifact),
      },
      promptAnnotations: {
        create: createAnnotation,
        update: updateAnnotation,
        discard: discardAnnotation,
        beginOverlayEdit,
        completeOverlayEdit,
        releaseOverlayEdit,
        setActiveDocument,
      },
      authToken: () => 'synthetic-token',
      baseOrigin: 'http://127.0.0.1:18791',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact: vi.fn(),
      platform: {
        id: 'desktop',
        capabilities: { canOpenArtifactsNatively: true },
        files: {},
      } as unknown as Platform,
      previewLeasesEnabled: true,
      pushToast,
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-preview')!
    const runtime = await definition.createRuntime!(item, {
      nativeWorkbenchApi: nativeApi,
      getRenderState: () => renderState,
      updateRenderState: patch => Object.assign(renderState, patch),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    })

    expect(renderState.annotationAvailable).toBe(false)
    workspace.document.capabilities.promptAnnotations = true
    workspace.document.capabilities.manualEdit = true
    workspace.document.capabilities.selectionContext = true
    workspace.document.capabilities.agentEdit = true
    await runtime.resume?.(item)
    await runtime.handleSurfaceRect?.({
      itemId: item.id,
      x: 300,
      y: 40,
      width: 600,
      height: 500,
      visible: true,
    }, item)
    expect(renderState.annotationAvailable).toBe(true)
    await runtime.performAction?.('toggle-annotation-mode', item)
    expect(setMode).toHaveBeenCalledWith({ version: 3, surfaceId: item.id, enabled: true })
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-selected',
      detail: {
        selection: {
          selectionId: 'selection-1',
          tagName: 'button',
          elementPath: '[["","button",1]]',
          elementProofSha256: 'b'.repeat(64),
          domSha256: 'a'.repeat(64),
          rect: { x: 1, y: 2, width: 30, height: 20 },
        },
      },
    }, item)
    expect(createAnnotation).toHaveBeenCalledWith(expect.objectContaining({
      documentId: 'document-1',
      revisionId: 'revision-1',
      selection: {
        selectionId: 'selection-1',
        tagName: 'button',
        elementPath: '[["","button",1]]',
        elementProofSha256: 'b'.repeat(64),
        domSha256: 'a'.repeat(64),
      },
    }))
    expect(showOverlay).toHaveBeenCalledWith(expect.objectContaining({
      version: 3,
      selectionId: 'selection-1',
    }))
    // Desktop already consumed the one-shot picker before emitting the
    // selection. A renderer-side disable here would invalidate selection-1
    // before the create RPC can resolve it.
    expect(setMode).toHaveBeenCalledTimes(1)
    expect(setMode).not.toHaveBeenCalledWith(expect.objectContaining({ enabled: false }))
    expect(screenshot).toHaveBeenCalledWith({ version: 3 })
    expect(screenshot.mock.invocationCallOrder[0]).toBeLessThan(
      showOverlay.mock.invocationCallOrder[0]!,
    )
    expect(createScreenshotUrl).toHaveBeenCalledOnce()
    const annotationId = String(showOverlay.mock.calls[0]?.[0].annotationId || '')
    expect(beginOverlayEdit).toHaveBeenCalledWith(annotationId, 'session-a')
    expect(beginOverlayEdit.mock.invocationCallOrder[0])
      .toBeLessThan(createAnnotation.mock.invocationCallOrder[0]!)
    expect(completeOverlayEdit).not.toHaveBeenCalled()
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-overlay-fallback',
      detail: {
        annotationId,
        reason: 'overlay-crashed',
      },
    }, item)
    expect(renderState.annotationFallback).toMatchObject({
      body: '',
      reason: 'overlay-crashed',
      screenshotUrl: 'blob:frozen-annotation-preview',
    })
    expect(definition.getProps?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: renderState,
    })).toMatchObject({ annotationFallback: expect.any(Object) })

    // A normal close is blocked when the latest trusted-editor body cannot be
    // saved. The draft owner, frozen screenshot, and user text stay available
    // for a later retry.
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-draft-change',
      detail: { annotationId, body: 'Keep this local body.' },
    }, item)
    rejectNextUpdate = new Error('synthetic close flush failure')
    await expect(runtime.beforeClose?.()).resolves.toBe(false)
    expect(renderState.annotationFallback).toMatchObject({
      annotationId,
      body: 'Keep this local body.',
      screenshotUrl: 'blob:frozen-annotation-preview',
    })
    expect(revokeScreenshotUrl).not.toHaveBeenCalled()

    // A required Preview/head rebuild may continue, but it transfers the
    // unsaved body to the Web fallback instead of clearing it. The replacement
    // native Preview stays hidden until this editor is submitted or cancelled.
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-draft-change',
      detail: { annotationId, body: 'Keep this body through refresh.' },
    }, item)
    rejectNextUpdate = new Error('synthetic rebuild flush failure')
    const createsBeforeUnflushedRebuild = createSurface.mock.calls.length
    await runtime.handleComponentEvent?.({ type: 'artifact-head-changed' }, item)
    expect(createSurface).toHaveBeenCalledTimes(createsBeforeUnflushedRebuild + 1)
    expect(renderState.annotationFallback).toMatchObject({
      annotationId,
      body: 'Keep this body through refresh.',
      reason: 'update-pending',
      screenshotUrl: 'blob:frozen-annotation-preview',
    })
    expect(renderState.annotationMode).toBe(true)
    expect(revokeScreenshotUrl).not.toHaveBeenCalled()
    expect(nativeApi.setSurfaceRect).toHaveBeenLastCalledWith(
      expect.objectContaining({ surfaceId: item.id, visible: false }),
    )

    // Native submit/cancel messages are only intents. Empty input and failed
    // persistence must retain ownership of the same visible editor; no native
    // close acknowledgement or hidden picker rearm may happen.
    const closeCountBeforeEmptySubmit = closeOverlay.mock.calls.length
    const updateCountBeforeEmptySubmit = updateAnnotation.mock.calls.length
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-submit',
      detail: { annotationId, body: '   ' },
    }, item)
    expect(updateAnnotation).toHaveBeenCalledTimes(updateCountBeforeEmptySubmit)
    expect(closeOverlay).toHaveBeenCalledTimes(closeCountBeforeEmptySubmit)
    expect(renderState.annotationFallback).toMatchObject({ annotationId })

    rejectNextUpdate = new Error('synthetic update failure')
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-submit',
      detail: { annotationId, body: 'Retry this instruction.' },
    }, item)
    expect(closeOverlay).toHaveBeenCalledTimes(closeCountBeforeEmptySubmit)
    expect(renderState.annotationFallback).toMatchObject({ annotationId })
    expect(pushToast).toHaveBeenCalledWith(
      'workbench.artifactAnnotation.updateFailed',
      { tone: 'danger' },
    )

    // After persistence succeeds, explicit close acknowledges this element's
    // editor and rearms the one-shot picker for another annotation. Only the
    // later accepted chat send may release the toolbar.
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-submit',
      detail: { annotationId, body: 'Make this clearer.' },
    }, item)
    expect(closeOverlay).toHaveBeenLastCalledWith({
      version: 3,
      surfaceId: item.id,
      annotationId,
    })
    expect(setMode.mock.calls.map(([request]) => request.enabled)).toEqual([
      true,
      false,
      true,
    ])
    expect(renderState.annotationMode).toBe(true)
    expect(completeOverlayEdit).toHaveBeenCalledWith(annotationId)
    expect(releaseOverlayEdit).toHaveBeenCalledWith(annotationId)

    // A typed, recoverable selection rejection stays visible and actionable;
    // it must not open an editor or silently release the rearmed one-shot mode.
    expect(renderState.annotationMode).toBe(true)
    rejectNextCreate = Object.assign(new Error('selected element changed'), {
      code: 'ARTIFACT_ELEMENT_CHANGED',
    })
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-selected',
      detail: {
        selection: {
          selectionId: 'selection-dynamic-unrelated',
          tagName: 'button',
          elementPath: '[["","button",1]]',
          elementProofSha256: 'c'.repeat(64),
          domSha256: 'f'.repeat(64),
          rect: { x: 2, y: 3, width: 31, height: 21 },
        },
      },
    }, item)
    expect(showOverlay).toHaveBeenCalledOnce()
    expect(renderState.annotationFallback).toBeNull()
    expect(renderState.annotationMode).toBe(true)
    expect(pushToast).toHaveBeenCalledWith(
      'workbench.artifactAnnotation.elementChanged',
      { tone: 'warn', duration: 12_000 },
    )
    expect(setMode.mock.calls.map(([request]) => request.enabled)).toEqual([
      true,
      false,
      true,
      true,
    ])

    // The recovered picker accepts a local proof after unrelated runtime DOM
    // changes, without a whole-DOM hash or an extra toolbar toggle.
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-selected',
      detail: {
        selection: {
          selectionId: 'selection-2',
          tagName: 'p',
          elementPath: '[["","p",1]]',
          elementProofSha256: 'd'.repeat(64),
          rect: { x: 4, y: 5, width: 32, height: 22 },
        },
      },
    }, item)
    expect(createAnnotation).toHaveBeenCalledTimes(3)
    expect(createAnnotation).toHaveBeenLastCalledWith(expect.objectContaining({
      selection: {
        selectionId: 'selection-2',
        tagName: 'p',
        elementPath: '[["","p",1]]',
        elementProofSha256: 'd'.repeat(64),
      },
    }))
    expect(showOverlay).toHaveBeenCalledTimes(2)

    // If the scoped surface disappears while closing the trusted editor, the
    // persisted draft remains valid. Acknowledge the vanished overlay, rebuild
    // once, and rearm without exposing capability details or making the user
    // select annotation mode again.
    setMode.mockResolvedValueOnce({
      ok: false,
      code: 'PREVIEW_CAPABILITY_EXPIRED',
      retryable: true,
    })
    const secondAnnotationId = String(showOverlay.mock.calls[1]?.[0].annotationId || '')
    const closeCountBeforeDiscardFailure = closeOverlay.mock.calls.length
    rejectNextDiscard = new Error('synthetic discard failure')
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-cancel',
      detail: { annotationId: secondAnnotationId, reason: 'user-cancelled' },
    }, item)
    expect(closeOverlay).toHaveBeenCalledTimes(closeCountBeforeDiscardFailure)
    expect(renderState.annotationFallback).toMatchObject({ annotationId: secondAnnotationId })
    expect(pushToast).toHaveBeenCalledWith(
      'workbench.artifactAnnotation.discardFailed',
      { tone: 'danger', duration: 9000 },
    )
    const createsBeforeCloseRecovery = createSurface.mock.calls.length
    const closeFailureToastCount = pushToast.mock.calls.filter(
      ([message]) => message === 'workbench.artifactAnnotation.closeFailed',
    ).length
    closeOverlay.mockResolvedValueOnce({
      ok: false,
      code: 'PREVIEW_CAPABILITY_EXPIRED',
      retryable: true,
    })
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-cancel',
      detail: { annotationId: secondAnnotationId, reason: 'user-cancelled' },
    }, item)
    const lastDiscardOrder = discardAnnotation.mock.invocationCallOrder[
      discardAnnotation.mock.invocationCallOrder.length - 1
    ]!
    const lastCloseOrder = closeOverlay.mock.invocationCallOrder[
      closeOverlay.mock.invocationCallOrder.length - 1
    ]!
    expect(lastDiscardOrder).toBeLessThan(lastCloseOrder)
    expect(completeOverlayEdit).not.toHaveBeenCalledWith(secondAnnotationId)
    expect(createSurface).toHaveBeenCalledTimes(createsBeforeCloseRecovery + 1)
    expect(renderState.annotationMode).toBe(true)
    expect(pushToast.mock.calls.filter(
      ([message]) => message === 'workbench.artifactAnnotation.closeFailed',
    )).toHaveLength(closeFailureToastCount)
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'ready',
    }, item)
    expect(definition.getToolbarItems?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: renderState,
    }).find(toolbarItem => toolbarItem.id === 'toggle-annotation-mode')).toMatchObject({
      pressed: true,
    })

    const lateSelection = (selectionId: string, proof: string) => ({
      version: 3 as const,
      surfaceId: item.id,
      type: 'annotation-selected' as const,
      detail: {
        selection: {
          selectionId,
          tagName: 'section',
          elementPath: '[["","section",1]]',
          elementProofSha256: proof.repeat(64),
          rect: { x: 8, y: 9, width: 40, height: 24 },
        },
      },
    })

    // A refresh fences a deferred create before its continuation can target
    // the refreshed surface. The late draft is discarded and never rendered.
    await runtime.performAction?.('toggle-annotation-mode', item)
    expect(renderState.annotationMode).toBe(false)
    await runtime.performAction?.('toggle-annotation-mode', item)
    deferNextCreate = true
    resolveDeferredCreate = null
    const reloadCreateIndex = createAnnotation.mock.calls.length
    const pendingReloadCreate = runtime.handleNativeSurfaceEvent?.(
      lateSelection('selection-late-reload', 'e'),
      item,
    )
    await vi.waitFor(() => expect(resolveDeferredCreate).toBeTypeOf('function'))
    const reloadAnnotationId = String(
      createAnnotation.mock.calls[reloadCreateIndex]?.[0].annotationId || '',
    )
    await runtime.performAction?.('refresh', item)
    finishDeferredCreate()
    await pendingReloadCreate
    expect(discardAnnotation).toHaveBeenCalledWith(reloadAnnotationId)
    expect(releaseOverlayEdit).toHaveBeenCalledWith(reloadAnnotationId)
    expect(showOverlay).toHaveBeenCalledTimes(2)
    expect(renderState.annotationFallback).toBeNull()
    expect(renderState.annotationMode).toBe(false)

    // A head replacement invalidates the old selection attempt, then restores
    // the picker on the current page without making the user restart it.
    await runtime.performAction?.('toggle-annotation-mode', item)
    deferNextCreate = true
    resolveDeferredCreate = null
    const headCreateIndex = createAnnotation.mock.calls.length
    const pendingHeadCreate = runtime.handleNativeSurfaceEvent?.(
      lateSelection('selection-late-head', 'f'),
      item,
    )
    await vi.waitFor(() => expect(resolveDeferredCreate).toBeTypeOf('function'))
    const headAnnotationId = String(
      createAnnotation.mock.calls[headCreateIndex]?.[0].annotationId || '',
    )
    workspace.document.headRevisionId = 'revision-2'
    await runtime.handleComponentEvent?.({ type: 'artifact-head-changed' }, item)
    finishDeferredCreate()
    await pendingHeadCreate
    expect(discardAnnotation).toHaveBeenCalledWith(headAnnotationId)
    expect(showOverlay).toHaveBeenCalledTimes(2)
    expect(renderState.annotationFallback).toBeNull()
    expect(renderState.annotationMode).toBe(true)
    expect(setMode).toHaveBeenLastCalledWith({
      version: 3,
      surfaceId: item.id,
      enabled: true,
    })

    // Turning annotation mode off is an immediate intent fence, even while
    // the create RPC is still outstanding. The old completion cannot rearm.
    deferNextCreate = true
    resolveDeferredCreate = null
    const toggleCreateIndex = createAnnotation.mock.calls.length
    const pendingToggleCreate = runtime.handleNativeSurfaceEvent?.(
      lateSelection('selection-late-toggle', '1'),
      item,
    )
    await vi.waitFor(() => expect(resolveDeferredCreate).toBeTypeOf('function'))
    const toggleAnnotationId = String(
      createAnnotation.mock.calls[toggleCreateIndex]?.[0].annotationId || '',
    )
    deferNextModeDisable = true
    const pendingToggleOff = runtime.performAction?.('toggle-annotation-mode', item)
    await vi.waitFor(() => expect(resolveDeferredModeDisable).toBeTypeOf('function'))
    expect(renderState.annotationMode).toBe(true)
    expect(renderState.annotationModeStopping).toBe(true)
    expect(definition.getToolbarItems?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: renderState,
    }).find(toolbarItem => toolbarItem.id === 'toggle-annotation-mode')).toMatchObject({
      disabled: true,
      pressed: true,
    })
    const setModeCountWhileToggleOffPending = setMode.mock.calls.length
    finishDeferredCreate()
    await pendingToggleCreate
    expect(discardAnnotation).toHaveBeenCalledWith(toggleAnnotationId)
    expect(showOverlay).toHaveBeenCalledTimes(2)
    expect(setMode).toHaveBeenCalledTimes(setModeCountWhileToggleOffPending)
    expect(renderState.annotationFallback).toBeNull()
    expect(renderState.annotationMode).toBe(true)
    finishDeferredModeDisable()
    await pendingToggleOff
    expect(renderState.annotationMode).toBe(false)
    expect(renderState.annotationModeStopping).toBe(false)

    // A rejected native stop is not reported as success. Renderer intent is
    // still fenced immediately, so late native selections cannot create a
    // draft, and the next toolbar click retries disable rather than enabling.
    await runtime.performAction?.('toggle-annotation-mode', item)
    deferNextModeDisable = true
    const pendingRejectedStop = runtime.performAction?.('toggle-annotation-mode', item)
    await vi.waitFor(() => expect(resolveDeferredModeDisable).toBeTypeOf('function'))
    const createCountBeforeRejectedStopSelection = createAnnotation.mock.calls.length
    await runtime.handleNativeSurfaceEvent?.(
      lateSelection('selection-after-stop-intent', '9'),
      item,
    )
    expect(createAnnotation).toHaveBeenCalledTimes(createCountBeforeRejectedStopSelection)
    finishDeferredModeDisable({ ok: false, message: 'native stop rejected' })
    await pendingRejectedStop
    expect(renderState.annotationMode).toBe(true)
    expect(renderState.annotationModeStopping).toBe(false)
    expect(definition.getToolbarItems?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: renderState,
    }).find(toolbarItem => toolbarItem.id === 'toggle-annotation-mode')).toMatchObject({
      disabled: false,
      pressed: true,
    })
    expect(pushToast).toHaveBeenCalledWith(
      'workbench.artifactAnnotation.unavailable',
      { tone: 'danger' },
    )
    expect(pushToast.mock.calls.flat().join(' ')).not.toContain('native stop rejected')
    const setModeCountBeforeStopRetry = setMode.mock.calls.length
    await runtime.performAction?.('toggle-annotation-mode', item)
    expect(setMode).toHaveBeenCalledTimes(setModeCountBeforeStopRetry + 1)
    expect(setMode).toHaveBeenLastCalledWith({
      version: 3,
      surfaceId: item.id,
      enabled: false,
    })
    expect(renderState.annotationMode).toBe(false)

    // The second fence, after screenshot capture, closes the same race window:
    // a refresh cannot let the completed screenshot continue into an overlay.
    await runtime.performAction?.('toggle-annotation-mode', item)
    deferNextScreenshot = true
    resolveDeferredScreenshot = null
    const screenshotCreateIndex = createAnnotation.mock.calls.length
    const pendingScreenshot = runtime.handleNativeSurfaceEvent?.(
      lateSelection('selection-late-screenshot', '2'),
      item,
    )
    await vi.waitFor(() => expect(resolveDeferredScreenshot).toBeTypeOf('function'))
    const screenshotAnnotationId = String(
      createAnnotation.mock.calls[screenshotCreateIndex]?.[0].annotationId || '',
    )
    await runtime.performAction?.('refresh', item)
    finishDeferredScreenshot()
    await pendingScreenshot
    expect(discardAnnotation).toHaveBeenCalledWith(screenshotAnnotationId)
    expect(showOverlay).toHaveBeenCalledTimes(2)
    expect(renderState.annotationFallback).toBeNull()
    expect(renderState.annotationMode).toBe(false)

    // Reselect keeps the original draft until the new trusted fallback has
    // taken over. A mode change while old-draft cleanup is pending must never
    // discard the replacement too.
    await runtime.handleComponentEvent?.({
      type: 'artifact-prompt-annotation-reselect',
      payload: { annotationId: 'annotation-stale-old', body: 'Keep this instruction.' },
    }, item)
    deferNextDiscard = true
    resolveDeferredDiscard = null
    const replacementCreateIndex = createAnnotation.mock.calls.length
    const pendingReplacement = runtime.handleNativeSurfaceEvent?.(
      lateSelection('selection-replacement', '3'),
      item,
    )
    await vi.waitFor(() => expect(resolveDeferredDiscard).toBeTypeOf('function'))
    const replacementAnnotationId = String(
      createAnnotation.mock.calls[replacementCreateIndex]?.[0].annotationId || '',
    )
    expect(renderState.annotationFallback).toMatchObject({
      annotationId: replacementAnnotationId,
    })
    await runtime.performAction?.('toggle-annotation-mode', item)
    finishDeferredDiscard()
    await pendingReplacement
    expect(discardAnnotation).toHaveBeenCalledWith('annotation-stale-old')
    expect(discardAnnotation).not.toHaveBeenCalledWith(replacementAnnotationId)
    expect(renderState.annotationFallback).toMatchObject({
      annotationId: replacementAnnotationId,
    })
    expect(renderState.annotationMode).toBe(false)

    // Before takeover, invalidation has the opposite ownership rule: retain
    // the old stale draft and discard only the uncommitted replacement.
    workspace.document.headRevisionId = 'revision-3'
    await runtime.handleComponentEvent?.({ type: 'artifact-head-changed' }, item)
    await runtime.handleComponentEvent?.({
      type: 'artifact-prompt-annotation-reselect',
      payload: { annotationId: 'annotation-stale-preserved', body: 'Preserve me.' },
    }, item)
    deferNextScreenshot = true
    resolveDeferredScreenshot = null
    const uncommittedCreateIndex = createAnnotation.mock.calls.length
    const pendingUncommitted = runtime.handleNativeSurfaceEvent?.(
      lateSelection('selection-uncommitted-replacement', '4'),
      item,
    )
    await vi.waitFor(() => expect(resolveDeferredScreenshot).toBeTypeOf('function'))
    const uncommittedAnnotationId = String(
      createAnnotation.mock.calls[uncommittedCreateIndex]?.[0].annotationId || '',
    )
    await runtime.performAction?.('refresh', item)
    finishDeferredScreenshot()
    await pendingUncommitted
    expect(discardAnnotation).toHaveBeenCalledWith(uncommittedAnnotationId)
    expect(discardAnnotation).not.toHaveBeenCalledWith('annotation-stale-preserved')
    expect(renderState.annotationFallback).toBeNull()

    // A submit whose update finishes after a head reload cannot close or clear
    // the newer overlay created on the replacement surface.
    await runtime.performAction?.('toggle-annotation-mode', item)
    const oldSubmitCreateIndex = createAnnotation.mock.calls.length
    await runtime.handleNativeSurfaceEvent?.(
      lateSelection('selection-old-submit', '5'),
      item,
    )
    const oldSubmitAnnotationId = String(
      createAnnotation.mock.calls[oldSubmitCreateIndex]?.[0].annotationId || '',
    )
    deferNextUpdate = true
    resolveDeferredUpdate = null
    const pendingOldSubmit = runtime.handleComponentEvent?.({
      type: 'artifact-annotation-fallback-submit',
      payload: { annotationId: oldSubmitAnnotationId, body: 'Updated old overlay.' },
    }, item)
    await vi.waitFor(() => expect(resolveDeferredUpdate).toBeTypeOf('function'))
    workspace.document.headRevisionId = 'revision-4'
    await runtime.handleComponentEvent?.({ type: 'artifact-head-changed' }, item)
    expect(renderState.annotationMode).toBe(true)
    const newSubmitCreateIndex = createAnnotation.mock.calls.length
    await runtime.handleNativeSurfaceEvent?.(
      lateSelection('selection-new-after-submit', '6'),
      item,
    )
    const newSubmitAnnotationId = String(
      createAnnotation.mock.calls[newSubmitCreateIndex]?.[0].annotationId || '',
    )
    const closeCountBeforeLateSubmit = closeOverlay.mock.calls.length
    finishDeferredUpdate()
    await pendingOldSubmit
    expect(closeOverlay).toHaveBeenCalledTimes(closeCountBeforeLateSubmit)
    expect(releaseOverlayEdit).toHaveBeenCalledWith(oldSubmitAnnotationId)
    expect(completeOverlayEdit).not.toHaveBeenCalledWith(oldSubmitAnnotationId)
    expect(completeOverlayEdit).not.toHaveBeenCalledWith(newSubmitAnnotationId)
    expect(renderState.annotationFallback).toMatchObject({
      annotationId: newSubmitAnnotationId,
    })

    // The same identity fence protects a newer overlay from a late cancel
    // whose discard RPC completes after another head reload.
    deferNextDiscard = true
    resolveDeferredDiscard = null
    const pendingOldCancel = runtime.handleComponentEvent?.({
      type: 'artifact-annotation-fallback-cancel',
      payload: { annotationId: newSubmitAnnotationId },
    }, item)
    await vi.waitFor(() => expect(resolveDeferredDiscard).toBeTypeOf('function'))
    workspace.document.headRevisionId = 'revision-5'
    await runtime.handleComponentEvent?.({ type: 'artifact-head-changed' }, item)
    expect(renderState.annotationMode).toBe(true)
    const newCancelCreateIndex = createAnnotation.mock.calls.length
    await runtime.handleNativeSurfaceEvent?.(
      lateSelection('selection-new-after-cancel', '7'),
      item,
    )
    const newCancelAnnotationId = String(
      createAnnotation.mock.calls[newCancelCreateIndex]?.[0].annotationId || '',
    )
    const closeCountBeforeLateCancel = closeOverlay.mock.calls.length
    finishDeferredDiscard()
    await pendingOldCancel
    expect(closeOverlay).toHaveBeenCalledTimes(closeCountBeforeLateCancel)
    expect(renderState.annotationFallback).toMatchObject({
      annotationId: newCancelAnnotationId,
    })

    // A native close rejection is not success: keep the matching editor state
    // intact and do not rearm a picker behind it.
    closeOverlay.mockResolvedValueOnce({ ok: false })
    const setModeCountBeforeCloseFailure = setMode.mock.calls.length
    await runtime.handleComponentEvent?.({
      type: 'artifact-annotation-fallback-submit',
      payload: { annotationId: newCancelAnnotationId, body: 'Keep editor open.' },
    }, item)
    expect(renderState.annotationFallback).toMatchObject({
      annotationId: newCancelAnnotationId,
    })
    expect(setMode).toHaveBeenCalledTimes(setModeCountBeforeCloseFailure)
    expect(pushToast).toHaveBeenCalledWith(
      'workbench.artifactAnnotation.closeFailed',
      { tone: 'danger', duration: 9000 },
    )

    // Discard is terminal, so if the native acknowledgement itself fails the
    // next cancel retries only that close rather than discarding twice or
    // trying to resurrect the draft through submit.
    const discardCountBeforeCloseRetry = discardAnnotation.mock.calls.length
    closeOverlay.mockResolvedValueOnce({ ok: false })
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-cancel',
      detail: { annotationId: newCancelAnnotationId, reason: 'user-cancelled' },
    }, item)
    expect(discardAnnotation).toHaveBeenCalledTimes(discardCountBeforeCloseRetry + 1)
    expect(renderState.annotationFallback).toMatchObject({
      annotationId: newCancelAnnotationId,
    })
    await runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-cancel',
      detail: { annotationId: newCancelAnnotationId, reason: 'user-cancelled' },
    }, item)
    expect(discardAnnotation).toHaveBeenCalledTimes(discardCountBeforeCloseRetry + 1)
    expect(renderState.annotationFallback).toBeNull()

    // If neither create nor the authoritative draft refetch can establish the
    // outcome, keep the one-shot picker suspended. Rearming here would mint a
    // stream of new IDs while the original durable draft may already exist.
    rejectNextCreate = Object.assign(new Error('create outcome is unknown'), {
      code: 'ARTIFACT_ANNOTATION_CREATE_AMBIGUOUS',
    })
    const setModeCountBeforeAmbiguousCreate = setMode.mock.calls.length
    const discardCountBeforeAmbiguousCreate = discardAnnotation.mock.calls.length
    await runtime.handleNativeSurfaceEvent?.(
      lateSelection('selection-ambiguous-create', '8'),
      item,
    )
    expect(setMode).toHaveBeenCalledTimes(setModeCountBeforeAmbiguousCreate)
    expect(discardAnnotation).toHaveBeenCalledTimes(discardCountBeforeAmbiguousCreate)
    expect(renderState.annotationFallback).toBeNull()
    expect(renderState.annotationMode).toBe(true)

    // Once the Gateway has accepted the prompt annotations, the renderer must
    // release the native picker instead of leaving the toolbar visibly pressed.
    // A slower rearm from the just-closed element editor must not resurrect the
    // mode after that newer accepted-send intent wins.
    await runtime.performAction?.('toggle-annotation-mode', item)
    await runtime.performAction?.('toggle-annotation-mode', item)
    const overlayCallsBeforeRendererRecovery = showOverlay.mock.calls.length
    const fallbackToastsBeforeRendererRecovery = pushToast.mock.calls.filter(
      ([message]) => message === 'workbench.artifactAnnotation.overlayFallback',
    ).length
    showOverlay
      .mockResolvedValueOnce({
        ok: false,
        code: 'PREVIEW_RENDERER_FAILED',
        retryable: true,
      })
      .mockResolvedValueOnce({ ok: true })
    await runtime.handleNativeSurfaceEvent?.(
      lateSelection('selection-acceptance-race', 'a'),
      item,
    )
    expect(showOverlay).toHaveBeenCalledTimes(overlayCallsBeforeRendererRecovery + 2)
    expect(pushToast.mock.calls.filter(
      ([message]) => message === 'workbench.artifactAnnotation.overlayFallback',
    )).toHaveLength(fallbackToastsBeforeRendererRecovery)
    expect(renderState.annotationFallback).toBeNull()
    const acceptanceRaceAnnotationId = String(
      showOverlay.mock.calls[showOverlay.mock.calls.length - 1]?.[0].annotationId || '',
    )
    deferNextModeEnable = true
    const pendingAcceptedSubmit = runtime.handleNativeSurfaceEvent?.({
      version: 3,
      surfaceId: item.id,
      type: 'annotation-submit',
      detail: {
        annotationId: acceptanceRaceAnnotationId,
        body: 'Keep selection active only until this batch is accepted.',
      },
    }, item)
    await vi.waitFor(() => expect(resolveDeferredModeEnable).toBeTypeOf('function'))
    await runtime.handleComponentEvent?.({
      type: 'artifact-prompt-annotations-accepted',
      payload: { acceptedIds: ['annotation-accepted'] },
    }, item)
    finishDeferredModeEnable()
    await pendingAcceptedSubmit
    expect(setMode).toHaveBeenLastCalledWith({
      version: 3,
      surfaceId: item.id,
      enabled: false,
    })
    expect(renderState.annotationMode).toBe(false)

    await runtime.dispose?.('closed')
    expect(revokeScreenshotUrl).toHaveBeenCalledWith('blob:frozen-annotation-preview')
    createScreenshotUrl.mockRestore()
    revokeScreenshotUrl.mockRestore()
  })

  it('routes collection selections to a preview without losing the full list', async () => {
    const openArtifact = vi.fn()
    const item = createArtifactCollectionWorkbenchItem({
      artifacts: [artifact],
      sessionKey: 'session-a',
      title: 'Deliverables (1)',
    })
    const definition = createArtifactWorkbenchDefinitions({
      authToken: () => '',
      baseOrigin: 'http://localhost',
      confirmRemoteResources: vi.fn(async () => true),
      currentSessionId: () => 'session-a',
      openArtifact,
      platform: {
        capabilities: { canOpenArtifactsNatively: false },
        files: {},
      } as unknown as Platform,
      pushToast: vi.fn(),
      t: key => key,
    }).find(candidate => candidate.kind === 'artifact-collection')!
    const context: WorkbenchRuntimeContext = {
      getRenderState: () => ({}),
      updateRenderState: vi.fn(),
      isItemOpen: () => true,
      setExpanded: vi.fn(),
      reportError: vi.fn(),
    }
    const runtime = await definition.createRuntime!(item, context)

    await runtime.handleComponentEvent?.({
      type: 'artifact-open',
      payload: artifact,
    }, item)

    expect(openArtifact).toHaveBeenCalledWith(artifact, 'session-a', [artifact])
    expect(definition.getProps?.(item, {
      active: true,
      hostAvailable: true,
      nativeSurface: false,
      runtimeState: {},
    })).toMatchObject({ artifacts: [artifact] })
  })
})
