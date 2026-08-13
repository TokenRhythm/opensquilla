import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

import messageListSource from '../components/chat/ChatMessageList.vue?raw'
import historySource from '../composables/chat/useChatHistory.ts?raw'
import transitionSource from '../utils/chat/forkTransition.ts?raw'
import chatViewSource from './ChatView.vue?raw'

const chatViewStyles = readFileSync(
  new URL('../styles/chat-view.css', import.meta.url),
  'utf8',
)

describe('chat fork hand-off contract', () => {
  it('uses durable inclusive turn ids for historical assistant branches', () => {
    expect(messageListSource).toContain("forkConversation: [throughTurnId?: string]")
    expect(messageListSource).toContain("turnOutcome?.turnId?.trim()")
    expect(messageListSource).toContain("@fork=\"$emit('forkConversation', forkThroughTurnId(entry.index))\"")
    expect(transitionSource).toContain("method: 'sessions.forkThroughTurn'")
    expect(transitionSource).toContain("params: { key: parentKey, throughTurnId }")
    expect(chatViewSource).toContain('rpc.call<ForkRpcResponse>(request.method, request.params)')
    expect(chatViewSource).toContain('validatedForkChildKey(res, normalizedTurnId)')
    expect(chatViewSource.indexOf('validatedForkChildKey(res, normalizedTurnId)')).toBeLessThan(
      chatViewSource.indexOf('query: { session: childKey }'),
    )
    expect(transitionSource).toContain("response?.forkMode !== 'through_turn'")
    expect(transitionSource).toContain('response.throughTurnId !== throughTurnId')
    expect(chatViewSource).not.toContain('beforeMessageId: normalizedTurnId')
    expect(chatViewSource).toContain("clearForkTransition(generation)\n      pushToast(t('chat.toast.forkFailed')")
  })

  it('keeps a read-only parent projection visible until child history is ready', () => {
    expect(chatViewSource).toContain(':messages="forkTransition?.previewMessages || visibleRenderedMessages"')
    expect(chatViewSource).toContain(':session-key="forkTransition?.parentKey || sessionKey"')
    expect(chatViewSource).toContain(':inert="forkTransition ? true : undefined"')
    expect(chatViewSource).toContain('Render-only snapshot; never becomes the child session\'s canonical messages.')
    expect(chatViewSource).toContain('loadedKey !== transition.targetKey')
    expect(chatViewSource).toContain("if (status === 'ready')")
    expect(historySource).toContain('historySessionKey.value = key')
    expect(historySource).toContain('historySessionKey,')
    expect(transitionSource).toContain('matchedBoundary = index')
    expect(transitionSource).toContain('source.slice(0, boundary + 1)')
  })

  it('renders status as an absolute shell sibling without shifting messages', () => {
    expect(chatViewSource).toContain('class="chat-fork-transition-overlay"')
    expect(chatViewSource.indexOf('class="chat-fork-transition-overlay"')).toBeLessThan(
      chatViewSource.indexOf('ref="threadRef"'),
    )
    expect(chatViewStyles).toMatch(
      /\.chat-fork-transition-overlay\s*\{[^}]*position:\s*absolute[^}]*inset-inline:\s*0[^}]*pointer-events:\s*none/s,
    )
    expect(chatViewStyles).toMatch(
      /\.chat-fork-transition-status\s*\{[^}]*max-width:[^}]*pointer-events:\s*auto/s,
    )
    expect(chatViewStyles).toMatch(
      /\.chat-fork-transition-status--error\s*\{[^}]*flex-wrap:\s*wrap/s,
    )
    expect(chatViewStyles).toMatch(
      /\.chat-fork-transition-status__action\s*\{[^}]*max-width:\s*100%[^}]*white-space:\s*normal/s,
    )
  })

  it('keeps the preview while returning until parent history is ready', () => {
    expect(chatViewSource).toContain("type ForkTransitionPhase = 'creating' | 'opening' | 'returning' | 'error'")
    expect(chatViewSource).toContain('targetKey: transition.parentKey')
    expect(chatViewSource).toContain("phase: 'returning'")
    expect(chatViewSource).toContain('activeKey !== transition.targetKey')
    expect(chatViewSource).toContain('loadedKey !== transition.targetKey')
    expect(transitionSource).toContain("targetKey === parentKey ? 'returning' : 'opening'")
    expect(chatViewSource).toContain('forkRouteHandoffAction(newSession, transition)')
    expect(chatViewSource).toContain("if (handoffAction === 'returning')")
  })

  it('invalidates every asynchronous fork continuation when the view unmounts', () => {
    expect(chatViewSource).toContain('forkTransitionLifetime.dispose()')
    expect(chatViewSource).toContain('forkTransition.value = null')
    expect(chatViewSource).toContain('chatViewActive\n    && !chatViewDisposed')
    expect(chatViewSource).toContain('if (!isForkTransitionActive(generation)) return')
    expect(transitionSource).toContain('export function createForkTransitionLifetime()')
  })

  it('keeps busy through navigation and retains recovery controls on child failures', () => {
    const failureHandler = chatViewSource.slice(
      chatViewSource.indexOf('function failForkTransition('),
      chatViewSource.indexOf('async function retryForkTransition()'),
    )
    expect(chatViewSource).toContain("forkTransition.value.phase !== 'error'")
    expect(chatViewSource).toContain('const navigationFailure = await router.push')
    expect(chatViewSource).not.toContain("router.push({ path: '/chat', query: { session: childKey } }).catch")
    expect(chatViewSource).toContain("transition.generation,\n        'history',")
    expect(chatViewSource).toContain("transition.generation,\n        'live',")
    expect(chatViewSource).toContain("phase: 'error'")
    expect(failureHandler).toContain("phase: 'error'")
    expect(failureHandler).not.toContain('clearForkTransition(')
    expect(chatViewSource).toContain('data-testid="chat-fork-retry"')
    expect(chatViewSource).toContain('data-testid="chat-fork-return"')
    expect(chatViewSource).toContain("pushToast(t('chat.toast.forkOpenFailed'), { tone: 'warn' })")
  })
})
