import { describe, expect, it, vi } from 'vitest'
import type {
  NativeWorkbenchApi,
  Platform,
} from '@/platform/types'
import type { ArtifactPayload } from '@/types/rpc'
import {
  createArtifactCollectionWorkbenchItem,
  createArtifactPreviewWorkbenchItem,
} from '@/workbench/artifactItems'
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
      createSurface: vi.fn(async () => ({ ok: true })),
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

  it('owns native surface actions, events, visibility, and render state', async () => {
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
    await runtime.setComponentHandle?.({ reload })
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

    await runtime.handleNativeSurfaceEvent?.({
      version: 1,
      surfaceId: item.id,
      type: 'error',
    }, item)
    expect(renderState.nativeSurfaceState).toBe('error')
    expect(setSurfaceRect).toHaveBeenLastCalledWith(
      expect.objectContaining({ surfaceId: item.id, visible: false }),
    )

    await runtime.handleComponentEvent?.({
      type: 'native-html-ready',
      payload: nativeResource,
    }, item)
    await runtime.handleNativeSurfaceEvent?.({
      version: 1,
      surfaceId: item.id,
      type: 'crashed',
      detail: { reason: 'unresponsive' },
    }, item)
    expect(renderState.nativeSurfaceState).toBe('crashed')
    expect(destroySurface).toHaveBeenLastCalledWith(item.id)
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
    expect(harness.renderState.nativeSurfaceState).toBe('error')
    expect(destroySurface).toHaveBeenCalledTimes(2)

    await harness.runtime.handleComponentEvent?.({
      type: 'preview-state-change',
      payload: 'unsupported',
    }, harness.item)
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
      previewLeaseError: 'Artifact integrity check failed.',
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
      previewErrorMessage: 'Artifact integrity check failed.',
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
