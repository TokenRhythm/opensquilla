// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'
import type { DesktopUpdateState } from '@/platform'
import { desktopUpdateEntryAvailable, desktopUpdateReady } from './useDesktopUpdate'
const state: DesktopUpdateState = {
  status: 'downloaded', currentVersion: '1.0.0', latestVersion: '2.0.0',
  progress: null, checkedAt: null, error: null, errorCode: null,
  snoozedUntil: null, canCheck: true, canNativeInstall: false,
  canInstall: true, canAutoDownload: true, installMode: 'manual',
  releaseUrl: null, source: 'github', fallbackUsed: false,
}
describe('desktop update entry policy', () => {
  it.each(['idle', 'checking', 'available', 'downloading', 'not-available', 'error', 'applying'] as const)(
    'keeps background %s quiet', status => {
      expect(desktopUpdateEntryAvailable({ ...state, status })).toBe(false)
    },
  )
  it('only calls a verified, installable target ready', () => {
    expect(desktopUpdateReady(state)).toBe(true)
    for (const patch of [
      { canInstall: false }, { latestVersion: null }, { error: 'failure' },
      { errorCode: 'signature_invalid' as const }, { installMode: 'unsupported' as const },
    ]) {
      expect(desktopUpdateReady({ ...state, ...patch })).toBe(false)
      expect(desktopUpdateEntryAvailable({ ...state, ...patch })).toBe(false)
    }
  })
  it.each([undefined, false])('preserves old/manual-only download entry when automatic preparation is %s', canAutoDownload => {
    const legacy = { ...state, canAutoDownload, canInstall: false, status: 'available' as const }
    expect(desktopUpdateEntryAvailable(legacy)).toBe(true)
    expect(desktopUpdateReady(legacy)).toBe(false)
    expect(desktopUpdateEntryAvailable({ ...legacy, status: 'downloaded' })).toBe(true)
    expect(desktopUpdateEntryAvailable({ ...legacy, status: 'error' })).toBe(false)
  })
  it('respects explicit native installation denial but supports older native shells', () => {
    expect(desktopUpdateReady({ ...state, installMode: 'native', canInstall: undefined })).toBe(true)
    expect(desktopUpdateReady({ ...state, installMode: 'native', canInstall: false })).toBe(false)
  })
})

it('restores a snoozed ready entry at its deadline without another host event', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-13T00:00:00Z'))
  vi.resetModules()
  ;(window as unknown as { opensquillaDesktop: unknown }).opensquillaDesktop = {
    getUpdateState: async () => ({ ...state, snoozedUntil: '2026-10-14T00:00:00Z' }),
    isAutoUpdateEnabled: async () => false,
    isDesktopUpdateManaged: async () => true,
    onUpdateState: () => () => undefined,
  }
  const { createApp } = await import('vue')
  const i18n = (await import('@/i18n')).default
  const { useDesktopUpdate } = await import('./useDesktopUpdate')
  let controller!: ReturnType<typeof useDesktopUpdate>
  const app = createApp({ setup() { controller = useDesktopUpdate(); controller.init(); return () => null } })
  const el = document.createElement('div')
  try {
    app.use(i18n).mount(el)
    await vi.advanceTimersByTimeAsync(0)
    expect(controller.state.value.status).toBe('downloaded')
    expect(controller.visible.value).toBe(false)
    expect(controller.snoozed.value).toBe(true)
    await vi.advanceTimersByTimeAsync(24 * 60 * 60 * 1000 - 1)
    expect(controller.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(controller.visible.value).toBe(true)
    expect(controller.snoozed.value).toBe(false)
  } finally {
    app.unmount()
    delete (window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop
    vi.useRealTimers()
  }
})
