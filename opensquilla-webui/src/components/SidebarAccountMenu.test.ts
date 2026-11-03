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

    mock.account.state.wallet.balances.available = '1234567890123456789.10001'
    await nextTick()
    expect(balance.textContent).toContain('¥1,234,567,890,123,456,789.11')
    expect(usage.textContent).toContain('84')
    mock.usage!.tokens.value = 125
    await nextTick()
    expect(usage.textContent).toContain('125')
    expect(balance.textContent).toContain('¥1,234,567,890,123,456,789.11')
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
    expect(usage.textContent).toContain('暂不可用')
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

  it('keeps one wallet status region through refresh, failure and recovery', async () => {
    const { host } = await mount()
    await open(host)
    const status = host.querySelector('[data-testid="sidebar-account-wallet-status"]')!
    const balance = host.querySelector('[data-testid="sidebar-account-balance"]')!
    expect(status).not.toBeNull()
    expect(status.textContent?.trim()).toBe('')

    mock.account.refreshing = true
    await nextTick()
    expect(host.querySelector('[data-testid="sidebar-account-wallet-status"]')).toBe(status)
    expect(status.textContent).toContain('加载中')
    expect(balance.textContent).toContain('23.45')
    expect(balance.getAttribute('aria-busy')).toBe('true')

    mock.account.refreshing = false
    mock.account.state.walletStatus = 'stale'
    await nextTick()
    expect(host.querySelector('[data-testid="sidebar-account-wallet-status"]')).toBe(status)
    expect(status.textContent).toContain('上次余额')
    expect(balance.textContent).toContain('23.45')

    mock.account.state.walletStatus = 'ready'
    await nextTick()
    expect(host.querySelector('[data-testid="sidebar-account-wallet-status"]')).toBe(status)
    expect(status.textContent?.trim()).toBe('')
    expect(balance.getAttribute('aria-busy')).toBe('false')
  })

  it('shows a confirmed zero only after local usage has finished loading', async () => {
    const { host } = await mount()
    mock.usage!.tokens.value = null
    mock.usage!.loading.value = true
    await open(host)
    const usage = host.querySelector('[data-testid="sidebar-account-today-usage"]')!
    expect(usage.textContent).not.toMatch(/\b0\b/)
    expect(usage.textContent).toContain('加载中')
    mock.usage!.tokens.value = 0
    mock.usage!.loading.value = false
    await nextTick()
    expect(usage.textContent).toMatch(/\b0\b/)
    expect(usage.textContent).not.toContain('—')
  })

  it('keeps the usage summary compact without a separate approximation hint', async () => {
    const { host } = await mount()
    await open(host)
    const usage = host.querySelector('[data-testid="sidebar-account-today-usage"]')!
    const label = usage.querySelector('.sidebar-account__usage-label')!
    expect(label.textContent).toContain('今日用量')
    expect(label.hasAttribute('title')).toBe(false)
    expect(label.querySelector('.icon svg')).not.toBeNull()
    expect(usage.textContent).not.toContain('≈')
    expect(usage.textContent).not.toContain('近似用量')

    mock.usage!.approximate.value = true
    await nextTick()
    expect(usage.textContent).toContain('≈')
    expect(usage.textContent).not.toContain('近似用量')
  })

  it('exposes the usage explanation to keyboard navigation and assistive technology', async () => {
    const { host } = await mount()
    await open(host)
    const info = button(host, 'sidebar-account-usage-info')
    const recharge = button(host, 'sidebar-account-recharge')
    expect(document.activeElement).toBe(recharge)
    recharge.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(info)

    const descriptionId = info.getAttribute('aria-describedby')
    expect(descriptionId).toBeTruthy()
    const tooltip = document.getElementById(descriptionId!)
    expect(tooltip?.getAttribute('role')).toBe('tooltip')
    expect(tooltip?.getAttribute('aria-hidden')).toBe('false')
    expect(tooltip?.textContent).toContain('按本地时区')
    expect(tooltip?.textContent).toContain('不代表 TokenRhythm 账号账单')

    info.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(tooltip?.getAttribute('aria-hidden')).toBe('true')
    expect(host.querySelector('[role="menu"]')).not.toBeNull()
    info.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(host.querySelector('[role="menu"]')).toBeNull()
    expect(document.activeElement).toBe(button(host, 'sidebar-account-trigger'))
  })

  it('lets touch and pointer users toggle the usage explanation without closing the menu', async () => {
    const { host } = await mount()
    await open(host)
    const info = button(host, 'sidebar-account-usage-info')
    const tooltip = document.getElementById(info.getAttribute('aria-describedby')!)!
    expect(tooltip.getAttribute('aria-hidden')).toBe('true')

    info.click()
    await nextTick()
    expect(tooltip.getAttribute('aria-hidden')).toBe('false')
    expect(host.querySelector('[role="menu"]')).not.toBeNull()

    info.click()
    await nextTick()
    expect(tooltip.getAttribute('aria-hidden')).toBe('true')
    expect(host.querySelector('[role="menu"]')).not.toBeNull()
  })

  it('offers direct sign-in from the signed-out capsule', async () => {
    const { host, router } = await mount(state({ status: 'signed_out', profile: null, wallet: null, walletStatus: 'idle', keyStatus: 'missing' }))
    const login = button(host, 'sidebar-account-login')
    expect(login.textContent).toContain('登录/注册')
    expect(host.querySelectorAll('button')).toHaveLength(1)
    expect(host.querySelector('[role="menu"]')).toBeNull()
    login.click()
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/settings/account'))
    expect(mock.account.action).toHaveBeenCalledExactlyOnceWith('login')
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
    expect(host.querySelector('[role="menu"]')).not.toBeNull()
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
