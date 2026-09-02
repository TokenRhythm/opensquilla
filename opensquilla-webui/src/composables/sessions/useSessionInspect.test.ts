import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInspection } from '@/modules/sessionInspection'
import type { TurnCommands } from '@/modules/turnCommands'
import {
  SessionReadHistoryCursorError,
  type SessionReadHistoryPage,
  type SessionReadMessage,
} from '@/modules/sessionReadLifecycle'
import { abortInspectedSession, useSessionInspect } from './useSessionInspect'

function message(id: string, text = id): SessionReadMessage {
  return {
    id,
    messageId: id,
    transcriptId: `transcript:${id}`,
    role: 'assistant',
    text,
    createdAt: 1,
    reasoningContent: null,
    routerDecision: null,
    artifacts: [],
    toolCalls: [],
    timeline: [],
    attachments: [],
    promptAnnotations: [],
    provenance: { kind: null, sourceSessionKey: null, sourceTool: null },
    turnContext: null,
    usage: null,
    model: null,
    inputTokens: null,
    outputTokens: null,
    additional: {},
  }
}

function page(
  id: string,
  cursor: string,
  hasMore: boolean,
  patch: Partial<SessionReadHistoryPage> = {},
): SessionReadHistoryPage {
  return {
    messages: [message(id)],
    hasMore,
    oldestCursor: cursor,
    newestCursor: cursor,
    scope: 'complete',
    loadedCount: 1,
    pageSize: 20,
    canonicalAvailable: true,
    canonicalComplete: true,
    compactionSummaries: [],
    turnOutcomes: [],
    additional: {},
    ...patch,
  }
}

const preview = vi.fn<SessionInspection['preview']>()
const latest = vi.fn<SessionInspection['history']['latest']>()
const before = vi.fn<SessionInspection['history']['before']>()
const inspection: SessionInspection = {
  preview,
  history: { latest, before },
}

beforeEach(() => {
  preview.mockReset().mockResolvedValue(null)
  latest.mockReset().mockResolvedValue(page('m2', 'cursor-2', true))
  before.mockReset().mockResolvedValue(page('m1', 'cursor-1', false))
})

describe('useSessionInspect canonical pagination', () => {
  it('aborts through the turn command domain seam', async () => {
    const cancel = vi.fn<TurnCommands['cancel']>().mockResolvedValue({ aborted: true })

    await expect(abortInspectedSession({ cancel }, 'agent:main:webchat:test'))
      .resolves.toBe(true)
    expect(cancel).toHaveBeenCalledWith({
      sessionKey: 'agent:main:webchat:test',
      source: 'session-inspection',
    })
  })

  it('reads a canonical domain page without exposing wire parameters', async () => {
    const inspect = useSessionInspect(inspection)

    await inspect.load('agent:main:webchat:test')

    expect(latest).toHaveBeenCalledWith('agent:main:webchat:test', {
      limit: 20,
      signal: expect.any(AbortSignal),
    })
    expect(inspect.canonicalComplete.value).toBe(true)
    expect(inspect.canonicalAvailable.value).toBe(true)
  })

  it('deduplicates prepended rows and allows retrying a failed cursor', async () => {
    before
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(page('m1', 'cursor-1', false, {
        messages: [message('m1'), message('m2', 'm2 duplicate')],
      }))
    const inspect = useSessionInspect(inspection)

    await inspect.load('agent:main:webchat:test')
    await inspect.loadEarlier()
    expect(inspect.loadEarlierError.value).toBe(true)

    await inspect.loadEarlier()
    expect(inspect.loadEarlierError.value).toBe(false)
    expect(inspect.messages.value.map(row => row.messageId)).toEqual(['m1', 'm2'])
    expect(before).toHaveBeenCalledTimes(2)
  })

  it.each([
    'invalid',
    'stale',
  ] as const)('reloads latest instead of replaying a rejected %s cursor', async (reason) => {
    latest
      .mockResolvedValueOnce(page('m4', 'cursor-4', true))
      .mockResolvedValueOnce(page('m9', 'cursor-9', false))
    before.mockRejectedValueOnce(new SessionReadHistoryCursorError(
      reason,
      'cursor rejected',
    ))
    const inspect = useSessionInspect(inspection)

    await inspect.load('agent:main:webchat:test')
    await inspect.loadEarlier()

    expect(inspect.loadEarlierError.value).toBe(true)
    expect(inspect.messages.value.map(message => message.messageId)).toEqual(['m4'])

    await inspect.retryHistory()

    expect(before).toHaveBeenCalledWith(
      'agent:main:webchat:test',
      'cursor-4',
      expect.objectContaining({ limit: 20 }),
    )
    expect(latest).toHaveBeenCalledTimes(2)
    expect(inspect.messages.value.map(message => message.messageId)).toEqual(['m9'])
    expect(inspect.loadEarlierError.value).toBe(false)
  })

  it('does not advance an unavailable earlier page and retries the same cursor', async () => {
    latest.mockResolvedValueOnce(page('m4', 'cursor-4', true))
    before
      .mockResolvedValueOnce(page('fallback', 'fallback-cursor', false, {
        canonicalAvailable: false,
        canonicalComplete: false,
      }))
      .mockResolvedValueOnce(page('m3', 'cursor-3', false))
    const inspect = useSessionInspect(inspection)

    await inspect.load('agent:main:webchat:test')
    await inspect.loadEarlier()

    expect(inspect.messages.value.map(row => row.messageId)).toEqual(['m4'])
    expect(inspect.oldestCursor.value).toBe('cursor-4')
    expect(inspect.hasEarlier.value).toBe(true)
    expect(inspect.canonicalAvailable.value).toBe(false)

    await inspect.retryHistory()

    expect(inspect.messages.value.map(row => row.messageId)).toEqual(['m3', 'm4'])
    expect(before).toHaveBeenLastCalledWith(
      'agent:main:webchat:test',
      'cursor-4',
      expect.objectContaining({ limit: 20 }),
    )
  })

  it('marks a legacy transcript incomplete only when the domain says so', async () => {
    latest.mockResolvedValueOnce(page('m1', 'cursor-1', false, {
      canonicalComplete: false,
    }))
    const inspect = useSessionInspect(inspection)

    await inspect.load('agent:main:webchat:legacy')

    expect(inspect.canonicalComplete.value).toBe(false)
    expect(inspect.hasEarlier.value).toBe(false)
  })

  it('keeps unavailable latest rows but does not advance their cursor', async () => {
    latest
      .mockResolvedValueOnce(page('m1', 'fallback-cursor', true, {
        canonicalAvailable: false,
        canonicalComplete: false,
      }))
      .mockResolvedValueOnce(page('m1', 'cursor-1', false))
    const inspect = useSessionInspect(inspection)

    await inspect.load('agent:main:webchat:retry')

    expect(inspect.messages.value.map(row => row.messageId)).toEqual(['m1'])
    expect(inspect.oldestCursor.value).toBeNull()
    expect(inspect.hasEarlier.value).toBe(false)
    expect(inspect.canonicalAvailable.value).toBe(false)

    await inspect.retryHistory()
    expect(latest).toHaveBeenCalledTimes(2)
    expect(inspect.canonicalAvailable.value).toBe(true)
    expect(inspect.oldestCursor.value).toBe('cursor-1')
  })

  it('invokes the prepend hook immediately before applying the returned page', async () => {
    const inspect = useSessionInspect(inspection)
    let visibleBeforeApply: string[] = []

    await inspect.load('agent:main:webchat:test')
    await inspect.loadEarlier(() => {
      visibleBeforeApply = inspect.messages.value.map(row => row.messageId ?? row.id)
    })

    expect(visibleBeforeApply).toEqual(['m2'])
    expect(inspect.messages.value.map(row => row.messageId)).toEqual(['m1', 'm2'])
  })

  it('fences a stale earlier page when switching sessions', async () => {
    let resolveOldEarlier!: (value: SessionReadHistoryPage) => void
    const oldEarlier = new Promise<SessionReadHistoryPage>(resolve => {
      resolveOldEarlier = resolve
    })
    latest.mockImplementation(async key => (
      key.endsWith(':b')
        ? page('b2', 'cursor-b2', true)
        : page('a2', 'cursor-a2', true)
    ))
    before.mockImplementation(async key => (
      key.endsWith(':a') ? oldEarlier : page('b1', 'cursor-b1', false)
    ))
    const inspect = useSessionInspect(inspection)

    await inspect.load('agent:main:webchat:a')
    const staleLoad = inspect.loadEarlier()
    await vi.waitFor(() => expect(inspect.loadingEarlier.value).toBe(true))

    await inspect.load('agent:main:webchat:b')
    expect(inspect.loadingEarlier.value).toBe(false)
    resolveOldEarlier(page('a1', 'cursor-a1', false))
    await staleLoad

    await inspect.loadEarlier()
    expect(before).toHaveBeenLastCalledWith(
      'agent:main:webchat:b',
      'cursor-b2',
      expect.any(Object),
    )
  })

  it('aborts pending inspection reads on reset', async () => {
    let observedSignal: AbortSignal | null = null
    latest.mockImplementation(async (_key, options) => {
      observedSignal = options?.signal ?? null
      await new Promise<void>(() => {})
      return page('never', 'never', false)
    })
    const inspect = useSessionInspect(inspection)

    void inspect.load('agent:main:webchat:pending')
    await vi.waitFor(() => expect(observedSignal).not.toBeNull())
    inspect.reset()

    expect((observedSignal as unknown as AbortSignal).aborted).toBe(true)
    expect(inspect.loading.value).toBe(false)
  })

  it('keeps a preview failure separate from transcript state', async () => {
    preview.mockRejectedValueOnce(new Error('preview unavailable'))
    const inspect = useSessionInspect(inspection)

    await inspect.load('agent:main:webchat:test')

    expect(inspect.preview.value).toBeNull()
    expect(inspect.transcriptError.value).toBe(false)
    expect(inspect.messages.value.map(row => row.messageId)).toEqual(['m2'])
  })
})
