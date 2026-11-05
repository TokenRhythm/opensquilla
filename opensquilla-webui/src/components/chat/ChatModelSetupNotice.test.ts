// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { TokenRhythmAccountState } from '../../../../contracts/desktop/tokenrhythm-account'

const routerPush = vi.fn(async () => {})
const cleanups: Array<() => void> = []
const settle = () => new Promise(resolve => setTimeout(resolve, 10))

function accountState(ready: boolean): TokenRhythmAccountState {
  return {
    revision: 1,
    status: ready ? 'signed_in' : 'verifying',
    keyStatus: ready ? 'ready' : 'loading',
    source: 'account',
    selectedAccountId: 'test-account',
    profile: {
      accountId: 'test-account', sessionId: 'test-session', displayName: 'Example', maskedPhone: '',
    },
    wallet: null,
    walletStatus: 'idle',
    errorCode: null,
    walletErrorCode: null,
    mediaSources: { image: 'inherit', video: 'inherit' },
    pendingRevocation: false,
    loginExpiresAt: null,
    primaryActivation: null,
  }
}

async function mountNotice(
  initial: Record<string, unknown> | Error,
  held = false,
  initialAccount?: TokenRhythmAccountState,
) {
  vi.resetModules()
  vi.doMock('vue-router', () => ({ useRouter: () => ({ push: routerPush }) }))
  const accountListeners = new Set<(state: TokenRhythmAccountState) => void>()
  let currentAccount = initialAccount
  if (initialAccount) {
    const api = {
      getState: vi.fn(async () => currentAccount!),
      onState(callback: (state: TokenRhythmAccountState) => void) {
        accountListeners.add(callback)
        return () => accountListeners.delete(callback)
      },
    }
    vi.doMock('@/platform', () => ({ usePlatform: () => ({ account: api }) }))
  } else {
    vi.doUnmock('@/platform')
  }
  const response = { value: initial }
  const status = vi.fn(async () => {
    if (response.value instanceof Error) throw response.value
    return response.value
  })
  const { createApp, h, nextTick, reactive } = await import('vue')
  const { acquireSessionBootstrapAdmission } = await import('@/composables/chat/sessionBootstrapAdmission')
  const releaseAdmission = held ? acquireSessionBootstrapAdmission() : () => {}
  cleanups.push(releaseAdmission)
  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const Component = (await import('./ChatModelSetupNotice.vue')).default
  const { SETUP_WORKFLOW_KEY } = await import('@/modules/setupWorkflow')
  const { GATEWAY_ACCESS_KEY } = await import('@/modules/gatewayAccess')
  const gateway = reactive({ isAvailable: true, subscriptionEpoch: 1 })
  const el = document.createElement('div')
  document.body.appendChild(el)
  const { useReadinessConnectionSync } = await import('@/composables/setup/useReadinessConnectionSync')
  const useAccountStore = initialAccount ? (await import('@/stores/account')).useAccountStore : undefined
  const pinia = initialAccount ? (await import('pinia')).createPinia() : undefined
  let account: ReturnType<NonNullable<typeof useAccountStore>> | undefined
  const app = createApp({
    setup() {
      account = useAccountStore?.()
      useReadinessConnectionSync(gateway, account)
      void account?.init()
      return () => h(Component)
    },
  })
  app.provide(SETUP_WORKFLOW_KEY, {
    status,
  } as unknown as import('@/modules/setupWorkflow').SetupWorkflow)
  app.provide(GATEWAY_ACCESS_KEY, gateway as unknown as import('@/modules/gatewayAccess').GatewayAccess)
  if (pinia) app.use(pinia)
  app.use(i18n)
  app.mount(el)
  let mounted = true
  const unmount = () => {
    if (mounted) app.unmount()
    mounted = false
  }
  cleanups.push(unmount)
  await settle()
  await nextTick()
  function publishAccount(state: TokenRhythmAccountState) {
    currentAccount = state
    for (const listener of accountListeners) listener(state)
  }
  return { el, status, response, gateway, releaseAdmission, unmount, account, publishAccount }
}

beforeEach(() => {
  routerPush.mockClear()
  document.body.innerHTML = ''
})
afterEach(() => {
  for (const cleanup of cleanups.splice(0).reverse()) cleanup()
  vi.doUnmock('@/platform')
})

describe('ChatModelSetupNotice', () => {
  it('offers the existing model settings overlay when local configuration is missing', async () => {
    const { el, status } = await mountNotice({ llmConfigured: false })
    expect(el.querySelector('[role="status"]')?.textContent).toContain('Configure a model service before chatting.')
    el.querySelector('button')!.click()
    await settle()
    expect(routerPush).toHaveBeenCalledWith('/settings/provider')
    // The notice only requests local status; it does not require a probe.
    expect(status).toHaveBeenCalledOnce()
  })

  it.each([
    { llmConfigured: true },
    {},
    new Error('Status unavailable'),
  ])('does not show a missing-model notice for ready or unknown state: %j', async response => {
    const { el } = await mountNotice(response)
    expect(el.querySelector('[role="status"]')).toBeNull()
    expect(el.querySelector('button')).toBeNull()
  })

  it('refreshes after a settings save and clears without a client restart', async () => {
    const { el, response } = await mountNotice({ llmConfigured: false })
    expect(el.querySelector('[role="status"]')).not.toBeNull()
    response.value = { llmConfigured: true }
    const { invalidateReadiness } = await import('@/composables/setup/readinessInvalidation')
    invalidateReadiness()
    await settle()
    expect(el.querySelector('[role="status"]')).toBeNull()
  })

  it('keeps setup reads behind chat session recovery and applies pending invalidation', async () => {
    const { el, status, response, releaseAdmission } = await mountNotice({ llmConfigured: false }, true)
    const { invalidateReadiness } = await import('@/composables/setup/readinessInvalidation')
    invalidateReadiness()
    expect(status).not.toHaveBeenCalled()
    expect(el.querySelector('[role="status"]')).toBeNull()
    response.value = { llmConfigured: true }
    releaseAdmission()
    await settle()
    expect(status).toHaveBeenCalled()
    expect(el.querySelector('[role="status"]')).toBeNull()
  })

  it('stops observing settings saves after unmount', async () => {
    const { status, unmount } = await mountNotice({ llmConfigured: false })
    unmount()
    status.mockClear()
    const { invalidateReadiness } = await import('@/composables/setup/readinessInvalidation')
    invalidateReadiness()
    await settle()
    expect(status).not.toHaveBeenCalled()
  })

  it('refreshes after native onboarding restarts the Gateway without a WebUI save event', async () => {
    const { el, status, response, gateway } = await mountNotice({ llmConfigured: false })
    expect(el.querySelector('[role="status"]')).not.toBeNull()
    status.mockClear()
    gateway.isAvailable = false
    gateway.subscriptionEpoch += 1
    response.value = { llmConfigured: true }
    await settle()
    expect(status).not.toHaveBeenCalled()
    gateway.isAvailable = true
    await settle()
    expect(status).toHaveBeenCalled()
    expect(el.querySelector('[role="status"]')).toBeNull()
  })

  it('clears the startup notice after a restored account becomes ready without restarting the Gateway', async () => {
    const f = await mountNotice({ llmConfigured: false }, false, accountState(false))
    expect(f.account?.ready).toBe(false)
    expect(f.el.querySelector('[role="status"]')).not.toBeNull()
    f.status.mockClear()
    f.response.value = { llmConfigured: true }

    f.publishAccount({ ...accountState(true), revision: 2 })
    await settle()

    expect(f.account?.ready).toBe(true)
    expect(f.account?.primaryActivation).toBeNull()
    expect(f.gateway).toEqual({ isAvailable: true, subscriptionEpoch: 1 })
    expect(f.status).toHaveBeenCalledOnce()
    expect(f.el.querySelector('[role="status"]')).toBeNull()
  })

  it('keeps the notice hidden when account logout leaves a configured manual provider available', async () => {
    const initialAccount = { ...accountState(true), source: 'manual' as const }
    const f = await mountNotice({ llmConfigured: true }, false, initialAccount)
    expect(f.el.querySelector('[role="status"]')).toBeNull()
    f.status.mockClear()

    f.publishAccount({
      ...initialAccount,
      revision: 2,
      status: 'signed_out',
      keyStatus: 'missing',
      profile: null,
      selectedAccountId: null,
    })
    await settle()

    expect(f.account?.ready).toBe(false)
    expect(f.status).toHaveBeenCalledOnce()
    expect(f.el.querySelector('[role="status"]')).toBeNull()
  })
})
