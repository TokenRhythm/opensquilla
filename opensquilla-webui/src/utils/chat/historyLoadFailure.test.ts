import { describe, expect, it } from 'vitest'
import { SessionReadFailure, SessionReadHistoryCursorError } from '@/modules/sessionReadLifecycle'
import { classifyHistoryLoadFailure } from './historyLoadFailure'

describe('history failure presentation', () => {
  it.each([
    ['too-large', false, 'too-large', 'none'],
    ['timeout', true, 'timeout', 'retry'],
    ['busy', true, 'busy', 'retry'],
    ['unavailable', true, 'unavailable', 'retry'],
    ['unavailable', false, 'failed', 'none'],
  ] as const)('classifies %s (retryable=%s)', (kind, retryable, expectedKind, action) => {
    expect(classifyHistoryLoadFailure(new SessionReadFailure(kind, 'private diagnostic', retryable)))
      .toEqual({ kind: expectedKind, action })
  })

  it('reloads a rejected history cursor without exposing the rejected value', () => {
    expect(classifyHistoryLoadFailure(new SessionReadHistoryCursorError('stale', 'private cursor')))
      .toEqual({ kind: 'cursor', action: 'reload-latest' })
  })

  it('keeps legacy uncoded failures retryable without exposing raw diagnostics', () => {
    expect(classifyHistoryLoadFailure(new Error('private diagnostic')))
      .toEqual({ kind: 'unavailable', action: 'retry' })
  })
})
