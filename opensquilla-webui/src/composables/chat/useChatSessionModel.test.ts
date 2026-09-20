import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'
import type { ResolvedSession } from '@/modules/sessionDirectory'
import { useChatSessionModel } from './useChatSessionModel'

function deferred() {
  let resolve!: (value: ResolvedSession) => void
  let reject!: (error: Error) => void
  const promise = new Promise<ResolvedSession>((accept, fail) => {
    resolve = accept
    reject = fail
  })
  return { promise, resolve, reject }
}

const scopes: ReturnType<typeof effectScope>[] = []
afterEach(() => { for (const scope of scopes.splice(0)) scope.stop() })

function harness(options: { draft?: boolean; available?: boolean; key?: string } = {}) {
  const sessionKey = ref(options.key ?? 'agent:main:webchat:one')
  const draft = ref(options.draft ?? false)
  const available = ref(options.available ?? true)
  const connectionEpoch = ref(1)
  const requests: Array<ReturnType<typeof deferred> & { key: string; signal?: AbortSignal }> = []
  const resolve = vi.fn((request: { key: string; signal?: AbortSignal }) => {
    const pending = deferred()
    requests.push({ ...pending, ...request })
    return pending.promise
  })
  const scope = effectScope()
  scopes.push(scope)
  const api = scope.run(() => useChatSessionModel({
    directory: { resolve }, sessionKey, isDraft: () => draft.value, available, connectionEpoch,
  }))!
  return { api, sessionKey, draft, available, connectionEpoch, resolve, requests, scope }
}

function stored(model: string | null = 'Exact-Model-ID', key = 'agent:main:webchat:one'): ResolvedSession {
  return { key, id: key, model }
}

describe('stored chat session model display', () => {
  it.each([
    { draft: true }, { available: false }, { key: '' },
  ])('does not resolve an unavailable or provisional session: %o', async (options) => {
    const h = harness(options)
    await h.api.refresh()
    expect(h.resolve).not.toHaveBeenCalled()
    expect(h.api.modelName.value).toBeNull()
  })

  it('loads the exact stored model when a draft becomes durable with the same session key', async () => {
    const h = harness({ draft: true })
    h.draft.value = false
    expect(h.resolve).toHaveBeenCalledExactlyOnceWith({
      key: h.sessionKey.value, signal: expect.any(AbortSignal),
    })
    h.requests[0]!.resolve(stored())
    await nextTick()
    expect(h.api.modelName.value).toBe('Exact-Model-ID')
  })

  it.each([null, undefined, '', '  '])('keeps an unknown stored model generic: %s', async (model) => {
    const h = harness()
    h.requests[0]!.resolve({ ...stored(), model })
    await nextTick()
    expect(h.api.modelName.value).toBeNull()
  })

  it('rejects stale replies even after switching away and back to the same session', async () => {
    const h = harness()
    h.sessionKey.value = 'agent:main:webchat:two'
    h.requests[1]!.resolve(stored('Second-Model', h.sessionKey.value))
    await nextTick()
    expect(h.api.modelName.value).toBe('Second-Model')

    h.sessionKey.value = 'agent:main:webchat:one'
    expect(h.api.modelName.value).toBeNull()
    expect(h.requests[0]!.signal?.aborted).toBe(true)
    h.requests[0]!.resolve(stored('Stale-First-Model'))
    await nextTick()
    expect(h.api.modelName.value).toBeNull()
    h.requests[2]!.resolve(stored('Current-First-Model'))
    await nextTick()
    expect(h.api.modelName.value).toBe('Current-First-Model')
  })

  it('clears the stored model immediately when returning to a provisional draft', async () => {
    const h = harness()
    h.requests[0]!.resolve(stored())
    await nextTick()
    h.draft.value = true
    expect(h.api.modelName.value).toBeNull()
    expect(h.resolve).toHaveBeenCalledTimes(1)
  })

  it('isolates reconnections to the same key and never displays the previous gateway model', async () => {
    const h = harness()
    h.requests[0]!.resolve(stored('Gateway-A-Model'))
    await nextTick()
    expect(h.api.modelName.value).toBe('Gateway-A-Model')
    h.connectionEpoch.value += 1
    expect(h.api.modelName.value).toBeNull()
    h.connectionEpoch.value += 1
    expect(h.requests[1]!.signal?.aborted).toBe(true)
    h.requests[1]!.resolve(stored('Stale-Gateway-B-Model'))
    await nextTick()
    expect(h.api.modelName.value).toBeNull()
    h.requests[2]!.resolve(stored('Gateway-C-Model'))
    await nextTick()
    expect(h.api.modelName.value).toBe('Gateway-C-Model')
  })

  it('aborts on disconnect and reloads on reconnect without accepting late results', async () => {
    const h = harness()
    h.available.value = false
    expect(h.requests[0]!.signal?.aborted).toBe(true)
    h.requests[0]!.resolve(stored('Disconnected-Model'))
    await nextTick()
    expect(h.api.modelName.value).toBeNull()
    h.available.value = true
    h.requests[1]!.resolve(stored('Reconnected-Model'))
    await nextTick()
    expect(h.api.modelName.value).toBe('Reconnected-Model')
  })

  it('falls back to no model after a failed refresh and allows a later retry', async () => {
    const h = harness()
    h.requests[0]!.resolve(stored())
    await nextTick()
    const failed = h.api.refresh()
    h.requests[1]!.reject(new Error('gateway unavailable'))
    await failed
    expect(h.api.modelName.value).toBeNull()
    const retry = h.api.refresh()
    h.requests[2]!.resolve(stored('Recovered-Model'))
    await retry
    expect(h.api.modelName.value).toBe('Recovered-Model')
  })

  it('keeps the current known model visible while refreshing the same session', async () => {
    const h = harness()
    h.requests[0]!.resolve(stored('Known-Model'))
    await nextTick()
    const refresh = h.api.refresh()
    expect(h.api.modelName.value).toBe('Known-Model')
    h.requests[1]!.resolve(stored('Updated-Model'))
    await refresh
    expect(h.api.modelName.value).toBe('Updated-Model')
  })

  it('keeps the latest refresh when a cancelled request still resolves', async () => {
    const h = harness()
    const refresh = h.api.refresh()
    h.requests[1]!.resolve(stored('Latest-Model'))
    await refresh
    h.requests[0]!.resolve(stored('Stale-Model'))
    await nextTick()
    expect(h.api.modelName.value).toBe('Latest-Model')
  })

  it('does not clear the current model when an older cancelled read fails', async () => {
    const h = harness()
    const refresh = h.api.refresh()
    h.requests[1]!.resolve(stored('Latest-Model'))
    await refresh
    h.requests[0]!.reject(new Error('cancelled old request'))
    await nextTick()
    expect(h.api.modelName.value).toBe('Latest-Model')
  })

  it('aborts in-flight reads and prevents updates or new reads after disposal', async () => {
    const h = harness()
    h.scope.stop()
    expect(h.requests[0]!.signal?.aborted).toBe(true)
    h.requests[0]!.resolve(stored('Disposed-Model'))
    await nextTick()
    await h.api.refresh()
    expect(h.api.modelName.value).toBeNull()
    expect(h.resolve).toHaveBeenCalledTimes(1)
  })
})
