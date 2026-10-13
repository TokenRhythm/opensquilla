// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { LocaleCode } from '@/i18n'

const settle = () => vi.isFakeTimers()
  ? vi.advanceTimersByTimeAsync(20)
  : new Promise((resolve) => setTimeout(resolve, 20))

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

async function mountPanel(api: ReturnType<typeof desktopUpdateApi>, locale: LocaleCode = 'en') {
  vi.resetModules()
  document.body.innerHTML = ''
  setDesktopApi(api)
  const { createApp, nextTick } = await import('vue')
  const { default: i18n, loadLocaleMessages } = await import('@/i18n')
  await loadLocaleMessages(locale)
  i18n.global.locale.value = locale
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
  it.each([
    ['en', 'may interrupt running tasks'], ['zh-Hans', '进行中的任务可能中断'],
    ['de', 'laufende Aufgaben unterbrechen'], ['fr', 'peut interrompre les tâches en cours'],
    ['es', 'puede interrumpir las tareas en curso'], ['ja', '実行中のタスクが中断される場合があります'],
  ] as const)('explains the Windows installation consequence in %s', async (locale, expected) => {
    const { app, el } = await mountPanel(desktopUpdateApi({
      status: 'downloaded', canInstall: true, canNativeInstall: false, installMode: 'manual',
    }), locale)
    expect(el.querySelector('.control-row__desc')?.textContent).toContain(expected)
    expect(el.querySelectorAll('[role="dialog"]')).toHaveLength(0)
    app.unmount()
  })

  it.each([0, 37.8, 120, null])('shows real bounded download progress without inventing an unknown percentage (%s)', async (progress) => {
    const { app, el } = await mountPanel(desktopUpdateApi({ status: 'downloading', progress }))
    const track = el.querySelector('[role="progressbar"]')!
    expect(track).not.toBeNull()
    expect(track.getAttribute('aria-valuenow')).toBe(progress === null ? null : String(Math.min(100, Math.round(progress))))
    expect(el.querySelector('.settings-update__percentage')?.textContent ?? '').toBe(progress === null ? '' : `${Math.min(100, Math.round(progress))}%`)
    expect(Array.from(el.querySelectorAll('button')).every(button => button.disabled)).toBe(true)
    app.unmount()
  })

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

  it.each([
    ['oss', false], ['github', false], ['oss', undefined], ['github', undefined],
  ] as const)('keeps manual reveal primary for %s with canInstall=%s', async (source, canInstall) => {
    const api = desktopUpdateApi({
      status: 'downloaded',
      canNativeInstall: false,
      ...(canInstall === undefined ? {} : { canInstall }),
      installMode: 'manual',
      source,
    }, {
      isAutoUpdateEnabled: async () => false,
    })
    const { app, el } = await mountPanel(api)

    expect(el.textContent).toContain('Verified')
    const description = el.querySelector('.control-row__desc') as HTMLElement
    expect(description.textContent).toContain('installer has been verified')
    expect(description.textContent).toContain('run it manually when ready')
    expect(description.textContent).not.toContain('GitHub')
    const show = el.querySelector('[data-testid="settings-update-download"]') as HTMLButtonElement
    expect(show.textContent).toContain('Show installer')
    expect(show.classList.contains('btn--primary')).toBe(true)
    expect(show.disabled).toBe(false)
    expect(api.downloadUpdate).not.toHaveBeenCalled()
    show.click()
    await settle()

    expect(api.downloadUpdate).toHaveBeenCalledTimes(1)
    expect(api.relaunchToUpdate).not.toHaveBeenCalled()
    expect(el.querySelector('[data-testid="settings-update-relaunch"]')).toBeNull()
    app.unmount()
  })

  it('offers installation as the primary Windows action while keeping installer reveal available', async () => {
    const api = desktopUpdateApi({
      status: 'downloaded', canNativeInstall: false, canInstall: true, installMode: 'manual',
    }, {
      isAutoUpdateEnabled: async () => false,
      downloadUpdate: vi.fn(async () => ({
        status: 'downloaded', canCheck: true, canNativeInstall: false, canInstall: true, installMode: 'manual',
      })),
      relaunchToUpdate: vi.fn(async () => ({
        status: 'applying', canCheck: true, canNativeInstall: false, canInstall: false, installMode: 'manual',
      })),
    })
    const { app, el } = await mountPanel(api)
    const install = el.querySelector('[data-testid="settings-update-relaunch"]') as HTMLButtonElement
    const reveal = el.querySelector('[data-testid="settings-update-download"]') as HTMLButtonElement
    expect(install.textContent).toContain('Quit and install')
    expect(install.classList.contains('btn--primary')).toBe(true)
    expect(reveal.textContent).toContain('Show installer')
    expect(reveal.classList.contains('btn--ghost')).toBe(true)
    expect(el.textContent).not.toContain('shown in its folder')
    reveal.click()
    await settle()
    expect(api.downloadUpdate).toHaveBeenCalledTimes(1)
    expect(api.relaunchToUpdate).not.toHaveBeenCalled()
    expect(install.disabled).toBe(false)
    install.click()
    await settle()
    expect(api.relaunchToUpdate).toHaveBeenCalledTimes(1)
    expect(el.textContent).toContain('Preparing installation')
    expect(el.textContent).toContain('When preparation is complete, OpenSquilla will quit')
    expect(el.querySelector('[data-testid="settings-update-relaunch"]')).toBe(install)
    expect(install.disabled).toBe(true)
    expect(install.getAttribute('aria-busy')).toBe('true')
    install.click()
    expect(api.relaunchToUpdate).toHaveBeenCalledTimes(1)
    expect(el.querySelector('[data-testid="settings-update-download"]')).toBeNull()
    expect((el.querySelector('button') as HTMLButtonElement).disabled).toBe(true)
    app.unmount()
  })

  it('does not install when an explicit shell permission is false', async () => {
    const api = desktopUpdateApi({ status: 'downloaded', canInstall: false })
    const { app, el } = await mountPanel(api)
    expect(el.querySelector('[data-testid="settings-update-relaunch"]')).toBeNull()
    expect(api.relaunchToUpdate).not.toHaveBeenCalled()
    app.unmount()
  })

  it.each([
    ['Close the other installed copy of OpenSquilla and retry.', 'Close the other installed copy of OpenSquilla and retry.'],
    [null, 'could not prepare or open the installer'],
  ])('retains a downloaded installer and presents its handoff recovery instruction (%s)', async (error, expected) => {
    const api = desktopUpdateApi({
      status: 'downloaded', canNativeInstall: false, canInstall: true, installMode: 'manual',
      errorCode: 'install_failed', error,
    }, { isAutoUpdateEnabled: async () => false })
    const { app, el } = await mountPanel(api)
    expect(el.textContent).toContain('Needs attention')
    expect(el.textContent).toContain(expected)
    expect(el.querySelector('[data-testid="settings-update-relaunch"]')).not.toBeNull()
    expect(el.querySelector('[data-testid="settings-update-download"]')).not.toBeNull()
    ;(el.querySelector('[data-testid="settings-update-relaunch"]') as HTMLButtonElement).click()
    await settle()
    expect(api.relaunchToUpdate).toHaveBeenCalledTimes(1)
    expect(api.checkForUpdates).not.toHaveBeenCalled()
    expect(api.downloadUpdate).not.toHaveBeenCalled()
    app.unmount()
  })

  it('retries a failed update through check exactly once and keeps the action first', async () => {
    let resolveCheck!: (state: Record<string, unknown>) => void
    const api = desktopUpdateApi({ status: 'error', errorCode: 'download_failed' }, {
      checkForUpdates: vi.fn(() => new Promise(resolve => { resolveCheck = resolve })),
    })
    const { app, el } = await mountPanel(api)
    const retry = el.querySelector('[data-testid="settings-update-check"]') as HTMLButtonElement
    expect(retry.textContent).toContain('Retry')
    expect(retry.classList.contains('btn--primary')).toBe(true)
    expect(el.querySelector('.settings-update__actions button')).toBe(retry)
    expect(el.querySelectorAll('[data-testid="settings-update-check"]')).toHaveLength(1)
    retry.click()
    await settle()
    expect(retry.disabled).toBe(true)
    retry.click()
    expect(api.checkForUpdates).toHaveBeenCalledTimes(1)
    expect(api.downloadUpdate).not.toHaveBeenCalled()
    expect(api.relaunchToUpdate).not.toHaveBeenCalled()
    resolveCheck(await api.getUpdateState())
    await settle()
    expect(retry.disabled).toBe(false)
    app.unmount()
  })

  it('acknowledges a reminder only after success and restores Later at its existing deadline', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-13T00:00:00Z'))
    let resolveDismiss!: (state: Record<string, unknown>) => void
    const expiresAt = Date.now() + 24 * 60 * 60 * 1000
    const api = desktopUpdateApi({ status: 'downloaded', canInstall: true }, {
      dismissUpdate: vi.fn(() => new Promise(resolve => { resolveDismiss = resolve })),
    })
    const { app, el } = await mountPanel(api)
    try {
      const later = el.querySelector('[data-testid="settings-update-later"]') as HTMLButtonElement
      later.click()
      await settle()
      expect(later.disabled).toBe(true)
      expect(later.textContent).toContain('Later')
      expect(el.textContent).not.toContain('Reminder snoozed')
      resolveDismiss({ ...await api.getUpdateState(), snoozedUntil: new Date(expiresAt).toISOString() })
      await settle()
      expect(el.querySelector('[data-testid="settings-update-later"]')).toBe(later)
      expect(later.textContent).toContain('Reminder snoozed')
      expect(later.disabled).toBe(true)
      expect((el.querySelector('[data-testid="settings-update-relaunch"]') as HTMLButtonElement).disabled).toBe(false)
      later.click()
      expect(api.dismissUpdate).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(expiresAt - Date.now() - 1)
      expect(later.textContent).toContain('Reminder snoozed')
      await vi.advanceTimersByTimeAsync(1)
      expect(later.textContent).toContain('Later')
      expect(later.disabled).toBe(false)
    } finally {
      app.unmount()
      vi.clearAllTimers()
      vi.useRealTimers()
    }
  })

  it('does not acknowledge a reminder when the dismiss IPC rejects', async () => {
    let rejectDismiss!: (reason: Error) => void
    const api = desktopUpdateApi({ status: 'downloaded', canInstall: true }, {
      dismissUpdate: vi.fn(() => new Promise((_resolve, reject) => { rejectDismiss = reject })),
    })
    const { app, el } = await mountPanel(api)
    const later = el.querySelector('[data-testid="settings-update-later"]') as HTMLButtonElement
    later.click()
    await settle()
    expect(later.textContent).toContain('Later')
    rejectDismiss(new Error('Could not defer reminder'))
    await settle()
    expect(el.textContent).toContain('Could not defer reminder')
    expect(el.textContent).not.toContain('Reminder snoozed')
    expect(later.disabled).toBe(false)
    app.unmount()
  })
  it('links to the version-specific release page in an external browser without installing', async () => {
    const api = desktopUpdateApi({
      status: 'downloaded', canInstall: true,
      releaseUrl: 'https://example.invalid/installer.exe',
      releaseNotesUrl: 'https://github.com/TokenRhythm/opensquilla/releases/tag/v99.0.0',
    })
    const { app, el } = await mountPanel(api)
    const link = el.querySelector<HTMLAnchorElement>('[data-testid="settings-update-release-notes"]')!
    expect(link.textContent).toContain('View what’s new')
    expect(link.href).toBe('https://github.com/TokenRhythm/opensquilla/releases/tag/v99.0.0')
    expect(link.target).toBe('_blank')
    expect(link.rel).toBe('noopener noreferrer')
    expect(api.relaunchToUpdate).not.toHaveBeenCalled()
    expect(api.downloadUpdate).not.toHaveBeenCalled()
    app.unmount()
  })
  it.each([null, 'javascript:alert(1)', 'file:///C:/installer.exe', 'https://user:pass@example.com/v2'])(
    'omits absent or unsafe release notes without guessing from installer URL (%s)', async releaseNotesUrl => {
      const { app, el } = await mountPanel(desktopUpdateApi({
        status: 'downloaded', releaseUrl: 'https://example.invalid/installer.exe', releaseNotesUrl,
      }))
      expect(el.querySelector('[data-testid="settings-update-release-notes"]')).toBeNull()
      expect(el.querySelector('[data-testid="settings-update-relaunch"]')).not.toBeNull()
      app.unmount()
    },
  )

})
