// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp } from 'vue'
import i18n from '@/i18n'
import OverviewHubView from './OverviewHubView.vue'
import SkillsChannelsHubView from './SkillsChannelsHubView.vue'

vi.mock('@/components/RouteHubShell.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      name: 'RouteHubShellProbe',
      props: {
        tabs: { type: Array, required: true },
        ariaLabelKey: { type: String, required: true },
        keepAliveMax: { type: Number, required: true },
        mobileEqualTabs: { type: Boolean, default: false },
      },
      setup(props, { slots }) {
        return () => h('section', {
          'data-testid': 'route-hub-probe',
          'data-paths': (props.tabs as Array<{ path: string }>).map(tab => tab.path).join(','),
          'data-max': String(props.keepAliveMax),
          'data-aria-label-key': props.ariaLabelKey,
          'data-mobile-equal': String(props.mobileEqualTabs),
        }, slots.actions?.())
      },
    }),
  }
})

vi.mock('@/components/SupportDiagnosticsMenu.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      name: 'SupportDiagnosticsMenuStub',
      setup() {
        return () => h('div', { 'data-testid': 'support-diagnostics' })
      },
    }),
  }
})

const apps: Array<ReturnType<typeof createApp>> = []

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

function mount(component: typeof OverviewHubView) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(component)
  apps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

describe('route hub views', () => {
  it('configures the two-tab Overview hub with diagnostics', () => {
    const host = mount(OverviewHubView)
    const probe = host.querySelector('[data-testid="route-hub-probe"]')

    expect(probe?.getAttribute('data-paths')).toBe('/overview,/usage')
    expect(probe?.getAttribute('data-max')).toBe('2')
    expect(probe?.getAttribute('data-aria-label-key')).toBe('nav.overview')
    expect(probe?.getAttribute('data-mobile-equal')).toBe('false')
    expect(host.querySelector('[data-testid="support-diagnostics"]')).not.toBeNull()
  })

  it('configures the two-tab Skills and Channels hub without monitor actions', () => {
    const host = mount(SkillsChannelsHubView)
    const probe = host.querySelector('[data-testid="route-hub-probe"]')

    expect(probe?.getAttribute('data-paths')).toBe('/skills,/channels')
    expect(probe?.getAttribute('data-max')).toBe('2')
    expect(probe?.getAttribute('data-aria-label-key')).toBe('nav.skillsChannels')
    expect(probe?.getAttribute('data-mobile-equal')).toBe('true')
    expect(host.querySelector('[data-testid="support-diagnostics"]')).toBeNull()
  })
})
