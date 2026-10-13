import { describe, expect, it, vi } from 'vitest'
import { SessionReadFailure } from './sessionReadLifecycle'
import {
  createConversationEventHub,
  type ConversationEventSourceHandlers,
  type ConversationRecoveryScope,
} from './conversationEventHub'

type Message = { key?: string, value: number }

function sourceHarness() {
  let active: ConversationEventSourceHandlers<Message> | null = null
  let subscriptions = 0
  let detachments = 0
  const source = {
    subscribe(handlers: ConversationEventSourceHandlers<Message>) {
      subscriptions += 1
      active = handlers
      return () => {
        detachments += 1
        active = null
      }
    },
    emit(message: Message) {
      return active?.onEvent?.(message)
    },
    state(value: string) {
      active?.onConnectionState?.(value)
    },
    recover(scope: ConversationRecoveryScope) {
      return active?.onRecoveryRequired?.(scope)
    },
    decodeError(error: unknown) {
      active?.onDecodeError?.(error)
    },
    counts() {
      return { subscriptions, detachments }
    },
  }
  return source
}

describe('conversation event hub', () => {
  it('keeps terminal A local while global recovery retries B, and reopens A for a new admission', async () => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source)
    const terminal = new SessionReadFailure('too-large', 'terminal', false)
    const alpha = vi.fn(async (): Promise<boolean> => { throw terminal })
    const beta = vi.fn(async () => true).mockResolvedValueOnce(false)
    hub.prepareReadRetirement('alpha')
    hub.prepareReadRetirement('beta')
    hub.observeRecoveryRequired(scope => scope.keys[0] === 'alpha' ? alpha() : beta())
    const global = { keys: [], global: true }
    await expect(source.recover(global)).resolves.toBe(false)
    await expect(source.recover(global)).resolves.toEqual({ retryable: false })
    expect(alpha).toHaveBeenCalledOnce()
    expect(beta).toHaveBeenCalledTimes(2)
    hub.prepareReadRetirement('alpha')
    alpha.mockImplementation(async () => true)
    await expect(source.recover({ keys: ['alpha'], global: false })).resolves.toBe(true)
    expect(alpha).toHaveBeenCalledTimes(2)
    hub.dispose()
  })

  it.each(['admission', 'connection'] as const)('ignores terminal recovery from an obsolete %s', async replacement => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source)
    hub.prepareReadRetirement('alpha')
    let fail!: (reason: unknown) => void
    const pending = new Promise<boolean>((_, reject) => { fail = reject })
    const recover = vi.fn(async () => true).mockImplementationOnce(() => pending)
    hub.observeRecoveryRequired(recover)
    const old = source.recover({ keys: ['alpha'], global: false })
    await Promise.resolve()
    if (replacement === 'admission') hub.prepareReadRetirement('alpha')
    else source.state('disconnected')
    fail(new SessionReadFailure('budget-exhausted', 'old owner', false))
    await expect(old).resolves.toBe(false)
    await expect(source.recover({ keys: ['alpha'], global: false })).resolves.toBe(true)
    expect(recover).toHaveBeenCalledTimes(2)
    hub.dispose()
  })

  it('requires explicit consumer ownership and awaits its applied-or-dirty result', async () => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source)
    const handle = hub.open('')
    const off = handle.observe(() => {})
    expect(source.emit({ value: 1 })).toBeUndefined()
    off()
    let finish!: (value: 'applied' | 'dirty') => void
    handle.observe(() => new Promise<'applied' | 'dirty'>(resolve => { finish = resolve }))
    const pending = source.emit({ value: 2 })
    const completed = vi.fn()
    void Promise.resolve(pending).then(completed)
    await Promise.resolve()
    expect(completed).not.toHaveBeenCalled()
    finish('applied')
    await expect(pending).resolves.toBe('applied')
    hub.dispose()
  })

  it('multiplexes logical handles over one source and fences keyed events', () => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source, {
      sessionKey: message => message.key,
    })
    const alpha = hub.open('alpha')
    const beta = hub.open('beta')
    const alphaEvents: Message[] = []
    const betaEvents: Message[] = []
    alpha.observe(message => alphaEvents.push(message))
    beta.observe(message => betaEvents.push(message))

    source.emit({ key: 'alpha', value: 1 })
    source.emit({ key: 'beta', value: 2 })
    source.emit({ value: 3 })

    expect(alphaEvents.map(item => item.value)).toEqual([1, 3])
    expect(betaEvents.map(item => item.value)).toEqual([2, 3])
    expect(source.counts()).toEqual({ subscriptions: 1, detachments: 0 })
  })

  it('keeps the physical source alive until the last logical owner closes', () => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source)
    const first = hub.open('')
    const second = hub.open('')
    const firstListener = vi.fn()
    const secondListener = vi.fn()
    first.observe(firstListener)
    second.observe(secondListener)

    first.close()
    source.emit({ value: 1 })
    expect(firstListener).not.toHaveBeenCalled()
    expect(secondListener).toHaveBeenCalledWith({ value: 1 }, expect.objectContaining({ commit: expect.any(Function) }))
    expect(source.counts()).toEqual({ subscriptions: 1, detachments: 0 })

    second.close()
    expect(source.counts()).toEqual({ subscriptions: 1, detachments: 1 })
    second.close()
    expect(source.counts()).toEqual({ subscriptions: 1, detachments: 1 })
  })

  it('forwards diagnostics and supports idempotent observer removal', () => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source)
    const state = vi.fn()
    const decodeError = vi.fn()
    const offState = hub.observeConnectionState(state)
    const offDecodeError = hub.observeDecodeError(decodeError)

    source.state('connected')
    expect(state).toHaveBeenCalledWith('connected')
    const error = new Error('malformed event')
    source.decodeError(error)
    expect(decodeError).toHaveBeenCalledWith(error)
    offState()
    offState()
    offDecodeError()
    offDecodeError()
    expect(source.counts()).toEqual({ subscriptions: 1, detachments: 1 })
  })

  it('dispose closes handles and prevents reconnection', () => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source)
    const handle = hub.open('')
    const listener = vi.fn()
    handle.observe(listener)
    hub.dispose()
    source.emit({ value: 4 })
    expect(listener).not.toHaveBeenCalled()
    expect(source.counts()).toEqual({ subscriptions: 1, detachments: 1 })
    expect(handle.observe(listener)).not.toThrow()
  })

  it.each(['reconcile', 'close', 'detach'] as const)('fences a delayed write after %s on the same source', async boundary => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source)
    const handle = hub.open('alpha')
    let finish!: () => void
    const write = vi.fn()
    const off = handle.observe(async (_event, context) => {
      await new Promise<void>(resolve => { finish = resolve })
      expect(context!.commit(write)).toBe(false)
      return 'applied'
    })
    const pending = Promise.resolve(source.emit({ key: 'alpha', value: 1 }))
    const rejection = expect(pending).rejects.toThrow('superseded')
    if (boundary === 'reconcile') hub.invalidateConsumption()
    if (boundary === 'close') handle.close()
    if (boundary === 'detach') off()
    finish()
    await rejection
    expect(write).not.toHaveBeenCalled()
    hub.dispose()
  })

  it('requires each active read owner for global recovery and never uses A to prove B', async () => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source, { sessionKey: event => event.key })
    hub.prepareReadRetirement('a')
    hub.prepareReadRetirement('b')
    const alpha = vi.fn(async (scope: ConversationRecoveryScope) => scope.keys.length === 1 && scope.keys[0] === 'a')
    hub.observeRecoveryRequired(alpha)
    expect(source.emit({ key: 'b', value: 1 })).toBeUndefined()
    await expect(source.recover({ keys: [], global: true })).resolves.toBe(false)
    expect(alpha).toHaveBeenCalledWith({ keys: ['b'], global: false })
    hub.observeRecoveryRequired(async scope => scope.keys.length === 1 && scope.keys[0] === 'b')
    await expect(source.recover({ keys: [], global: true })).resolves.toBe(true)
    await expect(source.recover({ keys: ['unknown'], global: false })).resolves.toBe(false)
    hub.dispose()
  })

  it('does not supersede an A consumer or invoke A recovery for an unowned B scope', async () => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source, { sessionKey: event => event.key })
    hub.prepareReadRetirement('a')
    const recovery = vi.fn(async () => true)
    hub.observeRecoveryRequired(recovery)
    const write = vi.fn()
    let finish!: () => void
    hub.open('a').observe(async (_event, context) => {
      await new Promise<void>(resolve => { finish = resolve })
      expect(context!.commit(write)).toBe(true)
      return 'applied'
    })
    const pending = source.emit({ key: 'a', value: 1 })
    await expect(source.recover({ keys: ['unknown-b'], global: false })).resolves.toBe(false)
    expect(recovery).not.toHaveBeenCalled()
    finish()
    await expect(pending).resolves.toBe('applied')
    expect(write).toHaveBeenCalledOnce()
    hub.dispose()
  })

  it('retires only released reads, clears retirement on new admission/connection, and bounds identities', async () => {
    const source = sourceHarness()
    const hub = createConversationEventHub(source, { sessionKey: event => event.key, invalidatesSession: event => event.value === -1 })
    hub.observeConnectionState(() => {})
    const retire = hub.prepareReadRetirement('b')
    expect(source.emit({ key: 'b', value: 1 })).toBeUndefined()
    retire()
    expect(source.emit({ key: 'b', value: 2 })).toBe('applied')
    await expect(source.recover({ keys: ['b'], global: false })).resolves.toBe(true)
    const retireNew = hub.prepareReadRetirement('b')
    retire() // The former admission cannot retire a replacement lease.
    expect(source.emit({ key: 'b', value: 3 })).toBeUndefined()
    retireNew()
    expect(source.emit({ key: 'b', value: -1 })).toBeUndefined()
    hub.prepareReadRetirement('failed')(false)
    expect(source.emit({ key: 'failed', value: 4 })).toBeUndefined()
    for (let index = 0; index < 129; index++) hub.prepareReadRetirement(`retired-${index}`)()
    expect(source.emit({ key: 'retired-0', value: 1 })).toBeUndefined()
    expect(source.emit({ key: 'retired-128', value: 1 })).toBe('applied')
    source.state('reconnecting')
    expect(source.emit({ key: 'retired-128', value: 1 })).toBeUndefined()
    await expect(source.recover({ keys: [], global: true })).resolves.toBe(false)
    hub.dispose()
  })
})
