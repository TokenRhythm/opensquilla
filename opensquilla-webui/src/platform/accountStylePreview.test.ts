import { describe, expect, it, vi } from 'vitest'

describe('local account preview', () => {
  it('supports sign-out and sign-in without mutating previously returned snapshots', async () => {
    vi.resetModules()
    const { accountStylePreviewRequest: request } = await import('./accountStylePreview')
    const initial = (await request('state')).state
    const signedOut = (await request('action', { action: 'logout' })).state
    expect(initial.status).toBe('signed_in')
    expect(signedOut.status).toBe('signed_out')
    expect(signedOut.profile).toBeNull()
    expect(signedOut.wallet).toBeNull()
    expect(signedOut.revision).toBeGreaterThan(initial.revision)
    const signedIn = (await request('action', { action: 'login' })).state
    expect(signedIn.profile?.displayName).toBe('Demo user')
    expect(signedIn.wallet?.balances.available).toBe('1241.62')
    expect(signedIn.revision).toBeGreaterThan(signedOut.revision)
    expect(signedOut.status).toBe('signed_out')
  })

  it('never opens a real account or payment destination', async () => {
    const { accountStylePreviewRequest: request } = await import('./accountStylePreview')
    for (const link of ['account', 'recharge', 'usage']) {
      await expect(request('link', { link })).rejects.toThrow('ACCOUNT_LINK_UNAVAILABLE')
    }
  })
})
