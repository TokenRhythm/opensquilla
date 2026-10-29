import { describe, expect, it } from 'vitest'
import { ref } from 'vue'
import type { ChatMessage } from '@/types/chat'
import { projectSessionCreationRouterPresentation } from '@/utils/chat/sessionCreationRouterPresentation'
import { useChatRenderedMessages } from './useChatRenderedMessages'

function renderedRows(messages: ChatMessage[]) {
  return useChatRenderedMessages({
    messages: ref(messages),
    sessionKey: ref('agent:main:webchat:turn-boundaries'),
    routerSlots: ref(['c0', 'c1']),
    routerModels: ref({}),
    routerTierConfigs: ref({
      c0: { model: 'test/fast', imageOnly: false },
      c1: { model: 'test/selected', imageOnly: false },
    }),
    routerVisualEffectsEnabled: ref(true),
    routerVisualMode: ref('real_candidates'),
    renderMarkdown: text => text,
    stripGeneratedArtifactMarkers: text => text,
    stripTimePrefix: text => text,
    isSubagentCompletionMessage: () => false,
  }).renderedMessages.value
}

function user(messageId: string, turnId: string): ChatMessage {
  return { role: 'user', text: messageId, messageId, turnId, ts: 1 }
}

function answer(messageId: string, turnId: string): ChatMessage {
  return { role: 'assistant', text: messageId, messageId, turnId, ts: 2 }
}

function route(turnId: string): ChatMessage {
  return {
    role: 'router', text: '', messageId: `route-${turnId}`, turnId, ts: 2,
    provenanceKind: 'router_decision',
    routerDecision: { tier: 'c1', model: 'test/selected', source: 'squilla_router' },
  }
}

describe('rendered turn boundaries', () => {
  it.each([false, true])('keeps a resumed parent route above its creation card with hidden input=%s', hiddenInput => {
    const messages = [
      user('request', 'creation'),
      ...(hiddenInput ? [{ ...user('resume-input', 'resume'), text: '' }] : []),
      route('resume'),
      {
        ...answer('creation-result', 'creation'), text: '',
        tool_calls: [{
          tool_use_id: 'spawn-child', name: 'sessions_spawn',
          result: JSON.stringify({
            session_key: 'agent:main:subagent:child', status: 'queued', title: 'Child task',
          }),
          execution_status: { status: 'success' },
        }],
      },
    ]
    const rows = projectSessionCreationRouterPresentation(renderedRows(messages), true).messages

    expect(rows.map(row => row.messageId)).toEqual(['request', 'route-resume', 'creation-result'])
    expect(rows[2]?.createdSessionLinks).toHaveLength(1)
  })

  it('does not move earlier output across an internal turn after a queued user', () => {
    const rows = renderedRows([
      user('first', 'a'), user('queued', 'b'), route('resume'), answer('continued', 'a'),
    ])

    expect(rows.map(row => row.sourceIndex)).toEqual([0, 1, 2, 3])
  })

  it('keeps a truncated history window in order without the earlier user anchor', () => {
    const rows = renderedRows([
      answer('earlier', 'a'), route('a'), user('queued', 'b'), answer('later', 'a'),
    ])

    expect(rows.map(row => row.sourceIndex)).toEqual([0, 1, 2, 3])
  })

  it('does not group unrelated legacy turns that share a timestamp', () => {
    const messages: ChatMessage[] = [
      { role: 'user', text: 'First request', ts: 1 },
      { role: 'assistant', text: 'First result', ts: 2 },
      { role: 'user', text: 'Second request', ts: 3 },
      { role: 'assistant', text: 'Second result', ts: 4 },
      { role: 'user', text: 'Third request', ts: 1 },
      { role: 'assistant', text: 'Third result', ts: 5 },
    ]

    expect(renderedRows(messages).map(row => row.text)).toEqual(messages.map(message => message.text))
  })

  it('retains arrival order within each of several accepted user turns', () => {
    const rows = renderedRows([
      user('first', 'a'), user('second', 'b'), user('third', 'c'),
      route('a'), answer('second-result', 'b'), answer('first-result', 'a'),
    ])

    expect(rows.map(row => row.sourceIndex)).toEqual([0, 3, 5, 1, 4, 2])
  })

  it('retains same-turn steer input after preceding output when a follow-up is queued', () => {
    const rows = renderedRows([
      user('first', 'a'), answer('progress', 'a'), user('queued', 'b'),
      user('steer', 'a'), answer('continued', 'a'),
    ])

    expect(rows.map(row => row.sourceIndex)).toEqual([0, 1, 3, 4, 2])
  })
})
