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

async function mountIndicator(api: ReturnType<typeof desktopUpdateApi>) {
  vi.resetModules()
  document.body.innerHTML = ''
  setDesktopApi(api)
  const { createApp, nextTick } = await import('vue')
  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const Component = (await import('./DesktopUpdateIndicator.vue')).default
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

describe('DesktopUpdateIndicator', () => {
  it('renders a compact available update control and downloads only after user action', async () => {
    const api = desktopUpdateApi({ status: 'available', latestVersion: '99.0.0' })
    const { app, el } = await mountIndicator(api)

    const trigger = el.querySelector('[data-testid="desktop-update-indicator"]') as HTMLButtonElement
    expect(trigger).toBeTruthy()
    expect(trigger.textContent).toContain('Update')
    expect(trigger.textContent).toContain('99.0.0')

    trigger.click()
    await settle()
    ;(document.querySelector('[data-testid="desktop-update-download"]') as HTMLButtonElement).click()
    await settle()

    expect(api.downloadUpdate).toHaveBeenCalledTimes(1)
    app.unmount()
  })

  it('renders relaunch action for a downloaded update', async () => {
    const api = desktopUpdateApi({ status: 'downloaded', latestVersion: '99.0.0' })
    const { app, el } = await mountIndicator(api)

    const trigger = el.querySelector('[data-testid="desktop-update-indicator"]') as HTMLButtonElement
    trigger.click()
    await settle()
    ;(document.querySelector('[data-testid="desktop-update-relaunch"]') as HTMLButtonElement).click()
    await settle()

    expect(api.relaunchToUpdate).toHaveBeenCalledTimes(1)
    app.unmount()
  })

  it('offers the versioned installer for a managed Windows update', async () => {
    const api = desktopUpdateApi({
      status: 'available',
      canNativeInstall: false,
      installMode: 'manual',
      source: 'oss',
    }, {
      isAutoUpdateEnabled: async () => false,
    })
    const { app, el } = await mountIndicator(api)

    ;(el.querySelector('[data-testid="desktop-update-indicator"]') as HTMLButtonElement).click()
    await settle()
    const download = document.querySelector('[data-testid="desktop-update-download"]') as HTMLButtonElement
    expect(download.textContent).toContain('Download installer')
    download.click()
    await settle()

    expect(api.downloadUpdate).toHaveBeenCalledTimes(1)
    app.unmount()
  })

  it('reveals a verified Windows installer without offering native relaunch', async () => {
    const api = desktopUpdateApi({
      status: 'downloaded',
      canNativeInstall: false,
      installMode: 'manual',
      source: 'oss',
    }, {
      isAutoUpdateEnabled: async () => false,
    })
    const { app, el } = await mountIndicator(api)

    ;(el.querySelector('[data-testid="desktop-update-indicator"]') as HTMLButtonElement).click()
    await settle()
    expect(document.body.textContent).toContain('Verified installer ready')
    expect(document.querySelector('[data-testid="desktop-update-relaunch"]')).toBeNull()
    ;(document.querySelector('[data-testid="desktop-update-show-installer"]') as HTMLButtonElement).click()
    await settle()

    expect(api.downloadUpdate).toHaveBeenCalledTimes(1)
    app.unmount()
  })

  it('keeps a managed unsupported error visible and localizes its error code', async () => {
    const api = desktopUpdateApi({
      status: 'error',
      canNativeInstall: false,
      installMode: 'unsupported',
      errorCode: 'manifest_invalid',
      error: 'GitHub releases request failed: 403',
    }, {
      isAutoUpdateEnabled: async () => false,
    })
    const { app, el } = await mountIndicator(api)

    const trigger = el.querySelector('[data-testid="desktop-update-indicator"]') as HTMLButtonElement
    expect(trigger).toBeTruthy()
    trigger.click()
    await settle()
    expect(document.body.textContent).toContain('release-channel data is invalid')
    expect(document.body.textContent).not.toContain('403')
    expect(document.querySelector('[data-testid="desktop-update-download"]')).toBeNull()

    app.unmount()
  })

  it('localizes installer integrity failures without exposing raw transport detail', async () => {
    const api = desktopUpdateApi({
      status: 'error',
      canNativeInstall: false,
      installMode: 'manual',
      errorCode: 'integrity_failed',
      error: 'mirror digest was deadbeef',
    }, {
      isAutoUpdateEnabled: async () => false,
    })
    const { app, el } = await mountIndicator(api)

    ;(el.querySelector('[data-testid="desktop-update-indicator"]') as HTMLButtonElement).click()
    await settle()
    expect(document.body.textContent).toContain('failed integrity verification and was deleted')
    expect(document.body.textContent).not.toContain('deadbeef')
    app.unmount()
  })
})
