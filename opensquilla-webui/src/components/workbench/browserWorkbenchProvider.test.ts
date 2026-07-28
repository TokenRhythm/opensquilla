import { describe, expect, it, vi } from 'vitest'
import type {
  NativeWorkbenchApi,
  NativeWorkbenchCapabilities,
  NativeWorkbenchSurfaceResult,
  Platform,
} from '@/platform/types'
import { createBrowserWorkbenchItem } from '@/workbench/browserItems'
import type {
  NativeSurfaceRect,
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
