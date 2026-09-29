// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import { OBSERVABILITY_KEY, type GatewayLogBatch } from '@/modules/observability'
import GatewayLogViewer from './GatewayLogViewer.vue'

const mocks = vi.hoisted(() => ({
  downloadBlob: vi.fn(),
  pushToast: vi.fn(),
}))
vi.mock('@/utils/browser', () => ({ downloadBlob: mocks.downloadBlob }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: mocks.pushToast }) }))

const tailLogs = vi.fn()
const mounted: App[] = []

async function flush() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
  await nextTick()
  await nextTick()
}

async function mountViewer(overrides: Partial<GatewayAccess> = {}) {
  const access = reactive({
    isAvailable: true,
    connectionHealth: 'healthy',
    isAuthenticated: true,
    isLocalOwner: false,
    supportBundleUnavailableReason: 'differentGateway',
    subscriptionEpoch: 1,
    ...overrides,
  }) as GatewayAccess
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(GatewayLogViewer)
  app.use(i18n)
  app.provide(GATEWAY_ACCESS_KEY, access)
  app.provide(OBSERVABILITY_KEY, { tailLogs } as never)
  app.mount(el)
  mounted.push(app)
  await flush()
  const trigger = el.querySelector<HTMLButtonElement>('[data-testid="support-view-logs"]')!
  return { app, el, access, trigger }
}

function refreshButton() {
  return document.querySelector<HTMLButtonElement>('[data-testid="gateway-logs-refresh"]')!
}

function saveButton() {
  return document.querySelector<HTMLButtonElement>('[data-testid="gateway-logs-save"]')!
}

function closeButton() {
  return document.querySelector<HTMLButtonElement>('[role="dialog"] button[aria-label="Close"]')!
}

beforeEach(() => {
  document.body.innerHTML = ''
  tailLogs.mockReset()
  mocks.downloadBlob.mockReset()
  mocks.pushToast.mockReset()
  tailLogs.mockResolvedValue({ entries: ['Gateway ready'], truncated: false })
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('GatewayLogViewer', () => {
  it('fetches once on open and only refreshes when asked, including for remote Gateways', async () => {
    const { el, trigger } = await mountViewer()
    expect(trigger.disabled).toBe(false)
    expect(trigger.hasAttribute('aria-describedby')).toBe(false)
    expect(el.querySelector('#gateway-logs-unavailable')).toBeNull()
    const anchor = el.querySelector<HTMLElement>('#settings-gateway-logs')!
    anchor.focus()
    await flush()
    expect(tailLogs).not.toHaveBeenCalled()
    trigger.focus()
    trigger.click()
    await flush()
    expect(tailLogs).toHaveBeenCalledExactlyOnceWith({ signal: expect.any(AbortSignal) })
    expect(document.querySelector('pre')?.textContent).toBe('Gateway ready')
    expect(document.activeElement).toBe(closeButton())

    vi.useFakeTimers()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(tailLogs).toHaveBeenCalledTimes(1)
    tailLogs.mockResolvedValue({ entries: ['New snapshot'], truncated: true })
    refreshButton().click()
    await flush()
    expect(tailLogs).toHaveBeenCalledTimes(2)
    expect(document.querySelector('pre')?.textContent).toBe('New snapshot')
  })

  it('renders log records as inert text, with no HTML interpretation', async () => {
    tailLogs.mockResolvedValue({
      entries: ['<img src=x onerror="alert(1)">', { message: '<script>danger()</script>', level: 'error' }],
      truncated: false,
    })
    const { trigger } = await mountViewer()
    trigger.click()
    await flush()
    expect(document.querySelector('pre')?.textContent).toContain('<img src=x onerror="alert(1)">')
    expect(document.querySelector('pre')?.textContent).toContain('"level":"error"')
    expect(document.querySelector('pre img, pre script')).toBeNull()
  })

  it('saves exactly the displayed snapshot as UTF-8 text without another read', async () => {
    const entries = Array.from({ length: 200 }, (_, index) => `日志 ${index}: <error> 🔎`)
    tailLogs.mockResolvedValue({ entries, truncated: true })
    const { trigger } = await mountViewer()
    trigger.click()
    await flush()
    const displayedText = document.querySelector('pre')!.textContent
    expect(saveButton().disabled).toBe(false)
    saveButton().click()
    expect(mocks.downloadBlob).toHaveBeenCalledOnce()
    const [blob, filename] = mocks.downloadBlob.mock.calls[0] as [Blob, string]
    expect(blob.type).toBe('text/plain;charset=utf-8')
    expect(await blob.text()).toBe(displayedText)
    expect(filename).toMatch(/^gateway-logs-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z\.txt$/)
    expect(tailLogs).toHaveBeenCalledOnce()
  })

  it('does not save an obsolete snapshot while refreshing, after failure, or when empty', async () => {
    const { trigger } = await mountViewer()
    trigger.click()
    await flush()
    expect(saveButton().disabled).toBe(false)
    let reject!: (error: Error) => void
    tailLogs.mockReturnValueOnce(new Promise<GatewayLogBatch>((_resolve, fail) => { reject = fail }))
    refreshButton().click()
    await flush()
    expect(saveButton().disabled).toBe(true)
    saveButton().click()
    reject(new Error('read failed'))
    await flush()
    expect(saveButton().disabled).toBe(true)
    saveButton().click()
    tailLogs.mockResolvedValueOnce({ entries: [], truncated: false })
    refreshButton().click()
    await flush()
    expect(saveButton().disabled).toBe(true)
    saveButton().click()
    expect(mocks.downloadBlob).not.toHaveBeenCalled()
  })

  it('rejects saving a previous Gateway snapshot before the connection watcher runs', async () => {
    const { access, trigger } = await mountViewer()
    trigger.click()
    await flush()
    const save = saveButton()
    expect(save.disabled).toBe(false)
    ;(access as { subscriptionEpoch: number }).subscriptionEpoch += 1
    save.click()
    expect(mocks.downloadBlob).not.toHaveBeenCalled()
    await flush()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
  })

  it('reports a local save failure and allows retry without fetching again', async () => {
    mocks.downloadBlob.mockImplementationOnce(() => { throw new Error('download blocked') })
    const { trigger } = await mountViewer()
    trigger.click()
    await flush()
    saveButton().click()
    expect(mocks.pushToast).toHaveBeenCalledWith(i18n.global.t('gatewayLogs.saveFailed'), { tone: 'danger' })
    expect(saveButton().disabled).toBe(false)
    saveButton().click()
    expect(mocks.downloadBlob).toHaveBeenCalledTimes(2)
    expect(tailLogs).toHaveBeenCalledOnce()
  })

  it.each([
    [{ isAvailable: false }, 'gatewayLogs.connectionRequired'],
    [{ connectionHealth: 'suspect' }, 'gatewayLogs.connectionRequired'],
    [{ connectionPhase: 'checking' }, 'gatewayLogs.connectionRequired'],
    [{ isResuming: true }, 'gatewayLogs.connectionRequired'],
    [{ isAuthenticated: false, isLocalOwner: false }, 'gatewayLogs.authenticationRequired'],
  ] as const)('keeps an unavailable log entry discoverable without issuing a read: %j', async (overrides, key) => {
    const { el, trigger } = await mountViewer(overrides)
    expect(trigger.disabled).toBe(true)
    const hint = el.querySelector<HTMLElement>('#gateway-logs-unavailable')!
    expect(hint.textContent).toBe(i18n.global.t(key))
    expect(trigger.getAttribute('aria-describedby')).toBe(hint.id)
    expect(hint.hidden).toBe(false)
    expect(el.querySelector('#settings-gateway-logs')?.getAttribute('tabindex')).toBe('-1')
    trigger.click()
    await flush()
    expect(tailLogs).not.toHaveBeenCalled()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
  })

  it('permits the local owner to read without an explicit credential', async () => {
    const { trigger } = await mountViewer({ isAuthenticated: false, isLocalOwner: true })
    expect(trigger.disabled).toBe(false)
    trigger.click()
    await flush()
    expect(tailLogs).toHaveBeenCalledOnce()
  })

  it('shows loading, empty and failure states and allows an explicit retry', async () => {
    let resolve!: (batch: GatewayLogBatch) => void
    tailLogs.mockReturnValueOnce(new Promise<GatewayLogBatch>(done => { resolve = done }))
    const { trigger } = await mountViewer()
    trigger.click()
    await flush()
    expect(document.querySelector('[role="status"]')?.textContent).toContain('Loading logs')
    expect(refreshButton().disabled).toBe(true)
    resolve({ entries: [], truncated: false })
    await flush()
    expect(document.querySelector('[role="status"]')?.textContent).toBe('No recent logs.')
    tailLogs.mockRejectedValueOnce(new Error('permission denied'))
    refreshButton().click()
    await flush()
    expect(document.querySelector('[role="alert"]')?.textContent).toBe(i18n.global.t('gatewayLogs.failed'))
    expect(refreshButton().disabled).toBe(false)
    refreshButton().click()
    await flush()
    expect(document.querySelector('pre')?.textContent).toBe('Gateway ready')
  })

  it('traps keyboard focus, closes on Escape and returns to its trigger', async () => {
    const { trigger } = await mountViewer()
    trigger.focus()
    trigger.click()
    await flush()
    closeButton().focus()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }))
    expect(document.activeElement).toBe(refreshButton())
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    expect(document.activeElement).toBe(closeButton())
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flush()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('cancels a read on close and rejects its stale result after a new open', async () => {
    let resolve!: (batch: GatewayLogBatch) => void
    tailLogs.mockReturnValueOnce(new Promise<GatewayLogBatch>(done => { resolve = done }))
    const { trigger } = await mountViewer()
    trigger.click()
    await flush()
    const signal = tailLogs.mock.calls[0][0].signal as AbortSignal
    closeButton().click()
    await flush()
    expect(signal.aborted).toBe(true)
    trigger.click()
    await flush()
    resolve({ entries: ['Old result'], truncated: false })
    await flush()
    expect(document.querySelector('pre')?.textContent).toBe('Gateway ready')
  })

  it.each([false, true])('clears logs on connection changes, including a queued result: %s', async (alreadyResolved) => {
    let resolve!: (batch: GatewayLogBatch) => void
    tailLogs.mockReturnValueOnce(new Promise<GatewayLogBatch>(done => { resolve = done }))
    const { access, trigger } = await mountViewer()
    trigger.click()
    await flush()
    const signal = tailLogs.mock.calls[0][0].signal as AbortSignal
    // An already-queued result must be rejected before the watcher can abort.
    if (alreadyResolved) resolve({ entries: ['Other Gateway'], truncated: false })
    ;(access as { subscriptionEpoch: number }).subscriptionEpoch += 1
    await flush()
    if (!alreadyResolved) {
      expect(signal.aborted).toBe(true)
      resolve({ entries: ['Other Gateway'], truncated: false })
      await flush()
    }
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    trigger.click()
    await flush()
    expect(document.querySelector('pre')?.textContent).toBe('Gateway ready')
  })

  it('cancels a pending read when its settings panel unmounts', async () => {
    tailLogs.mockReturnValue(new Promise(() => {}))
    const { app, trigger } = await mountViewer()
    trigger.click()
    await flush()
    const signal = tailLogs.mock.calls[0][0].signal as AbortSignal
    app.unmount()
    mounted.splice(mounted.indexOf(app), 1)
    expect(signal.aborted).toBe(true)
    expect(document.querySelector('[role="dialog"]')).toBeNull()
  })
})
