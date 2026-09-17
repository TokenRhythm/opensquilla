// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { createApp, nextTick, ref } from 'vue'
import RouterFxStrip from '@/components/chat/RouterFxStrip.vue'
import { useChatRenderedMessages } from '@/composables/chat/useChatRenderedMessages'
import i18n from '@/i18n'
import type { ChatMessage, ChatUsagePayload } from '@/types/chat'
import { projectConversationEvent } from './conversationContentV4'
import { decodeConversationEvent } from './conversationEventsV4'

const selectedModel = 'logical-c3-model'
const baselineModel = 'unused-baseline-model'
const baselineLeg = { kind: 'primary', provider: 'test-provider', model: baselineModel }
const routePlan = {
  tier: 'c3', model: selectedModel, source: 'squilla_router', routing_applied: true,
  router_tier_snapshot: {
    version: 1, request_kind: 'text',
    tiers: [
      { tier: 'c0', model: 'fast-model', execution_kind: 'single_model' },
      { tier: 'c3', model: selectedModel, execution_kind: 'ensemble' },
    ],
  },
}

function ensembleTrace(role = 'aggregator', model = 'actual-aggregator') {
  return {
    profile: 'synthetic', mode: 'ensemble', final_request_role: role,
    final_request: {
      role, request_started: true, execution: { provider: 'test-provider', model },
      usage: { model: 'provider-reported-alias' },
    },
  }
}

function projectedStrip(
  usage: ChatUsagePayload,
  options: { history?: boolean; liveModel?: string } = {},
) {
  const event = projectConversationEvent(decodeConversationEvent('session.event.done', {
    key: 'synthetic-session', task_id: 'turn-A', usage: {
      routed_tier: 'c3', routed_model: selectedModel, routing_source: 'squilla_router',
      route_plan: routePlan, ...usage,
    },
  }))
  if (event.semanticKind !== 'turn-completed') throw new Error('Missing turn completion')
  const { renderedMessages } = useChatRenderedMessages({
    messages: ref<ChatMessage[]>([
      { role: 'user', text: 'Synthetic request', ts: 1, turnId: 'turn-A' },
      {
        role: 'assistant', text: 'Synthetic result', ts: 2, turnId: 'turn-A',
        usage: event.payload.usage, restoredFromHistory: options.history,
        routerExecutionModel: options.liveModel,
      },
    ]),
    isStreaming: ref(false), sessionKey: ref('synthetic-session'), routerSlots: ref(['c0', 'c3']),
    routerModels: ref({ c0: 'fast-model', c3: selectedModel }), routerTierConfigs: ref({}),
    routerVisualEffectsEnabled: ref(true), routerVisualMode: ref('real_candidates'),
    renderMarkdown: text => text, stripGeneratedArtifactMarkers: text => text,
    stripTimePrefix: text => text, isSubagentCompletionMessage: () => false,
  })
  const strip = renderedMessages.value.find(message => message.isRouterStrip)
  if (!strip) throw new Error('Missing router strip')
  return strip
}

describe('ensemble physical execution evidence', () => {
  it.each(['aggregator', 'fixed_aggregator', 'fixed_direct'])(
    'uses the started %s execution over the legacy wrapper baseline and reported alias', async role => {
      const usage = { execution_legs: [baselineLeg], ensemble_trace: ensembleTrace(role) }
      for (const history of [false, true]) {
        const strip = projectedStrip(usage, { history })
        expect(strip).toMatchObject({
          routerPanel: 'router-ensemble-sequence', routerSelectedModel: selectedModel,
          routerExecutionModel: 'actual-aggregator', routerStatic: history,
        })
        const element = document.createElement('div')
        const app = createApp(RouterFxStrip, { message: strip })
        app.use(i18n)
        app.mount(element)
        try {
          await nextTick()
          expect(element.querySelector('[data-testid="router-execution-model"]')?.textContent)
            .toBe('Executed by actual-aggregator')
          expect(element.querySelector('.router-fx')?.getAttribute('aria-label'))
            .toBe('Executed by actual-aggregator')
          expect(element.querySelector('.router-fx-cell.win')).toBeNull()
        } finally {
          app.unmount()
        }
      }
    },
  )

  it.each([
    undefined,
    { role: 'aggregator', execution: { model: 'planned-model' } },
    { role: 'aggregator', request_started: false, execution: { model: 'planned-model' } },
    { role: 'aggregator', request_started: 'true', execution: { model: 'planned-model' } },
    { role: 'proposer', request_started: true, execution: { model: 'candidate-model' } },
    { role: 'aggregator', request_started: true, execution: { model: 42 } },
    { role: 'aggregator', request_started: true, execution: { model: ' ' } },
  ])('does not treat a plan or malformed final request as dispatched: %j', finalRequest => {
    const usage = {
      execution_legs: [baselineLeg],
      ensemble_trace: { profile: 'legacy', ...(finalRequest ? { final_request: finalRequest } : {}) },
    }
    expect(projectedStrip(usage).routerExecutionModel).toBeUndefined()
    expect(projectedStrip(usage, { liveModel: 'observed-physical-model' }).routerExecutionModel)
      .toBe('observed-physical-model')
  })

  it('accepts the existing camel-case receipt containers', () => {
    expect(projectedStrip({ executionLegs: [baselineLeg], ensembleTrace: ensembleTrace() })
      .routerExecutionModel).toBe('actual-aggregator')
  })

  it('keeps a later single-model selector fallback ahead of an earlier ensemble trace', () => {
    expect(projectedStrip({
      execution_legs: [baselineLeg, { kind: 'provider_fallback', model: 'later-single-model' }],
      ensemble_trace: ensembleTrace(),
    }).routerExecutionModel).toBe('later-single-model')
  })

  it('keeps legacy trace-free receipts compatible without assuming the planned ensemble ran', () => {
    expect(projectedStrip({ execution_legs: [baselineLeg] }).routerExecutionModel).toBe(baselineModel)
    expect(projectedStrip({}).routerExecutionModel).toBeUndefined()
  })
})
