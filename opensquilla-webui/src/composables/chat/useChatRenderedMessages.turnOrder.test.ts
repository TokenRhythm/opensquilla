import { describe, expect, it } from 'vitest'
import { ref } from 'vue'
import type { ChatMessage, ChatRouterTierConfig } from '@/types/chat'
import { useChatRenderedMessages, type NormalizedRouterDecision } from './useChatRenderedMessages'

const models = ['accepted/z-fast', 'accepted/a-balanced', 'accepted/m-reasoning', 'accepted/b-quality']

function tierConfigs(): Record<string, ChatRouterTierConfig> {
  return Object.fromEntries(models.map((model, index) => [`c${index}`, { model, imageOnly: false }]))
}

const decision: NormalizedRouterDecision = {
  tier: 'c0',
  model: models[0]!,
  source: 'llm_ensemble',
}

function renderedRows(messages: ChatMessage[]) {
  const api = useChatRenderedMessages({
    messages: ref<ChatMessage[]>(messages),
    sessionKey: ref('agent:main:webchat:synthetic-turn-order'),
    routerSlots: ref(['c0', 'c1', 'c2', 'c3']),
    routerModels: ref({}),
    routerTierConfigs: ref(tierConfigs()),
    routerVisualEffectsEnabled: ref(true),
    routerVisualMode: ref('real_candidates'),
    isStreaming: ref(true),
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
})
