import { nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import type { RpcCallOptions, RpcConnectionWaitOptions } from '@/lib/rpc'
import type { ChatMessage } from '@/types/chat'
import type { ArtifactsListResponse, ArtifactPayload } from '@/types/rpc'
import { useSessionArtifacts } from './useSessionArtifacts'

type RpcCall = <T = unknown>(
  method: string,
  params?: Record<string, unknown>,
  callOptions?: RpcCallOptions,
) => Promise<T>

function assistantWithArtifacts(artifacts: ArtifactPayload[]): ChatMessage {
  return { role: 'assistant', text: 'done', ts: null, artifacts }
}

function makeHarness(options: {
  supported?: boolean
  call?: (
    method: string,
    params?: Record<string, unknown>,
    callOptions?: RpcCallOptions,
  ) => Promise<unknown>
  waitForConnection?: (
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: RpcConnectionWaitOptions,
  ) => Promise<void>
  messages?: ChatMessage[]
  streamArtifacts?: ArtifactPayload[]
} = {}) {
  const sessionKey = ref('agent:main:webchat:one')
  const messages = ref<ChatMessage[]>(options.messages || [])
  const streamArtifacts = ref<ArtifactPayload[]>(options.streamArtifacts || [])
  const callMock = vi.fn(options.call || (async () => ({ artifacts: [], has_more: false })))
  const rpc = {
    waitForConnection: vi.fn(options.waitForConnection || (async () => {})),
    supportsMethod: vi.fn(() => options.supported ?? true),
    markMethodUnavailable: vi.fn(),
    call: callMock as unknown as RpcCall,
  }
  const api = useSessionArtifacts({ rpc, sessionKey, messages, streamArtifacts })
  return { api, callMock, messages, rpc, sessionKey, streamArtifacts }
}

describe('useSessionArtifacts', () => {
  it('promotes a live publication event into the durable index before stream reset', async () => {
    const published = {
      id: 'art-published',
      name: 'published.html',
      mime: 'text/html',
      sha256: 'b'.repeat(64),
    }
    const { api, callMock, streamArtifacts } = makeHarness({
      call: async () => ({ artifacts: [published], has_more: false }),
    })

    streamArtifacts.value = [published]
    await nextTick()
    await vi.waitFor(() => expect(callMock).toHaveBeenCalledOnce())
    await vi.waitFor(() => expect(api.indexedArtifacts.value).toEqual([published]))

    // Starting the next turn clears the transient stream. The published
    // deliverable must remain discoverable through the durable index.
    streamArtifacts.value = []
    await nextTick()
    expect(api.artifacts.value).toEqual([published])
  })

  it('loads every index page and merges index, history, and live fields by identity', async () => {
    const call = async (_method: string, params?: Record<string, unknown>) => {
      if (params?.before === 'cursor-2') {
        return {
          artifacts: [{
            id: 'art-1',
            name: 'first.txt',
            created_at: '2026-08-01T00:00:00Z',
          }],
          has_more: false,
          oldest_cursor: 'cursor-1',
        } satisfies ArtifactsListResponse
      }
      return {
        artifacts: [
          {
            id: 'art-2',
            name: 'indexed-name.txt',
            mime: 'text/plain',
            sha256: 'a'.repeat(64),
          },
          { id: 'art-3', name: 'third.csv', mime: 'text/csv' },
        ],
        hasMore: true,
        oldestCursor: 'cursor-2',
      } satisfies ArtifactsListResponse
    }
    const { api, callMock, rpc } = makeHarness({
      call,
      messages: [assistantWithArtifacts([
        {
          id: 'art-2',
          name: 'history-name.txt',
          size: 42,
          thumbnail_url: '/api/v1/artifacts/art-2?variant=thumb',
        },
        { id: 'art-history', name: 'history-only.pdf' },
      ])],
      streamArtifacts: [
        { id: 'art-2', name: 'live-name.txt', mime: undefined, stream_seq: 9 },
        { id: 'art-live', name: 'live-only.png' },
      ],
    })

    await expect(api.load()).resolves.toBe(true)

    expect(callMock).toHaveBeenNthCalledWith(
      1,
      'artifacts.list',
      {
        sessionKey: 'agent:main:webchat:one',
        limit: 200,
      },
      expect.objectContaining({
        timeoutMs: 10_000,
        timeoutAction: 'reconnect',
        abortAction: 'reconnect',
        signal: expect.any(AbortSignal),
      }),
    )
    expect(callMock).toHaveBeenNthCalledWith(
      2,
      'artifacts.list',
      {
        sessionKey: 'agent:main:webchat:one',
        limit: 200,
        before: 'cursor-2',
      },
      expect.objectContaining({
        timeoutMs: 10_000,
        timeoutAction: 'reconnect',
        abortAction: 'reconnect',
        signal: expect.any(AbortSignal),
      }),
    )
    expect(rpc.waitForConnection).toHaveBeenCalledWith(
      10_000,
      expect.any(AbortSignal),
      { timeoutAction: 'reconnect', abortAction: 'reconnect' },
    )
    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual([
      'art-1',
      'art-2',
      'art-3',
      'art-history',
      'art-live',
    ])
    expect(api.artifacts.value[1]).toMatchObject({
      id: 'art-2',
      name: 'live-name.txt',
      mime: 'text/plain',
      sha256: 'a'.repeat(64),
      size: 42,
      thumbnail_url: '/api/v1/artifacts/art-2?variant=thumb',
      stream_seq: 9,
    })
    expect(api.indexAvailable.value).toBe(true)
  })

  it('waits for Hello capability and preserves history/live fallback when unsupported', async () => {
    const { api, callMock, rpc } = makeHarness({
      supported: false,
      messages: [assistantWithArtifacts([{ id: 'art-history', name: 'old.txt' }])],
      streamArtifacts: [{ id: 'art-live', name: 'new.txt' }],
    })

    await expect(api.load()).resolves.toBe(false)

    expect(rpc.waitForConnection).toHaveBeenCalledOnce()
    expect(rpc.supportsMethod).toHaveBeenCalledWith('artifacts.list')
    expect(callMock).not.toHaveBeenCalled()
    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual([
      'art-history',
      'art-live',
    ])
  })

  it('disables a missing method for the connection and falls back without throwing', async () => {
    const missing = Object.assign(new Error('Method not found: artifacts.list'), {
      code: 'METHOD_NOT_FOUND',
    })
    const { api, rpc } = makeHarness({
      call: async () => { throw missing },
      messages: [assistantWithArtifacts([{ id: 'art-history', name: 'old.txt' }])],
    })

    await expect(api.load()).resolves.toBe(false)

    expect(rpc.markMethodUnavailable).toHaveBeenCalledWith('artifacts.list')
    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual(['art-history'])
    expect(api.loading.value).toBe(false)
  })

  it('keeps a previous same-session index when a refresh becomes unsupported', async () => {
    const { api, callMock, rpc } = makeHarness({
      call: async () => ({
        artifacts: [{ id: 'art-index', name: 'saved.txt' }],
        has_more: false,
      }),
    })

    await expect(api.load()).resolves.toBe(true)
    rpc.supportsMethod.mockReturnValue(false)
    await expect(api.load()).resolves.toBe(false)

    expect(callMock).toHaveBeenCalledOnce()
    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual(['art-index'])
    expect(api.indexAvailable.value).toBe(false)
  })

  it('keeps a previous same-session index when refresh reports METHOD_NOT_FOUND', async () => {
    let missing = false
    const missingError = Object.assign(new Error('Method not found: artifacts.list'), {
      code: 'METHOD_NOT_FOUND',
    })
    const { api, rpc } = makeHarness({
      call: async () => {
        if (missing) throw missingError
        return {
          artifacts: [{ id: 'art-index', name: 'saved.txt' }],
          has_more: false,
        }
      },
    })

    await expect(api.load()).resolves.toBe(true)
    missing = true
    await expect(api.load()).resolves.toBe(false)

    expect(rpc.markMethodUnavailable).toHaveBeenCalledWith('artifacts.list')
    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual(['art-index'])
    expect(api.indexAvailable.value).toBe(false)
  })

  it('keeps a previous same-session index when a refresh fails transiently', async () => {
    let fail = false
    const { api } = makeHarness({
      call: async () => {
        if (fail) throw new Error('temporary disconnect')
        return { artifacts: [{ id: 'art-index', name: 'saved.txt' }], has_more: false }
      },
    })

    await expect(api.load()).resolves.toBe(true)
    fail = true
    await expect(api.load()).resolves.toBe(false)

    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual(['art-index'])
    expect(api.indexAvailable.value).toBe(false)
  })

  it('keeps a previous same-session index when a bounded page request times out', async () => {
    let timeout = false
    const { api, callMock } = makeHarness({
      call: async () => {
        if (timeout) throw Object.assign(new Error('artifacts.list timed out'), {
          code: 'RPC_TIMEOUT',
        })
        return { artifacts: [{ id: 'art-index', name: 'saved.txt' }], has_more: false }
      },
    })

    await expect(api.load()).resolves.toBe(true)
    timeout = true
    await expect(api.load()).resolves.toBe(false)

    expect(callMock.mock.calls[1]?.[2]).toMatchObject({
      timeoutMs: 10_000,
      timeoutAction: 'reconnect',
      abortAction: 'reconnect',
    })
    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual(['art-index'])
    expect(api.indexAvailable.value).toBe(false)
  })

  it('suppresses only the reconnect caused by a timed-out page request', async () => {
    let pageCalls = 0
    const { api, callMock } = makeHarness({
      call: async () => {
        pageCalls += 1
        if (pageCalls === 1) {
          throw Object.assign(new Error('artifacts.list timed out'), {
            code: 'RPC_TIMEOUT',
          })
        }
        return {
          artifacts: [{ id: 'art-recovered', name: 'recovered.txt' }],
          has_more: false,
        }
      },
    })

    await expect(api.load()).resolves.toBe(false)
    await expect(api.loadAfterReconnect()).resolves.toBe(false)
    expect(callMock).toHaveBeenCalledOnce()

    await expect(api.loadAfterReconnect()).resolves.toBe(true)
    expect(callMock).toHaveBeenCalledTimes(2)
    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual(['art-recovered'])
  })

  it('clears page-timeout reconnect suppression when the Session resets', async () => {
    let firstCall = true
    const { api, callMock, sessionKey } = makeHarness({
      call: async () => {
        if (firstCall) {
          firstCall = false
          throw Object.assign(new Error('artifacts.list timed out'), {
            code: 'RPC_TIMEOUT',
          })
        }
        return {
          artifacts: [{ id: 'art-two', name: 'two.txt' }],
          has_more: false,
        }
      },
    })

    await expect(api.load()).resolves.toBe(false)
    sessionKey.value = 'agent:main:webchat:two'
    api.reset()

    await expect(api.loadAfterReconnect()).resolves.toBe(true)
    expect(callMock).toHaveBeenCalledTimes(2)
    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual(['art-two'])
  })

  it('does not suppress reconnect loading after a connection-wait timeout', async () => {
    let waitCalls = 0
    const { api, callMock } = makeHarness({
      waitForConnection: async () => {
        waitCalls += 1
        if (waitCalls === 1) {
          throw Object.assign(new Error('waitForConnection timed out'), {
            code: 'RPC_TIMEOUT',
          })
        }
      },
      call: async () => ({
        artifacts: [{ id: 'art-connected', name: 'connected.txt' }],
        has_more: false,
      }),
    })

    await expect(api.load()).resolves.toBe(false)
    await expect(api.loadAfterReconnect()).resolves.toBe(true)

    expect(callMock).toHaveBeenCalledOnce()
    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual(['art-connected'])
  })

  it('aborts a superseded same-session page walk without losing the newer result', async () => {
    let resolveFirst!: (response: ArtifactsListResponse) => void
    const first = new Promise<ArtifactsListResponse>(resolve => { resolveFirst = resolve })
    let callCount = 0
    let firstSignal: AbortSignal | undefined
    const { api, callMock } = makeHarness({
      call: async (_method, _params, callOptions) => {
        callCount += 1
        if (callCount === 1) {
          firstSignal = callOptions?.signal
          return first
        }
        return {
          artifacts: [{ id: 'art-new', name: 'new.txt' }],
          has_more: false,
        }
      },
    })

    const staleLoad = api.load()
    await vi.waitFor(() => expect(callMock).toHaveBeenCalledOnce())
    const currentLoad = api.load()
    await vi.waitFor(() => expect(callMock).toHaveBeenCalledTimes(2))

    expect(firstSignal?.aborted).toBe(true)
    await expect(currentLoad).resolves.toBe(true)
    resolveFirst({ artifacts: [{ id: 'art-old', name: 'old.txt' }], has_more: false })
    await expect(staleLoad).resolves.toBe(false)
    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual(['art-new'])
  })

  it('aborts pending connection waits on reset and cleanup', async () => {
    const waitingSignals: AbortSignal[] = []
    const { api } = makeHarness({
      waitForConnection: async (_timeoutMs, signal) => {
        if (!signal) throw new Error('missing abort signal')
        waitingSignals.push(signal)
        await new Promise<void>((_resolve, reject) => {
          signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })
        })
      },
    })

    const resetLoad = api.load()
    await vi.waitFor(() => expect(waitingSignals).toHaveLength(1))
    api.reset()
    expect(waitingSignals[0]?.aborted).toBe(true)
    await expect(resetLoad).resolves.toBe(false)

    const cleanupLoad = api.load()
    await vi.waitFor(() => expect(waitingSignals).toHaveLength(2))
    api.cleanup()
    expect(waitingSignals[1]?.aborted).toBe(true)
    await expect(cleanupLoad).resolves.toBe(false)
  })

  it('clears the previous index immediately when the Session changes', async () => {
    let resolveSecond!: (response: ArtifactsListResponse) => void
    const second = new Promise<ArtifactsListResponse>(resolve => { resolveSecond = resolve })
    const { api, callMock, sessionKey } = makeHarness({
      call: async (_method, params) => {
        if (params?.sessionKey === 'agent:main:webchat:one') {
          return {
            artifacts: [{ id: 'art-one', name: 'one.txt' }],
            has_more: false,
          }
        }
        return second
      },
    })

    await expect(api.load()).resolves.toBe(true)
    sessionKey.value = 'agent:main:webchat:two'
    const secondLoad = api.load()
    await vi.waitFor(() => expect(callMock).toHaveBeenCalledTimes(2))

    expect(api.indexedArtifacts.value).toEqual([])
    resolveSecond({ artifacts: [{ id: 'art-two', name: 'two.txt' }], has_more: false })
    await expect(secondLoad).resolves.toBe(true)
  })

  it('ignores a late response from the previous session', async () => {
    let resolveFirst!: (response: ArtifactsListResponse) => void
    let resolveSecond!: (response: ArtifactsListResponse) => void
    const first = new Promise<ArtifactsListResponse>(resolve => { resolveFirst = resolve })
    const second = new Promise<ArtifactsListResponse>(resolve => { resolveSecond = resolve })
    const requestSignals = new Map<string, AbortSignal | undefined>()
    const { api, callMock, sessionKey } = makeHarness({
      call: async (_method, params, callOptions) => {
        requestSignals.set(String(params?.sessionKey), callOptions?.signal)
        return params?.sessionKey === 'agent:main:webchat:one' ? first : second
      },
    })

    const firstLoad = api.load()
    await vi.waitFor(() => expect(callMock).toHaveBeenCalledTimes(1))

    sessionKey.value = 'agent:main:webchat:two'
    const secondLoad = api.load()
    await vi.waitFor(() => expect(callMock).toHaveBeenCalledTimes(2))
    expect(requestSignals.get('agent:main:webchat:one')?.aborted).toBe(true)
    resolveSecond({ artifacts: [{ id: 'art-two', name: 'two.txt' }], has_more: false })
    await expect(secondLoad).resolves.toBe(true)

    resolveFirst({ artifacts: [{ id: 'art-one', name: 'one.txt' }], has_more: false })
    await expect(firstLoad).resolves.toBe(false)
    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual(['art-two'])
  })

  it('keeps a live artifact received while the durable index is loading', async () => {
    let resolveList!: (response: ArtifactsListResponse) => void
    const pendingList = new Promise<ArtifactsListResponse>(resolve => { resolveList = resolve })
    const { api, callMock, streamArtifacts } = makeHarness({
      call: async () => pendingList,
    })

    const load = api.load()
    await vi.waitFor(() => expect(callMock).toHaveBeenCalledOnce())
    streamArtifacts.value = [{ id: 'art-live', name: 'live.png', mime: 'image/png' }]

    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual(['art-live'])

    resolveList({
      artifacts: [{ id: 'art-old', name: 'old.pdf', mime: 'application/pdf' }],
      has_more: false,
    })
    await expect(load).resolves.toBe(true)
    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual(['art-old', 'art-live'])
  })

  it('fails closed to fallback when a pagination cursor stalls', async () => {
    const { api, callMock } = makeHarness({
      call: async () => ({
        artifacts: [{ id: 'art-partial', name: 'partial.txt' }],
        has_more: true,
        oldest_cursor: 'same-cursor',
      }),
      messages: [assistantWithArtifacts([{ id: 'art-history', name: 'history.txt' }])],
    })

    await expect(api.load()).resolves.toBe(false)

    expect(callMock).toHaveBeenCalledTimes(2)
    expect(api.indexedArtifacts.value).toEqual([])
    expect(api.artifacts.value.map(artifact => artifact.id)).toEqual(['art-history'])
  })
})
