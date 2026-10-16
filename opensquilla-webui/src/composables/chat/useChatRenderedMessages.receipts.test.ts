import { describe, expect, it } from 'vitest'
import { ref } from 'vue'
import type { ChatMessage } from '@/types/chat'
import type { RouterTierSnapshotV1 } from '@/utils/chat/routerTierSnapshot'
import { useChatRenderedMessages } from './useChatRenderedMessages'

const acceptedSnapshot: RouterTierSnapshotV1 = {
  version: 1, request_kind: 'image',
  tiers: [
    { tier: 'c0', model: 'accepted/zeta', execution_kind: 'single_model' },
    { tier: 'c1', model: 'accepted/alpha', execution_kind: 'single_model' },
  ],
}

function render(messages: ChatMessage[]) {
  const source = ref(messages)
  const api = useChatRenderedMessages({
    messages: source, sessionKey: ref('synthetic-receipt-session'),
    routerSlots: ref(['c0', 'c1']), routerModels: ref({}),
    routerTierConfigs: ref({
      c0: { model: 'current/zeta', imageOnly: false },
      c1: { model: 'current/alpha', imageOnly: false },
    }),
    routerVisualMode: ref('real_candidates'), routerVisualEffectsEnabled: ref(true),
    renderMarkdown: text => text, stripGeneratedArtifactMarkers: text => text,
    stripTimePrefix: text => text, isSubagentCompletionMessage: () => false,
  })
  return { messages: source, cards: () => api.renderedMessages.value.filter(message => message.isRouterStrip) }
}

describe('router completion receipt reconciliation', () => {
  it.each(['router', 'ensemble'] as const)('inherits only the matching call when an earlier %s receipt arrives late', mode => {
    const laterSnapshot: RouterTierSnapshotV1 = {
      ...acceptedSnapshot,
      tiers: acceptedSnapshot.tiers.map(entry => ({ ...entry, model: entry.model.replace('accepted/', 'later/') })),
    }
    const firstRoute = {
      tier: 'c1', model: 'accepted/alpha', source: 'classifier', accepted_routing_mode: mode,
      router_tier_snapshot: acceptedSnapshot,
    }
    const api = render([
      { role: 'user', text: 'Compare the outlines.', ts: 1, turnId: 'multi-call-turn' },
      {
        role: 'router', text: '', ts: 2, turnId: 'multi-call-turn', messageId: 'first-call',
        provenanceKind: 'router_decision', routerModelCallId: '1.0', routerDecision: firstRoute,
      },
      {
        role: 'router', text: '', ts: 3, turnId: 'multi-call-turn', messageId: 'later-call',
        provenanceKind: 'router_decision', routerModelCallId: '2.0',
        routerDecision: { tier: 'c1', model: 'later/alpha', source: 'classifier', router_tier_snapshot: laterSnapshot },
      },
      {
        role: 'assistant', text: 'The first comparison is ready.', ts: 4, turnId: 'multi-call-turn',
        usage: {
          router_model_call_id: '1.0',
          route_plan: { tier: 'c1', model: 'accepted/alpha', source: 'classifier', router_tier_snapshot: null },
          ...(mode === 'ensemble' ? {
            ensemble_trace: { profile: 'synthetic-fusion' },
            model_usage_breakdown: [{ role: 'aggregator', provider: 'synthetic', model: 'accepted/alpha' }],
          } : {}),
        },
      },
    ])

    expect(api.cards()).toHaveLength(2)
    expect(api.cards().map(card => card.routerModelCallId)).toEqual(['1.0', '2.0'])
    expect(api.cards()[0]?.routerPanel).toBe(mode === 'ensemble' ? 'llm-ensemble' : 'real-candidates')
    if (mode === 'router') {
      expect(api.cards()[0]?.gridCells?.map(cell => cell.model)).toEqual(['accepted/zeta', 'accepted/alpha'])
    }
    expect(api.cards()[1]?.gridCells?.map(cell => cell.model)).toEqual(['later/zeta', 'later/alpha'])
  })

  it.each(['live', 'usage'] as const)('retains a valid %s snapshot when a receipt has an unusable optional snapshot', origin => {
    for (const invalidSnapshot of [null, undefined, { version: 1, request_kind: 'image', tiers: [] }]) {
      const route = { tier: 'c1', model: 'accepted/alpha', source: 'image_route', routing_applied: true }
      const messages: ChatMessage[] = [
        { role: 'user', text: 'Describe the outline.', ts: 1, turnId: 'receipt-turn' },
      ]
      if (origin === 'live') messages.push({
        role: 'router', text: '', ts: 2, turnId: 'receipt-turn', provenanceKind: 'router_decision',
        routerDecision: { ...route, router_tier_snapshot: acceptedSnapshot },
      })
      messages.push({
        role: 'assistant', text: 'A circular outline.', ts: 3, turnId: 'receipt-turn',
        usage: {
          router_model_call_id: '1.0',
          route_plan: { ...route, router_tier_snapshot: invalidSnapshot },
          ...(origin === 'usage' ? { router_tier_snapshot: acceptedSnapshot } : {}),
        },
      })
      const before = JSON.stringify(messages)
      const api = render(messages)

      expect(api.cards()).toHaveLength(1)
      expect(api.cards()[0]?.gridCells?.map(cell => cell.model)).toEqual(['accepted/zeta', 'accepted/alpha'])
      expect(api.cards()[0]?.routerSettled).toBe(true)
      expect(JSON.stringify(messages)).toBe(before)
    }
  })

  it('keeps the accepted ensemble mode when a completion receipt omits it', () => {
    const api = render([
      { role: 'user', text: 'Compare two geometric patterns.', ts: 1, turnId: 'ensemble-turn' },
      {
        role: 'router', text: '', ts: 2, turnId: 'ensemble-turn', provenanceKind: 'router_decision',
        routerModelCallId: '1.0',
        routerDecision: {
          tier: 'c1', model: 'accepted/alpha', source: 'classifier', accepted_routing_mode: 'ensemble',
        },
      },
    ])
    expect(api.cards()[0]?.routerPanel).toBe('llm-ensemble')
    api.messages.value.push({
      role: 'assistant', text: 'The patterns are similar.', ts: 3, turnId: 'ensemble-turn',
      usage: {
        router_model_call_id: '1.0',
        route_plan: { tier: 'c1', model: 'accepted/alpha', source: 'classifier' },
        ensemble_trace: { profile: 'synthetic-fusion' },
        model_usage_breakdown: [{ role: 'aggregator', provider: 'synthetic', model: 'accepted/alpha' }],
      },
    })

    expect(api.cards()).toHaveLength(1)
    expect(api.cards()[0]?.routerPanel).toBe('llm-ensemble')
  })
})
