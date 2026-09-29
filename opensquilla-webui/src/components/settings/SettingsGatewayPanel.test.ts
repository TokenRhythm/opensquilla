// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import SettingsGatewayPanel from './SettingsGatewayPanel.vue'
import { setAgentTraceEnabled } from '@/modules/agentTracePreference'

vi.mock('@/components/settings/SetupConnectionPanel.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      props: { managed: Boolean },
      setup: props => () => h('div', { 'data-testid': 'connection', 'data-managed': props.managed }),
    }),
  }
})
vi.mock('@/components/settings/DesktopRuntimePanel.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ setup: () => () => h('div', { 'data-testid': 'runtime' }, 'Local Gateway') }) }
})
vi.mock('@/components/settings/DesktopLogLocationButton.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ setup: () => () => h('button', { 'data-testid': 'local-log' }, 'Open local log') }) }
})
vi.mock('@/components/settings/SettingsUpdatePanel.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ setup: () => () => h('h3', { id: 'settings-gateway-updates-title', 'data-testid': 'updates' }, 'Desktop updates') }) }
})
vi.mock('@/components/SupportBundleButton.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ setup: () => () => h('button', { 'data-testid': 'support-download-bundle' }, 'Download support bundle') }) }
})
vi.mock('@/components/GatewayLogViewer.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ setup: () => () => h('button', { 'data-testid': 'support-view-logs' }, 'View logs') }) }
})
vi.mock('@/components/trace/TraceInspectorDialog.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ setup: () => () => h('button', { 'data-testid': 'trace-inspector' }, 'Trace inspector') }) }
})

const mounted: App[] = []
afterEach(() => {
  setAgentTraceEnabled(false)
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('SettingsGatewayPanel support entry', () => {
  it('shows the manual Trace entry only while the privacy switch is enabled', async () => {
    setAgentTraceEnabled(false)
    const host = document.createElement('div')
    const app = createApp(SettingsGatewayPanel, { isDesktop: false })
    app.use(i18n)
    app.mount(host)
    mounted.push(app)
    expect(host.querySelector('[data-testid="trace-inspector"]')).toBeNull()
    setAgentTraceEnabled(true)
    await nextTick()
    expect(host.querySelector('[data-testid="trace-inspector"]')).not.toBeNull()
    setAgentTraceEnabled(false)
    await nextTick()
    expect(host.querySelector('[data-testid="trace-inspector"]')).toBeNull()
  })

  it.each([false, true])('keeps support separate from local runtime with isDesktop=%s', async isDesktop => {
    i18n.global.locale.value = 'en'
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(SettingsGatewayPanel, { isDesktop })
    app.use(i18n)
    app.mount(el)
    mounted.push(app)
    await nextTick()

    const support = el.querySelector('#settings-gateway-support')
    expect(support?.getAttribute('tabindex')).toBe('-1')
    expect(support?.textContent).toContain(i18n.global.t('monitorSupport.title'))
    expect(support?.querySelector('[data-testid="support-download-bundle"]')).toBeTruthy()
    expect(support?.querySelector('[data-testid="support-view-logs"]')).toBeTruthy()
    expect(Boolean(support?.querySelector('[data-testid="local-log"]'))).toBe(isDesktop)
    const connection = el.querySelector('#settings-gateway-connection')!
    expect(support!.compareDocumentPosition(connection) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(el.querySelector('#settings-gateway-runtime [data-testid="local-log"]')).toBeNull()
    const runtime = el.querySelector('#settings-gateway-runtime')
    const updates = el.querySelector('#settings-gateway-updates')
    expect(Boolean(runtime?.querySelector('[data-testid="runtime"]'))).toBe(isDesktop)
    expect(Boolean(updates?.querySelector('[data-testid="updates"]'))).toBe(isDesktop)
    if (isDesktop) {
      expect(runtime?.querySelector('[data-testid="updates"]')).toBeNull()
      expect(runtime!.compareDocumentPosition(updates!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(el.textContent).toContain(i18n.global.t('settings.gateway.desktopDesc'))
    } else {
      expect(el.textContent).toContain(i18n.global.t('settings.gateway.desc'))
    }
    expect(el.querySelector('[data-testid="connection"]')?.getAttribute('data-managed')).toBe(String(isDesktop))
  })
})
