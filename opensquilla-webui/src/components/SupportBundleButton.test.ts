// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import { OBSERVABILITY_KEY } from '@/modules/observability'
import { setAgentTraceEnabled } from '@/modules/agentTracePreference'
import SupportBundleButton from './SupportBundleButton.vue'

const mocks = vi.hoisted(() => ({
  downloadSupportBundle: vi.fn(),
  pushToast: vi.fn(),
  downloadBlob: vi.fn(),
}))

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast: mocks.pushToast }),
}))
vi.mock('@/utils/browser', () => ({ downloadBlob: mocks.downloadBlob }))

const mounted: App[] = []

async function flush() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
  await nextTick()
  await nextTick()
}

async function mountButton(overrides: Partial<GatewayAccess> = {}) {
  const access = reactive({
    isAvailable: true,
    isLocalOwner: true,
    connectionHealth: 'healthy',
    supportBundleUnavailableReason: null,
    subscriptionEpoch: 1,
    ...overrides,
  }) as GatewayAccess
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(SupportBundleButton)
  app.use(i18n)
  app.provide(GATEWAY_ACCESS_KEY, access)
  app.provide(OBSERVABILITY_KEY, { downloadSupportBundle: mocks.downloadSupportBundle } as never)
  app.mount(el)
  mounted.push(app)
  await flush()
  const trigger = el.querySelector<HTMLButtonElement>('[data-testid="support-download-bundle"]')!
  return { el, access, trigger }
}

function dialogButton(key: string): HTMLButtonElement {
  const label = i18n.global.t(key)
  const found = [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
    .find(button => button.textContent?.includes(label))
  if (!found) throw new Error(`Missing dialog button: ${label}`)
  return found
}

beforeEach(() => {
  document.body.innerHTML = ''
  setAgentTraceEnabled(false)
  vi.clearAllMocks()
  i18n.global.locale.value = 'en'
  mocks.downloadSupportBundle.mockResolvedValue({
    blob: new Blob(['bundle']), filename: 'opensquilla-support.zip',
  })
})

afterEach(() => {
  setAgentTraceEnabled(false)
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('SupportBundleButton', () => {
  it('opens the bundle confirmation directly and keeps content excluded by default', async () => {
    setAgentTraceEnabled(true)
    const { el, trigger } = await mountButton()
    expect(trigger.disabled).toBe(false)
    expect(trigger.textContent).toContain('Download redacted support bundle')
    expect(trigger.hasAttribute('aria-haspopup')).toBe(false)
    expect(el.querySelector('[role="menu"]')).toBeNull()
    expect(el.querySelector('[data-testid="support-copy-readiness"]')).toBeNull()
    expect(el.querySelector('[data-testid="support-view-logs"]')).toBeNull()
    expect(mocks.downloadSupportBundle).not.toHaveBeenCalled()

    trigger.click()
    await flush()
    expect(document.querySelector<HTMLInputElement>('[role="dialog"] input[type="checkbox"]')?.checked).toBe(false)
    expect(document.activeElement).toBe(dialogButton('monitorSupport.bundleCancel'))
    dialogButton('monitorSupport.bundleConfirm').click()
    await flush()

    expect(mocks.downloadSupportBundle).toHaveBeenCalledExactlyOnceWith({
      includeContent: false, days: 1, signal: expect.any(AbortSignal),
    })
    expect(mocks.downloadBlob).toHaveBeenCalledWith(expect.any(Blob), 'opensquilla-support.zip')
    expect(mocks.pushToast).toHaveBeenCalledWith(i18n.global.t('monitorSupport.bundleReady'), { tone: 'ok' })
    expect(document.activeElement).toBe(trigger)
  })

  it('uses an explicit content opt-in for one download and resets it for the next', async () => {
    setAgentTraceEnabled(true)
    const { trigger } = await mountButton()
    trigger.click()
    await flush()
    const checkbox = document.querySelector<HTMLInputElement>('[role="dialog"] input[type="checkbox"]')!
    checkbox.checked = true
    checkbox.dispatchEvent(new Event('change'))
    await flush()
    dialogButton('monitorSupport.bundleConfirm').click()
    await flush()
    expect(mocks.downloadSupportBundle.mock.calls[0][0].includeContent).toBe(true)

    trigger.click()
    await flush()
    expect(document.querySelector<HTMLInputElement>('[role="dialog"] input[type="checkbox"]')?.checked).toBe(false)
    dialogButton('monitorSupport.bundleCancel').click()
    await flush()
    expect(document.activeElement).toBe(trigger)
    expect(mocks.downloadSupportBundle).toHaveBeenCalledTimes(1)
  })

  it.each([
    [{ supportBundleUnavailableReason: 'disconnected' }, 'monitorSupport.bundleConnectionRequired'],
    [{ supportBundleUnavailableReason: 'permission' }, 'monitorSupport.bundleOwnerRequired'],
    [{ supportBundleUnavailableReason: 'differentGateway' }, 'monitorSupport.bundleDifferentGateway'],
  ] as const)('keeps unavailable or non-owner downloads disabled: %j', async (overrides, reason) => {
    const { el, trigger } = await mountButton(overrides)
    expect(trigger.disabled).toBe(true)
    expect(el.textContent).toContain(i18n.global.t(reason))
    trigger.click()
    await flush()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(mocks.downloadSupportBundle).not.toHaveBeenCalled()
  })

  it('closes the confirmation if its Gateway connection is lost', async () => {
    const { access, trigger } = await mountButton()
    trigger.click()
    await flush()
    ;(access as { supportBundleUnavailableReason: string | null }).supportBundleUnavailableReason = 'disconnected'
    await flush()
    expect(trigger.disabled).toBe(true)
    await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeNull())
    expect(mocks.downloadSupportBundle).not.toHaveBeenCalled()
  })

  it('does not save a pending bundle after the connection changes', async () => {
    let resolveBundle!: (value: { blob: Blob; filename: string }) => void
    mocks.downloadSupportBundle.mockReturnValue(new Promise(resolve => { resolveBundle = resolve }))
    const { access, trigger } = await mountButton()
    trigger.click()
    await flush()
    dialogButton('monitorSupport.bundleConfirm').click()
    await flush()
    expect(trigger.disabled).toBe(true)
    ;(access as { subscriptionEpoch: number }).subscriptionEpoch += 1
    await flush()
    expect(mocks.downloadSupportBundle.mock.calls[0][0].signal.aborted).toBe(true)
    resolveBundle({ blob: new Blob(['old target']), filename: 'old-target.zip' })
    await flush()
    expect(mocks.downloadBlob).not.toHaveBeenCalled()
    expect(mocks.pushToast).not.toHaveBeenCalled()
    expect(trigger.disabled).toBe(false)
  })

  it('reports download failure and restores the direct action for retry', async () => {
    mocks.downloadSupportBundle.mockRejectedValue(new Error('Forbidden'))
    const { trigger } = await mountButton()
    trigger.click()
    await flush()
    dialogButton('monitorSupport.bundleConfirm').click()
    await flush()
    expect(mocks.downloadBlob).not.toHaveBeenCalled()
    expect(mocks.pushToast).toHaveBeenCalledWith(i18n.global.t('monitorSupport.bundleFailed'), { tone: 'danger' })
    expect(trigger.disabled).toBe(false)
    expect(document.activeElement).toBe(trigger)
  })

  it('rejects an old result when the epoch changes before the queued watcher runs', async () => {
    let resolveBundle!: (value: { blob: Blob; filename: string }) => void
    mocks.downloadSupportBundle.mockReturnValue(new Promise(resolve => { resolveBundle = resolve }))
    const { access, trigger } = await mountButton()
    trigger.click()
    await flush()
    dialogButton('monitorSupport.bundleConfirm').click()
    await flush()

    // Queue the await continuation before Vue's connection watcher. The epoch
    // changes synchronously, so the result guard must reject it without abort.
    resolveBundle({ blob: new Blob(['old target']), filename: 'old-target.zip' })
    ;(access as { subscriptionEpoch: number }).subscriptionEpoch += 1
    expect(mocks.downloadSupportBundle.mock.calls[0][0].signal.aborted).toBe(false)
    await flush()

    expect(mocks.downloadBlob).not.toHaveBeenCalled()
    expect(mocks.pushToast).not.toHaveBeenCalled()
    expect(trigger.disabled).toBe(false)
  })
})
