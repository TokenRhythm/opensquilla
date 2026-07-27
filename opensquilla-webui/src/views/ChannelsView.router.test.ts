// @vitest-environment happy-dom
// Routing regressions that a mocked vue-router cannot catch: the query-sync
// watcher racing (and cancelling) real pushes, the route-leave guard
// self-cancelling the user's navigation, and query-driven draft guards.
// Everything here runs against a REAL router on memory history.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const channelRows = [
  {
    name: 'ops-slack',
    type: 'slack',
    status: 'connected',
    connected: true,
    enabled: true,
    configured: true,
    connected_since: '2026-07-13T08:00:00Z',
    bot_user_id: 'U-BOT',
    capability_profile: { transports: ['webhook'], maturity: 'YELLOW-experimental', evidence: {} },
    diagnostics: { network_probe: 'not_run' },
  },
  {
    name: 'alerts-telegram',
    type: 'telegram',
    status: 'stopped',
    connected: false,
    enabled: true,
    configured: true,
    capability_profile: { transports: ['polling'], maturity: 'YELLOW-experimental', evidence: {} },
    diagnostics: { network_probe: 'not_run' },
  },
  // Four configured channels put the home into fleet mode, so the enroll strip
  // (the compose entry these routing tests exercise) is present.
  {
    name: 'wecom-hr',
    type: 'wecom',
    status: 'connected',
    connected: true,
    enabled: true,
    configured: true,
    diagnostics: { network_probe: 'not_run' },
  },
  {
    name: 'discord-lab',
    type: 'discord',
    status: 'connected',
    connected: true,
    enabled: true,
    configured: true,
    diagnostics: { network_probe: 'not_run' },
  },
]

function buttonWithText(root: ParentNode, label: string): HTMLButtonElement {
  const button = Array.from(root.querySelectorAll<HTMLButtonElement>('button'))
    .find(candidate => candidate.textContent?.trim() === label)
  if (!button) throw new Error(`button not found: ${label}`)
  return button
}

function channelCard(root: ParentNode, name: string): HTMLElement {
  const card = Array.from(root.querySelectorAll<HTMLElement>('.chb-story'))
    .find(candidate => candidate.querySelector('.chb-story__name')?.textContent === name)
  if (!card) throw new Error(`channel card not found: ${name}`)
  return card
}

async function mountWithRealRouter(options: { webHistory?: boolean } = {}) {
  vi.resetModules()

  const { KeepAlive, createApp, defineComponent, h, nextTick, ref } = await import('vue')
  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'

  Element.prototype.scrollIntoView = vi.fn()

  const pushToast = vi.fn()
  const confirm = vi.fn(async () => true)
  const slackSpec = {
    type: 'slack',
    label: 'Slack',
    description: 'Slack workspace bot.',
    transport: 'mixed',
    setupAids: [],
    fields: [
      { name: 'name', label: 'Channel name', type: 'text', required: true },
      { name: 'slack_channel_id', label: 'Default channel id', type: 'text', default: '' },
      { name: 'token', label: 'Bot token', type: 'password', required: true, secret: true, group: 'credentials' },
    ],
  }
  const rpcCall = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'onboarding.catalog') return { channels: [slackSpec] }
    if (method === 'onboarding.channel.probe') return { status: 'validated', connected: false, warnings: [] }
    if (method === 'onboarding.channel.upsert') {
      const entry = params?.entry as Record<string, unknown> | undefined
      return { changed: true, restartRequired: true, entry: { name: entry?.name } }
    }
    if (method === 'channels.probe') return { status: 'verified', connected: true, latencyMs: 17 }
    if (method === 'channels.restart') return { status: 'restarted' }
    if (method === 'channels.get') {
      return {
        entry: { name: 'ops-slack', type: 'slack', token: '***' },
        secretFields: ['token'],
      }
    }
    if (method === 'config.get') return {}
    if (method === 'channels.pairings') return { pairings: [] }
    throw new Error(`unexpected rpc method: ${method}`)
  })

  const iconStub = defineComponent({
    name: 'IconStub',
    setup() {
      return () => h('span', { 'data-testid': 'icon' })
    },
  })
  const emptyStub = (name: string) => defineComponent({
    name,
    setup() {
      return () => h('div', { 'data-testid': name })
    },
  })

  vi.doMock('@/stores/rpc', () => ({
    useRpcStore: () => ({
      call: rpcCall,
      on: vi.fn(() => () => {}),
      waitForConnection: vi.fn(async () => {}),
    }),
  }))
  vi.doMock('@/composables/useRequest', () => ({
    useRequest: () => ({
      data: ref({ channels: channelRows }),
      loading: ref(false),
      error: ref(null),
      execute: vi.fn(async () => ({ channels: channelRows })),
      refresh: vi.fn(async () => ({ channels: channelRows })),
    }),
  }))
  vi.doMock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast }) }))
  vi.doMock('@/composables/useConfirm', () => ({ useConfirm: () => ({ confirm }) }))
  vi.doMock('@/components/Icon.vue', () => ({ default: iconStub }))
  vi.doMock('@/components/ErrorState.vue', () => ({ default: emptyStub('error-state') }))
  vi.doMock('@/components/LoadingSpinner.vue', () => ({ default: emptyStub('loading-spinner') }))

  // Same module registry as the component's own vue-router import.
  const { RouterView, createMemoryHistory, createRouter, createWebHistory } = await import('vue-router')
  const Component = (await import('./ChannelsView.vue')).default

  // Web history (happy-dom's real History) when a test needs the maintained
  // back/forward state (history.state.forward); memory history otherwise.
  const history = options.webHistory ? createWebHistory() : createMemoryHistory()
  // finalizeNavigation calls history.push only when a PUSH actually lands —
  // a push superseded by a same-tick replace never reaches it.
  const historyPush = vi.spyOn(history, 'push')
  const router = createRouter({
    history,
    routes: [
      { path: '/', redirect: '/channels' },
      { path: '/channels', component: Component, meta: { keepAlive: true } },
      { path: '/overview', component: emptyStub('overview-view') },
      { path: '/skills', component: emptyStub('skills-view') },
    ],
  })

  // The app shell mirrors App.vue: a scrolling main.content hosting a
  // KeepAlive-wrapped router-view (onActivated + route-leave guards live).
  const AppShell = defineComponent({
    setup() {
      return () => h('main', { class: 'content', id: 'content' }, [
        h(RouterView, null, {
          default: ({ Component: Active }: { Component: unknown }) =>
            h(KeepAlive, null, Active ? [h(Active as never)] : []),
        }),
      ])
    },
  })

  const el = document.createElement('div')
  document.body.appendChild(el)
  const application = createApp(AppShell)
  application.use(i18n)
  application.use(router)
  await router.push('/channels')
  await router.isReady()
  application.mount(el)
  await nextTick()

  const flush = async (rounds = 3) => {
    for (let i = 0; i < rounds; i += 1) {
      await new Promise(resolve => setTimeout(resolve, 0))
      await nextTick()
    }
  }

  return { app: application, el, flush, nextTick, router, historyPush, rpcCall }
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.doUnmock('@/stores/rpc')
  vi.doUnmock('@/composables/useRequest')
  vi.doUnmock('@/composables/useToasts')
  vi.doUnmock('@/composables/useConfirm')
  vi.doUnmock('@/components/Icon.vue')
  vi.doUnmock('@/components/ErrorState.vue')
  vi.doUnmock('@/components/LoadingSpinner.vue')
})

describe('ChannelsView with a real router', () => {
  it('drill-in lands as a real history push and Back returns to the dashboard', async () => {
    const ctx = await mountWithRealRouter()
    const { app, el, flush, router, historyPush } = ctx
    try {
      await flush()
      const baseline = historyPush.mock.calls.length

      channelCard(el, 'ops-slack').click()
      await flush()

      // The push actually landed — the state watcher's replace did not
      // supersede it.
      const pushed = historyPush.mock.calls.slice(baseline).map(call => String(call[0]))
      expect(pushed.some(location => location.includes('channel=ops-slack'))).toBe(true)
      expect(router.currentRoute.value.query.channel).toBe('ops-slack')
      expect(el.querySelector('.chd')).toBeTruthy()

      // Browser Back pops the drill entry and returns to the card grid —
      // never out of /channels.
      router.back()
      await flush(6)
      expect(router.currentRoute.value.path).toBe('/channels')
      expect(router.currentRoute.value.query.channel).toBeUndefined()
      expect(el.querySelector('.chd')).toBeNull()
      expect(el.querySelector('.chb-ledger')).toBeTruthy()
    } finally {
      app.unmount()
    }
  })

  it('compose entry lands as a real history push', async () => {
    const ctx = await mountWithRealRouter()
    const { app, el, flush, router, historyPush } = ctx
    try {
      await flush()
      const baseline = historyPush.mock.calls.length
      el.querySelector<HTMLButtonElement>('.chb-enroll__title')!.click()
      await flush()
      const pushed = historyPush.mock.calls.slice(baseline).map(call => String(call[0]))
      expect(pushed.some(location => location.includes('compose=1'))).toBe(true)
      expect(router.currentRoute.value.query.compose).toBe('1')
      expect(el.querySelector('.chc')).toBeTruthy()
    } finally {
      app.unmount()
    }
  })

  it('leaving /channels with the editor open succeeds on the first attempt', async () => {
    const ctx = await mountWithRealRouter()
    const { app, el, flush, router } = ctx
    try {
      await flush()
      channelCard(el, 'ops-slack').click()
      await flush()
      buttonWithText(el.querySelector<HTMLElement>('.chd')!, 'Edit').click()
      await flush()
      expect(router.currentRoute.value.query.edit).toBe('1')

      // A clean draft: the leave guard answers true and nothing may cancel
      // the in-flight navigation (the old watcher replace did exactly that).
      const failure = await router.push('/overview')
      expect(failure).toBeUndefined()
      expect(router.currentRoute.value.path).toBe('/overview')
      await flush()
      expect(el.querySelector('[data-testid="overview-view"]')).toBeTruthy()
    } finally {
      app.unmount()
    }
  })

  it('guards a dirty editor when the Skills hub tab is selected', async () => {
    const ctx = await mountWithRealRouter()
    const { app, el, flush, router } = ctx
    try {
      await flush()
      channelCard(el, 'ops-slack').click()
      await flush()
      const page = el.querySelector<HTMLElement>('.chd')!
      buttonWithText(page, 'Edit').click()
      await flush()

      const input = page.querySelector<HTMLInputElement>('[data-field="slack_channel_id"] input')!
      input.value = 'C-SKILLS-GUARD'
      input.dispatchEvent(new Event('input', { bubbles: true }))
      await flush()

      const keptNavigation = router.push('/skills')
      await flush(6)
      expect(router.currentRoute.value.path).toBe('/channels')
      expect(buttonWithText(el, 'Keep editing')).toBeTruthy()

      buttonWithText(el, 'Keep editing').click()
      await keptNavigation
      await flush()
      expect(router.currentRoute.value.path).toBe('/channels')
      expect(input.value).toBe('C-SKILLS-GUARD')

      const discardedNavigation = router.push('/skills')
      await flush(6)
      buttonWithText(el, 'Discard').click()
      await discardedNavigation
      await flush()
      expect(router.currentRoute.value.path).toBe('/skills')
      expect(el.querySelector('[data-testid="skills-view"]')).toBeTruthy()
    } finally {
      app.unmount()
    }
  })

  it('browser Back out of a dirty compose draft raises the discard confirm and can restore', async () => {
    const ctx = await mountWithRealRouter()
    const { app, el, flush, router } = ctx
    try {
      await flush()
      el.querySelector<HTMLButtonElement>('.chb-enroll__title')!.click()
      await flush()
      const surface = el.querySelector<HTMLElement>('.chc')!
      surface.querySelector<HTMLButtonElement>('[data-channel-type="slack"]')!.click()
      await flush(6)
      const nameInput = surface.querySelector<HTMLInputElement>('[data-field="name"] input')!
      nameInput.value = 'draft-1'
      nameInput.dispatchEvent(new Event('input', { bubbles: true }))
      await flush()
      expect(router.currentRoute.value.query.type).toBe('slack')

      // Back is a query-driven compose exit: it must take the SAME guard as
      // the button paths instead of silently destroying the typed draft.
      router.back()
      await flush(6)
      expect(el.querySelector('.chc__footer--confirm')).toBeTruthy()

      buttonWithText(surface, 'Keep editing').click()
      await flush(6)
      // Kept: the draft survives and the URL is restored.
      expect(el.querySelector('.chc')).toBeTruthy()
      expect(surface.querySelector<HTMLInputElement>('[data-field="name"] input')?.value).toBe('draft-1')
      expect(router.currentRoute.value.query.compose).toBe('1')
      expect(router.currentRoute.value.query.type).toBe('slack')
    } finally {
      app.unmount()
    }
  })

  it('keep-editing after browser Back restores the drill entry from history forward', async () => {
    const ctx = await mountWithRealRouter({ webHistory: true })
    const { app, el, flush, router } = ctx
    try {
      await flush()
      channelCard(el, 'ops-slack').click()
      await flush()
      const page = el.querySelector<HTMLElement>('.chd')!
      buttonWithText(page, 'Edit').click()
      await flush()
      const input = page.querySelector<HTMLInputElement>('[data-field="slack_channel_id"] input')!
      input.value = 'C123'
      input.dispatchEvent(new Event('input', { bubbles: true }))
      await flush()

      router.back()
      await flush(10)
      // Back landed on the dashboard entry; the guard held the drill view.
      expect(router.currentRoute.value.query.channel).toBeUndefined()
      expect(el.querySelector('.chd')).toBeTruthy()

      const goSpy = vi.spyOn(router, 'go')
      const replaceSpy = vi.spyOn(router, 'replace')
      buttonWithText(el, 'Keep editing').click()
      await flush(10)
      // The still-intact FORWARD entry is reused — no replace rewrites the
      // dashboard entry underneath the popstate.
      expect(goSpy).toHaveBeenCalledWith(1)
      expect(replaceSpy).not.toHaveBeenCalled()
      expect(router.currentRoute.value.query.channel).toBe('ops-slack')
      expect(router.currentRoute.value.query.edit).toBe('1')
      expect(page.querySelector<HTMLInputElement>('[data-field="slack_channel_id"] input')?.value).toBe('C123')

      // The dashboard entry survived: Back still returns to it.
      router.back()
      await flush(10)
      expect(router.currentRoute.value.query.channel).toBeUndefined()
    } finally {
      app.unmount()
    }
  })

  it('a superseded discard guard does not cancel the pending navigation', async () => {
    const ctx = await mountWithRealRouter()
    const { app, el, flush, router } = ctx
    try {
      await flush()
      channelCard(el, 'ops-slack').click()
      await flush()
      const page = el.querySelector<HTMLElement>('.chd')!
      buttonWithText(page, 'Edit').click()
      await flush()
      const input = page.querySelector<HTMLInputElement>('[data-field="slack_channel_id"] input')!
      input.value = 'C123'
      input.dispatchEvent(new Event('input', { bubbles: true }))
      await flush()

      // Browser Back raises the query-driven discard guard…
      router.back()
      await flush(6)
      expect(buttonWithText(el, 'Keep editing')).toBeTruthy()

      // …then the user clicks through to another page. The route-leave guard
      // supersedes the pending confirm, and the superseded handler must NOT
      // fire its URL-restoring replace (that used to cancel this navigation).
      const nav = router.push('/overview')
      await flush()
      buttonWithText(el, 'Discard').click()
      await flush(6)
      const failure = await nav
      expect(failure).toBeUndefined()
      expect(router.currentRoute.value.path).toBe('/overview')
      expect(el.querySelector('[data-testid="overview-view"]')).toBeTruthy()
    } finally {
      app.unmount()
    }
  })
})
