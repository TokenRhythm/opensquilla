// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import i18n from '@/i18n'
import RouteHubShell from './RouteHubShell.vue'

const mountedApps: Array<ReturnType<typeof createApp>> = []

afterEach(() => {
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

function statefulView(id: string) {
  return defineComponent({
    name: `${id}View`,
    setup() {
      const count = ref(0)
      return () => h('button', {
        'data-view': id,
        onClick: () => { count.value += 1 },
      }, `${id}:${count.value}`)
    },
  })
}

async function mountShell() {
  const AlphaView = statefulView('alpha')
  const BetaView = statefulView('beta')
  const OutsideView = statefulView('outside')
  const tabs = [
    { path: '/alpha', labelKey: 'nav.skills', icon: 'skills' as const, component: AlphaView },
    { path: '/beta', labelKey: 'nav.channels', icon: 'channels' as const, component: BetaView },
  ]
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/alpha', component: AlphaView },
      { path: '/beta', component: BetaView },
      { path: '/outside', component: OutsideView },
    ],
  })
  await router.push('/alpha')
  await router.isReady()

  const Root = defineComponent({
    setup() {
      return () => h(RouteHubShell, {
        tabs,
        ariaLabelKey: 'nav.skills',
        keepAliveMax: 2,
      })
    },
  })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(Root)
  mountedApps.push(app)
  app.use(i18n)
  app.use(router)
  app.mount(host)
  await nextTick()

  async function settle() {
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()
  }

  return { host, router, settle }
}

describe('RouteHubShell', () => {
  it('renders real links with current-page semantics and preserves tab state', async () => {
    const { host, settle } = await mountShell()
    const links = Array.from(host.querySelectorAll<HTMLAnchorElement>('.route-hub__tab'))

    expect(links).toHaveLength(2)
    expect(links.map(link => link.getAttribute('href'))).toEqual(['/alpha', '/beta'])
    expect(links[0]?.getAttribute('aria-current')).toBe('page')
    expect(links[1]?.hasAttribute('aria-current')).toBe(false)
    expect(host.querySelector('[role="tablist"]')).toBeNull()
    expect(host.querySelector('[role="tab"]')).toBeNull()
    expect(host.querySelector('[role="tabpanel"]')).toBeNull()

    const alpha = host.querySelector<HTMLButtonElement>('[data-view="alpha"]')
    alpha?.click()
    await nextTick()
    expect(alpha?.textContent).toBe('alpha:1')

    links[1]?.click()
    await settle()
    expect(host.querySelector('[data-view="beta"]')?.textContent).toBe('beta:0')
    expect(links[1]?.getAttribute('aria-current')).toBe('page')

    links[0]?.click()
    await settle()
    expect(host.querySelector('[data-view="alpha"]')?.textContent).toBe('alpha:1')
  })

  it('keeps the last valid child selected while the route leaves the hub', async () => {
    const { host, router, settle } = await mountShell()
    const links = Array.from(host.querySelectorAll<HTMLAnchorElement>('.route-hub__tab'))

    links[1]?.click()
    await settle()
    await router.push('/outside')
    await settle()

    expect(host.querySelector('[data-view="beta"]')?.textContent).toBe('beta:0')
    expect(host.querySelector('[data-view="alpha"]')).toBeNull()
    expect(links.every(link => !link.hasAttribute('aria-current'))).toBe(true)
    expect(host.querySelector('.route-hub__panel')).not.toBeNull()
  })
})
