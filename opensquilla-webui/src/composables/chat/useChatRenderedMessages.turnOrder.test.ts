import { describe, expect, it } from 'vitest'
import { effectScope, ref } from 'vue'
import { projectConversationContent } from '@/adapters/gateway/conversationContentV4'
import { projectHistoryMessage } from '@/adapters/gateway/sessionHistoryV4'
import type { ChatMessage, ChatRouterTierConfig } from '@/types/chat'
import { projectConversationHistoryMessage } from '@/utils/chat/conversationHistory'
import { useChatRenderedMessages, type NormalizedRouterDecision } from './useChatRenderedMessages'
import { useChatRouterDecisionRuntime } from './useChatRouterDecisionRuntime'
import { useChatStream } from './useChatStream'

const models = ['accepted/z-fast', 'accepted/a-balanced', 'accepted/m-reasoning', 'accepted/b-quality']

function tierConfigs(): Record<string, ChatRouterTierConfig> {
  return Object.fromEntries(models.map((model, index) => [`c${index}`, { model, imageOnly: false }]))
}

const decision: NormalizedRouterDecision = {
  tier: 'c0',
  model: models[0]!,
  source: 'llm_ensemble',
}

function renderedRows(messages: ChatMessage[], isStreaming = true) {
  const api = useChatRenderedMessages({
    messages: ref<ChatMessage[]>(messages),
    sessionKey: ref('agent:main:webchat:synthetic-turn-order'),
    routerSlots: ref(['c0', 'c1', 'c2', 'c3']),
    routerModels: ref({}),
    routerTierConfigs: ref(tierConfigs()),
    routerVisualEffectsEnabled: ref(true),
    routerVisualMode: ref('real_candidates'),
    isStreaming: ref(isStreaming),
    renderMarkdown: text => text,
    stripGeneratedArtifactMarkers: text => text,
    stripTimePrefix: text => text,
    isSubagentCompletionMessage: () => false,
  })
  return api.renderedMessages.value
}

describe('rendered turn order', () => {
  it('keeps a late router receipt with its own turn instead of below the queued follow-up', () => {
    const rows = renderedRows([
      { role: 'user', text: 'Start the long task', ts: 1, turnId: 'turn-a' },
      { role: 'user', text: 'Queued follow-up', ts: 2, turnId: 'turn-b' },
      {
        role: 'router', text: '', ts: 3, turnId: 'turn-a', messageId: 'router-a',
        provenanceKind: 'router_decision', routerDecision: decision,
      },
    ])

    expect(rows.map(row => row.displayRole)).toEqual(['user', 'router', 'user'])
    expect(rows[1]?.turnKey).toBe(rows[0]?.turnKey)
    expect(rows[2]?.turnKey).not.toBe(rows[1]?.turnKey)
  })

  it('groups text that arrives after the queued follow-up with the turn that is running', () => {
    const rows = renderedRows([
      { role: 'user', text: 'Start the long task', ts: 1, turnId: 'turn-a' },
      { role: 'user', text: 'Queued follow-up', ts: 2, turnId: 'turn-b' },
      {
        role: 'router', text: '', ts: 3, turnId: 'turn-a', messageId: 'router-a',
        provenanceKind: 'router_decision', routerDecision: decision,
      },
      { role: 'assistant', text: 'Still working on the first task', ts: 4, turnId: 'turn-a' },
    ])

    expect(rows.map(row => row.displayRole)).toEqual(['user', 'router', 'assistant', 'user'])
    expect(rows[3]?.turnKey).not.toBe(rows[2]?.turnKey)
  })

  it('leaves an in-order transcript untouched', () => {
    const rows = renderedRows([
      { role: 'user', text: 'Start', ts: 1, turnId: 'turn-a' },
      {
        role: 'router', text: '', ts: 2, turnId: 'turn-a', messageId: 'router-a',
        provenanceKind: 'router_decision', routerDecision: decision,
      },
      { role: 'user', text: 'Next', ts: 3, turnId: 'turn-b' },
    ])

    expect(rows.map(row => row.displayRole)).toEqual(['user', 'router', 'user'])
  })

  it.each([
    { turn_id: 'turn-a' },
    { task_id: 'turn-a' },
  ])('keeps a completed stream before an accepted follow-up using %j', identity => {
    const messages = ref<ChatMessage[]>([
      { role: 'user', text: 'First request', ts: 1, turnId: 'turn-a' },
      { role: 'user', text: 'Accepted follow-up', ts: 2, turnId: 'turn-b' },
    ])
    const scope = effectScope()
    const stream = scope.run(() => useChatStream({
      messages,
      lastHeaderRole: ref(''),
      aborted: ref(false),
      autoScroll: ref(false),
      applySessionRunState: () => {},
      renderMarkdown: text => text,
      stripDirectiveTags: text => text,
      stripGeneratedArtifactMarkers: text => text,
      scrollToBottom: () => {},
    }))!

    try {
      const done = projectConversationContent({ ...identity, text: 'Completed result' }, 'turn-completed')
      stream.appendDelta(done.finalText || '')
      stream.endStreaming()
      const completed = messages.value[messages.value.length - 1]!
      completed.turnId = done.completedTurnId

      const rows = renderedRows(messages.value, false)
      expect(rows.map(row => [row.displayRole, row.turnKey])).toEqual([
        ['user', 'turn:turn-a'],
        ['assistant', 'turn:turn-a'],
        ['user', 'turn:turn-b'],
      ])
      expect(rows[1]).toMatchObject({ text: 'Completed result', sourceIndex: 2 })
      expect(messages.value.map(message => message.role)).toEqual(['user', 'user', 'assistant'])
    } finally {
      stream.cleanup()
      scope.stop()
    }
  })

  it('retains historical turn ownership when a router receipt arrives after the completion', () => {
    const messages = ref([
      { id: 'user-a', role: 'user', text: 'First request', ts: 1, turn_context: { turn_id: 'turn-a' } },
      { id: 'user-b', role: 'user', text: 'Accepted follow-up', ts: 2, turn_context: { turn_id: 'turn-b' } },
      { id: 'answer-a', role: 'assistant', text: 'Completed result', ts: 3, turn_context: { turn_id: 'turn-a' } },
    ].map((message, index) => projectConversationHistoryMessage(projectHistoryMessage(message, index))))
    const router = useChatRouterDecisionRuntime({
      messages,
      sessionKey: ref('agent:main:webchat:synthetic-turn-order'),
      isStreaming: ref(false),
      autoScroll: ref(false),
      activeTurnUsesEnsemble: ref(false),
      activeTurnId: ref(''),
      streamBubble: ref(false),
      streamHasVisibleOutput: ref(false),
      startStreaming: () => {},
      resetStreamForRouterReplay: () => {},
      resetStreamIdleTimer: () => {},
      setStreamActivity: () => {},
      scrollToBottom: () => {},
    })

    router.queueRouterDecision({
      turn_id: 'turn-a', stream_seq: 10,
      tier: decision.tier, model: decision.model, source: decision.source,
    })
    router.flushPendingRouterDecision()

    const rows = renderedRows(messages.value, false)
    expect(rows.map(row => [row.displayRole, row.turnKey])).toEqual([
      ['user', 'turn:turn-a'],
      ['assistant', 'turn:turn-a'],
      ['router', 'turn:turn-a'],
      ['user', 'turn:turn-b'],
    ])
    expect(rows.map(row => row.sourceIndex)).toEqual([0, 2, 3, 1])
    expect(messages.value.map(message => message.role)).toEqual(['user', 'user', 'assistant', 'router'])
    expect(messages.value.slice(0, 3).every(message => message.restoredFromHistory)).toBe(true)
  })
})
