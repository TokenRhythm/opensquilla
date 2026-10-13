// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick } from 'vue'
import { createPinia } from 'pinia'
import type { NativeWorkbenchApi, NativeWorkbenchSurfaceRectRequest, NativeWorkbenchSurfaceResult, Platform } from '@/platform/types'
import { createBrowserWorkbenchItem } from '@/workbench/browserItems'
import { attachWorkbenchRuntime, WorkbenchPanelRegistry, WorkbenchRuntimeManager } from '@/workbench/runtime'
import { useWorkbenchStore } from '@/workbench/store'
import type { NativeSurfaceRect, WorkbenchItem } from '@/workbench/types'
import { createBrowserWorkbenchDefinition } from './browserWorkbenchProvider'
import WorkbenchHost from './WorkbenchHost.vue'

function deferredResult() {
  let resolve!: (result: NativeWorkbenchSurfaceResult) => void
  const promise = new Promise<NativeWorkbenchSurfaceResult>(done => { resolve = done })
  return { promise, resolve }
}

afterEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('browser tab close', () => {
  it.each(['create', 'navigate'] as const)(
    'closes before pending native %s completes and fences its late result',
    async operation => {
      const pending = deferredResult()
      const successful = async (): Promise<NativeWorkbenchSurfaceResult> => ({ ok: true })
      const createSurface = vi.fn(operation === 'create' ? () => pending.promise : successful)
      const navigateSurface = vi.fn(() => pending.promise)
      const setSurfaceRect = vi.fn(async (_request: NativeWorkbenchSurfaceRectRequest) => ({ ok: true }))
      const activateSurface = vi.fn(successful)
      const destroySurface = vi.fn(successful)
      const api: NativeWorkbenchApi = {
        getCapabilities: async () => ({ protocolVersions: [1, 2], modes: ['full'] }),
        createSurface, navigateSurface, setSurfaceRect, activateSurface, destroySurface,
        onSurfaceEvent: () => () => undefined,
      }
      const registry = new WorkbenchPanelRegistry()
      registry.register(createBrowserWorkbenchDefinition({
        platform: {} as Platform,
        confirmPermission: async () => false,
        openExternal: () => undefined,
        t: key => key,
      }))
      const onError = vi.fn()
      const manager = new WorkbenchRuntimeManager(registry, { nativeWorkbenchApi: api, onError })
      const pinia = createPinia()
      const store = useWorkbenchStore(pinia)
      const detach = attachWorkbenchRuntime(store, manager)
      const item = createBrowserWorkbenchItem({ scopeId: 'session', url: 'https://example.test/' })!
      const host = document.createElement('div')
      document.body.appendChild(host)
      const app = createApp(() => h(WorkbenchHost, {
        allowEmpty: true,
        availableWidth: 1200,
        beforeCloseItem: (current: WorkbenchItem) => manager.beforeClose(current),
        onSurfaceRect: (rect: NativeSurfaceRect) => manager.handleSurfaceRect(rect),
      }))
      app.use(pinia)
      store.openItem(item)
      app.mount(host)
      try {
        await vi.waitFor(() => expect(createSurface).toHaveBeenCalledOnce())
        if (operation === 'navigate') {
          await manager.flush()
          manager.handleComponentEvent(store.items[0]!, {
            type: 'browser-action',
            payload: { action: 'navigate', url: 'https://example.test/next' },
          })
          await vi.waitFor(() => expect(navigateSurface).toHaveBeenCalledOnce())
        }
        await nextTick()
        setSurfaceRect.mockClear()
        activateSurface.mockClear()
        const close = host.querySelector<HTMLButtonElement>('.workbench-host__tab-close')!
        expect(close).not.toBeNull()
        close.click()

        await vi.waitFor(() => {
          expect(store.items).toHaveLength(0)
          expect(host.querySelector('.workbench-host__tab-close')).toBeNull()
        }, { timeout: 250 })
        expect(setSurfaceRect).toHaveBeenCalledWith(expect.objectContaining({
          surfaceId: item.id, visible: false,
        }))
        expect(destroySurface).not.toHaveBeenCalled()
        expect(store.expanded).toBe(true)
        expect(host.querySelector('[data-testid="workbench-host"]')).not.toBeNull()

        pending.resolve({ ok: true })
        await manager.flush()
        await nextTick()
        expect(store.items).toHaveLength(0)
        expect(manager.hasRuntime(item.id)).toBe(false)
        expect(activateSurface).not.toHaveBeenCalled()
        expect(setSurfaceRect.mock.calls.every(([request]) => request.visible === false)).toBe(true)
        expect(destroySurface).toHaveBeenCalledWith(item.id)
        expect(onError).not.toHaveBeenCalled()
      } finally {
        pending.resolve({ ok: true })
        app.unmount()
        await detach()
      }
    },
  )
})
