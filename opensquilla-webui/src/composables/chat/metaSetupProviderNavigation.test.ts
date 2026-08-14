import { describe, expect, it, vi } from 'vitest'

import { navigateMetaSetupProviderSettings } from './metaSetupProviderNavigation'

function harness(overrides: {
  currentRouteSession?: unknown
  replaceResult?: unknown
  pushResult?: unknown
  pushError?: Error
} = {}) {
  const beginHandoff = vi.fn(() => 'meta-resume-request-1')
  const cancelHandoff = vi.fn()
  const materializeSession = vi.fn()
  const replace = vi.fn(async () => overrides.replaceResult)
  const push = vi.fn(async () => {
    if (overrides.pushError) throw overrides.pushError
    return overrides.pushResult
  })
  const run = () => navigateMetaSetupProviderSettings({
    providerId: 'acme-media',
    sessionKey: 'agent:main:webchat:draft-1',
    currentRouteSession: overrides.currentRouteSession,
    router: { replace, push },
    beginHandoff,
    cancelHandoff,
    materializeSession,
  })
  return { beginHandoff, cancelHandoff, materializeSession, replace, push, run }
}

describe('navigateMetaSetupProviderSettings', () => {
  it('materializes a provisional draft before opening the requested provider', async () => {
    const test = harness()

    await expect(test.run()).resolves.toBe(true)

    expect(test.beginHandoff).toHaveBeenCalledWith('acme-media')
    expect(test.replace).toHaveBeenCalledWith({
      path: '/chat',
      query: { session: 'agent:main:webchat:draft-1' },
    })
    expect(test.materializeSession).toHaveBeenCalledWith('agent:main:webchat:draft-1')
    expect(test.push).toHaveBeenCalledWith({
      path: '/settings/provider',
      hash: '#provider-acme-media',
    })
    expect(test.cancelHandoff).not.toHaveBeenCalled()
  })

  it('keeps an established chat route and opens provider settings directly', async () => {
    const test = harness({ currentRouteSession: 'agent:main:webchat:draft-1' })

    await expect(test.run()).resolves.toBe(true)

    expect(test.replace).not.toHaveBeenCalled()
    expect(test.materializeSession).not.toHaveBeenCalled()
    expect(test.push).toHaveBeenCalledOnce()
  })

  it('cancels the one-shot handoff when either navigation does not complete', async () => {
    const replaceFailure = harness({ replaceResult: { type: 'aborted' } })
    await expect(replaceFailure.run()).resolves.toBe(false)
    expect(replaceFailure.push).not.toHaveBeenCalled()
    expect(replaceFailure.cancelHandoff).not.toHaveBeenCalled()

    const pushFailure = harness({
      currentRouteSession: 'agent:main:webchat:draft-1',
      pushError: new Error('route unavailable'),
    })
    await expect(pushFailure.run()).resolves.toBe(false)
    expect(pushFailure.cancelHandoff).toHaveBeenCalledWith(
      'acme-media',
      'meta-resume-request-1',
    )
  })

  it('does not leave chat when the handoff checkpoint cannot be started', async () => {
    const test = harness({ currentRouteSession: 'agent:main:webchat:draft-1' })
    test.beginHandoff.mockReturnValue('')

    await expect(test.run()).resolves.toBe(false)

    expect(test.push).not.toHaveBeenCalled()
    expect(test.cancelHandoff).not.toHaveBeenCalled()
  })
})
