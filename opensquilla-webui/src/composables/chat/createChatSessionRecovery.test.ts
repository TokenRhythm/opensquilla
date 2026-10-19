import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { createChatSessionRecovery } from './createChatSessionRecovery'
import type { SessionRecoveryProof } from './sessionRecoveryProof'
import { SessionReadFailure } from '@/modules/sessionReadLifecycle'
import type { SessionSubscriptionOutcome } from './useChatSessionSubscription'

const READY: SessionSubscriptionOutcome = { authoritative: true, live: false, backgroundOnly: false }
const proof = (
  isSatisfied: () => boolean = () => true,
  isCurrent: () => boolean = () => true,
): SessionRecoveryProof => ({
  revision: ref(0), capture: () => Object.assign(isSatisfied, { isCurrent }),
})

describe('current session recovery callback', () => {
  it.each(['session', 'proof', 'disposal', 'result'] as const)('rejects success superseded by %s', async transition => {
    let key = 'session-a'
    let currentProof = true
    let disposed = false
    let isCurrent = true
    let complete!: (result: SessionSubscriptionOutcome) => void
    const pending = new Promise<SessionSubscriptionOutcome>(resolve => { complete = resolve })
    const recover = createChatSessionRecovery({
      sessionKey: () => key, recoveryProof: proof(() => currentProof), isDisposed: () => disposed,
      retryLive: () => pending, isCurrentLiveResult: () => isCurrent,
    })
    const recovery = recover()
    if (transition === 'session') key = 'session-b'
    else if (transition === 'proof') currentProof = false
    else if (transition === 'disposal') disposed = true
    else isCurrent = false
    complete(READY)
    await expect(recovery).resolves.toBe(false)
  })

  it('retains terminal failure evidence for flow suspension', async () => {
    const failure = new SessionReadFailure('budget-exhausted', 'No remaining recovery budget', false)
    const recover = createChatSessionRecovery({
      sessionKey: () => 'session-a', recoveryProof: proof(() => false), isDisposed: () => false,
      retryLive: async () => ({ ...READY, authoritative: false, error: failure }),
      isCurrentLiveResult: () => false,
    })
    await expect(recover()).rejects.toBe(failure)
  })

  it('does not suspend the new authority with an older terminal failure', async () => {
    const failure = new SessionReadFailure('budget-exhausted', 'Old authority exhausted its recovery budget', false)
    let authorityCurrent = true
    let complete!: (result: SessionSubscriptionOutcome) => void
    const pending = new Promise<SessionSubscriptionOutcome>(resolve => { complete = resolve })
    const recover = createChatSessionRecovery({
      sessionKey: () => 'session-a', recoveryProof: proof(() => false, () => authorityCurrent),
      isDisposed: () => false, retryLive: () => pending, isCurrentLiveResult: () => false,
    })
    const recovery = recover()
    complete({ ...READY, authoritative: false, error: failure })
    // Authority can change after the coordinator finishes but before this
    // callback resumes to classify the result for the transport flow.
    authorityCurrent = false
    await expect(recovery).resolves.toBe(false)
  })

  it('does not start work for an unrelated recovery scope', async () => {
    const retryLive = vi.fn(async () => READY)
    const recover = createChatSessionRecovery({
      sessionKey: () => 'session-a', recoveryProof: proof(), isDisposed: () => false,
      retryLive, isCurrentLiveResult: () => true,
    })
    await expect(recover({ keys: ['session-b'], global: false })).resolves.toBe(false)
    expect(retryLive).not.toHaveBeenCalled()
    await expect(recover({ keys: ['session-a'], global: false })).resolves.toBe(true)
    expect(retryLive).toHaveBeenCalledOnce()
  })

  it('captures the fresh-read proof before starting the retry', async () => {
    const steps: string[] = []
    const recover = createChatSessionRecovery({
      sessionKey: () => 'session-a', isDisposed: () => false,
      recoveryProof: {
        revision: ref(0),
        capture: () => { steps.push('capture'); return Object.assign(() => true, { isCurrent: () => true }) },
      },
      retryLive: async () => { steps.push('retry'); return READY },
      isCurrentLiveResult: () => true,
    })
    await expect(recover()).resolves.toBe(true)
    expect(steps).toEqual(['capture', 'retry'])
  })
})
