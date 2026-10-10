import { describe, expect, it } from 'vitest'

import chatViewSource from './ChatView.vue?raw'

describe('ChatView missing-session wiring', () => {
  it('promotes only accepted local drafts before opening the replacement read lease', () => {
    expect(chatViewSource).toContain('isProvisionalDraft: isProvisionalDraftSession')
    const start = chatViewSource.indexOf('materializeDraftSession: key => {')
    const end = chatViewSource.indexOf('\n  aborted,', start)
    const accepted = chatViewSource.slice(start, end)
    const guard = accepted.indexOf('if (!isProvisionalDraftSession()) return')
    const clearDraft = accepted.indexOf('pendingSessionIntent.value = null')
    const bootstrap = accepted.indexOf('startSessionBootstrap({ includeHistory: false, force: true })')
    expect(guard).toBeGreaterThanOrEqual(0)
    expect(clearDraft).toBeGreaterThan(guard)
    expect(bootstrap).toBeGreaterThan(clearDraft)
    expect(chatViewSource).toContain('|| historyState.sessionMissing')
  })

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
})
