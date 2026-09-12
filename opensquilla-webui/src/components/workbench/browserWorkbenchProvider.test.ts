import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  NativeWorkbenchApi,
  NativeWorkbenchCapabilities,
  NativeWorkbenchSurfaceResult,
  Platform,
} from '@/platform/types'
import { createBrowserWorkbenchItem } from '@/workbench/browserItems'
import { WorkbenchPanelRegistry, WorkbenchRuntimeManager } from '@/workbench/runtime'
import type {
  NativeSurfaceRect,
  WorkbenchItem,
  WorkbenchRuntimeContext,
} from '@/workbench/types'
import { createBrowserWorkbenchDefinition } from './browserWorkbenchProvider'

function successfulResult(): Promise<NativeWorkbenchSurfaceResult> {
  return Promise.resolve({ ok: true })
}

function nativeApi(
  overrides: Partial<NativeWorkbenchApi> = {},
): NativeWorkbenchApi {
  return {
    getCapabilities: vi.fn(async (): Promise<NativeWorkbenchCapabilities> => ({
      protocolVersions: [1, 2],
      modes: ['full', 'offline'],
      maxSurfaces: 8,
    })),
    createSurface: vi.fn(successfulResult),
    setSurfaceRect: vi.fn(successfulResult),
    activateSurface: vi.fn(successfulResult),
    destroySurface: vi.fn(successfulResult),
    onSurfaceEvent: vi.fn(() => () => undefined),
    ...overrides,
  }
}

async function createHarness(api: NativeWorkbenchApi) {
  const item = createBrowserWorkbenchItem({
    scopeId: 'session-a',
    url: 'https://example.test/start',
  })!
  const renderState: Record<string, unknown> = {}
  const reportError = vi.fn()
  const context: WorkbenchRuntimeContext = {
    nativeWorkbenchApi: api,
    getRenderState: () => renderState,
    updateRenderState: patch => Object.assign(renderState, patch),
    isItemOpen: () => true,
    setExpanded: vi.fn(),
    reportError,
  }
  const definition = createBrowserWorkbenchDefinition({
    confirmPermission: vi.fn(async () => false),
    openExternal: vi.fn(),
    platform: {} as Platform,
    t: key => key,
  })
  const runtime = await definition.createRuntime!(item, context)
  return {
    api,
    definition,
    item,
    renderState,
    reportError,
    runtime,
  }
}

const visibleRect: NativeSurfaceRect = {
  itemId: 'ignored-by-provider',
  x: 320,
  y: 40,
  width: 640,
  height: 520,
  visible: true,
}

it('adopts an agent-opened surface without opening another page at the same URL', async () => {
  const api = nativeApi()
  const item = createBrowserWorkbenchItem({ scopeId: 'session-a', url: 'https://example.test/' })!
  item.id = 'browser-target-2'
  item.payload = { ...item.payload, adoptedNativeSurface: true }
  const state: Record<string, unknown> = {}
  const definition = createBrowserWorkbenchDefinition({
    confirmPermission: vi.fn(async () => false), openExternal: vi.fn(),
    platform: {} as Platform, t: key => key,
  })
  const runtime = await definition.createRuntime!(item, {
    nativeWorkbenchApi: api, getRenderState: () => state,
    updateRenderState: patch => Object.assign(state, patch),
    isItemOpen: () => true, setExpanded: vi.fn(), reportError: vi.fn(),
  })
  expect(api.activateSurface).not.toHaveBeenCalled()
  expect(api.createSurface).not.toHaveBeenCalled()
  expect(api.setSurfaceRect).not.toHaveBeenCalled()
  await runtime.handleSurfaceRect?.(visibleRect, item)
  expect(api.activateSurface).toHaveBeenCalledWith('browser-target-2')
  expect(api.setSurfaceRect).toHaveBeenCalledWith({
    surfaceId: 'browser-target-2', x: 320, y: 40, width: 640, height: 520, visible: true,
  })
  expect(api.createSurface).not.toHaveBeenCalled()
  await runtime.dispose?.('closed')
  expect(api.destroySurface).toHaveBeenCalledWith('browser-target-2')
})

describe('browser adoption through the Workbench runtime manager', () => {
  let api: NativeWorkbenchApi
  let manager: WorkbenchRuntimeManager
  let items: WorkbenchItem[]
  let onError = vi.fn()
  let instances: Map<string, { identity: number; note: string; visible: boolean }>

  beforeEach(() => {
    items = ['first', 'second'].map((name) => {
      const item = createBrowserWorkbenchItem({
        scopeId: 'session-a', url: 'https://example.test/shared',
      })!
      item.id = `browser-${name}`
      item.payload = { ...item.payload, adoptedNativeSurface: true }
      return item
    })
    instances = new Map(items.map((item, index) => [item.id, {
      identity: index + 1, note: `${item.id} memory`, visible: false,
    }]))
    let nextIdentity = 3
    api = nativeApi({
      createSurface: vi.fn(async (request) => {
        instances.set(request.surfaceId, {
          identity: nextIdentity++, note: '', visible: false,
        })
        return { ok: true }
      }),
      setSurfaceRect: vi.fn(async (request) => {
        const instance = instances.get(request.surfaceId)
        if (!instance) return { ok: false, message: 'native surface missing' }
        instance.visible = request.visible
        return { ok: true }
      }),
      activateSurface: vi.fn(async (id) => (
        instances.has(id) ? { ok: true } : { ok: false, message: 'native surface missing' }
      )),
      destroySurface: vi.fn(async (id) => {
        instances.delete(id)
        return { ok: true }
      }),
    })
    const registry = new WorkbenchPanelRegistry()
    registry.register(createBrowserWorkbenchDefinition({
      confirmPermission: vi.fn(async () => false), openExternal: vi.fn(),
      platform: {} as Platform, t: key => key,
    }))
    onError = vi.fn()
    manager = new WorkbenchRuntimeManager(registry, { nativeWorkbenchApi: api, onError })
  })

  afterEach(async () => {
    await manager.disposeAll()
  })

  it('retains an adopted page while the host has no visible rect', async () => {
    const item = items[0]!
    const original = instances.get(item.id)
    manager.handle({ type: 'open', item })
    manager.handle({ type: 'resume', item })
    await manager.flush()

    expect(instances.get(item.id)).toBe(original)
    expect(api.createSurface).not.toHaveBeenCalled()
    expect(api.activateSurface).not.toHaveBeenCalled()
    expect(api.destroySurface).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
  })

  it('shows the original adopted instance when its visible rect arrives', async () => {
    const item = items[0]!
    const original = instances.get(item.id)
    manager.handle({ type: 'open', item })
    manager.handle({ type: 'resume', item })
    await manager.flush()
    manager.handleSurfaceRect({ ...visibleRect, itemId: item.id })
    await manager.flush()

    expect(instances.get(item.id)).toBe(original)
    expect(original?.visible).toBe(true)
    expect(api.activateSurface).toHaveBeenCalledWith(item.id)
    expect(api.createSurface).not.toHaveBeenCalled()
    expect(api.destroySurface).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
  })

  it('keeps both instances and their memory while switching pages at the same URL', async () => {
    const first = items[0]!
    const second = items[1]!
    const originals = items.map(item => instances.get(item.id))
    expect(first.payload.initialUrl).toBe(second.payload.initialUrl)
    expect(first.id).not.toBe(second.id)
    manager.handle({ type: 'open', item: first })
    manager.handle({ type: 'resume', item: first })
    await manager.flush()
    manager.handleSurfaceRect({ ...visibleRect, itemId: first.id })
    await manager.flush()
    manager.handle({ type: 'suspend', item: first })
    manager.handle({ type: 'open', item: second })
    manager.handle({ type: 'resume', item: second })
    await manager.flush()
    manager.handleSurfaceRect({ ...visibleRect, itemId: second.id })
    await manager.flush()
    expect(instances.get(first.id)?.visible).toBe(false)
    expect(instances.get(second.id)?.visible).toBe(true)
    manager.handle({ type: 'suspend', item: second })
    manager.handle({ type: 'resume', item: first })
    await manager.flush()

    for (const [index, item] of items.entries()) {
      expect(instances.get(item.id)).toBe(originals[index])
      expect(instances.get(item.id)?.note).toBe(`${item.id} memory`)
    }
    expect(instances.get(first.id)?.visible).toBe(true)
    expect(instances.get(second.id)?.visible).toBe(false)
    expect(api.createSurface).not.toHaveBeenCalled()
    expect(api.destroySurface).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
  })

  it('creates a fresh page on retry after an adopted page crashes', async () => {
    const item = items[0]!
    const original = instances.get(item.id)
    // Supply layout before opening so this case isolates crash recovery.
    manager.handleSurfaceRect({ ...visibleRect, itemId: item.id })
    manager.handle({ type: 'open', item })
    manager.handle({ type: 'resume', item })
    await manager.flush()
    expect(instances.get(item.id)).toBe(original)
    manager.handleNativeSurfaceEvent({
      version: 2, surfaceId: item.id, type: 'crashed', detail: { reason: 'synthetic crash' },
    })
    await manager.flush()
    expect(instances.has(item.id)).toBe(false)
    expect(manager.getRenderState(item.id).errorMessage).toBe('synthetic crash')
    expect(onError).toHaveBeenCalledOnce()
    vi.mocked(api.activateSurface).mockClear()

    manager.handleComponentEvent(item, { type: 'browser-action', payload: { action: 'reload' } })
    await manager.flush()
    expect(api.createSurface).toHaveBeenCalledOnce()
    expect(instances.get(item.id)).toMatchObject({ visible: true, note: '' })
    expect(instances.get(item.id)?.identity).not.toBe(original?.identity)
    expect(api.activateSurface).toHaveBeenCalledWith(item.id)
    expect(manager.getRenderState(item.id).errorMessage).toBe('')
    expect(onError).toHaveBeenCalledOnce()
  })

  it('retains the page when suspension rejects activation after positioning awaits', async () => {
    const item = items[0]!
    item.payload = { ...item.payload, adoptedNativeSurface: false }
    instances.delete(item.id)
    manager.handle({ type: 'open', item })
    manager.handle({ type: 'resume', item })
    await manager.flush()
    const original = instances.get(item.id)
    expect(original).toBeDefined()
    const position = vi.mocked(api.setSurfaceRect).getMockImplementation()!
    let releasePosition!: () => void
    let positionEntered!: () => void
    const entered = new Promise<void>(resolve => { positionEntered = resolve })
    const released = new Promise<void>(resolve => { releasePosition = resolve })
    vi.mocked(api.setSurfaceRect).mockImplementationOnce(async (request) => {
      positionEntered()
      await released
      return position(request)
    })
    manager.handleSurfaceRect({ ...visibleRect, itemId: item.id })
    await entered
    manager.handle({ type: 'suspend', item })
    releasePosition()
    await manager.flush()

    expect(instances.get(item.id)).toBe(original)
    expect(original?.visible).toBe(false)
    expect(api.activateSurface).not.toHaveBeenCalled()
    expect(api.destroySurface).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
    manager.handle({ type: 'resume', item })
    await manager.flush()
    expect(instances.get(item.id)).toBe(original)
    expect(original?.visible).toBe(true)
    expect(api.activateSurface).toHaveBeenCalledWith(item.id)
    expect(api.createSurface).toHaveBeenCalledOnce()
  })
})

describe('browser Workbench provider', () => {
  it('shows an upgrade error instead of leaving an old Desktop shell loading', async () => {
    const api = nativeApi({
      getCapabilities: vi.fn(async (): Promise<NativeWorkbenchCapabilities> => ({
        protocolVersions: [1],
        modes: ['offline'],
        maxSurfaces: 8,
      })),
    })
    const harness = await createHarness(api)

    expect(harness.renderState).toMatchObject({
      errorMessage: 'Update OpenSquilla Desktop to use the side browser.',
      loading: false,
    })
    expect(api.createSurface).not.toHaveBeenCalled()
    expect(api.destroySurface).toHaveBeenCalledWith(harness.item.id)
    expect(harness.definition.getProps?.(harness.item, {
      active: true,
      hostAvailable: true,
      nativeSurface: true,
      runtimeState: harness.renderState,
    })).toMatchObject({
      errorMessage: 'Update OpenSquilla Desktop to use the side browser.',
      loading: false,
    })
  })

  it('turns a rejected create into a visible recoverable error', async () => {
    const api = nativeApi({
      createSurface: vi.fn(async () => ({
        ok: false,
        message: 'native create failed',
      })),
    })
    const harness = await createHarness(api)

    expect(harness.renderState).toMatchObject({
      errorMessage: 'native create failed',
      loading: false,
    })
    expect(harness.reportError).toHaveBeenCalledOnce()
    expect(api.destroySurface).toHaveBeenCalledWith(harness.item.id)
  })

  it.each(['navigate', 'rect', 'activate'] as const)(
    'hides and destroys the native surface after a %s failure',
    async failingOperation => {
      const setSurfaceRect = vi.fn(async (request) => {
        if (failingOperation === 'rect' && request.visible) {
          return { ok: false, message: 'rect failed' }
        }
        return { ok: true }
      })
      const activateSurface = vi.fn(async () => (
        failingOperation === 'activate'
          ? { ok: false, message: 'activate failed' }
          : { ok: true }
      ))
      const navigateSurface = vi.fn(async () => {
        if (failingOperation === 'navigate') throw new Error('navigate failed')
        return { ok: true }
      })
      const api = nativeApi({
        setSurfaceRect,
        activateSurface,
        navigateSurface,
      })
      const harness = await createHarness(api)

      if (failingOperation === 'navigate') {
        await harness.runtime.handleSurfaceRect?.(visibleRect, harness.item)
        await harness.runtime.handleComponentEvent?.({
          type: 'browser-action',
          payload: { action: 'navigate', url: 'https://example.test/next' },
        }, harness.item)
      } else {
        await harness.runtime.handleSurfaceRect?.(visibleRect, harness.item)
      }

      expect(harness.renderState).toMatchObject({
        errorMessage: `${failingOperation} failed`,
        loading: false,
      })
      expect(setSurfaceRect).toHaveBeenLastCalledWith(
        expect.objectContaining({
          surfaceId: harness.item.id,
          visible: false,
        }),
      )
      expect(api.destroySurface).toHaveBeenCalledWith(harness.item.id)
    },
  )

  it.each(['crashed', 'unresponsive'] as const)(
    'fails closed and exposes a visible error after a native %s event',
    async eventType => {
      const api = nativeApi()
      const harness = await createHarness(api)
      await harness.runtime.handleSurfaceRect?.(visibleRect, harness.item)

      await harness.runtime.handleNativeSurfaceEvent?.({
        version: 2,
        surfaceId: harness.item.id,
        type: eventType,
        detail: { reason: `${eventType}-reason` },
      }, harness.item)

      expect(harness.renderState).toMatchObject({
        errorMessage: `${eventType}-reason`,
        loading: false,
      })
      expect(api.setSurfaceRect).toHaveBeenLastCalledWith(
        expect.objectContaining({ visible: false }),
      )
      expect(api.destroySurface).toHaveBeenCalledWith(harness.item.id)
    },
  )

  it('reloads an errored browser with a fresh native surface', async () => {
    const createSurface = vi.fn()
      .mockResolvedValueOnce({ ok: false, message: 'first create failed' })
      .mockResolvedValue({ ok: true })
    const api = nativeApi({ createSurface })
    const harness = await createHarness(api)
    await harness.runtime.handleSurfaceRect?.(visibleRect, harness.item)

    await harness.runtime.handleComponentEvent?.({
      type: 'browser-action',
      payload: { action: 'reload' },
    }, harness.item)

    expect(createSurface).toHaveBeenCalledTimes(2)
    expect(api.destroySurface).toHaveBeenCalled()
    expect(api.setSurfaceRect).toHaveBeenLastCalledWith(
      expect.objectContaining({
        surfaceId: harness.item.id,
        visible: true,
      }),
    )
    expect(api.activateSurface).toHaveBeenCalledWith(harness.item.id)
    expect(harness.renderState).toMatchObject({
      errorMessage: '',
      loading: true,
    })
  })
})
