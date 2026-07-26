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
  const button = Array.from(el.querySelectorAll<HTMLButtonElement>('button'))
    .find((candidate) => candidate.textContent?.includes('Restart runtime'))
  if (!button) throw new Error('Restart runtime button was not rendered')
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
})
