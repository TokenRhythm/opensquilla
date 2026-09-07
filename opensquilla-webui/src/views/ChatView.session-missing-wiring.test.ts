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
})
