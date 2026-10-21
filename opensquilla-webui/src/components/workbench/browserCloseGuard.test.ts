import { describe, expect, it, vi } from 'vitest'
import type { NativeWorkbenchApi, NativeWorkbenchCapabilities } from '@/platform/types'
import { createBrowserWorkbenchItem } from '@/workbench/browserItems'
import { requestNativeBrowserClose } from './browserCloseGuard'

const item = createBrowserWorkbenchItem({ scopeId: 'synthetic-task',
  url: 'https://example.test/page' })!

function api(
  navigateSurface: NativeWorkbenchApi['navigateSurface'],
  actions: NativeWorkbenchCapabilities['navigationActions'] = ['close'],
): NativeWorkbenchApi {
  return {
    getCapabilities: vi.fn(async (): Promise<NativeWorkbenchCapabilities> => ({
      protocolVersions: [1, 2], modes: ['full'], navigationActions: actions,
    })),
    navigateSurface,
    createSurface: vi.fn(async () => ({ ok: true })),
    setSurfaceRect: vi.fn(async () => ({ ok: true })),
    activateSurface: vi.fn(async () => ({ ok: true })),
    destroySurface: vi.fn(async () => ({ ok: true })),
    onSurfaceEvent: vi.fn(() => () => undefined),
  }
}

describe('native browser close guard', () => {
  it('retains a tab when the page cancels beforeunload', async () => {
    const navigate = vi.fn(async () => ({ ok: false, code: 'CLOSE_CANCELLED' }))
    expect(await requestNativeBrowserClose(item, api(navigate))).toBe(false)
    expect(navigate).toHaveBeenCalledExactlyOnceWith({ version: 2, surfaceId: item.id,
      action: 'close' })
  })

  it('removes a tab after confirmed close or an already absent target', async () => {
    expect(await requestNativeBrowserClose(item, api(vi.fn(async () => ({ ok: true }))))).toBe(true)
    expect(await requestNativeBrowserClose(item, api(vi.fn(async () => ({ ok: false,
      code: 'TARGET_NOT_FOUND' }))))).toBe(true)
  })

  it('uses the older close path when the native action is unavailable', async () => {
    const navigate = vi.fn(async () => ({ ok: true }))
    expect(await requestNativeBrowserClose(item, api(navigate, ['back']))).toBeNull()
    expect(await requestNativeBrowserClose(item, api(undefined))).toBeNull()
    expect(navigate).not.toHaveBeenCalled()
  })

  it('retains a visible tab when a native close request fails to return', async () => {
    const navigate = vi.fn(async () => { throw new Error('bridge disconnected') })
    expect(await requestNativeBrowserClose(item, api(navigate))).toBe(false)
    const unavailable = api(navigate)
    unavailable.getCapabilities = vi.fn(async () => { throw new Error('bridge disconnected') })
    expect(await requestNativeBrowserClose(item, unavailable)).toBe(false)
  })
})
