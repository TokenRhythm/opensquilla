import { describe, expect, it } from 'vitest'
import { ref } from 'vue'
import { useChatRenderedMessages } from '@/composables/chat/useChatRenderedMessages'
import type { ChatMessage } from '@/types/chat'
import { projectConversationEvent, projectConversationSnapshotEvent } from './conversationContentV4'
import { decodeConversationEvent } from './conversationEventsV4'

const legs = [
  { index: 0, kind: 'primary', model: 'deepseek-v4-pro', plan_id: 'route-A' },
  { index: 1, kind: 'provider_fallback', model: 'kimi-k2.7-code', plan_id: 'route-A' },
  { index: 2, kind: 'provider_fallback', model: 'deepseek-v4-pro-0813', plan_id: 'route-A' },
]

function completion(payload: Record<string, unknown>) {
  const event = projectConversationEvent(decodeConversationEvent('session.event.done', payload))
  if (event.semanticKind !== 'turn-completed') throw new Error('Missing turn completion')
  return event.payload
}

function physicalRouterView() {
  const isStreaming = ref(true)
  const messages = ref<ChatMessage[]>([
    { role: 'user', text: 'Synthetic request', ts: 1, turnId: 'turn-A' },
    {
      role: 'router', text: '', ts: 2, turnId: 'turn-A', messageId: 'router-A',
      provenanceKind: 'router_decision',
      routerDecision: { tier: 'c2', model: 'deepseek-v4-pro', source: 'classifier' },
      routerExecutionModel: 'kimi-k2.7-code',
    },
  ])
  const { renderedMessages } = useChatRenderedMessages({
    messages, isStreaming, sessionKey: ref('alpha'), routerSlots: ref(['c1', 'c2']),
    routerModels: ref({ c1: 'kimi-k2.7-code', c2: 'deepseek-v4-pro' }),
    routerTierConfigs: ref({}), routerVisualEffectsEnabled: ref(true),
    routerVisualMode: ref('real_candidates'), renderMarkdown: text => text,
    stripGeneratedArtifactMarkers: text => text, stripTimePrefix: text => text,
    isSubagentCompletionMessage: () => false,
  })
  return { messages, isStreaming, strip: () => renderedMessages.value.find(message => message.isRouterStrip) }
}

describe('terminal physical execution model projection', () => {
  it.each(['failed', 'cancelled', 'timeout'])('settles the physical model after %s without a usage receipt', status => {
    const view = physicalRouterView()
    expect(view.strip()?.routerSettled).toBe(false)
    view.messages.value.push({
      role: 'assistant', text: '', ts: 3, turnId: 'turn-A',
      turnOutcome: { status, turnId: 'turn-A', taskId: 'turn-A' },
    })
    view.isStreaming.value = false
    expect(view.strip()).toMatchObject({ routerSettled: true, routerExecutionModel: 'kimi-k2.7-code' })
    expect(view.messages.value[1]?.routerSettled).toBeUndefined()

    view.messages.value.push({ role: 'user', text: 'Next synthetic request', ts: 4, turnId: 'turn-B' })
    view.isStreaming.value = true
    expect(view.strip()?.routerSettled).toBe(true)
  })

  it('keeps a physical call active across an applied same-turn steer', () => {
    const view = physicalRouterView()
    view.messages.value.push({
      role: 'user', text: 'Synthetic adjustment', ts: 3, turnId: 'turn-A', inputDisposition: 'applied',
    })
    expect(view.strip()).toMatchObject({ routerSettled: false, routerExecutionModel: 'kimi-k2.7-code' })
    view.isStreaming.value = false
    expect(view.strip()?.routerSettled).toBe(true)
  })

  it.each(['execution_legs', 'executionLegs'])('restores the last physical model from %s without live activity', field => {
    const usage = {
      routed_tier: 'c2', routed_model: 'deepseek-v4-pro', routing_source: 'classifier',
      route_plan: { tier: 'c2', model: 'deepseek-v4-pro', source: 'classifier', routing_applied: true },
      [field]: legs,
    }
    const payload = { key: 'alpha', task_id: 'turn-A', usage }
    const projected = completion(payload)
    const snapshot = projectConversationSnapshotEvent('session.event.done', payload)
    expect(projected.usage?.execution_legs).toEqual(legs)
    expect(snapshot?.payload).toEqual(projected)
    const { renderedMessages } = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'Synthetic request', ts: 1, turnId: 'turn-A' },
        { role: 'assistant', text: 'Synthetic result', ts: 2, turnId: 'turn-A', usage: projected.usage },
      ]),
      sessionKey: ref('alpha'), routerSlots: ref(['c1', 'c2']),
      routerModels: ref({ c1: 'kimi-k2.7-code', c2: 'deepseek-v4-pro' }),
      routerTierConfigs: ref({}), routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'), renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text, stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
    })
    expect(renderedMessages.value.find(message => message.isRouterStrip)).toMatchObject({
      routerSelectedModel: 'deepseek-v4-pro', routerExecutionModel: 'deepseek-v4-pro-0813',
      routerSettled: true,
    })
    expect(usage.route_plan.model).toBe('deepseek-v4-pro')
  })

  it.each([
    { payload: { execution_legs: legs, usage: { input_tokens: 1 } }, expected: legs },
    { payload: { executionLegs: legs, usage: { input_tokens: 1 } }, expected: legs },
    { payload: { execution_legs: legs, usage: { executionLegs: [{ model: 'nested-alias' }] } }, expected: legs },
    { payload: { execution_legs: [{ model: 'outer' }], usage: { execution_legs: legs } }, expected: legs },
    { payload: { execution_legs: [], executionLegs: legs }, expected: [] },
    { payload: { execution_legs: false, executionLegs: legs }, expected: legs },
  ])('preserves terminal evidence precedence: $payload', ({ payload, expected }) => {
    expect(completion(payload).usage?.execution_legs).toEqual(expected)
    expect(completion(payload).usage).not.toHaveProperty('executionLegs')
  })

  it.each([undefined, null, false, 42, 'model-a', { model: 'model-a' }])(
    'accepts missing or malformed execution legs without losing legacy usage: %j', value => {
      const projected = completion({ usage: { input_tokens: 12, ...(value === undefined ? {} : { execution_legs: value }) } })
      expect(projected.usage).toEqual({ input_tokens: 12 })
    },
  )

  it('keeps finite execution facts and ignores malformed entries without spreading provider fields', () => {
    const projected = completion({ usage: { executionLegs: [
      null, false, 12, 'bad', [],
      { model: 12, index: -1 },
      { model: 'model-a', provider: 'test-provider', planId: 'route-A', executionId: 'call-A', callKind: 'primary', reason: 'retry', index: 0, privateProviderBody: 'must not escape' },
    ] } })
    expect(projected.usage?.execution_legs).toEqual([
      {},
      { model: 'model-a', provider: 'test-provider', plan_id: 'route-A', execution_id: 'call-A', call_kind: 'primary', reason: 'retry', index: 0 },
    ])
  })
})
