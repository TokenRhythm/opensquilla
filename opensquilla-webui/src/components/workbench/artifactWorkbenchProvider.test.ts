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
