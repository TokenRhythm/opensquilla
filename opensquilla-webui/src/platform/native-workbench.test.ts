// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createDesktopPlatform } from './desktop'
import { createWebPlatform } from './web'

function setDesktopApi(api: unknown): void {
  ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = api
}

afterEach(() => {
  setDesktopApi(undefined)
})

describe('native Workbench platform bridge', () => {
  it('is absent on web and older desktop shells', () => {
    expect(createWebPlatform().workbench.native).toBeUndefined()
    expect(createWebPlatform().capabilities.hasNativeWorkbenchSurfaces).toBe(false)

    setDesktopApi({})
    const legacy = createDesktopPlatform()
    expect(legacy.workbench.native).toBeUndefined()
    expect(legacy.capabilities.hasNativeWorkbenchSurfaces).toBe(false)
  })

  it('forwards the complete typed bridge and filters malformed events', async () => {
    const create = vi.fn(async () => ({ ok: true }))
    const setRect = vi.fn(async () => ({ ok: true }))
    const activate = vi.fn(async () => ({ ok: true }))
    const destroy = vi.fn(async () => ({ ok: true }))
    const createLease = vi.fn(async () => ({
      ok: true as const,
      status: 201,
      payload: { version: 1 },
    }))
    const renewLease = vi.fn(async () => ({
      ok: true as const,
      status: 200,
      payload: { version: 1 },
    }))
    const revokeLease = vi.fn(async () => ({
      ok: true as const,
      status: 204,
      payload: undefined,
    }))
    let emit: ((payload: unknown) => void) | undefined
    const unsubscribe = vi.fn()
    setDesktopApi({
      createWorkbenchSurface: create,
      setWorkbenchSurfaceRect: setRect,
      activateWorkbenchSurface: activate,
      destroyWorkbenchSurface: destroy,
      createArtifactPreviewLease: createLease,
      renewArtifactPreviewLease: renewLease,
      revokeArtifactPreviewLease: revokeLease,
      onWorkbenchSurfaceEvent: (callback: (payload: unknown) => void) => {
        emit = callback
        return unsubscribe
      },
    })

    const platform = createDesktopPlatform()
    const native = platform.workbench.native
    expect(native).toBeDefined()
    expect(platform.capabilities.hasNativeWorkbenchSurfaces).toBe(true)

    const data = new TextEncoder().encode('<title>Fixture</title>').buffer
    await native!.createSurface({
      version: 1,
      surfaceId: 'artifact:fixture',
      kind: 'artifact-html',
      payload: {
        data,
        name: 'fixture.html',
        mime: 'text/html',
        scopeId: 'session:fixture',
        allowRemoteResources: false,
      },
    })
    await native!.setSurfaceRect({
      surfaceId: 'artifact:fixture',
      x: 600,
      y: 80,
      width: 520,
      height: 700,
      visible: true,
    })
    await native!.activateSurface('artifact:fixture')
    await native!.destroySurface('artifact:fixture')
    await native!.createArtifactPreviewLease?.({
      version: 1,
      artifactId: 'art-fixture',
      scopeId: 'session:fixture',
      mode: 'full',
    })
    await native!.renewArtifactPreviewLease?.({
      version: 1,
      leaseId: 'apl-fixture',
      scopeId: 'session:fixture',
    })
    await native!.revokeArtifactPreviewLease?.({
      version: 1,
      leaseId: 'apl-fixture',
      scopeId: 'session:fixture',
    })

    expect(create).toHaveBeenCalledTimes(1)
    expect(create).toHaveBeenCalledWith(expect.objectContaining({
      payload: expect.objectContaining({ allowRemoteResources: false }),
    }))
    expect(setRect).toHaveBeenCalledTimes(1)
    expect(activate).toHaveBeenCalledWith('artifact:fixture')
    expect(destroy).toHaveBeenCalledWith('artifact:fixture')
    expect(createLease).toHaveBeenCalledTimes(1)
    expect(renewLease).toHaveBeenCalledTimes(1)
    expect(revokeLease).toHaveBeenCalledTimes(1)

    const listener = vi.fn()
    expect(native!.onSurfaceEvent(listener)).toBe(unsubscribe)
    emit?.({ version: 3, surfaceId: 'artifact:fixture', type: 'ready' })
    emit?.({
      version: 1,
      surfaceId: 'artifact:fixture',
      type: 'missing-resource',
      detail: { path: '/assets/app.css', ignored: 'value' },
    })
    expect(listener).toHaveBeenCalledTimes(1)
    expect(listener).toHaveBeenCalledWith({
      version: 1,
      surfaceId: 'artifact:fixture',
      type: 'missing-resource',
      detail: { path: '/assets/app.css' },
    })
  })

  it('fails closed when only part of the shell bridge exists', () => {
    setDesktopApi({
      createWorkbenchSurface: async () => ({ ok: true }),
      setWorkbenchSurfaceRect: async () => ({ ok: true }),
      activateWorkbenchSurface: async () => ({ ok: true }),
      // destroy and event subscription deliberately absent
    })
    const platform = createDesktopPlatform()
    expect(platform.workbench.native).toBeUndefined()
    expect(platform.capabilities.hasNativeWorkbenchSurfaces).toBe(false)
  })
})
