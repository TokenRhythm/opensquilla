import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { switchChatViewSession } from './chatViewSessionNavigation'

const AUTHORITATIVE_OUTCOME = {
  authoritative: true,
  authoritativeIdle: true,
  backgroundOnly: false,
}

describe('ChatView post-subscription session recovery', () => {
  it('recovers an alias navigation against the canonical mounted session key', async () => {
    const sessionKey = ref('agent:main:webchat:source')
    const recoveredSessionKeys: string[] = []
    const switchSession = vi.fn(async () => {
      sessionKey.value = 'agent:main:webchat:target'
      return AUTHORITATIVE_OUTCOME
    })
    const handleAuthoritativeSubscription = vi.fn(async (targetSessionKey: string) => {
      if (sessionKey.value !== targetSessionKey) return
      recoveredSessionKeys.push(targetSessionKey)
    })

    await switchChatViewSession(
      'sess-target',
      switchSession,
      handleAuthoritativeSubscription,
    )

    expect(switchSession).toHaveBeenCalledWith('sess-target')
    expect(handleAuthoritativeSubscription).toHaveBeenCalledWith(
      'agent:main:webchat:target',
    )
    expect(recoveredSessionKeys).toEqual(['agent:main:webchat:target'])
  })

  it('retains the completed navigation key so a superseding session stays guarded', async () => {
    const sessionKey = ref('agent:main:webchat:source')
    const recoveredSessionKeys: string[] = []
    let finishSwitch!: (value: typeof AUTHORITATIVE_OUTCOME) => void
    const switchSession = vi.fn(() => new Promise<typeof AUTHORITATIVE_OUTCOME>(resolve => {
      finishSwitch = resolve
    }))
    const handleAuthoritativeSubscription = vi.fn(async (targetSessionKey: string) => {
      if (sessionKey.value !== targetSessionKey) return
      recoveredSessionKeys.push(targetSessionKey)
    })

    const switching = switchChatViewSession(
      'sess-target',
      switchSession,
      handleAuthoritativeSubscription,
    )
    sessionKey.value = 'agent:main:webchat:target'
    finishSwitch(AUTHORITATIVE_OUTCOME)
    sessionKey.value = 'agent:main:webchat:newer'
    await switching

    expect(handleAuthoritativeSubscription).toHaveBeenCalledWith(
      'agent:main:webchat:target',
    )
    expect(recoveredSessionKeys).toEqual([])
  })
})
