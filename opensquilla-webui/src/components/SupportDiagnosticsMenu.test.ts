// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'

const mocks = vi.hoisted(() => ({
  route: { path: '/overview' },
  routerPush: vi.fn(),
  rpcCall: vi.fn(),
  waitForConnection: vi.fn(),
  pushToast: vi.fn(),
  copyText: vi.fn(),
  downloadBlob: vi.fn(),
  filenameFromContentDisposition: vi.fn(),
  fetch: vi.fn(),
  messages: {
    'monitorSupport.title': 'Support & diagnostics',
    'monitorSupport.menuLabel': 'Support and diagnostics actions',
    'monitorSupport.viewLogs': 'View runtime logs',
    'monitorSupport.viewLogsDescription': 'Inspect the gateway log stream',
    'monitorSupport.copyReport': 'Copy current readiness report',
    'monitorSupport.copyReportDescription': 'Share the current connection, configuration, and runtime status',
    'monitorSupport.copySuccess': 'Readiness report copied',
    'monitorSupport.copyFailed': 'Could not copy readiness report: {error}',
    'monitorSupport.downloadBundle': 'Download redacted support bundle',
    'monitorSupport.downloadBundleDescription': 'Includes logs, a configuration summary, and diagnostic results',
    'monitorSupport.privacySummary': 'Conversation content is excluded by default and known credentials are redacted',
    'monitorSupport.bundleTitle': 'Download redacted support bundle',
    'monitorSupport.bundleSubtitle': 'Create a ZIP to share with OpenSquilla support.',
    'monitorSupport.bundleDefaultIncludes': 'Included by default',
    'monitorSupport.bundleReadiness': 'Readiness and diagnostics snapshot',
    'monitorSupport.bundleConfig': 'Redacted configuration summary',
    'monitorSupport.bundleLogs': 'Errors, logs, and trace information',
    'monitorSupport.bundlePlatform': 'Version and platform information',
    'monitorSupport.bundleScopeTitle': 'Recent diagnostic records (up to 1 day)',
    'monitorSupport.bundleScopeBody': 'Error and trace records cover no more than one day; runtime logs follow local rotation, so actual coverage may differ.',
    'monitorSupport.bundleIncludeContentTitle': 'Include conversation content',
    'monitorSupport.bundleIncludeContentBody': 'Enable only when support explicitly asks; this may contain sensitive business information.',
    'monitorSupport.bundleCredentialsTitle': 'Known credential fields are redacted',
    'monitorSupport.bundleCredentialsBody': 'Recognized API key, token, and secret values are redacted before packaging.',
    'monitorSupport.bundleCancel': 'Cancel',
    'monitorSupport.bundleConfirm': 'Generate and download',
    'monitorSupport.bundleFailed': 'Support bundle download failed',
    'monitorSupport.bundleReady': 'Support bundle downloaded',
    'common.close': 'Close',
  } as Record<string, string>,
}))

vi.mock('vue-router', () => ({
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.routerPush }),
}))

vi.mock('vue-i18n', () => ({
  useI18n: () => ({
    t: (key: string, values?: Record<string, unknown>) => {
      let value = mocks.messages[key] ?? key
      for (const [name, replacement] of Object.entries(values ?? {})) {
        value = value.replace(`{${name}}`, String(replacement))
      }
      return value
    },
  }),
}))

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({
    call: mocks.rpcCall,
    waitForConnection: mocks.waitForConnection,
  }),
}))

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast: mocks.pushToast }),
}))

vi.mock('@/utils/browser', () => ({
  copyTextWithFallback: mocks.copyText,
  downloadBlob: mocks.downloadBlob,
  filenameFromContentDisposition: mocks.filenameFromContentDisposition,
}))

vi.mock('@/components/Icon.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      name: 'IconStub',
      props: { name: { type: String, default: '' } },
      setup(props) {
        return () => h('span', { 'data-icon': props.name })
      },
    }),
  }
})

const mountedApps: Array<{ app: App; el: HTMLElement }> = []

async function flush() {
  const { nextTick } = await import('vue')
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
  await nextTick()
  await nextTick()
}

async function mountMenu() {
  const { createApp } = await import('vue')
  const Component = (await import('./SupportDiagnosticsMenu.vue')).default
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Component)
  app.mount(el)
  mountedApps.push({ app, el })
  await flush()
  return { el }
}

beforeEach(() => {
  document.body.innerHTML = ''
  window.sessionStorage.clear()
  vi.clearAllMocks()
  mocks.route.path = '/overview'

  mocks.waitForConnection.mockResolvedValue(undefined)
  mocks.rpcCall.mockResolvedValue({
    status: 'degraded',
    configPath: ['', 'Users', 'dummyuser', '.opensquilla', 'config.toml'].join('/'),
    gatewayUrl: 'ws://127.0.0.1:18791/ws',
  })
  mocks.copyText.mockResolvedValue(undefined)
  mocks.filenameFromContentDisposition.mockReturnValue('opensquilla-support.zip')
  mocks.fetch.mockResolvedValue({
    ok: true,
    blob: vi.fn(async () => new Blob(['bundle'])),
    headers: new Headers({
      'content-disposition': 'attachment; filename="opensquilla-support.zip"',
    }),
  })
  vi.stubGlobal('fetch', mocks.fetch)
})

afterEach(() => {
  while (mountedApps.length) {
    const { app, el } = mountedApps.pop()!
    app.unmount()
    el.remove()
  }
  window.sessionStorage.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('SupportDiagnosticsMenu', () => {
  it('exposes runtime logs, the readiness report, and the support bundle as shared actions', async () => {
    const { el } = await mountMenu()
    const trigger = el.querySelector<HTMLButtonElement>('[data-testid="support-diagnostics-trigger"]')
    expect(trigger).toBeTruthy()
    expect(trigger?.getAttribute('aria-expanded')).toBe('false')

    trigger!.click()
    await flush()

    expect(trigger?.getAttribute('aria-expanded')).toBe('true')
    expect(el.querySelector('[role="menu"]')?.getAttribute('aria-label'))
      .toBe('Support and diagnostics actions')
    expect(el.querySelector('[data-testid="support-view-logs"]')?.textContent)
      .toContain('View runtime logs')
    expect(el.querySelector('[data-testid="support-copy-readiness"]')?.textContent)
      .toContain('Copy current readiness report')
    expect(el.querySelector('[data-testid="support-download-bundle"]')?.textContent)
      .toContain('Download redacted support bundle')
  })

  it('opens runtime logs and hides the self-referential action on the logs route', async () => {
    const { el } = await mountMenu()
    el.querySelector<HTMLButtonElement>('[data-testid="support-diagnostics-trigger"]')!.click()
    await flush()

    el.querySelector<HTMLButtonElement>('[data-testid="support-view-logs"]')!.click()
    await flush()

    expect(mocks.routerPush).toHaveBeenCalledWith('/logs')
    expect(el.querySelector('[role="menu"]')).toBeNull()

    mocks.route.path = '/logs'
    const second = await mountMenu()
    second.el.querySelector<HTMLButtonElement>('[data-testid="support-diagnostics-trigger"]')!.click()
    await flush()

    expect(second.el.querySelector('[data-testid="support-view-logs"]')).toBeNull()
    expect(document.activeElement)
      .toBe(second.el.querySelector('[data-testid="support-copy-readiness"]'))
  })

  it('moves focus through the menu and restores it after closing the bundle dialog', async () => {
    const { el } = await mountMenu()
    const trigger = el.querySelector<HTMLButtonElement>('[data-testid="support-diagnostics-trigger"]')!
    trigger.click()
    await flush()

    const logsItem = el.querySelector<HTMLButtonElement>('[data-testid="support-view-logs"]')!
    const copyItem = el.querySelector<HTMLButtonElement>('[data-testid="support-copy-readiness"]')!
    const bundleItem = el.querySelector<HTMLButtonElement>('[data-testid="support-download-bundle"]')!
    expect(document.activeElement).toBe(logsItem)

    logsItem.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await flush()
    expect(document.activeElement).toBe(copyItem)

    copyItem.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await flush()
    expect(document.activeElement).toBe(bundleItem)

    bundleItem.click()
    await flush()
    const cancel = Array.from(document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button'))
      .find(button => button.textContent?.trim() === 'Cancel')
    expect(document.activeElement).toBe(cancel)

    cancel!.click()
    await flush()
    expect(document.activeElement).toBe(trigger)
  })

  it('copies a fresh doctor.status report after normalizing local home paths', async () => {
    const { el } = await mountMenu()
    el.querySelector<HTMLButtonElement>('[data-testid="support-diagnostics-trigger"]')!.click()
    await flush()
    el.querySelector<HTMLButtonElement>('[data-testid="support-copy-readiness"]')!.click()
    await flush()

    expect(mocks.waitForConnection).toHaveBeenCalledTimes(1)
    expect(mocks.rpcCall).toHaveBeenCalledWith('doctor.status', {
      agentId: 'main',
      deep: true,
    })
    expect(mocks.copyText).toHaveBeenCalledTimes(1)

    const copied = String(mocks.copyText.mock.calls[0][0])
    expect(copied).not.toContain('dummyuser')
    const report = JSON.parse(copied) as Record<string, unknown>
    expect(report.configPath).toBe('~/.opensquilla/config.toml')
    expect(report.gatewayUrl).toBe('ws://127.0.0.1:18791/ws')
    expect(Number.isNaN(Date.parse(String(report.copiedAt)))).toBe(false)
    expect(mocks.pushToast).toHaveBeenCalledWith('Readiness report copied', { tone: 'ok' })
  })

  it('always requests a one-day bundle and keeps conversation content opt-in', async () => {
    window.sessionStorage.setItem('opensquilla.wsToken', 'test-owner-token')
    const { el } = await mountMenu()
    const trigger = el.querySelector<HTMLButtonElement>('[data-testid="support-diagnostics-trigger"]')!
    trigger.click()
    await flush()
    el.querySelector<HTMLButtonElement>('[data-testid="support-download-bundle"]')!.click()
    await flush()

    const checkbox = document.querySelector<HTMLInputElement>('[role="dialog"] input[type="checkbox"]')
    expect(checkbox?.checked).toBe(false)
    const confirm = Array.from(document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button'))
      .find(button => button.textContent?.includes('Generate and download'))
    expect(confirm).toBeTruthy()
    confirm!.click()
    await flush()

    expect(mocks.fetch).toHaveBeenCalledTimes(1)
    const [url, init] = mocks.fetch.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/diagnostics/bundle')
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('same-origin')
    expect(init.headers).toEqual({
      'Content-Type': 'application/json',
      Authorization: 'Bearer test-owner-token',
    })
    expect(JSON.parse(String(init.body))).toEqual({
      include_content: false,
      days: 1,
    })
    expect(mocks.downloadBlob).toHaveBeenCalledWith(expect.any(Blob), 'opensquilla-support.zip')
    expect(mocks.pushToast).toHaveBeenCalledWith('Support bundle downloaded', { tone: 'ok' })
    expect(document.activeElement).toBe(trigger)
  })
})
