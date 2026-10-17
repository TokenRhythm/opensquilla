import { describe, expect, it } from 'vitest'
import { ref } from 'vue'
import type { ChatMessage, ChatRouterTierConfig } from '@/types/chat'
import type { RouterTierSnapshotV1 } from '@/utils/chat/routerTierSnapshot'
import { useChatRenderedMessages, type NormalizedRouterDecision } from './useChatRenderedMessages'

const acceptedModels = ['accepted/z-fast', 'accepted/a-balanced', 'accepted/m-reasoning', 'accepted/b-quality']
const currentModels = ['current/z-fast', 'current/a-balanced', 'current/m-reasoning', 'current/b-quality']

function tierConfigs(models: string[]): Record<string, ChatRouterTierConfig> {
  return Object.fromEntries(models.map((model, index) => [`c${index}`, { model, imageOnly: false }]))
}

function snapshot(models: string[], requestKind: 'text' | 'image'): RouterTierSnapshotV1 {
  return {
    version: 1,
    request_kind: requestKind,
    tiers: models.map((model, index) => ({ tier: `c${index}`, model, execution_kind: 'single_model' })),
  }
}

function renderer(messages: ChatMessage[], models = currentModels) {
  const routerTierConfigs = ref(tierConfigs(models))
  const routerSlots = ref(['c0', 'c1', 'c2', 'c3'])
  const api = useChatRenderedMessages({
    messages: ref<ChatMessage[]>(messages),
    sessionKey: ref('agent:main:webchat:synthetic-router-snapshots'),
    routerSlots,
    routerModels: ref({}),
    routerTierConfigs,
    routerVisualEffectsEnabled: ref(true),
    routerVisualMode: ref('real_candidates'),
    renderMarkdown: text => text,
    stripGeneratedArtifactMarkers: text => text,
    stripTimePrefix: text => text,
    isSubagentCompletionMessage: () => false,
  })
  return { ...api, routerTierConfigs, routerSlots }
}

describe('useChatRenderedMessages accepted router snapshots', () => {
  it.each(['live', 'settled', 'restored'] as const)(
    'keeps the accepted history-image pool for a text-only %s follow-up after settings change',
    stage => {
      const decision: NormalizedRouterDecision = {
        tier: 'c0',
        model: acceptedModels[0]!,
        source: 'image_route',
        router_tier_snapshot: snapshot(acceptedModels, 'image'),
      }
      const messages: ChatMessage[] = [{ role: 'user', text: 'Compare the shapes.', ts: 1, turnId: 'turn-followup' }]
      if (stage !== 'restored') {
        messages.push({
          role: 'router', text: '', ts: 2, turnId: 'turn-followup', messageId: 'router-followup',
          provenanceKind: 'router_decision', routerDecision: decision,
        })
      }
      if (stage !== 'live') {
        messages.push({
          role: 'assistant', text: 'The shapes differ.', ts: 3, turnId: 'turn-followup',
          restoredFromHistory: stage === 'restored',
          usage: {
            routed_tier: decision.tier,
            routed_model: decision.model,
            routing_source: decision.source,
            image_route_reason: 'history_context',
            // Older receipts may omit the snapshot already supplied live.
            route_plan: stage === 'restored'
              ? decision
              : { tier: decision.tier, model: decision.model, source: decision.source },
          },
        })
      }
      const before = JSON.stringify(messages)
      const api = renderer(messages)
      const strips = api.renderedMessages.value.filter(message => message.isRouterStrip)

      expect(strips).toHaveLength(1)
      expect(strips[0]?.gridCells?.map(cell => cell.model)).toEqual(acceptedModels)
      expect(strips[0]?.winnerIdx).toBe(0)
      expect(strips[0]?.routerStatic).toBe(stage === 'restored')
      expect(JSON.stringify(messages)).toBe(before)
    },
  )

  it('keeps a single accepted history-image candidate despite a larger next-turn pool', () => {
    const api = renderer([
      { role: 'user', text: 'Describe its outline.', ts: 1, turnId: 'turn-single' },
      {
        role: 'router', text: '', ts: 2, turnId: 'turn-single', provenanceKind: 'router_decision',
        routerDecision: {
          tier: 'c0', model: acceptedModels[0]!, source: 'image_route',
          router_tier_snapshot: snapshot([acceptedModels[0]!], 'image'),
        },
      },
    ])

    expect(api.renderedMessages.value.find(message => message.isRouterStrip)?.gridCells?.map(cell => cell.model))
      .toEqual([acceptedModels[0]])
  })

  it('keeps adjacent image and text snapshots attached to their own turns as settings change', () => {
    const textModels = ['text/z-fast', 'text/a-balanced']
    const api = renderer([
      { role: 'user', text: 'Describe the geometric figure.', ts: 1, turnId: 'turn-image' },
      {
        role: 'assistant', text: 'A closed outline.', ts: 2, turnId: 'turn-image', restoredFromHistory: true,
        usage: { route_plan: { tier: 'c0', model: acceptedModels[0]!, source: 'image_route', router_tier_snapshot: snapshot(acceptedModels, 'image') } },
      },
      {
        role: 'user', text: 'Label the figure.', ts: 3, turnId: 'turn-text',
        attachments: [{ kind: 'file', displayId: 'synthetic-figure', renderKey: 'synthetic-figure', name: 'figure.png', mime: 'image/png' }],
      },
      {
        role: 'router', text: '', ts: 4, turnId: 'turn-text', provenanceKind: 'router_decision',
        routerDecision: { tier: 'c1', model: textModels[1]!, source: 'classifier', router_tier_snapshot: snapshot(textModels, 'text') },
      },
      { role: 'user', text: 'Count three objects.', ts: 5, turnId: 'turn-legacy' },
      {
        role: 'router', text: '', ts: 6, turnId: 'turn-legacy', provenanceKind: 'router_decision',
        routerDecision: { tier: 'c0', model: currentModels[0]!, source: 'classifier' },
      },
    ])
    const pools = () => api.renderedMessages.value.filter(message => message.isRouterStrip)
      .map(message => message.gridCells?.map(cell => cell.model))

    expect(pools()).toEqual([acceptedModels, textModels, currentModels])
    const futureModels = ['future/z-fast', 'future/a-balanced', 'future/m-reasoning', 'future/b-quality']
    api.routerTierConfigs.value = tierConfigs(futureModels)
    expect(pools()).toEqual([acceptedModels, textModels, [currentModels[0], ...futureModels.slice(1)]])
  })

  it.each(['missing', 'damaged'] as const)(
    'keeps canonical tier order and shared-model winners for an older Gateway with a %s snapshot',
    snapshotKind => {
      const models = ['configured/z-fast', 'configured/a-shared', 'configured/a-shared', 'configured/m-quality']
      const api = renderer([
        { role: 'user', text: 'List the geometric figures.', ts: 1, turnId: 'turn-compatibility' },
        {
          role: 'router', text: '', ts: 2, turnId: 'turn-compatibility', provenanceKind: 'router_decision',
          routerDecision: {
            tier: 't2', model: models[2]!, source: 'classifier',
            ...(snapshotKind === 'damaged' ? {
              router_tier_snapshot: {
                version: 1, request_kind: 'image', tiers: [
                  { tier: 'c0', model: 'invalid/first', execution_kind: 'single_model' },
                  { tier: 't0', model: 'invalid/duplicate', execution_kind: 'single_model' },
                ],
              },
            } : {}),
          },
        },
      ], models)
      api.routerSlots.value = ['t3', 't1', 't0', 't2']
      const strip = api.renderedMessages.value.find(message => message.isRouterStrip)

      expect(strip?.gridCells?.map(cell => cell.model)).toEqual([models[0], models[1], models[3]])
      expect(strip?.gridCells?.map(cell => cell.tiers)).toEqual([['c0'], ['c1', 'c2'], ['c3']])
      expect(strip?.winnerIdx).toBe(1)
    },
  )

  it.each(['snapshot', 'configuration'] as const)(
    'keeps same-model deployments separate and labels providers for a %s pool',
    source => {
      const entries = [
        { tier: 'c0', provider: 'profile-z', model: 'shared-model', execution_kind: 'single_model' },
        { tier: 'c1', provider: 'profile-a', model: 'shared-model', execution_kind: 'single_model' },
      ]
      const api = renderer([
        { role: 'user', text: 'Count the vertices.', ts: 1, turnId: 'turn-provider' },
        {
          role: 'router', text: '', ts: 2, turnId: 'turn-provider', provenanceKind: 'router_decision',
          routerDecision: {
            tier: 'c1', model: 'shared-model', source: 'classifier',
            ...(source === 'snapshot' ? {
              router_tier_snapshot: { version: 1, request_kind: 'image', tiers: entries },
            } : {}),
          },
        },
      ])
      api.routerTierConfigs.value = Object.fromEntries(entries.map(entry => [
        entry.tier, { model: entry.model, provider: entry.provider, imageOnly: false },
      ]))
      const strip = api.renderedMessages.value.find(message => message.isRouterStrip)

      expect(strip?.gridCells?.map(cell => [cell.model, cell.provider, cell.tiers])).toEqual([
        ['shared-model', 'profile-z', ['c0']], ['shared-model', 'profile-a', ['c1']],
      ])
      expect(strip?.gridCells?.map(cell => cell.displayName)).toEqual([
        'shared-model · profile-z', 'shared-model · profile-a',
      ])
      expect(strip?.winnerIdx).toBe(1)
    },
  )

  it('disambiguates equal short names without reordering accepted model identifiers', () => {
    const models = ['namespace-z/shared', 'namespace-a/shared']
    const api = renderer([
      { role: 'user', text: 'Name the geometric figure.', ts: 1, turnId: 'turn-labels' },
      {
        role: 'router', text: '', ts: 2, turnId: 'turn-labels', provenanceKind: 'router_decision',
        routerDecision: {
          tier: 'c1', model: models[1]!, source: 'classifier', router_tier_snapshot: snapshot(models, 'text'),
        },
      },
    ])
    const strip = api.renderedMessages.value.find(message => message.isRouterStrip)

    expect(strip?.gridCells?.map(cell => cell.displayName)).toEqual(models)
    expect(strip?.gridCells?.map(cell => cell.model)).toEqual(models)
    expect(strip?.winnerIdx).toBe(1)
  })
})
