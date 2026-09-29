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

async function mountPanel(api: ReturnType<typeof desktopApi>) {
  vi.resetModules()
  document.body.innerHTML = ''
  setDesktopApi(api)
  const { createApp, nextTick } = await import('vue')
  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const Component = (await import('./DesktopRuntimePanel.vue')).default
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

function findRestartButton(el: HTMLElement): HTMLButtonElement {
  const button = el.querySelector<HTMLButtonElement>('[data-testid="runtime-restart-gateway"]')
  if (!button) throw new Error('Restart local Gateway button was not rendered')
  return button
}

beforeEach(() => setDesktopApi(undefined))

describe('DesktopRuntimePanel runtime restart', () => {
  it('announces restarting only when the desktop retry succeeds', async () => {
    const getGatewayStatus = vi.fn(async () => ({
      url: 'http://127.0.0.1:1',
      port: 1,
      owned: true,
      status: 'ready' as const,
      logPath: '',
    }))
    const retryStartup = vi.fn(async () => ({ ok: true }))
    const { app, el, toasts } = await mountPanel(desktopApi({
      getGatewayStatus,
      retryStartup,
    }))

    findRestartButton(el).click()
    await settle()

    expect(retryStartup).toHaveBeenCalledTimes(1)
    expect(getGatewayStatus).toHaveBeenCalledTimes(2)
    expect(toasts.value[toasts.value.length - 1]).toMatchObject({
      message: 'Restarting the local runtime…',
      tone: 'info',
    })
    app.unmount()
  })

  it('surfaces an explicit retry failure without claiming the runtime is restarting', async () => {
    const getGatewayStatus = vi.fn(async () => ({
      url: 'http://127.0.0.1:1',
      port: 1,
      owned: true,
      status: 'ready' as const,
      logPath: '',
    }))
    const retryStartup = vi.fn(async () => ({
      ok: false,
      error: 'The previous gateway is still shutting down.',
    }))
    const { app, el, toasts } = await mountPanel(desktopApi({
      getGatewayStatus,
      retryStartup,
    }))

    findRestartButton(el).click()
    await settle()

    expect(retryStartup).toHaveBeenCalledTimes(1)
    expect(getGatewayStatus).toHaveBeenCalledTimes(1)
    expect(toasts.value.map((toast) => toast.message)).not.toContain(
      'Restarting the local runtime…',
    )
    expect(toasts.value[toasts.value.length - 1]).toMatchObject({
      message: 'Restart failed: The previous gateway is still shutting down.',
      tone: 'danger',
    })
    app.unmount()
  })

  it('does not read or render migration state in the runtime panel', async () => {
    const migrationPeekLastResult = vi.fn(async () => ({
      ok: false,
      migrationApplied: true,
      restartOk: false,
      failureCode: 'gateway_restart_failed',
      failureStage: 'restart',
    }))
    const { app, el } = await mountPanel(desktopApi({
      migrationPeekLastResult,
      migrationSummary: async () => ({ ok: true }),
      migrationRun: async () => ({ ok: true }),
    }))

    expect(migrationPeekLastResult).not.toHaveBeenCalled()
    expect(el.querySelector('[data-testid="runtime-migration-restart"]')).toBeNull()
    expect(el.textContent).not.toContain('Data transfer')
    app.unmount()
  })

  it('keeps a startup error visible outside the initially collapsed address and log details', async () => {
    const { app, el } = await mountPanel(desktopApi({
      getGatewayStatus: async () => ({
        url: 'http://127.0.0.1:1',
        port: 1,
        owned: true,
        status: 'error',
        logPath: 'C:/isolated-profile/gateway.log',
        error: 'Port already in use',
      }),
    }))

    const details = el.querySelector<HTMLDetailsElement>('[data-testid="runtime-details"]')!
    const error = el.querySelector('[role="alert"]')!
    expect(details.open).toBe(false)
    expect(error.textContent).toBe('Port already in use')
    expect(details.contains(error)).toBe(false)
    expect(details.textContent).toContain('C:/isolated-profile/gateway.log')
    expect(details.textContent).toContain('http://127.0.0.1:1')
    expect(el.querySelector('[role="status"]')?.textContent).toContain('Error')
    app.unmount()
  })

  it('keeps a status read failure visible and clears it after a successful manual refresh', async () => {
    const getGatewayStatus = vi.fn()
      .mockRejectedValueOnce(new Error('Native bridge unavailable'))
      .mockResolvedValue({ url: '', port: 0, owned: true, status: 'stopped', logPath: '' })
    const { app, el } = await mountPanel(desktopApi({ getGatewayStatus }))
    const details = el.querySelector<HTMLDetailsElement>('[data-testid="runtime-details"]')!
    expect(details.open).toBe(false)
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('Native bridge unavailable')
    expect(el.querySelector('[role="status"]')?.textContent).toContain('Unknown')
    el.querySelector<HTMLButtonElement>('[data-testid="runtime-refresh-status"]')!.click()
    await settle()
    expect(getGatewayStatus).toHaveBeenCalledTimes(2)
    expect(el.querySelector('[role="alert"]')).toBeNull()
    expect(el.querySelector('[role="status"]')?.textContent).toContain('Stopped')
    app.unmount()
  })

  it('does not load desktop updates or duplicate the relocated log action', async () => {
    const getUpdateState = vi.fn(async () => ({ status: 'idle' }))
    const isAutoUpdateEnabled = vi.fn(async () => true)
    const revealGatewayLog = vi.fn(async () => true)
    const { app, el } = await mountPanel(desktopApi({ getUpdateState, isAutoUpdateEnabled, revealGatewayLog }))
    expect(getUpdateState).not.toHaveBeenCalled()
    expect(isAutoUpdateEnabled).not.toHaveBeenCalled()
    expect(el.textContent).not.toContain('Desktop updates')
    expect(el.textContent).not.toContain('Reveal log')
    expect(el.querySelector('[data-testid="support-open-local-logs"]')).toBeNull()
    expect(revealGatewayLog).not.toHaveBeenCalled()
    app.unmount()
  })
})
