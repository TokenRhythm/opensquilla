import { describe, expect, it } from 'vitest'
import {
  resolveChatHistoryRecoveryState,
  shouldShowConfirmedEmptySession,
} from './sessionLoadState'

describe('resolveChatHistoryRecoveryState', () => {
  it.each([
    ['pending before requests start', 'pending', false, 'history-loading'],
    ['initial history request', 'loading', false, 'history-loading'],
    ['initial retry', 'loading', true, 'history-retrying'],
    ['initial history failure', 'error', false, 'history-error'],
    ['settled history', 'ready', false, null],
  ] as const)('%s resolves independently from live hydration', (
    _label,
    initialHistoryStatus,
    retrying,
    expected,
  ) => {
    expect(resolveChatHistoryRecoveryState({
      isDraftLanding: false,
      initialHistoryStatus,
      retrying,
    })).toBe(expected)
  })

  it('surfaces a failed refresh after history was already ready', () => {
    expect(resolveChatHistoryRecoveryState({
      isDraftLanding: false,
      initialHistoryStatus: 'ready',
      retrying: false,
      recoveryError: true,
    })).toBe('history-error')
  })

  it('does not render recovery state on the draft landing', () => {
    expect(resolveChatHistoryRecoveryState({
      isDraftLanding: true,
      initialHistoryStatus: 'loading',
      retrying: false,
    })).toBeNull()
  })
})

describe('shouldShowConfirmedEmptySession', () => {
  it('shows empty only after authoritative history confirms it', () => {
    expect(shouldShowConfirmedEmptySession({
      isDraftLanding: false,
      isStreaming: false,
      messageCount: 0,
      initialHistoryStatus: 'ready',
    })).toBe(true)
  })

  it.each(['pending', 'loading', 'error'] as const)(
    'does not misreport %s history as an empty session',
    (initialHistoryStatus) => {
      expect(shouldShowConfirmedEmptySession({
        isDraftLanding: false,
        isStreaming: false,
        messageCount: 0,
        initialHistoryStatus,
      })).toBe(false)
    },
  )

  it('does not replace live content or the draft landing with empty state', () => {
    expect(shouldShowConfirmedEmptySession({
      isDraftLanding: false,
      isStreaming: false,
      messageCount: 1,
      initialHistoryStatus: 'ready',
    })).toBe(false)
    expect(shouldShowConfirmedEmptySession({
      isDraftLanding: true,
      isStreaming: false,
      messageCount: 0,
      initialHistoryStatus: 'ready',
    })).toBe(false)
  })
})
