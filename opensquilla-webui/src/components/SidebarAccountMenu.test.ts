// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, ref, type Ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import i18n from '@/i18n'
import zhHans from '@/locales/zh-Hans.json'
import type { TokenRhythmAccountState } from '../../../contracts/desktop/tokenrhythm-account'
import SidebarAccountMenu from './SidebarAccountMenu.vue'

const mock = vi.hoisted(() => ({
  account: null as any,
  usage: null as {
    tokens: Ref<number | null>
    loading: Ref<boolean>
    error: Ref<boolean>
    approximate: Ref<boolean>
  } | null,
}))
vi.mock('@/stores/account', () => ({ useAccountStore: () => mock.account }))
vi.mock('@/composables/usage/useTodayUsage', () => ({ useTodayUsage: () => mock.usage }))

const apps: ReturnType<typeof createApp>[] = []
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.replaceChildren()
})

function state(patch: Partial<TokenRhythmAccountState> = {}): TokenRhythmAccountState {
  return {
    revision: 1,
    status: 'signed_in',
    profile: { accountId: 'example-account', sessionId: 'example-session', displayName: 'Demo user', maskedPhone: '' },
    wallet: {
      currency: 'CNY',
      balances: { available: '23.45', recharge: '20', bonus: '3.45', debt: '0', frozen: '0' },
      asOf: '2026-01-12T08:00:00Z',
    },
    walletStatus: 'ready',
    keyStatus: 'ready',
    errorCode: null,
    walletErrorCode: null,
    source: 'manual',
    selectedAccountId: null,
    mediaSources: { image: 'inherit', video: 'inherit' },
    pendingRevocation: false,
    loginExpiresAt: null,
    ...patch,
  }
}

async function mount(value = state(), collapsed = false) {
  mock.account = reactive({
    state: value,
    supported: true,
    loggedIn: ['signed_in', 'unverified'].includes(value.status),
    name: 'Demo user',
    initial: 'D',
    busy: false,
    refreshing: false,
    error: '',
    init: vi.fn(async () => {}),
    action: vi.fn(async () => value),
    openLink: vi.fn(async () => {}),
  })
  mock.usage = { tokens: ref(84), loading: ref(false), error: ref(false), approximate: ref(false) }
  const props = reactive({ collapsed, onSettings: vi.fn() })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })
  await router.push('/chat')
  const host = document.createElement('div')
  document.body.append(host)
  i18n.global.setLocaleMessage('zh-Hans', zhHans)
  i18n.global.locale.value = 'zh-Hans'
  const app = createApp({ render: () => h(SidebarAccountMenu, props) })
  app.use(i18n)
  app.use(router)
  app.mount(host)
  apps.push(app)
  await nextTick()
  return { host, props, router }
}

function button(host: HTMLElement, id: string) {
  const element = host.querySelector<HTMLButtonElement>(`[data-testid="${id}"]`)
  expect(element).not.toBeNull()
  return element!
}

async function open(host: HTMLElement) {
  button(host, 'sidebar-account-trigger').click()
  await nextTick()
  await nextTick()
  expect(host.querySelector('[role="menu"]')).not.toBeNull()
}

describe('sidebar account summary', () => {
  it('keeps remote balance and local usage independent when a manual connection is selected', async () => {
    const { host } = await mount()
    await open(host)
    const balance = host.querySelector('[data-testid="sidebar-account-balance"]')!
    const usage = host.querySelector('[data-testid="sidebar-account-today-usage"]')!
    expect(host.querySelector('[role="menu"]')?.textContent).toContain('Demo user')
    expect(balance.textContent).toContain('23.45')
    expect(usage.textContent).toContain('84')
    expect(mock.account.state.source).toBe('manual')

    mock.account.state.wallet.balances.available = '19.10'
    await nextTick()
    expect(balance.textContent).toContain('19.10')
    expect(usage.textContent).toContain('84')
    mock.usage!.tokens.value = 125
    await nextTick()
    expect(usage.textContent).toContain('125')
    expect(balance.textContent).toContain('19.10')
  })

  it('opens remote recharge but routes usage to the local today view', async () => {
    const { host, router } = await mount()
    await open(host)
    button(host, 'sidebar-account-recharge').click()
    await nextTick()
    expect(mock.account.openLink).toHaveBeenCalledExactlyOnceWith('recharge')
    expect(router.currentRoute.value.path).toBe('/chat')
    if (!host.querySelector('[role="menu"]')) await open(host)
    button(host, 'sidebar-account-usage').click()
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/usage?range=today'))
    expect(host.querySelector('[role="menu"]')).toBeNull()
    expect(mock.account.openLink).toHaveBeenCalledTimes(1)
  })

  it('does not present unavailable balance or local usage as zero', async () => {
    const { host } = await mount(state({ wallet: null, walletStatus: 'error', walletErrorCode: 'ACCOUNT_NETWORK_UNAVAILABLE' }))
    mock.usage!.tokens.value = null
    mock.usage!.error.value = true
    await open(host)
    const balance = host.querySelector('[data-testid="sidebar-account-balance"]')!
    const usage = host.querySelector('[data-testid="sidebar-account-today-usage"]')!
    expect(balance.textContent).toContain('—')
    expect(balance.textContent).not.toContain('0.00')
    expect(usage.textContent).toContain('—')
    expect(usage.textContent).not.toMatch(/\b0\b/)
    expect(button(host, 'sidebar-account-settings').disabled).toBe(false)
  })

  it('retains a stale balance while local usage can continue updating', async () => {
    const { host } = await mount(state({ walletStatus: 'stale', walletErrorCode: 'ACCOUNT_NETWORK_UNAVAILABLE' }))
    await open(host)
    expect(host.querySelector('[data-testid="sidebar-account-balance"]')?.textContent).toContain('23.45')
    mock.usage!.tokens.value = 216
    await nextTick()
    expect(host.querySelector('[data-testid="sidebar-account-today-usage"]')?.textContent).toContain('216')
    expect(host.querySelector('[data-testid="sidebar-account-balance"]')?.textContent).toContain('23.45')
  })

  it('shows a confirmed zero only after local usage has finished loading', async () => {
    const { host } = await mount()
    mock.usage!.tokens.value = null
    mock.usage!.loading.value = true
    await open(host)
    const usage = host.querySelector('[data-testid="sidebar-account-today-usage"]')!
    expect(usage.textContent).toContain('—')
    expect(usage.textContent).not.toMatch(/\b0\b/)
    expect(usage.textContent).toContain('加载中')
    mock.usage!.tokens.value = 0
    mock.usage!.loading.value = false
    await nextTick()
    expect(usage.textContent).toMatch(/\b0\b/)
    expect(usage.textContent).not.toContain('—')
  })

  it('keeps settings reachable without logging in', async () => {
    const { host, props } = await mount(state({ status: 'signed_out', profile: null, wallet: null, walletStatus: 'idle', keyStatus: 'missing' }))
    await open(host)
    expect(host.querySelector('[data-testid="sidebar-account-balance"]')).toBeNull()
    const settings = button(host, 'sidebar-account-settings')
    expect(settings.disabled).toBe(false)
    settings.click()
    await nextTick()
    expect(props.onSettings).toHaveBeenCalledOnce()
    expect(mock.account.action).not.toHaveBeenCalled()
    expect(host.querySelector('[role="menu"]')).toBeNull()
  })

  it('navigates menu actions by keyboard and restores trigger focus with Escape', async () => {
    const { host } = await mount()
    await open(host)
    const items = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')]
    expect(document.activeElement).toBe(items[0])
    document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }))
    expect(document.activeElement).toBe(items[items.length - 1])
    document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true }))
    expect(document.activeElement).toBe(items[0])
    document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    expect(document.activeElement).toBe(items[1])
    document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(host.querySelector('[role="menu"]')).toBeNull()
    expect(document.activeElement).toBe(button(host, 'sidebar-account-trigger'))
  })

  it('hides the account entry when collapsed and does not reopen an old popup on expansion', async () => {
    const { host, props } = await mount()
    await open(host)
    props.collapsed = true
    await nextTick()
    expect(host.querySelector('[data-testid="sidebar-account-trigger"]')).toBeNull()
    expect(host.querySelector('[role="menu"]')).toBeNull()
    props.collapsed = false
    await nextTick()
    expect(button(host, 'sidebar-account-trigger').getAttribute('aria-expanded')).toBe('false')
    expect(host.querySelector('[role="menu"]')).toBeNull()
  })
})
