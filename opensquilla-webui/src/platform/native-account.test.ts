// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AccountPrimaryActivation, TokenRhythmAccountState } from '../../../contracts/desktop/tokenrhythm-account'
import { createDesktopPlatform } from './desktop'

const state = { revision: 1, status: 'signed_in', source: 'manual' } as TokenRhythmAccountState
const request: AccountPrimaryActivation = { id: 'login-a', accountId: 'a', sessionId: 'session-a' }

afterEach(() => { delete window.opensquillaDesktop })

describe('native account host compatibility', () => {
  it('keeps manual primary selection working with a host that has no automatic login default', async () => {
    const getAccountState = vi.fn(async () => state)
    const selectAccountSource = vi.fn(async () => state)
    window.opensquillaDesktop = { getAccountState, selectAccountSource } as unknown as OpenSquillaDesktopApi
    const account = createDesktopPlatform().account!
    expect(await account.cancelPrimaryActivation()).toBe(state)
    expect(await account.selectSource('account')).toBe(state)
    expect(getAccountState).toHaveBeenCalledOnce()
    expect(selectAccountSource).toHaveBeenCalledExactlyOnceWith('account')
  })

  it('never sends a guarded automatic bind through an older unguarded host', async () => {
    const selectAccountSource = vi.fn(async () => state)
    window.opensquillaDesktop = { getAccountState: async () => state, selectAccountSource } as unknown as OpenSquillaDesktopApi
    await expect(createDesktopPlatform().account!.selectSource('account', request)).rejects.toThrow('ACCOUNT_PRIMARY_ACTIVATION_STALE')
    expect(selectAccountSource).not.toHaveBeenCalled()
  })

  it('forwards the expected login identity and scoped cancellation to a current host', async () => {
    const selectAccountSource = vi.fn(async () => state)
    const cancelAccountPrimaryActivation = vi.fn(async () => state)
    window.opensquillaDesktop = { getAccountState: async () => state, selectAccountSource, cancelAccountPrimaryActivation } as unknown as OpenSquillaDesktopApi
    const account = createDesktopPlatform().account!
    await account.selectSource('account', request)
    await account.cancelPrimaryActivation(request.id)
    expect(selectAccountSource).toHaveBeenCalledExactlyOnceWith('account', request)
    expect(cancelAccountPrimaryActivation).toHaveBeenCalledExactlyOnceWith(request.id)
  })
})
