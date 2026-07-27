import { describe, expect, it } from 'vitest'
import {
  resolveChatSessionLoadState,
  shouldShowHistorySentinelError,
  shouldShowHistorySentinelLoading,
  type ResolveChatSessionLoadStateOptions,
} from './sessionLoadState'

const settledEmpty: ResolveChatSessionLoadStateOptions = {
  isDraftLanding: false,
  isStreaming: false,
  messageCount: 0,
  initialHistoryStatus: 'ready',
  sessionHydrating: false,
}

describe('resolveChatSessionLoadState', () => {
  it.each([
    ['pending before requests start', { initialHistoryStatus: 'pending' }, 'loading'],
    ['initial history request', { initialHistoryStatus: 'loading' }, 'loading'],
    ['session subscription hydration', { sessionHydrating: true }, 'loading'],
    ['initial history failure', { initialHistoryStatus: 'error', sessionHydrating: true }, 'error'],
    ['confirmed empty session', {}, null],
  ] as const)('%s resolves to %s', (_label, overrides, expected) => {
    expect(resolveChatSessionLoadState({ ...settledEmpty, ...overrides })).toBe(expected)
  })

  it.each([
    ['draft landing', { isDraftLanding: true }],
    ['live recovery', { isStreaming: true, initialHistoryStatus: 'loading' as const }],
    ['persisted messages', { messageCount: 1, initialHistoryStatus: 'loading' as const }],
  ])('does not cover %s', (_label, overrides) => {
    expect(resolveChatSessionLoadState({ ...settledEmpty, ...overrides })).toBeNull()
  })

  it('keeps an initial history failure retryable after live or persisted content takes over', () => {
    expect(shouldShowHistorySentinelError({
      loadEarlierError: false,
      initialHistoryStatus: 'error',
      initialLoadSurface: null,
    })).toBe(true)
    expect(shouldShowHistorySentinelError({
      loadEarlierError: false,
      initialHistoryStatus: 'error',
      initialLoadSurface: 'error',
    })).toBe(false)
  })

  it('moves initial retry progress to the sentinel when content owns the thread', () => {
    expect(shouldShowHistorySentinelLoading({
      loadingEarlier: false,
      historyLoading: true,
      historyRetrying: false,
      initialHistoryStatus: 'loading',
      initialLoadSurface: null,
    })).toBe(true)
    expect(shouldShowHistorySentinelLoading({
      loadingEarlier: false,
      historyLoading: true,
      historyRetrying: false,
      initialHistoryStatus: 'loading',
      initialLoadSurface: 'loading',
    })).toBe(false)
  })

  it('keeps a settled empty-session retry visible in the sentinel', () => {
    expect(shouldShowHistorySentinelLoading({
      loadingEarlier: false,
      historyLoading: true,
      historyRetrying: true,
      initialHistoryStatus: 'ready',
      initialLoadSurface: null,
    })).toBe(true)
  })
})
