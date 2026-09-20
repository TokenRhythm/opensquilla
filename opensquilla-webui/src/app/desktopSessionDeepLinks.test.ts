import { describe, expect, it, vi } from 'vitest'
import type { ResolvedSession } from '@/modules/sessionDirectory'
import type { PlatformWindowApi } from '@/platform/types'
import { bindDesktopSessionDeepLinks } from './desktopSessionDeepLinks'

const FIRST_KEY = 'agent:main:webchat:first'
const SECOND_KEY = 'agent:main:webchat:second'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(yes => { resolve = yes })
  return { promise, resolve }
}

async function flush() {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}

function fixture(pending: string | null = null) {
  let notify!: (key: string) => void
  let gatewayChanged = () => {}
  const unsubscribe = vi.fn()
  const unsubscribeGateway = vi.fn()
  const window: PlatformWindowApi = {
    getPendingSessionDeepLink: vi.fn(async () => pending),
    onSessionDeepLink: vi.fn(callback => {
      notify = callback
      return unsubscribe
    }),
  }
  const resolve = vi.fn(async ({ key }: { key: string; signal?: AbortSignal }): Promise<ResolvedSession> => ({
    key, id: 'session-id',
  }))
  const context = { endpoint: 'ws://gateway.example/ws', epoch: 1 as number | null, authenticated: true }
  const options = {
    window,
    directory: { resolve },
    gatewayContext: () => ({ ...context }),
    onGatewayContextChange: vi.fn((callback: () => void) => {
      gatewayChanged = callback
      return unsubscribeGateway
    }),
    openSession: vi.fn(),
    unavailable: vi.fn(),
  }
  return {
    options, resolve, context, unsubscribe, unsubscribeGateway,
    gatewayChanged: () => gatewayChanged(),
    notify: (key: string) => notify(key),
  }
}

describe('desktop session deep links', () => {
  it('consumes startup pending state once and waits for an exact resolution before navigation', async () => {
    const test = fixture(FIRST_KEY)
    const response = deferred<ResolvedSession>()
    test.resolve.mockReturnValue(response.promise)
    bindDesktopSessionDeepLinks(test.options)
    await flush()

    expect(test.options.window.getPendingSessionDeepLink).toHaveBeenCalledOnce()
    expect(test.resolve).toHaveBeenCalledWith({ key: FIRST_KEY, signal: expect.any(AbortSignal) })
    expect(test.options.openSession).not.toHaveBeenCalled()
    response.resolve({ key: FIRST_KEY, id: 'first-id' })
    await flush()
    expect(test.options.openSession).toHaveBeenCalledExactlyOnceWith(FIRST_KEY)
    expect(test.options.unavailable).not.toHaveBeenCalled()
    expect(test.options.window.getPendingSessionDeepLink).toHaveBeenCalledOnce()
  })

  it('does nothing when there is no pending target or desktop API', async () => {
    const test = fixture()
    const cleanup = bindDesktopSessionDeepLinks(test.options)
    await flush()
    cleanup()
    bindDesktopSessionDeepLinks({ ...test.options, window: {} })()
    expect(test.resolve).not.toHaveBeenCalled()
    expect(test.options.unavailable).not.toHaveBeenCalled()
  })

  it('acknowledges each live delivery once without recursively consuming a newer target', async () => {
    const test = fixture()
    const getPending = vi.mocked(test.options.window.getPendingSessionDeepLink!)
    getPending.mockResolvedValueOnce(null).mockResolvedValueOnce(FIRST_KEY).mockResolvedValueOnce(SECOND_KEY)
    bindDesktopSessionDeepLinks(test.options)
    await flush()
    test.notify(FIRST_KEY)
    await flush()

    expect(getPending).toHaveBeenCalledTimes(2)
    expect(test.options.openSession).toHaveBeenCalledExactlyOnceWith(FIRST_KEY)
    test.notify(SECOND_KEY)
    await flush()
    expect(getPending).toHaveBeenCalledTimes(3)
    expect(test.options.openSession.mock.calls.map(([key]) => key)).toEqual([FIRST_KEY, SECOND_KEY])
  })

  it('uses the validated live target when its pending value was already consumed', async () => {
    const test = fixture()
    bindDesktopSessionDeepLinks(test.options)
    await flush()
    test.notify(FIRST_KEY)
    await flush()
    expect(test.options.openSession).toHaveBeenCalledExactlyOnceWith(FIRST_KEY)
  })

  it.each([
    '', ' ', 'a/b', 'a\\b', 'a\nb', 'x'.repeat(513),
  ])('rejects invalid renderer target %j', async key => {
    const test = fixture(key)
    bindDesktopSessionDeepLinks(test.options)
    await flush()
    expect(test.resolve).not.toHaveBeenCalled()
    expect(test.options.openSession).not.toHaveBeenCalled()
    expect(test.options.unavailable).toHaveBeenCalledOnce()
  })

  it.each([
    { key: SECOND_KEY, id: 'different-id' },
    { key: FIRST_KEY, id: '' },
  ])('refuses a mismatched or missing session identity', async result => {
    const test = fixture(FIRST_KEY)
    test.resolve.mockResolvedValue(result)
    bindDesktopSessionDeepLinks(test.options)
    await flush()
    expect(test.options.openSession).not.toHaveBeenCalled()
    expect(test.options.unavailable).toHaveBeenCalledOnce()
  })

  it.each(['forbidden', 'not-found', 'unavailable'])('shows failure without changing route on %s', async reason => {
    const test = fixture(FIRST_KEY)
    test.resolve.mockRejectedValue(new Error(reason))
    bindDesktopSessionDeepLinks(test.options)
    await flush()
    expect(test.options.openSession).not.toHaveBeenCalled()
    expect(test.options.unavailable).toHaveBeenCalledOnce()
  })

  it('only opens the latest delivery when resolves finish out of order', async () => {
    const test = fixture(FIRST_KEY)
    const first = deferred<ResolvedSession>()
    test.resolve.mockReturnValueOnce(first.promise)
    bindDesktopSessionDeepLinks(test.options)
    await flush()
    const signal = test.resolve.mock.calls[0]![0].signal!
    vi.mocked(test.options.window.getPendingSessionDeepLink!).mockResolvedValue(SECOND_KEY)
    test.notify(SECOND_KEY)
    await flush()
    first.resolve({ key: FIRST_KEY, id: 'first-id' })
    await flush()
    expect(signal.aborted).toBe(true)
    expect(test.options.openSession).toHaveBeenCalledExactlyOnceWith(SECOND_KEY)
    expect(test.options.unavailable).not.toHaveBeenCalled()
  })

  it('does not let a late startup pending result replace a live delivery', async () => {
    const test = fixture()
    const startup = deferred<string | null>()
    vi.mocked(test.options.window.getPendingSessionDeepLink!)
      .mockReturnValueOnce(startup.promise).mockResolvedValueOnce(SECOND_KEY)
    bindDesktopSessionDeepLinks(test.options)
    test.notify(SECOND_KEY)
    await flush()
    startup.resolve(FIRST_KEY)
    await flush()
    expect(test.resolve).toHaveBeenCalledOnce()
    expect(test.options.openSession).toHaveBeenCalledExactlyOnceWith(SECOND_KEY)
  })

  it.each(['endpoint', 'epoch', 'disconnected', 'authentication'])('refuses a target if Gateway %s changes during resolution', async change => {
    const test = fixture(FIRST_KEY)
    const response = deferred<ResolvedSession>()
    test.resolve.mockReturnValue(response.promise)
    bindDesktopSessionDeepLinks(test.options)
    await flush()
    if (change === 'endpoint') test.context.endpoint = 'ws://another.example/ws'
    else if (change === 'authentication') test.context.authenticated = false
    else test.context.epoch = change === 'disconnected' ? null : 2
    response.resolve({ key: FIRST_KEY, id: 'first-id' })
    await flush()
    expect(test.options.openSession).not.toHaveBeenCalled()
    expect(test.options.unavailable).toHaveBeenCalledOnce()
  })

  it('retains startup links beyond the RPC timeout and resolves only after authenticated hello', async () => {
    vi.useFakeTimers()
    const test = fixture(FIRST_KEY)
    test.context.epoch = null
    test.context.endpoint = ''
    test.context.authenticated = false
    const cleanup = bindDesktopSessionDeepLinks(test.options)
    try {
      await flush()
      await vi.advanceTimersByTimeAsync(60_000)
      expect(test.resolve).not.toHaveBeenCalled()
      expect(test.options.unavailable).not.toHaveBeenCalled()
      test.context.endpoint = 'ws://127.0.0.1:19382/ws'
      test.context.epoch = 1
      test.gatewayChanged()
      await flush()
      expect(test.resolve).not.toHaveBeenCalled()
      test.context.authenticated = true
      test.gatewayChanged()
      await flush()
      expect(test.options.openSession).toHaveBeenCalledExactlyOnceWith(FIRST_KEY)
      expect(test.options.window.getPendingSessionDeepLink).toHaveBeenCalledOnce()
    } finally {
      cleanup()
      vi.useRealTimers()
    }
  })

  it('only retains the newest target while the Gateway is starting', async () => {
    const test = fixture(FIRST_KEY)
    test.context.epoch = null
    test.context.authenticated = false
    bindDesktopSessionDeepLinks(test.options)
    await flush()
    vi.mocked(test.options.window.getPendingSessionDeepLink!).mockResolvedValue(SECOND_KEY)
    test.notify(SECOND_KEY)
    await flush()
    test.context.epoch = 1
    test.context.authenticated = true
    test.gatewayChanged()
    await flush()
    expect(test.resolve).toHaveBeenCalledOnce()
    expect(test.options.openSession).toHaveBeenCalledExactlyOnceWith(SECOND_KEY)
  })

  it('invalidates a waiting link when its known Gateway endpoint changes', async () => {
    const test = fixture(FIRST_KEY)
    test.context.epoch = null
    test.context.authenticated = false
    bindDesktopSessionDeepLinks(test.options)
    await flush()
    test.context.endpoint = 'ws://another.example/ws'
    test.gatewayChanged()
    test.context.epoch = 1
    test.context.authenticated = true
    test.gatewayChanged()
    await flush()
    expect(test.resolve).not.toHaveBeenCalled()
    expect(test.options.unavailable).toHaveBeenCalledOnce()
  })

  it('aborts resolution immediately on a connection change and never replays its target', async () => {
    const test = fixture(FIRST_KEY)
    const response = deferred<ResolvedSession>()
    test.resolve.mockReturnValue(response.promise)
    bindDesktopSessionDeepLinks(test.options)
    await flush()
    test.context.epoch = null
    test.gatewayChanged()
    expect(test.resolve.mock.calls[0]![0].signal!.aborted).toBe(true)
    test.context.epoch = 2
    test.gatewayChanged()
    response.resolve({ key: FIRST_KEY, id: 'first-id' })
    await flush()
    expect(test.resolve).toHaveBeenCalledOnce()
    expect(test.options.openSession).not.toHaveBeenCalled()
    expect(test.options.unavailable).toHaveBeenCalledOnce()
  })

  it('disposes a target still waiting for startup without resolving it later', async () => {
    const test = fixture(FIRST_KEY)
    test.context.epoch = null
    test.context.authenticated = false
    const cleanup = bindDesktopSessionDeepLinks(test.options)
    await flush()
    cleanup()
    test.context.epoch = 1
    test.context.authenticated = true
    test.gatewayChanged()
    await flush()
    expect(test.resolve).not.toHaveBeenCalled()
    expect(test.options.unavailable).not.toHaveBeenCalled()
    expect(test.unsubscribeGateway).toHaveBeenCalledOnce()
  })

  it('cancels pending resolution and delivery listeners on unmount', async () => {
    const test = fixture(FIRST_KEY)
    const response = deferred<ResolvedSession>()
    test.resolve.mockReturnValue(response.promise)
    const cleanup = bindDesktopSessionDeepLinks(test.options)
    await flush()
    cleanup()
    response.resolve({ key: FIRST_KEY, id: 'first-id' })
    await flush()
    expect(test.unsubscribe).toHaveBeenCalledOnce()
    expect(test.unsubscribeGateway).toHaveBeenCalledOnce()
    expect(test.resolve.mock.calls[0]![0].signal!.aborted).toBe(true)
    expect(test.options.openSession).not.toHaveBeenCalled()
    expect(test.options.unavailable).not.toHaveBeenCalled()
  })
})
