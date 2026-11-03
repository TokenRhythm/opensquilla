// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAccountStore } from './account'
import type { TokenRhythmAccountState } from '../../../contracts/desktop/tokenrhythm-account'

const mock = vi.hoisted(() => ({ api: { action: vi.fn(), getState: vi.fn(), onState: vi.fn(), selectSource: vi.fn(), cancelPrimaryActivation: vi.fn() } }))
vi.mock('@/platform', () => ({ usePlatform: () => ({ account: mock.api }) }))

const state: TokenRhythmAccountState = {
  revision: 1, status: 'signed_in', profile: { accountId: 'a', sessionId: 's', displayName: '小明', maskedPhone: '' },
  wallet: null, walletStatus: 'idle', keyStatus: 'ready', errorCode: null, walletErrorCode: null,
  source: 'manual', selectedAccountId: null, mediaSources: { image: 'inherit', video: 'inherit' },
  pendingRevocation: false, loginExpiresAt: null,
}

afterEach(() => { vi.restoreAllMocks(); vi.resetAllMocks(); setActivePinia(undefined) })

describe('account refresh', () => {
  it('coalesces refreshes without blocking account actions, and rejects older results after logout', async () => {
    setActivePinia(createPinia())
    const store = useAccountStore()
    let finish!: (value: TokenRhythmAccountState) => void
    mock.api.action.mockImplementationOnce(() => new Promise<TokenRhythmAccountState>(resolve => { finish = resolve }))
    const first = store.action('refresh')
    const second = store.action('refresh')
    expect(mock.api.action).toHaveBeenCalledExactlyOnceWith('refresh')
    expect(store.refreshing).toBe(true)
    expect(store.busy).toBe(false)
    const signedOut = { ...state, revision: 3, status: 'signed_out' as const, profile: null }
    mock.api.action.mockResolvedValueOnce(signedOut)
    const logout = store.action('logout')
    expect(store.busy).toBe(true)
    await logout
    expect(store.busy).toBe(false)
    expect(store.refreshing).toBe(true)
    finish({ ...state, revision: 2 })
    await Promise.all([first, second])
    expect(store.state).toEqual(signedOut)
    expect(store.refreshing).toBe(false)
  })

  it('releases the refresh guard after a network failure and allows retry', async () => {
    setActivePinia(createPinia())
    const store = useAccountStore()
    mock.api.action.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(state)
    await expect(store.action('refresh')).rejects.toThrow('offline')
    expect(store.error).toBe('ACCOUNT_NETWORK_UNAVAILABLE')
    expect(store.refreshing).toBe(false)
    expect(store.busy).toBe(false)
    await store.action('refresh')
    expect(store.error).toBe('')
    expect(store.state).toEqual(state)
  })

  it('uses non-blocking refresh on focus and skips it while a refresh or mutation is pending', async () => {
    setActivePinia(createPinia())
    const listener = vi.spyOn(window, 'addEventListener')
    const store = useAccountStore()
    mock.api.getState.mockResolvedValueOnce(state)
    await store.init()
    const focus = listener.mock.calls.find(([event]) => event === 'focus')![1] as () => void
    let finish!: (value: TokenRhythmAccountState) => void
    mock.api.action.mockImplementationOnce(() => new Promise<TokenRhythmAccountState>(resolve => { finish = resolve }))
    focus(); focus()
    expect(mock.api.action).toHaveBeenCalledExactlyOnceWith('refresh')
    expect(store.busy).toBe(false)
    expect(store.refreshing).toBe(true)
    finish(state)
    await vi.waitFor(() => expect(store.refreshing).toBe(false))
    mock.api.action.mockImplementationOnce(() => new Promise<TokenRhythmAccountState>(resolve => { finish = resolve }))
    const mutation = store.action('logout')
    focus()
    expect(mock.api.action).toHaveBeenCalledTimes(2)
    finish({ ...state, revision: 2, status: 'signed_out', profile: null })
    await mutation
    window.removeEventListener('focus', focus)
  })
})

describe('login primary activation intent', () => {
  const request = { id: 'login-a', accountId: 'a', sessionId: 's' }

  async function initialize(initial: TokenRhythmAccountState = state) {
    setActivePinia(createPinia())
    mock.api.getState.mockResolvedValueOnce(initial)
    const store = useAccountStore()
    await store.init()
    const publish = mock.api.onState.mock.calls[0]![0] as (value: TokenRhythmAccountState) => void
    return { store, publish }
  }

  it('does not invent activation on restored login or ordinary refresh', async () => {
    const { store, publish } = await initialize()
    expect(store.ready).toBe(true)
    expect(store.primaryActivation).toBeNull()
    publish({ ...state, revision: 2, walletStatus: 'ready' })
    mock.api.action.mockResolvedValueOnce({ ...state, revision: 3 })
    await store.action('refresh')
    expect(store.primaryActivation).toBeNull()
    expect(mock.api.selectSource).not.toHaveBeenCalled()
  })

  it('immediately supersedes a pending OAuth login when the user makes a manual choice', async () => {
    const { store, publish } = await initialize({ ...state, status: 'authorizing', profile: null, keyStatus: 'missing' })
    let finish!: (value: TokenRhythmAccountState) => void
    mock.api.cancelPrimaryActivation.mockImplementationOnce(() => new Promise<TokenRhythmAccountState>(resolve => { finish = resolve }))
    const cancellation = store.cancelPrimaryActivation()
    publish({ ...state, revision: 3, primaryActivation: request })
    expect(store.primaryActivation).toBeNull()
    finish({ ...state, revision: 2, primaryActivation: null })
    await cancellation
    expect(store.state?.revision).toBe(3)
    expect(store.primaryActivation).toBeNull()

    mock.api.action.mockResolvedValueOnce({ ...state, revision: 4, status: 'authorizing', profile: null, primaryActivation: null })
    await store.action('login')
    publish({ ...state, revision: 5, primaryActivation: { ...request, id: 'new-login-a' } })
    expect(store.primaryActivation?.id).toBe('new-login-a')
  })

  it('finishing an older request never suppresses a newer account login', async () => {
    const { store, publish } = await initialize({ ...state, primaryActivation: request })
    let finish!: (value: TokenRhythmAccountState) => void
    mock.api.cancelPrimaryActivation.mockImplementationOnce(() => new Promise<TokenRhythmAccountState>(resolve => { finish = resolve }))
    const cancellation = store.cancelPrimaryActivation(request.id)
    expect(store.primaryActivation).toBeNull()
    const newer = { id: 'login-b', accountId: 'b', sessionId: 'session-b' }
    publish({ ...state, revision: 4, profile: { ...state.profile!, accountId: 'b', sessionId: 'session-b' }, primaryActivation: newer })
    expect(store.primaryActivation).toEqual(newer)
    finish({ ...state, revision: 2, primaryActivation: null })
    await cancellation
    expect(store.primaryActivation).toEqual(newer)
  })

  it('forwards the exact login identity to guarded binding without cancelling it', async () => {
    const { store } = await initialize({ ...state, primaryActivation: request })
    mock.api.selectSource.mockResolvedValueOnce({ ...state, revision: 2, source: 'account', selectedAccountId: 'a', primaryActivation: null })
    await store.selectSource('account', request)
    expect(mock.api.selectSource).toHaveBeenCalledExactlyOnceWith('account', request)
    expect(mock.api.cancelPrimaryActivation).not.toHaveBeenCalled()
    expect(store.primaryActivation).toBeNull()
  })

  it('still logs out when cancelling the automatic request cannot reach the host', async () => {
    const { store } = await initialize({ ...state, primaryActivation: request })
    mock.api.cancelPrimaryActivation.mockRejectedValueOnce(new Error('IPC unavailable'))
    mock.api.action.mockResolvedValueOnce({ ...state, revision: 2, status: 'signed_out', profile: null, primaryActivation: null })
    await store.action('logout')
    expect(mock.api.action).toHaveBeenCalledWith('logout')
    expect(store.primaryActivation).toBeNull()
  })
})
