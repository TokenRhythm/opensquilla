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
  const updateNetworkReporting = vi.fn()
  const updateAgentTrace = vi.fn()
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Component, {
    panel: {
      networkReportingEnabled: true,
      networkReportingForcedOff: false,
      agentTraceEnabled: false,
      ...overrides,
    },
    onUpdateNetworkReportingEnabled: updateNetworkReporting,
    onUpdateAgentTraceEnabled: updateAgentTrace,
  })
  app.use(i18n)
  app.mount(el)
  mounted.push(app)
  await nextTick()
  return { el, updateNetworkReporting, updateAgentTrace }
}

describe('SettingsPrivacyPanel', () => {
  it('renders reporting and a default-off agent trace control', async () => {
    const { el, updateNetworkReporting } = await mountPanel()
    const networkReporting = el.querySelector<HTMLInputElement>(
      'input[name="setup_disable_network_observability"]',
    )!
    expect(networkReporting.checked).toBe(true)
    expect(el.querySelectorAll('input[type="checkbox"]')).toHaveLength(2)
    const agentTrace = el.querySelector<HTMLInputElement>('input[name="privacy_agent_trace_enabled"]')!
    expect(agentTrace.checked).toBe(false)
    expect(el.textContent).toContain('Agent run trace')
    expect(el.textContent).toContain('Diagnostics and usage reporting')
    expect(el.textContent).toContain('anonymous diagnostics and usage statistics')
    expect(el.textContent).not.toContain('No choice has been saved yet')

    networkReporting.checked = false
    networkReporting.dispatchEvent(new Event('change', { bubbles: true }))
    expect(updateNetworkReporting).toHaveBeenCalledWith(false)
  })

  it('emits a trace change independently of network reporting', async () => {
    const { el, updateAgentTrace } = await mountPanel()
    const agentTrace = el.querySelector<HTMLInputElement>('input[name="privacy_agent_trace_enabled"]')!
    agentTrace.checked = true
    agentTrace.dispatchEvent(new Event('change', { bubbles: true }))
    expect(updateAgentTrace).toHaveBeenCalledWith(true)
  })

  it('shows and locks the effective disabled state when the environment disables reporting', async () => {
    const { el } = await mountPanel({
      networkReportingEnabled: false,
      networkReportingForcedOff: true,
    })
    const networkReporting = el.querySelector<HTMLInputElement>(
      'input[name="setup_disable_network_observability"]',
    )!

    expect(networkReporting.disabled).toBe(true)
    expect(networkReporting.checked).toBe(false)
    expect(el.textContent).toContain('Disabled by an environment setting.')
  })
})
