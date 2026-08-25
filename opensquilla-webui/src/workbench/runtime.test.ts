import { describe, expect, it, vi } from 'vitest'
import {
  WorkbenchPanelRegistry,
  WorkbenchRuntimeManager,
} from './runtime'
import type { NativeWorkbenchApi } from '@/platform/types'
import type {
  WorkbenchItem,
  WorkbenchPanelRuntime,
  WorkbenchRuntimeContext,
} from './types'

function item(
  id: string,
  retention: WorkbenchItem['retention'] = 'keep-alive',
): WorkbenchItem {
  return {
    id,
    kind: 'artifact-preview',
    title: id,
    scope: { type: 'session', id: 'session' },
    hostKind: 'dom',
    retention,
    payload: {},
  }
}

describe('workbench runtime registry', () => {
  it('registers one provider per panel kind and supports removal', () => {
    const registry = new WorkbenchPanelRegistry()
    const definition = { kind: 'artifact-preview' as const }
    const unregister = registry.register(definition)

    expect(registry.resolve(item('a'))).toBe(definition)
    expect(() => registry.register(definition)).toThrow(/already registered/)
    unregister()
    expect(registry.resolve(item('a'))).toBeNull()
  })

  it('allows application built-ins to be replaced without widening the host switch', () => {
    const registry = new WorkbenchPanelRegistry()
    const first = { kind: 'artifact-preview' as const }
    const replacement = {
      kind: 'artifact-preview' as const,
      getProps: () => ({ presentation: 'replacement' }),
    }
    registry.register(first)
    registry.register(replacement, { replace: true })

    expect(registry.resolve(item('a'))).toBe(replacement)
    expect(registry.resolve(item('a'))?.getProps?.(item('a'), {
      active: true,
      hostAvailable: true,
      nativeSurface: false,
      runtimeState: {},
    })).toEqual({ presentation: 'replacement' })
  })

  it('keeps runtime resources outside descriptors and cleans them on close', async () => {
    const registry = new WorkbenchPanelRegistry()
    const runtime: WorkbenchPanelRuntime = {
      activate: vi.fn(),
      resume: vi.fn(),
      suspend: vi.fn(),
      dispose: vi.fn(),
    }
    const createRuntime = vi.fn(() => runtime)
    registry.register({ kind: 'artifact-preview', createRuntime })
    const manager = new WorkbenchRuntimeManager(registry)
    const descriptor = item('a')

    manager.handle({ type: 'open', item: descriptor })
    manager.handle({ type: 'activate', item: descriptor })
    manager.handle({ type: 'resume', item: descriptor })
    manager.handle({ type: 'suspend', item: descriptor })
    manager.handle({ type: 'dispose', item: descriptor, reason: 'closed' })
    await manager.flush()

    expect(createRuntime).toHaveBeenCalledOnce()
    expect(runtime.activate).toHaveBeenCalledOnce()
    expect(runtime.resume).toHaveBeenCalledOnce()
    expect(runtime.suspend).toHaveBeenCalledOnce()
    expect(runtime.dispose).toHaveBeenCalledWith('closed')
    expect(manager.hasRuntime('a')).toBe(false)
  })

  it('lets a live panel reject removal before the store drops its descriptor', async () => {
    const registry = new WorkbenchPanelRegistry()
    const beforeClose = vi.fn(async () => false)
    registry.register({
      kind: 'artifact-preview',
      createRuntime: () => ({ beforeClose }),
    })
    const manager = new WorkbenchRuntimeManager(registry)
    const descriptor = item('dirty')
    manager.handle({ type: 'open', item: descriptor })
    await manager.flush()

    await expect(manager.beforeClose(descriptor)).resolves.toBe(false)
    expect(beforeClose).toHaveBeenCalledOnce()
    expect(manager.hasRuntime(descriptor.id)).toBe(true)
  })

  it('forwards a preserve-runtime flush without disposing the live panel', async () => {
    const registry = new WorkbenchPanelRegistry()
    const beforeClose = vi.fn(async () => true)
    registry.register({
      kind: 'artifact-preview',
      createRuntime: () => ({ beforeClose }),
    })
    const manager = new WorkbenchRuntimeManager(registry)
    const descriptor = item('send-flush')
    manager.handle({ type: 'open', item: descriptor })
    await manager.flush()

    await expect(manager.beforeClose(descriptor, { preserveRuntime: true }))
      .resolves.toBe(true)
    expect(beforeClose).toHaveBeenCalledWith({ preserveRuntime: true })
    expect(manager.hasRuntime(descriptor.id)).toBe(true)
  })

  it('recreates dispose-on-suspend resources when resumed', async () => {
    const registry = new WorkbenchPanelRegistry()
    const runtimes: WorkbenchPanelRuntime[] = []
    registry.register({
      kind: 'artifact-preview',
      createRuntime: () => {
        const runtime = { dispose: vi.fn(), resume: vi.fn() }
        runtimes.push(runtime)
        return runtime
      },
    })
    const manager = new WorkbenchRuntimeManager(registry)
    const descriptor = item('a', 'dispose-on-suspend')

    manager.handle({ type: 'open', item: descriptor })
    manager.handle({ type: 'suspend', item: descriptor })
    manager.handle({ type: 'resume', item: descriptor })
    await manager.flush()

    expect(runtimes).toHaveLength(2)
    expect(runtimes[0]?.dispose).toHaveBeenCalledWith('suspended')
    expect(runtimes[1]?.resume).toHaveBeenCalledOnce()
  })

  it('contains provider failures and reports the affected descriptor', async () => {
    const registry = new WorkbenchPanelRegistry()
    const failure = new Error('failed')
    registry.register({
      kind: 'artifact-preview',
      createRuntime: () => {
        throw failure
      },
    })
    const onError = vi.fn()
    const manager = new WorkbenchRuntimeManager(registry, { onError })
    const descriptor = item('a')

    manager.handle({ type: 'open', item: descriptor })
    await manager.flush()

    expect(onError).toHaveBeenCalledWith(failure, descriptor)
  })

  it('fails closed for every stale provider input after an item is disposed', async () => {
    const registry = new WorkbenchPanelRegistry()
    const createRuntime = vi.fn(() => ({ dispose: vi.fn() }))
    registry.register({ kind: 'artifact-preview', createRuntime })
    const manager = new WorkbenchRuntimeManager(registry)
    const descriptor = item('closed')

    manager.handle({ type: 'open', item: descriptor })
    await manager.flush()
    expect(createRuntime).toHaveBeenCalledOnce()

    manager.handle({ type: 'dispose', item: descriptor, reason: 'closed' })
    manager.setComponentHandle(descriptor, { stale: true })
    manager.handleComponentEvent(descriptor, { type: 'stale-component-event' })
    manager.handleSurfaceRect({
      itemId: descriptor.id,
      x: 0,
      y: 0,
      width: 100,
      height: 100,
      visible: false,
    })
    manager.handleNativeSurfaceEvent({
      version: 1,
      surfaceId: descriptor.id,
      type: 'ready',
    })
    manager.performAction(descriptor, 'stale-action')
    await manager.flush()

    expect(createRuntime).toHaveBeenCalledOnce()
    expect(manager.hasRuntime(descriptor.id)).toBe(false)
  })

  it('replays a surface rect that arrives before its descriptor opens', async () => {
    const registry = new WorkbenchPanelRegistry()
    const handleSurfaceRect = vi.fn()
    registry.register({
      kind: 'artifact-preview',
      createRuntime: () => ({ handleSurfaceRect }),
    })
    const manager = new WorkbenchRuntimeManager(registry)
    const descriptor = item('late-open')
    const rect = {
      itemId: descriptor.id,
      x: 480,
      y: 70,
      width: 600,
      height: 480,
      visible: true,
    }

    manager.handleSurfaceRect(rect)
    expect(handleSurfaceRect).not.toHaveBeenCalled()

    manager.handle({ type: 'open', item: descriptor })
    await manager.flush()

    expect(handleSurfaceRect).toHaveBeenCalledOnce()
    expect(handleSurfaceRect).toHaveBeenCalledWith(rect, descriptor)
  })

  it('keeps a reopened descriptor with the same id reachable after close', async () => {
    const registry = new WorkbenchPanelRegistry()
    const runtimes: Array<Required<Pick<
      WorkbenchPanelRuntime,
      'handleNativeSurfaceEvent' | 'handleSurfaceRect'
    >>> = []
    const createRuntime = vi.fn(() => {
      const runtime = {
        handleNativeSurfaceEvent: vi.fn(),
        handleSurfaceRect: vi.fn(),
      }
      runtimes.push(runtime)
      return runtime
    })
    registry.register({ kind: 'artifact-preview', createRuntime })
    const manager = new WorkbenchRuntimeManager(registry)
    const first = item('same-id')
    const reopened = { ...first, title: 'reopened.html' }

    manager.handle({ type: 'open', item: first })
    await manager.flush()
    manager.handle({ type: 'dispose', item: first, reason: 'closed' })
    manager.handle({ type: 'open', item: reopened })
    await manager.flush()

    manager.handleSurfaceRect({
      itemId: reopened.id,
      x: 1,
      y: 2,
      width: 300,
      height: 200,
      visible: true,
    })
    manager.handleNativeSurfaceEvent({
      version: 1,
      surfaceId: reopened.id,
      type: 'ready',
    })
    await manager.flush()

    expect(createRuntime).toHaveBeenCalledTimes(2)
    expect(manager.hasRuntime(reopened.id)).toBe(true)
    expect(runtimes[1]?.handleSurfaceRect).toHaveBeenCalledWith(
      expect.objectContaining({ visible: true }),
      reopened,
    )
    expect(runtimes[1]?.handleNativeSurfaceEvent).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'ready' }),
      reopened,
    )
  })

  it('rejects old descriptor component and action input after same-id reopen', async () => {
    const registry = new WorkbenchPanelRegistry()
    const setComponentHandle = vi.fn()
    const handleComponentEvent = vi.fn()
    const performAction = vi.fn()
    registry.register({
      kind: 'artifact-preview',
      createRuntime: () => ({
        setComponentHandle,
        handleComponentEvent,
        performAction,
      }),
    })
    const manager = new WorkbenchRuntimeManager(registry)
    const first = item('same-id-input')
    const reopened = { ...first, title: 'new descriptor' }
    manager.handle({ type: 'open', item: first })
    await manager.flush()
    manager.handle({ type: 'dispose', item: first, reason: 'closed' })
    manager.handle({ type: 'open', item: reopened })
    await manager.flush()
    setComponentHandle.mockClear()
    handleComponentEvent.mockClear()
    performAction.mockClear()

    manager.setComponentHandle(first, { stale: true })
    manager.handleComponentEvent(first, { type: 'stale-event' })
    manager.performAction(first, 'stale-action')
    await manager.flush()
    expect(setComponentHandle).not.toHaveBeenCalled()
    expect(handleComponentEvent).not.toHaveBeenCalled()
    expect(performAction).not.toHaveBeenCalled()

    manager.setComponentHandle(reopened, { current: true })
    manager.handleComponentEvent(reopened, { type: 'current-event' })
    manager.performAction(reopened, 'current-action')
    await manager.flush()
    expect(setComponentHandle).toHaveBeenCalledWith({ current: true })
    expect(handleComponentEvent).toHaveBeenCalledWith(
      { type: 'current-event' },
      reopened,
    )
    expect(performAction).toHaveBeenCalledWith('current-action', reopened)
  })

  it('hides native surfaces immediately while a queued action is pending', async () => {
    const registry = new WorkbenchPanelRegistry()
    const pendingCreateResolvers: Array<() => void> = []
    const performAction = vi.fn()
    const setSurfaceRect = vi.fn(async () => ({ ok: true }))
    const activateSurface = vi.fn(async () => ({ ok: true }))
    const nativeApi: NativeWorkbenchApi = {
      createSurface: vi.fn(() => new Promise<{ ok: boolean }>(resolve => {
        pendingCreateResolvers.push(() => resolve({ ok: true }))
      })),
      setSurfaceRect,
      activateSurface,
      destroySurface: vi.fn(async () => ({ ok: true })),
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const manager = new WorkbenchRuntimeManager(registry, {
      nativeWorkbenchApi: nativeApi,
    })
    const descriptor: WorkbenchItem = {
      ...item('blocked-native'),
      kind: 'browser',
      hostKind: 'native-webcontents',
    }
    registry.register({
      kind: 'browser',
      createRuntime: (_item, context) => ({
        performAction: vi.fn(async () => {
          performAction()
          await context.nativeWorkbenchApi?.createSurface({
            version: 1,
            surfaceId: descriptor.id,
            kind: 'artifact-html',
            payload: {
              data: new ArrayBuffer(0),
              name: 'preview.html',
              mime: 'text/html',
              scopeId: 'session',
              allowRemoteResources: false,
            },
          })
          await context.nativeWorkbenchApi?.setSurfaceRect({
            surfaceId: descriptor.id,
            x: 20,
            y: 30,
            width: 700,
            height: 500,
            visible: true,
          })
          await context.nativeWorkbenchApi?.activateSurface(descriptor.id)
        }),
      }),
    })
    manager.handle({ type: 'open', item: descriptor })
    manager.handle({ type: 'resume', item: descriptor })
    await manager.flush()
    manager.handleSurfaceRect({
      itemId: descriptor.id,
      x: 20,
      y: 30,
      width: 700,
      height: 500,
      visible: true,
    })
    await manager.flush()

    manager.performAction(descriptor, 'long-running')
    await vi.waitFor(() => expect(pendingCreateResolvers).toHaveLength(1))
    manager.handle({ type: 'suspend', item: descriptor })
    expect(setSurfaceRect).toHaveBeenLastCalledWith(
      expect.objectContaining({ surfaceId: descriptor.id, visible: false }),
    )
    pendingCreateResolvers.shift()?.()
    await manager.flush()
    expect(setSurfaceRect).toHaveBeenLastCalledWith(
      expect.objectContaining({ surfaceId: descriptor.id, visible: false }),
    )
    expect(activateSurface).not.toHaveBeenCalled()

    manager.handle({ type: 'resume', item: descriptor })
    await manager.flush()
    manager.performAction(descriptor, 'long-running-again')
    await vi.waitFor(() => expect(pendingCreateResolvers).toHaveLength(1))
    manager.handle({ type: 'dispose', item: descriptor, reason: 'closed' })
    expect(setSurfaceRect).toHaveBeenLastCalledWith(
      expect.objectContaining({ surfaceId: descriptor.id, visible: false }),
    )
    pendingCreateResolvers.shift()?.()
    await manager.flush()
    expect(setSurfaceRect).toHaveBeenLastCalledWith(
      expect.objectContaining({ surfaceId: descriptor.id, visible: false }),
    )
    expect(activateSurface).not.toHaveBeenCalled()
    expect(manager.hasRuntime(descriptor.id)).toBe(false)
  })

  it('revokes a pending native surface before disposeAll waits for its queue', async () => {
    const createControl: {
      resolve: ((result: { ok: boolean }) => void) | null
    } = { resolve: null }
    const createSurface = vi.fn(() => new Promise<{ ok: boolean }>(resolve => {
      createControl.resolve = resolve
    }))
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
    const registry = new WorkbenchPanelRegistry()
    const descriptor: WorkbenchItem = {
      ...item('detach-pending-native'),
      kind: 'browser',
      hostKind: 'native-webcontents',
    }
    registry.register({
      kind: 'browser',
      createRuntime: (_item, context) => ({
        async performAction() {
          await context.nativeWorkbenchApi?.createSurface({
            version: 1,
            surfaceId: descriptor.id,
            kind: 'artifact-html',
            payload: {
              data: new ArrayBuffer(0),
              name: 'preview.html',
              mime: 'text/html',
              scopeId: 'session',
              allowRemoteResources: false,
            },
          })
          await context.nativeWorkbenchApi?.setSurfaceRect({
            surfaceId: descriptor.id,
            x: 20,
            y: 30,
            width: 700,
            height: 500,
            visible: true,
          })
          await context.nativeWorkbenchApi?.activateSurface(descriptor.id)
        },
      }),
    })
    const manager = new WorkbenchRuntimeManager(registry, {
      nativeWorkbenchApi: nativeApi,
    })
    manager.handle({ type: 'open', item: descriptor })
    manager.handle({ type: 'resume', item: descriptor })
    await manager.flush()
    manager.handleSurfaceRect({
      itemId: descriptor.id,
      x: 20,
      y: 30,
      width: 700,
      height: 500,
      visible: true,
    })
    await manager.flush()

    manager.performAction(descriptor, 'create-preview')
    await vi.waitFor(() => expect(createSurface).toHaveBeenCalledOnce())
    const disposing = manager.disposeAll()
    expect(setSurfaceRect).toHaveBeenLastCalledWith(
      expect.objectContaining({ surfaceId: descriptor.id, visible: false }),
    )

    createControl.resolve?.({ ok: true })
    await disposing

    expect(destroySurface).toHaveBeenCalledWith(descriptor.id)
    expect(activateSurface).not.toHaveBeenCalled()
    expect(setSurfaceRect).not.toHaveBeenCalledWith(
      expect.objectContaining({ surfaceId: descriptor.id, visible: true }),
    )
    expect(manager.hasRuntime(descriptor.id)).toBe(false)
  })

  it('forwards generic native provider inputs without a host kind switch', async () => {
    const registry = new WorkbenchPanelRegistry()
    const setComponentHandle = vi.fn()
    const handleComponentEvent = vi.fn()
    const handleSurfaceRect = vi.fn()
    const handleNativeSurfaceEvent = vi.fn()
    const performAction = vi.fn()
    const runtimeContext: { current: WorkbenchRuntimeContext | null } = {
      current: null,
    }
    const nativeApi: NativeWorkbenchApi = {
      createSurface: vi.fn(async () => ({ ok: true })),
      setSurfaceRect: vi.fn(async () => ({ ok: true })),
      activateSurface: vi.fn(async () => ({ ok: true })),
      destroySurface: vi.fn(async () => ({ ok: true })),
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const setExpanded = vi.fn()
    const descriptor: WorkbenchItem = {
      ...item('future-native'),
      kind: 'browser',
      hostKind: 'native-webcontents',
    }
    registry.register({
      kind: 'browser',
      getToolbarItems: (_item, state) => [{
        kind: 'action',
        id: 'take-control',
        icon: 'monitor',
        label: String(state.runtimeState.controlLabel || 'Take control'),
      }],
      createRuntime: (_item, context) => {
        runtimeContext.current = context
        return {
          setComponentHandle,
          handleComponentEvent,
          handleSurfaceRect,
          handleNativeSurfaceEvent,
          performAction,
        }
      },
    })
    const manager = new WorkbenchRuntimeManager(registry, {
      nativeWorkbenchApi: nativeApi,
      setExpanded,
    })

    manager.handle({ type: 'open', item: descriptor })
    manager.setComponentHandle(descriptor, { provider: 'browser' })
    manager.handleComponentEvent(descriptor, {
      type: 'navigation-requested',
      payload: 'https://example.test',
    })
    manager.handleSurfaceRect({
      itemId: descriptor.id,
      x: 10,
      y: 20,
      width: 640,
      height: 480,
      visible: true,
    })
    manager.handleNativeSurfaceEvent({
      version: 1,
      surfaceId: descriptor.id,
      type: 'ready',
    })
    manager.performAction(descriptor, 'take-control')
    await manager.flush()

    const context = runtimeContext.current
    expect(context).not.toBeNull()
    if (!context) throw new Error('provider context was not created')
    expect(context.nativeWorkbenchApi).toBeDefined()
    context.updateRenderState({ controlLabel: 'Return control' })
    expect(manager.getRenderState(descriptor.id)).toEqual({
      controlLabel: 'Return control',
    })
    expect(context.getRenderState()).toEqual({
      controlLabel: 'Return control',
    })
    expect(context.isItemOpen()).toBe(true)
    context.setExpanded(false)
    expect(setExpanded).toHaveBeenCalledWith(false)
    expect(setComponentHandle).toHaveBeenCalledWith({ provider: 'browser' })
    expect(handleComponentEvent).toHaveBeenCalledWith(
      { type: 'navigation-requested', payload: 'https://example.test' },
      descriptor,
    )
    expect(handleSurfaceRect).toHaveBeenCalledWith(
      expect.objectContaining({ visible: true, width: 640 }),
      descriptor,
    )
    expect(handleNativeSurfaceEvent).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'ready' }),
      descriptor,
    )
    expect(performAction).toHaveBeenCalledWith('take-control', descriptor)
    expect(registry.resolve(descriptor)?.getToolbarItems?.(descriptor, {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: manager.getRenderState(descriptor.id),
    })).toEqual([
      expect.objectContaining({ id: 'take-control', label: 'Return control' }),
    ])
  })

  it('forwards the v3 annotation and screenshot bridge only for the active surface', async () => {
    const registry = new WorkbenchPanelRegistry()
    let scoped: NativeWorkbenchApi | undefined
    const getCapabilities = vi.fn(async () => ({
      version: 3 as const,
      available: true,
      picker: true,
      trustedOverlay: true,
    }))
    const setMode = vi.fn(async () => ({ ok: true as const }))
    const showOverlay = vi.fn(async () => ({ ok: true as const }))
    const closeOverlay = vi.fn(async () => ({ ok: true as const }))
    const screenshot = vi.fn(async () => ({
      ok: true as const,
      method: 'screenshot' as const,
      value: {
        mime: 'image/png' as const,
        data: new Uint8Array([1, 2, 3]),
        width: 10,
        height: 10,
      },
    }))
    const nativeApi: NativeWorkbenchApi = {
      getArtifactAnnotationCapabilities: getCapabilities,
      setArtifactAnnotationMode: setMode,
      showArtifactAnnotationOverlay: showOverlay,
      closeArtifactAnnotationOverlay: closeOverlay,
      screenshot,
      createSurface: vi.fn(async () => ({ ok: true })),
      setSurfaceRect: vi.fn(async () => ({ ok: true })),
      activateSurface: vi.fn(async () => ({ ok: true })),
      destroySurface: vi.fn(async () => ({ ok: true })),
      onSurfaceEvent: vi.fn(() => () => undefined),
    }
    const descriptor: WorkbenchItem = {
      ...item('annotation-native'),
      hostKind: 'native-webcontents',
    }
    registry.register({
      kind: 'artifact-preview',
      createRuntime: (_item, context) => {
        scoped = context.nativeWorkbenchApi
        return {}
      },
    })
    const manager = new WorkbenchRuntimeManager(registry, { nativeWorkbenchApi: nativeApi })
    manager.handle({ type: 'open', item: descriptor })
    manager.handle({ type: 'resume', item: descriptor })
    manager.handleSurfaceRect({
      itemId: descriptor.id,
      x: 0,
      y: 0,
      width: 640,
      height: 480,
      visible: true,
    })
    await manager.flush()

    await expect(scoped?.getArtifactAnnotationCapabilities?.()).resolves.toMatchObject({
      available: true,
    })
    await scoped?.setArtifactAnnotationMode?.({
      version: 3,
      surfaceId: descriptor.id,
      enabled: true,
    })
    await scoped?.showArtifactAnnotationOverlay?.({
      version: 3,
      surfaceId: descriptor.id,
      selectionId: 'selection-1',
      annotationId: 'annotation-1',
    })
    await scoped?.closeArtifactAnnotationOverlay?.({
      version: 3,
      surfaceId: descriptor.id,
      annotationId: 'annotation-1',
    })
    await expect(scoped?.screenshot?.({ version: 3 })).resolves.toMatchObject({
      ok: true,
      method: 'screenshot',
    })
    expect(getCapabilities).toHaveBeenCalledOnce()
    expect(setMode).toHaveBeenCalledOnce()
    expect(showOverlay).toHaveBeenCalledOnce()
    expect(closeOverlay).toHaveBeenCalledOnce()
    expect(screenshot).toHaveBeenCalledOnce()

    manager.handle({ type: 'suspend', item: descriptor })
    await manager.flush()
    await expect(scoped?.showArtifactAnnotationOverlay?.({
      version: 3,
      surfaceId: descriptor.id,
      selectionId: 'selection-2',
      annotationId: 'annotation-2',
    })).resolves.toMatchObject({ ok: false })
    expect(showOverlay).toHaveBeenCalledOnce()

    // Disabling the picker is cleanup, not a request to show anything. It
    // must still reach the native bridge while the preview is suspended so a
    // response handoff cannot leave the orange picker active for the next
    // selection.
    await scoped?.setArtifactAnnotationMode?.({
      version: 3,
      surfaceId: descriptor.id,
      enabled: false,
    })
    expect(setMode).toHaveBeenLastCalledWith({
      version: 3,
      surfaceId: descriptor.id,
      enabled: false,
    })
  })
})
