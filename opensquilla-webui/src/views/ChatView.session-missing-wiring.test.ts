import { describe, expect, it } from 'vitest'

import rawChatViewSource from './ChatView.vue?raw'
const chatViewSource = rawChatViewSource.replace(/\r\n/g, '\n')

describe('ChatView missing-session wiring', () => {
  it('keeps an identity-scoped unknown delivery check reachable for an absent first session', () => {
    const start = chatViewSource.indexOf('const composerRecoveryAction = computed')
    const end = chatViewSource.indexOf('\nwatch([sessionKey, deliveryIdentity]', start)
    const recovery = chatViewSource.slice(start, end)
    const check = recovery.indexOf("return { kind: 'check', ids: entries.map(entry => entry.id) }")
    const missingGuard = recovery.indexOf('if (historyState.value.sessionMissing || composerHasExecution.value) return null')
    expect(check).toBeGreaterThanOrEqual(0)
    expect(missingGuard).toBeGreaterThan(check)
    expect(recovery).toContain('entries.length && (historyState.value.sessionMissing || !composerHasExecution.value)')
  })

  it('requires durable negative proof before transferring an absent session editor to a fresh draft', () => {
    const start = chatViewSource.indexOf('function recoverMissingRetiredSession()')
    const end = chatViewSource.indexOf('\nconst recoveryNoticeState', start)
    const recovery = chatViewSource.slice(start, end)
    expect(recovery).toContain('retiredMissingSessionCandidate(records, capturedIdentity, capturedKey)')
    expect(recovery).toContain('listSessionDeliveries!(capturedIdentity, capturedKey)')
    expect(recovery).toContain('chatSend.deliveryRecoveryEntries.value.length')
    expect(recovery).toContain('preserveComposer: true, isCurrent: recoveryIsCurrent')
    expect(recovery).toContain("router.replace({ path: '/chat/new'")
    expect(recovery).not.toContain('sendMessage(')
    expect(recovery).toContain('() => { void recoverMissingRetiredSession() }')
    expect(chatViewSource).toContain('rebindDraftSession: key => draftPersistence.rebindCurrentDraft(key, { retainSource: true })')
    expect(chatViewSource).toContain('prepareDraftSession: key => { chatHistory.beginProvisionalSession(key) }')
  })

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
