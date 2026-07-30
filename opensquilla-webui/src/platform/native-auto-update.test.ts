// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createWebPlatform } from './web'
import { createDesktopPlatform } from './desktop'

// Native installation and shell-owned update presentation are separate
// capabilities: unsigned Windows uses a managed manual-installer flow, while an
// older Windows shell still falls back to the passive gateway banner.

function setDesktopApi(api: unknown): void {
  ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = api
}

afterEach(() => {
  setDesktopApi(undefined)
})

describe('nativeAutoUpdateEnabled', () => {
  it('is false on web — the browser never auto-updates, so the banner shows', async () => {
    expect(await createWebPlatform().nativeAutoUpdateEnabled()).toBe(false)
  })

  it('mirrors the shell when native update is ON (macOS / signed Windows) → banner hidden', async () => {
    setDesktopApi({ isAutoUpdateEnabled: async () => true })
    expect(await createDesktopPlatform().nativeAutoUpdateEnabled()).toBe(true)
  })

  it('mirrors the shell when native update is OFF (unsigned Windows) → banner shown', async () => {
    setDesktopApi({ isAutoUpdateEnabled: async () => false })
    expect(await createDesktopPlatform().nativeAutoUpdateEnabled()).toBe(false)
  })

  it('defaults to true (suppress) if an older shell lacks the bridge', async () => {
    setDesktopApi({})
    expect(await createDesktopPlatform().nativeAutoUpdateEnabled()).toBe(true)
  })

  it('defaults to true (suppress) if the bridge throws', async () => {
    setDesktopApi({
      isAutoUpdateEnabled: async () => {
        throw new Error('ipc boom')
      },
    })
    expect(await createDesktopPlatform().nativeAutoUpdateEnabled()).toBe(true)
  })
})

describe('desktopUpdateManaged', () => {
  it('is false on web', async () => {
    expect(await createWebPlatform().desktopUpdateManaged()).toBe(false)
  })

  it('is true for a managed unsigned Windows shell without native installation', async () => {
    setDesktopApi({
      isAutoUpdateEnabled: async () => false,
      isDesktopUpdateManaged: async () => true,
    })
    expect(await createDesktopPlatform().desktopUpdateManaged()).toBe(true)
  })

  it('uses native capability for an older shell without the managed bridge', async () => {
    setDesktopApi({ isAutoUpdateEnabled: async () => false })
    expect(await createDesktopPlatform().desktopUpdateManaged()).toBe(false)
  })

  it('fails closed when an exposed managed bridge throws', async () => {
    setDesktopApi({
      isAutoUpdateEnabled: async () => false,
      isDesktopUpdateManaged: async () => {
        throw new Error('ipc boom')
      },
    })
    expect(await createDesktopPlatform().desktopUpdateManaged()).toBe(true)
  })
})

describe('desktop update platform bridge', () => {
  it('web exposes an inert update API with a non-native idle state', async () => {
    const state = await createWebPlatform().updates.getState()

    expect(state).toMatchObject({
      status: 'idle',
      currentVersion: '',
      latestVersion: null,
      canCheck: false,
      canNativeInstall: false,
      installMode: 'unsupported',
    })
  })

  it('forwards desktop update state, actions, and subscriptions from the shell bridge', async () => {
    const unsubscribe = vi.fn()
    const checkForUpdates = vi.fn(async () => ({ status: 'checking' }))
    const downloadUpdate = vi.fn(async () => ({ status: 'downloading' }))
    const relaunchToUpdate = vi.fn(async () => ({ status: 'applying' }))
    const dismissUpdate = vi.fn(async () => ({ status: 'available', snoozedUntil: '2026-07-04T00:00:00.000Z' }))
    setDesktopApi({
      isAutoUpdateEnabled: async () => true,
      isDesktopUpdateManaged: async () => true,
      getUpdateState: async () => ({
        status: 'available',
        currentVersion: '1.0.0',
        latestVersion: '2.0.0',
        progress: null,
        checkedAt: null,
        error: null,
        errorCode: null,
        snoozedUntil: null,
        canCheck: true,
        canNativeInstall: true,
        installMode: 'native',
        releaseUrl: null,
        source: 'github',
        fallbackUsed: false,
      }),
      checkForUpdates,
      downloadUpdate,
      relaunchToUpdate,
      dismissUpdate,
      onUpdateState: () => unsubscribe,
    })

    const updates = createDesktopPlatform().updates
    expect(await updates.getState()).toMatchObject({ status: 'available', latestVersion: '2.0.0' })
    await updates.check()
    await updates.download()
    await updates.relaunch()
    await updates.dismiss()
    expect(checkForUpdates).toHaveBeenCalledTimes(1)
    expect(downloadUpdate).toHaveBeenCalledTimes(1)
    expect(relaunchToUpdate).toHaveBeenCalledTimes(1)
    expect(dismissUpdate).toHaveBeenCalledTimes(1)
    expect(updates.onState(() => undefined)).toBe(unsubscribe)
  })

  it('normalizes a managed Windows state without claiming native installation', async () => {
    setDesktopApi({
      isAutoUpdateEnabled: async () => false,
      isDesktopUpdateManaged: async () => true,
      getUpdateState: async () => ({
        status: 'available',
        currentVersion: '1.0.0',
        latestVersion: '2.0.0',
        canCheck: true,
        canNativeInstall: false,
        installMode: 'manual',
        errorCode: null,
        source: 'oss',
        fallbackUsed: true,
      }),
    })

    expect(await createDesktopPlatform().updates.getState()).toMatchObject({
      status: 'available',
      canCheck: true,
      canNativeInstall: false,
      installMode: 'manual',
      source: 'oss',
      fallbackUsed: true,
    })
  })

  it('preserves structured checksum and integrity failures from the shell', async () => {
    setDesktopApi({
      isAutoUpdateEnabled: async () => false,
      isDesktopUpdateManaged: async () => true,
      getUpdateState: async () => ({
        status: 'error',
        currentVersion: '1.0.0',
        canCheck: true,
        canNativeInstall: false,
        installMode: 'manual',
        errorCode: 'checksum_unavailable',
      }),
    })

    expect(await createDesktopPlatform().updates.getState()).toMatchObject({
      status: 'error',
      errorCode: 'checksum_unavailable',
      installMode: 'manual',
    })
  })

  it('derives the legacy state contract from an older shell capability', async () => {
    setDesktopApi({
      isAutoUpdateEnabled: async () => true,
      getUpdateState: async () => ({
        status: 'available',
        currentVersion: '1.0.0',
        latestVersion: '2.0.0',
      }),
    })

    expect(await createDesktopPlatform().updates.getState()).toMatchObject({
      status: 'available',
      canCheck: true,
      canNativeInstall: true,
      installMode: 'native',
      errorCode: null,
      source: null,
      fallbackUsed: false,
    })
  })

  it('keeps an older non-native Windows shell on the passive update path', async () => {
    setDesktopApi({
      isAutoUpdateEnabled: async () => false,
      getUpdateState: async () => ({ status: 'idle', currentVersion: '1.0.0' }),
    })

    expect(await createDesktopPlatform().updates.getState()).toMatchObject({
      canCheck: false,
      canNativeInstall: false,
      installMode: 'unsupported',
    })
  })
})
