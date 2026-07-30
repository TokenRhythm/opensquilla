// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'

const settle = () => new Promise((resolve) => setTimeout(resolve, 20))

function setDesktopApi(api: unknown): void {
  ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = api
}

function desktopUpdateApi(state: Record<string, unknown>, overrides: Record<string, unknown> = {}) {
  return {
    isAutoUpdateEnabled: async () => true,
    isDesktopUpdateManaged: async () => true,
    getUpdateState: async () => ({
      status: 'available',
      currentVersion: '1.0.0',
      latestVersion: '99.0.0',
      progress: null,
      checkedAt: null,
      error: null,
      errorCode: null,
      snoozedUntil: null,
      canCheck: true,
      canNativeInstall: true,
      installMode: 'native',
      releaseUrl: null,
      source: null,
      fallbackUsed: false,
      ...state,
    }),
    checkForUpdates: vi.fn(async () => ({ ok: true })),
    downloadUpdate: vi.fn(async () => ({ ok: true })),
    relaunchToUpdate: vi.fn(async () => ({ ok: true })),
    dismissUpdate: vi.fn(async () => ({ ok: true })),
    onUpdateState: () => () => undefined,
    ...overrides,
  }
}

async function mountPanel(api: ReturnType<typeof desktopUpdateApi>) {
  vi.resetModules()
  document.body.innerHTML = ''
  setDesktopApi(api)
  const { createApp, nextTick } = await import('vue')
  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const Component = (await import('./SettingsUpdatePanel.vue')).default
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Component)
  app.use(i18n)
  app.mount(el)
  await settle()
  await nextTick()
  return { app, el }
}

beforeEach(() => {
  setDesktopApi(undefined)
})

describe('SettingsUpdatePanel', () => {
  it('shows available version details and starts download from Settings', async () => {
    const api = desktopUpdateApi({ status: 'available', latestVersion: '99.0.0' })
    const { app, el } = await mountPanel(api)

    expect(el.textContent).toContain('Desktop updates')
    expect(el.textContent).toContain('99.0.0')
    ;(el.querySelector('[data-testid="settings-update-download"]') as HTMLButtonElement).click()
    await settle()

    expect(api.downloadUpdate).toHaveBeenCalledTimes(1)
    app.unmount()
  })

  it('shows relaunch action when the update has downloaded', async () => {
    const api = desktopUpdateApi({ status: 'downloaded', latestVersion: '99.0.0' })
    const { app, el } = await mountPanel(api)

    expect(el.textContent).toContain('Ready to install')
    ;(el.querySelector('[data-testid="settings-update-relaunch"]') as HTMLButtonElement).click()
    await settle()

    expect(api.relaunchToUpdate).toHaveBeenCalledTimes(1)
    app.unmount()
  })

  it('shows the manual Windows installer action and selected fallback source', async () => {
    const api = desktopUpdateApi({
      status: 'available',
      canNativeInstall: false,
      installMode: 'manual',
      source: 'github',
      fallbackUsed: true,
    }, {
      isAutoUpdateEnabled: async () => false,
    })
    const { app, el } = await mountPanel(api)

    expect(el.textContent).toContain('Manual install')
    expect(el.textContent).toContain('GitHub Releases')
    expect(el.textContent).toContain('Switched to backup source')
    const download = el.querySelector('[data-testid="settings-update-download"]') as HTMLButtonElement
    expect(download.textContent).toContain('Download installer')
    download.click()
    await settle()

    expect(api.downloadUpdate).toHaveBeenCalledTimes(1)
    expect(el.querySelector('[data-testid="settings-update-relaunch"]')).toBeNull()
    app.unmount()
  })

  it('shows the verified Windows installer again without a relaunch action', async () => {
    const api = desktopUpdateApi({
      status: 'downloaded',
      canNativeInstall: false,
      installMode: 'manual',
      source: 'oss',
    }, {
      isAutoUpdateEnabled: async () => false,
    })
    const { app, el } = await mountPanel(api)

    expect(el.textContent).toContain('Verified')
    expect(el.textContent).toContain('verified against the canonical GitHub checksum')
    const show = el.querySelector('[data-testid="settings-update-download"]') as HTMLButtonElement
    expect(show.textContent).toContain('Show installer')
    show.click()
    await settle()

    expect(api.downloadUpdate).toHaveBeenCalledTimes(1)
    expect(el.querySelector('[data-testid="settings-update-relaunch"]')).toBeNull()
    app.unmount()
  })
})
