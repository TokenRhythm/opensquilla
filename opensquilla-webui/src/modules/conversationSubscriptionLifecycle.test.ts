import { describe, expect, it, vi } from 'vitest'
import { createConversationSubscriptionLifecycle } from './conversationSubscriptionLifecycle'

const identity = (key = 'agent:main:test') => ({
  key,
  sinceStreamGeneration: null,
  sinceStreamSeq: 0,
  bootstrapGeneration: -1,
  bootstrapAttempt: -1,
})

describe('conversation subscription lifecycle', () => {
  it('deduplicates an identical active attempt', async () => {
    const lifecycle = createConversationSubscriptionLifecycle<number>()
    const run = vi.fn(async () => 7)
    const first = lifecycle.start(identity(), undefined, run)
    const second = lifecycle.start(identity(), undefined, run)

    expect(second).toBe(first)
    expect(await first).toBe(7)
    expect(run).toHaveBeenCalledTimes(1)
    expect(lifecycle.leases.size).toBe(0)
  })

  it('aborts the predecessor and fences its late completion', async () => {
    const lifecycle = createConversationSubscriptionLifecycle<string>()
    let predecessor!: ReturnType<typeof lifecycle.start>
    const predecessorSignal = vi.fn()
    predecessor = lifecycle.start(identity('alpha'), undefined, async attempt => {
      attempt.controller.signal.addEventListener('abort', predecessorSignal)
      await new Promise<void>(resolve => setTimeout(resolve, 0))
      return 'old'
    })
    const successor = lifecycle.start(identity('beta'), undefined, async attempt => {
      expect(lifecycle.isCurrent(attempt, 'beta')).toBe(true)
      return 'new'
    })

    await Promise.all([predecessor, successor])
    expect(predecessorSignal).toHaveBeenCalledTimes(1)
    expect(await successor).toBe('new')
    expect(lifecycle.leases.size).toBe(0)
  })

  it('relays external cancellation without owning the transport', async () => {
    const lifecycle = createConversationSubscriptionLifecycle<void>()
    const external = new AbortController()
    let signal!: AbortSignal
    const promise = lifecycle.start(identity(), external.signal, async attempt => {
      signal = attempt.controller.signal
      await new Promise<void>(resolve => setTimeout(resolve, 0))
    })
    external.abort()
    await promise
    expect(signal.aborted).toBe(true)
    expect(lifecycle.leases.size).toBe(0)
  })

  it('keeps a finished attempt current for detached hydration until cancelled', async () => {
    const lifecycle = createConversationSubscriptionLifecycle<boolean>()
    let attemptRef: Parameters<typeof lifecycle.isCurrent>[0] | undefined
    const promise = lifecycle.start(identity(), undefined, async attempt => {
      attemptRef = attempt
      return true
    })
    await promise
    expect(attemptRef).toBeDefined()
    expect(lifecycle.isCurrent(attemptRef!, 'agent:main:test')).toBe(true)
    lifecycle.cancel()
    expect(lifecycle.isCurrent(attemptRef!, 'agent:main:test')).toBe(false)
    expect(lifecycle.leases.size).toBe(0)
  })

  it('does not retire a transport-activated lease when its attempt settles', async () => {
    const lifecycle = createConversationSubscriptionLifecycle<boolean>()
    let lease: Parameters<typeof lifecycle.leases.activate>[0] | undefined
    await lifecycle.start(identity(), undefined, async attempt => {
      lease = attempt.lease
      lifecycle.leases.activate(attempt.lease)
      return true
    })

    expect(lease?.state).toBe('active')
    expect(lifecycle.leases.size).toBe(1)
    lifecycle.cancel()
    expect(lease?.state).toBe('active')
    lifecycle.leases.retire(lease!)
    expect(lifecycle.leases.size).toBe(0)
  })

  it('leaves a pending transport-activated lease for its transport to release', async () => {
    const lifecycle = createConversationSubscriptionLifecycle<boolean>()
    let lease: Parameters<typeof lifecycle.leases.activate>[0] | undefined
    let release!: () => void
    const pending = lifecycle.start(identity(), undefined, async attempt => {
      lease = attempt.lease
      lifecycle.leases.activate(attempt.lease)
      await new Promise<void>(resolve => { release = resolve })
      return true
    })

    lifecycle.cancel()
    expect(lease?.state).toBe('active')
    expect(lifecycle.leases.size).toBe(1)
    release()
    await pending
    expect(lease?.state).toBe('active')
    lifecycle.leases.retire(lease!)
    expect(lifecycle.leases.size).toBe(0)
  })
})
