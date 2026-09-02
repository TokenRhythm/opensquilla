import { describe, expect, it } from 'vitest'

import chatViewSource from './ChatView.vue?raw'

describe('ChatView missing-session wiring', () => {
  it('connects the subscription domain signal to key-fenced history state', () => {
    const historyStart = chatViewSource.indexOf('const {\n  historySessionKey,')
    const historyEnd = chatViewSource.indexOf('\n} = chatHistory', historyStart)
    const historyWiring = chatViewSource.slice(historyStart, historyEnd)
    expect(historyWiring).toContain('markSessionMissing')

    const subscriptionStart = chatViewSource.indexOf(
      'const chatSessionSubscription = useChatSessionSubscription({',
    )
    const subscriptionEnd = chatViewSource.indexOf('\n})', subscriptionStart)
    const subscriptionWiring = chatViewSource.slice(subscriptionStart, subscriptionEnd)
    expect(subscriptionWiring).toContain('onSessionMissing: markSessionMissing')
    expect(subscriptionWiring).not.toContain('SESSION_NOT_FOUND')
    expect(subscriptionWiring).not.toContain('NOT_FOUND')
  })

  it('forwards complete session-read metadata to pending-input reconciliation', () => {
    const assignmentStart = chatViewSource.indexOf(
      'applyPendingUserInputSnapshot = (snapshot, snapshotStreamGeneration)',
    )
    expect(assignmentStart).toBeGreaterThan(-1)
    const assignmentEnd = chatViewSource.indexOf('\n})', assignmentStart)
    const assignment = chatViewSource.slice(assignmentStart, assignmentEnd)
    expect(assignment).toContain('...snapshot')
    expect(assignment).toContain('streamGeneration: snapshotStreamGeneration')
    expect(assignment).not.toContain('streamGeneration.value')

    const subscriptionStart = chatViewSource.indexOf(
      'const chatSessionSubscription = useChatSessionSubscription({',
    )
    const subscriptionEnd = chatViewSource.indexOf('\n})', subscriptionStart)
    const subscriptionWiring = chatViewSource.slice(subscriptionStart, subscriptionEnd)
    expect(subscriptionWiring).toContain('onSnapshot: (snapshot, snapshotStreamGeneration)')
    expect(subscriptionWiring).toContain(
      'applyPendingUserInputSnapshot(snapshot, snapshotStreamGeneration)',
    )
  })
})
