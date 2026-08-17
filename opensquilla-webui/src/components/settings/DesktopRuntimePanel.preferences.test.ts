// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'

const settle = () => new Promise((resolve) => setTimeout(resolve, 20))

function setDesktopApi(api: unknown): void {
  ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = api
}

function desktopApi(overrides: Record<string, unknown> = {}) {
  return {
    getOsLocale: async () => 'en',
    isAutoUpdateEnabled: async () => true,
    getGatewayStatus: async () => ({
      url: 'http://127.0.0.1:1',
      port: 1,
      owned: true,
      status: 'ready',
      logPath: '',
    }),
    ...overrides,
  }
}

async function mountPanel(
  api: ReturnType<typeof desktopApi>,
  kind: 'close' | 'preview' = 'close',
) {
  vi.resetModules()
  document.body.innerHTML = ''
  setDesktopApi(api)
  const { createApp, nextTick } = await import('vue')
  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const Component = kind === 'close'
    ? (await import('./DesktopCloseBehaviorSetting.vue')).default
    : (await import('./DesktopPreviewModeSetting.vue')).default
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Component)
  app.use(i18n)
  app.mount(el)
  await settle()
  await nextTick()
  const { toasts } = (await import('@/composables/useToasts')).useToasts()
  toasts.value = []
  return { app, el, toasts }
}

function findCloseBehaviorSelect(el: HTMLElement): HTMLSelectElement {
  const select = el.querySelector<HTMLSelectElement>(
    '[data-testid="desktop-close-behavior-select"]',
  )
  if (!select) throw new Error('Desktop close behavior select was not rendered')
  return select
}

function findPreviewModeSelect(el: HTMLElement): HTMLSelectElement {
  const select = el.querySelector<HTMLSelectElement>(
    '[data-testid="desktop-preview-mode-select"]',
  )
  if (!select) throw new Error('Desktop preview mode select was not rendered')
  return select
}

beforeEach(() => setDesktopApi(undefined))

describe('DesktopRuntimePanel close behavior preference', () => {
  it('stays hidden when an older desktop shell does not expose the preference bridge', async () => {
    const { app, el } = await mountPanel(desktopApi())

    expect(el.querySelector('[data-testid="desktop-close-behavior"]')).toBeNull()
    app.unmount()
  })

  it('loads the current preference and saves a selection immediately', async () => {
    const getDesktopPreferences = vi.fn(async () => ({
      mainWindowCloseBehavior: 'quit' as const,
      canRunInBackground: true,
      platform: 'darwin' as const,
    }))
    const saveDesktopPreferences = vi.fn(async (payload: {
      mainWindowCloseBehavior: 'background' | 'quit' | 'ask'
    }) => ({
      mainWindowCloseBehavior: payload.mainWindowCloseBehavior,
      canRunInBackground: true,
      platform: 'darwin' as const,
    }))
    const { app, el } = await mountPanel(desktopApi({
      getDesktopPreferences,
      saveDesktopPreferences,
    }))
    const select = findCloseBehaviorSelect(el)

    expect(getDesktopPreferences).toHaveBeenCalledTimes(1)
    expect(select.value).toBe('quit')
    // The label/select association must hold whenever the select exists.
    const label = el.querySelector<HTMLLabelElement>('.control-row__label')
    expect(label?.getAttribute('for')).toBe('desktop-close-behavior-select')

    select.value = 'ask'
    select.dispatchEvent(new Event('change', { bubbles: true }))
    await settle()

    expect(saveDesktopPreferences).toHaveBeenCalledWith({
      mainWindowCloseBehavior: 'ask',
    })
    expect(select.value).toBe('ask')
    app.unmount()
  })

  it('rolls back the selection and shows a danger toast when saving fails', async () => {
    const saveDesktopPreferences = vi.fn(async () => {
      throw new Error('preferences are read-only')
    })
    const { app, el, toasts } = await mountPanel(desktopApi({
      getDesktopPreferences: async () => ({
        mainWindowCloseBehavior: 'background' as const,
        canRunInBackground: true,
        platform: 'win32' as const,
      }),
      saveDesktopPreferences,
    }))
    const select = findCloseBehaviorSelect(el)

    select.value = 'ask'
    select.dispatchEvent(new Event('change', { bubbles: true }))
    await settle()

    expect(select.value).toBe('background')
    expect(toasts.value[toasts.value.length - 1]).toMatchObject({
      message: 'Could not save the window close setting: preferences are read-only',
      tone: 'danger',
    })
    app.unmount()
  })

  it('keeps the row rendered with an inline error and Retry when reading fails', async () => {
    const getDesktopPreferences = vi.fn()
      .mockRejectedValueOnce(new Error('bridge timed out'))
      .mockResolvedValue({
        mainWindowCloseBehavior: 'ask' as const,
        canRunInBackground: true,
        platform: 'darwin' as const,
      })
    const { app, el } = await mountPanel(desktopApi({
      getDesktopPreferences,
      saveDesktopPreferences: vi.fn(),
    }))

    const row = el.querySelector<HTMLElement>('[data-testid="desktop-close-behavior"]')
    if (!row) throw new Error('Desktop close behavior row was not rendered')
    expect(row.textContent).toContain('When closing the main window')
    expect(row.textContent).toContain(
      'Could not read the window close setting: bridge timed out',
    )
    expect(el.querySelector('[data-testid="desktop-close-behavior-select"]')).toBeNull()
    // With the select absent, the label must not carry a dangling `for` —
    // assistive tech resolves that to nothing, which is worse than no `for`.
    const label = el.querySelector<HTMLLabelElement>('.control-row__label')
    if (!label) throw new Error('Close behavior label was not rendered')
    expect(label.hasAttribute('for')).toBe(false)

    const retry = el.querySelector<HTMLButtonElement>(
      '[data-testid="desktop-close-behavior-retry"]',
    )
    if (!retry) throw new Error('Retry button was not rendered')
    expect(retry.textContent).toContain('Retry')
    retry.click()
    await settle()

    expect(getDesktopPreferences).toHaveBeenCalledTimes(2)
    expect(el.textContent).not.toContain('Could not read the window close setting')
    expect(el.querySelector('[data-testid="desktop-close-behavior-retry"]')).toBeNull()
    expect(findCloseBehaviorSelect(el).value).toBe('ask')
    // Once the select is back, so is the association.
    expect(label.getAttribute('for')).toBe('desktop-close-behavior-select')
    app.unmount()
  })

  it('flags a stored selection this platform cannot honor without rewriting it', async () => {
    const saveDesktopPreferences = vi.fn()
    const { app, el } = await mountPanel(desktopApi({
      getDesktopPreferences: async () => ({
        mainWindowCloseBehavior: 'background' as const,
        canRunInBackground: false,
        platform: 'linux' as const,
      }),
      saveDesktopPreferences,
    }))
    const select = findCloseBehaviorSelect(el)

    expect(select.value).toBe('background')
    const mismatch = el.querySelector<HTMLElement>(
      '[data-testid="desktop-close-behavior-mismatch"]',
    )
    if (!mismatch) throw new Error('Unavailable-selection notice was not rendered')
    expect(mismatch.textContent).toContain(
      'Background mode is unavailable on this platform, so closing the window will quit the app instead.',
    )
    expect(saveDesktopPreferences).not.toHaveBeenCalled()
    app.unmount()
  })

  it('limits close behavior to Quit when the shell cannot stay running', async () => {
    const { app, el } = await mountPanel(desktopApi({
      getDesktopPreferences: async () => ({
        mainWindowCloseBehavior: 'quit' as const,
        canRunInBackground: false,
        platform: 'linux' as const,
      }),
      saveDesktopPreferences: async () => ({
        mainWindowCloseBehavior: 'quit' as const,
        canRunInBackground: false,
        platform: 'linux' as const,
      }),
    }))

    const background = findCloseBehaviorSelect(el)
      .querySelector<HTMLOptionElement>('option[value="background"]')
    const ask = findCloseBehaviorSelect(el)
      .querySelector<HTMLOptionElement>('option[value="ask"]')
    expect(background?.disabled).toBe(true)
    expect(ask?.disabled).toBe(true)
    expect(el.textContent).toContain('Background mode is unavailable on this platform.')
    app.unmount()
  })

  it('loads and saves the default webpage preview mode', async () => {
    const saveDesktopPreferences = vi.fn(async (payload: {
      workbenchPreviewMode?: 'full' | 'offline'
    }) => ({
      mainWindowCloseBehavior: 'quit' as const,
      canRunInBackground: true,
      platform: 'darwin' as const,
      schemaVersion: 2,
      workbenchPreviewMode: payload.workbenchPreviewMode ?? 'offline',
      effectiveWorkbenchPreviewMode: payload.workbenchPreviewMode ?? 'offline',
      workbenchPreviewNoticeShown: false,
      workbenchPreviewForcedOffline: false,
    }))
    const { app, el } = await mountPanel(desktopApi({
      getDesktopPreferences: async () => ({
        mainWindowCloseBehavior: 'quit' as const,
        canRunInBackground: true,
        platform: 'darwin' as const,
        schemaVersion: 2,
        workbenchPreviewMode: 'offline' as const,
        effectiveWorkbenchPreviewMode: 'offline' as const,
        workbenchPreviewNoticeShown: false,
        workbenchPreviewForcedOffline: false,
      }),
      saveDesktopPreferences,
    }), 'preview')
    const select = findPreviewModeSelect(el)

    expect(select.value).toBe('offline')
    select.value = 'full'
    select.dispatchEvent(new Event('change', { bubbles: true }))
    await settle()

    expect(saveDesktopPreferences).toHaveBeenCalledWith({
      workbenchPreviewMode: 'full',
    })
    expect(select.value).toBe('full')
    app.unmount()
  })

  it('explains when the process-level incident switch forces offline previews', async () => {
    const { app, el } = await mountPanel(desktopApi({
      getDesktopPreferences: async () => ({
        mainWindowCloseBehavior: 'quit' as const,
        canRunInBackground: true,
        platform: 'linux' as const,
        schemaVersion: 2,
        workbenchPreviewMode: 'full' as const,
        effectiveWorkbenchPreviewMode: 'offline' as const,
        workbenchPreviewNoticeShown: false,
        workbenchPreviewForcedOffline: true,
      }),
      saveDesktopPreferences: vi.fn(),
    }), 'preview')

    expect(findPreviewModeSelect(el).value).toBe('full')
    const warning = el.querySelector<HTMLElement>('[data-testid="desktop-preview-mode-forced"]')
    expect(warning?.textContent).toContain('OPENSQUILLA_PREVIEW_FORCE_OFFLINE')
    app.unmount()
  })
})
