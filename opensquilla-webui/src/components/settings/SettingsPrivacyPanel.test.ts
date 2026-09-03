// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'

const mounted: App[] = []

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
})

async function mountPanel(overrides: Record<string, unknown> = {}) {
  const { createApp, nextTick } = await import('vue')
  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const Component = (await import('./SettingsPrivacyPanel.vue')).default
  const updateReliability = vi.fn()
  const updateProductAnalytics = vi.fn()
  const updateNetworkReporting = vi.fn()
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Component, {
    panel: {
      networkReportingEnabled: true,
      networkReportingForcedOff: false,
      reliabilityDiagnosticsEnabled: false,
      reliabilityDiagnosticsDecision: null,
      reliabilityDiagnosticsForcedOff: false,
      productAnalyticsEnabled: false,
      productAnalyticsDecision: null,
      productAnalyticsForcedOff: false,
      ...overrides,
    },
    onUpdateNetworkReportingEnabled: updateNetworkReporting,
    onUpdateReliabilityDiagnosticsEnabled: updateReliability,
    onUpdateProductAnalyticsEnabled: updateProductAnalytics,
  })
  app.use(i18n)
  app.mount(el)
  mounted.push(app)
  await nextTick()
  return { el, updateNetworkReporting, updateReliability, updateProductAnalytics }
}

describe('SettingsPrivacyPanel', () => {
  it('renders the legacy veto and updates two independent telemetry controls', async () => {
    const { el, updateNetworkReporting, updateReliability, updateProductAnalytics } = await mountPanel()
    const networkReporting = el.querySelector<HTMLInputElement>(
      'input[name="setup_disable_network_observability"]',
    )!
    const reliability = el.querySelector<HTMLInputElement>(
      'input[name="setup_reliability_diagnostics"]',
    )!
    const productAnalytics = el.querySelector<HTMLInputElement>(
      'input[name="setup_product_analytics"]',
    )!

    expect(networkReporting.checked).toBe(true)
    expect(reliability.checked).toBe(false)
    expect(productAnalytics.checked).toBe(false)
    expect(el.textContent).toContain('Stability diagnostics')
    expect(el.textContent).toContain('Product and growth analytics')

    networkReporting.checked = false
    networkReporting.dispatchEvent(new Event('change', { bubbles: true }))
    reliability.checked = true
    reliability.dispatchEvent(new Event('change', { bubbles: true }))
    productAnalytics.checked = true
    productAnalytics.dispatchEvent(new Event('change', { bubbles: true }))

    expect(updateNetworkReporting).toHaveBeenCalledWith(false)
    expect(updateReliability).toHaveBeenCalledWith(true)
    expect(updateProductAnalytics).toHaveBeenCalledWith(true)
  })

  it('allows a granted scope to be revoked while blocking a forced-off unset scope', async () => {
    const { el, updateReliability } = await mountPanel({
      reliabilityDiagnosticsEnabled: true,
      reliabilityDiagnosticsDecision: true,
      reliabilityDiagnosticsForcedOff: true,
      productAnalyticsEnabled: false,
      productAnalyticsDecision: null,
      productAnalyticsForcedOff: true,
    })
    const reliability = el.querySelector<HTMLInputElement>(
      'input[name="setup_reliability_diagnostics"]',
    )!
    const productAnalytics = el.querySelector<HTMLInputElement>(
      'input[name="setup_product_analytics"]',
    )!

    expect(reliability.disabled).toBe(false)
    expect(reliability.checked).toBe(true)
    expect(productAnalytics.disabled).toBe(true)
    expect(productAnalytics.checked).toBe(false)
    reliability.checked = false
    reliability.dispatchEvent(new Event('change', { bubbles: true }))
    expect(updateReliability).toHaveBeenCalledWith(false)
    expect(el.textContent).toContain('Disabled by an environment setting.')
  })
})
