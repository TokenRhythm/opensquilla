// @vitest-environment happy-dom
import { computed, createApp, nextTick, reactive } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { TokenRhythmAccountState } from '../../../../contracts/desktop/tokenrhythm-account'
import {
  acquireSessionBootstrapAdmission,
  optionalSessionRpcAllowed,
} from '@/composables/chat/sessionBootstrapAdmission'
import { onReadinessInvalidated } from './readinessInvalidation'
import { useReadinessConnectionSync } from './useReadinessConnectionSync'
import { useSetupStatus, type SetupStatusState } from './useSetupStatus'

const cleanups: Array<() => void> = []
afterEach(() => { for (const cleanup of cleanups.splice(0).reverse()) cleanup() })

type Status = { llmConfigured: boolean }

function accountState(ready = false): TokenRhythmAccountState {
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

function accountFixture(ready = false) {
  return reactive({
    supported: true,
    state: accountState(ready) as TokenRhythmAccountState | null,
    get ready(): boolean {
      return this.state?.status === 'signed_in' && this.state.keyStatus === 'ready'
    },
  })
}

async function settle() {
  await nextTick()
  await Promise.resolve()
  await nextTick()
}

function deferred() {
  let resolve!: (value: Status) => void
  const promise = new Promise<Status>(done => { resolve = done })
  return { promise, resolve }
}

function mount(options: {
  account?: ReturnType<typeof accountFixture>
  readStatus?: () => Promise<Status>
} = {}) {
  const access = reactive({ isAvailable: true, subscriptionEpoch: 1 })
  const settingsRefresh = vi.fn()
  const composerRefresh = vi.fn()
  cleanups.push(onReadinessInvalidated(settingsRefresh), onReadinessInvalidated(composerRefresh))
  const status = vi.fn(options.readStatus ?? (async () => ({ llmConfigured: options.account?.ready ?? false })))
  const port = { status }
  let first!: SetupStatusState<Status>
  let second!: SetupStatusState<Status>
  const app = createApp({
    setup() {
      useReadinessConnectionSync(access, options.account)
      const allowed = computed(() => access.isAvailable && optionalSessionRpcAllowed.value)
      first = useSetupStatus<Status>(port, { allowed })
      second = useSetupStatus<Status>(port, { allowed })
      return () => null
    },
  })
  const el = document.createElement('div')
  app.mount(el)
  let mounted = true
  function unmount() {
    if (mounted) app.unmount()
    mounted = false
  }
  cleanups.push(unmount)
  return { access, settingsRefresh, composerRefresh, status, first, second, unmount }
}

describe('app readiness synchronization', () => {
  it('broadcasts one refresh to every subscriber after a Gateway reconnect', async () => {
    const { access, settingsRefresh, composerRefresh } = mount()
    access.isAvailable = false
    access.subscriptionEpoch += 1
    await nextTick()
    expect(settingsRefresh).not.toHaveBeenCalled()
    access.isAvailable = true
    await nextTick()
    expect(settingsRefresh).toHaveBeenCalledOnce()
    expect(composerRefresh).toHaveBeenCalledOnce()
  })

  it('defers reconnect refresh until critical session recovery releases admission', async () => {
    const { access, settingsRefresh, composerRefresh } = mount()
    const release = acquireSessionBootstrapAdmission()
    cleanups.push(release)
    access.subscriptionEpoch += 1
    await nextTick()
    expect(settingsRefresh).not.toHaveBeenCalled()
    release()
    await nextTick()
    expect(settingsRefresh).toHaveBeenCalledOnce()
    expect(composerRefresh).toHaveBeenCalledOnce()

    const releaseUnrelatedHold = acquireSessionBootstrapAdmission()
    cleanups.push(releaseUnrelatedHold)
    await nextTick()
    releaseUnrelatedHold()
    await nextTick()
    expect(settingsRefresh).toHaveBeenCalledOnce()
  })

  it('refreshes both cached consumers once when a restored account becomes ready without activation', async () => {
    const account = accountFixture()
    const f = mount({ account })
    await settle()
    expect(f.first.data.value?.llmConfigured).toBe(false)
    expect(f.second.data.value?.llmConfigured).toBe(false)
    expect(f.status).toHaveBeenCalledOnce()
    expect(f.settingsRefresh).not.toHaveBeenCalled()

    Object.assign(account.state!, { status: 'signed_in', keyStatus: 'ready', revision: 2 })
    await settle()

    expect(account.ready).toBe(true)
    expect(account.state?.primaryActivation).toBeNull()
    expect(f.access).toEqual({ isAvailable: true, subscriptionEpoch: 1 })
    expect(f.first.data.value?.llmConfigured).toBe(true)
    expect(f.second.data.value?.llmConfigured).toBe(true)
    expect(f.status).toHaveBeenCalledTimes(2)
    expect(f.settingsRefresh).toHaveBeenCalledOnce()
    expect(f.composerRefresh).toHaveBeenCalledOnce()
  })

  it('refreshes a ready snapshot after the account loses its model credential', async () => {
    const account = accountFixture(true)
    const f = mount({ account })
    await settle()
    expect(f.first.data.value?.llmConfigured).toBe(true)

    account.state!.keyStatus = 'unavailable'
    await settle()

    expect(f.first.data.value?.llmConfigured).toBe(false)
    expect(f.second.data.value?.llmConfigured).toBe(false)
    expect(f.status).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['credential source', (state: TokenRhythmAccountState) => { state.source = 'manual' }],
    ['selected account', (state: TokenRhythmAccountState) => { state.selectedAccountId = 'other-account' }],
    ['account identity', (state: TokenRhythmAccountState) => { state.profile!.accountId = 'other-account' }],
    ['session identity', (state: TokenRhythmAccountState) => { state.profile!.sessionId = 'other-session' }],
    ['image source', (state: TokenRhythmAccountState) => { state.mediaSources.image = 'manual' }],
    ['video source', (state: TokenRhythmAccountState) => { state.mediaSources.video = 'account' }],
  ] as const)('refreshes status when the %s changes without changing account readiness', async (_name, change) => {
    const account = accountFixture(true)
    const f = mount({ account })
    await settle()

    change(account.state!)
    await settle()

    expect(account.ready).toBe(true)
    expect(f.status).toHaveBeenCalledTimes(2)
    expect(f.settingsRefresh).toHaveBeenCalledOnce()
  })

  it('ignores account revisions, wallet refreshes, and profile display changes', async () => {
    const account = accountFixture(true)
    const f = mount({ account })
    await settle()
    account.state = {
      ...account.state!,
      revision: 2,
      walletStatus: 'ready',
      wallet: {
        currency: 'USD',
        balances: { available: '5', recharge: '5', bonus: '0', debt: '0', frozen: '0' },
        asOf: '2026-01-01T00:00:00Z',
      },
      profile: { ...account.state!.profile!, displayName: 'Renamed example' },
    }
    await settle()

    expect(f.status).toHaveBeenCalledOnce()
    expect(f.settingsRefresh).not.toHaveBeenCalled()
  })

  it.each([false, true])('coalesces batched account changes into one read (with reconnect: %s)', async reconnect => {
    const account = accountFixture()
    const f = mount({ account })
    await settle()

    account.state = { ...accountState(true), revision: 2, selectedAccountId: 'other-account' }
    account.state.profile!.accountId = 'other-account'
    account.state.mediaSources.image = 'account'
    if (reconnect) f.access.subscriptionEpoch += 1
    await settle()

    expect(f.first.data.value?.llmConfigured).toBe(true)
    expect(f.second.data.value?.llmConfigured).toBe(true)
    expect(f.status).toHaveBeenCalledTimes(2)
    expect(f.settingsRefresh).toHaveBeenCalledOnce()
  })

  it('waits for a disconnected Gateway before refreshing account changes once', async () => {
    const account = accountFixture()
    const f = mount({ account })
    await settle()
    f.access.isAvailable = false
    await settle()
    account.state = accountState(true)
    await settle()
    expect(f.status).toHaveBeenCalledOnce()
    expect(f.settingsRefresh).not.toHaveBeenCalled()

    f.access.isAvailable = true
    await settle()
    expect(f.first.data.value?.llmConfigured).toBe(true)
    expect(f.status).toHaveBeenCalledTimes(2)
    expect(f.settingsRefresh).toHaveBeenCalledOnce()
  })

  it('defers account refresh behind session recovery and reads the latest state once', async () => {
    const account = accountFixture()
    const f = mount({ account })
    await settle()
    const release = acquireSessionBootstrapAdmission()
    cleanups.push(release)
    await nextTick()
    account.state = accountState(true)
    await settle()
    account.state!.mediaSources.image = 'account'
    await settle()
    expect(f.status).toHaveBeenCalledOnce()
    expect(f.settingsRefresh).not.toHaveBeenCalled()

    release()
    await settle()
    expect(f.first.data.value?.llmConfigured).toBe(true)
    expect(f.second.data.value?.llmConfigured).toBe(true)
    expect(f.status).toHaveBeenCalledTimes(2)
    expect(f.settingsRefresh).toHaveBeenCalledOnce()
  })

  it('does not let an older status response overwrite the refreshed account snapshot', async () => {
    const account = accountFixture()
    const before = deferred()
    const after = deferred()
    const readStatus = vi.fn().mockReturnValueOnce(before.promise).mockReturnValueOnce(after.promise)
    const f = mount({ account, readStatus })
    expect(f.status).toHaveBeenCalledOnce()
    account.state = accountState(true)
    await nextTick()
    expect(f.status).toHaveBeenCalledTimes(2)
    after.resolve({ llmConfigured: true })
    await settle()
    expect(f.first.data.value?.llmConfigured).toBe(true)

    before.resolve({ llmConfigured: false })
    await settle()
    expect(f.first.data.value?.llmConfigured).toBe(true)
    expect(f.second.data.value?.llmConfigured).toBe(true)
  })

  it('does not subscribe to unavailable account support', async () => {
    const account = accountFixture()
    account.supported = false
    const f = mount({ account })
    await settle()
    account.state = accountState(true)
    await settle()
    expect(f.status).toHaveBeenCalledOnce()
    expect(f.settingsRefresh).not.toHaveBeenCalled()
  })

  it('stops refreshing account changes after the app scope is unmounted', async () => {
    const account = accountFixture()
    const f = mount({ account })
    await settle()
    f.unmount()
    account.state = accountState(true)
    await settle()
    expect(f.status).toHaveBeenCalledOnce()
    expect(f.settingsRefresh).not.toHaveBeenCalled()
  })
})
