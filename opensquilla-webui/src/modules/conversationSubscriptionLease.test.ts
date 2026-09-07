import { describe, expect, it } from 'vitest'
import { createConversationSubscriptionLeaseRegistry } from './conversationSubscriptionLease'

describe('conversation subscription lease registry', () => {
  it('keeps a newer acquire separate until it is active', () => {
    const registry = createConversationSubscriptionLeaseRegistry()
    const first = registry.acquire('agent:main:alpha')
    const second = registry.acquire('agent:main:alpha')

    expect(registry.latestReleasable('agent:main:alpha')).toBe(second)
    expect(first.state).toBe('acquiring')
    registry.activate(second)
    expect(first.state).toBe('retired')
    expect(second.state).toBe('active')
    expect(registry.active).toBe(second)
  })

  it('does not retire a releasing predecessor when a replacement activates', () => {
    const registry = createConversationSubscriptionLeaseRegistry()
    const predecessor = registry.acquire('agent:main:alpha')
    registry.activate(predecessor)
    predecessor.state = 'releasing'
    const replacement = registry.acquire('agent:main:alpha')

    registry.activate(replacement)

    expect(predecessor.state).toBe('releasing')
    expect(replacement.state).toBe('active')
    expect(registry.size).toBe(2)
  })

  it('retires leases owned by an obsolete socket generation', () => {
    const registry = createConversationSubscriptionLeaseRegistry()
    const oldLease = registry.acquire('agent:main:old')
    oldLease.socketGeneration = 2
    const currentLease = registry.acquire('agent:main:new')
    currentLease.socketGeneration = 3

    registry.retirePriorGenerations(3)

    expect(oldLease.state).toBe('retired')
    expect(currentLease.state).toBe('acquiring')
    expect(registry.size).toBe(1)
  })

  it('only returns live leases and clears active ownership explicitly', () => {
    const registry = createConversationSubscriptionLeaseRegistry()
    const lease = registry.acquire('agent:main:alpha')
    registry.clearActive(lease)
    lease.state = 'releasing'

    expect(registry.active).toBeNull()
    expect(registry.latestReleasable('agent:main:alpha')).toBeNull()
    registry.retire(lease)
    expect(registry.size).toBe(0)
  })
})

