// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref } from 'vue'
import RouterFxStrip from '@/components/chat/RouterFxStrip.vue'
import { useChatRenderedMessages } from '@/composables/chat/useChatRenderedMessages'
import { useChatRouterDecisionRuntime } from '@/composables/chat/useChatRouterDecisionRuntime'
import i18n from '@/i18n'
import type { ChatMessage } from '@/types/chat'

const turnId = 'synthetic-replay-turn'
const selected = 'deepseek-v4-pro'
const firstModel = 'kimi-k2.7-code'
const replayModel = 'deepseek-v4-pro-0813'

function setup() {
  const messages = ref<ChatMessage[]>([
    { role: 'user', text: 'Synthetic request', ts: 1, turnId },
  ])
  const isStreaming = ref(true)
  const sessionKey = ref('synthetic-replay-session')
  const runtime = useChatRouterDecisionRuntime({
    messages, sessionKey, isStreaming, autoScroll: ref(false),
    activeTurnUsesEnsemble: ref(false), activeTurnId: ref(turnId),
    streamBubble: ref(true), streamHasVisibleOutput: ref(false),
    startStreaming: vi.fn(), resetStreamForRouterReplay: vi.fn(),
    resetStreamIdleTimer: vi.fn(), setStreamActivity: vi.fn(), scrollToBottom: vi.fn(),
  })
  const { renderedMessages } = useChatRenderedMessages({
    messages, sessionKey, isStreaming, routerSlots: ref(['c1', 'c2']),
    routerModels: ref({ c1: selected, c2: firstModel }), routerTierConfigs: ref({}),
    routerVisualEffectsEnabled: ref(true), routerVisualMode: ref('real_candidates'),
    renderMarkdown: text => text, stripGeneratedArtifactMarkers: text => text,
    stripTimePrefix: text => text, isSubagentCompletionMessage: () => false,
  })
  const cards = () => renderedMessages.value.filter(message => message.isRouterStrip)
  const decision = (seq: number, model: string) => {
    runtime.queueRouterDecision({
      turn_id: turnId, stream_seq: seq, tier: 'c1', model: selected, source: 'classifier',
    })
    runtime.bindRouterDecisionToModelCall('1.0', 1, turnId)
    runtime.updateRouterExecutionModel(model, turnId)
  }
  return { messages, isStreaming, runtime, cards, decision }
}

describe('replayed router call numbering', () => {
  beforeEach(() => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({
      matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })))
  })
  afterEach(() => vi.unstubAllGlobals())

  it.each([false, true])('keeps both 1.0 attempts and settles only the receipt owner (batched: %s)', async batched => {
    const view = setup()
    const element = document.createElement('div')
    const app = createApp({
      render: () => h('div', view.cards().map(message => h(RouterFxStrip, {
        key: message.routerTurnKey, message,
      }))),
    })
    app.use(i18n)
    app.mount(element)
    try {
      view.decision(10, firstModel)
      if (!batched) await nextTick()
      const firstElement = element.querySelector('.router-fx')
      view.runtime.handleRouterControlReplay({ turn_id: turnId, stream_seq: 20 })
      view.decision(21, replayModel)
      await nextTick()

      expect(view.cards().map(card => [card.routerExecutionModel, card.routerSettled])).toEqual([
        [firstModel, true], [replayModel, false],
      ])
      expect(new Set(view.cards().map(card => card.routerTurnKey)).size).toBe(2)
      expect(view.cards().map(card => card.routerSelectedModel)).toEqual([selected, selected])
      const strips = Array.from(element.querySelectorAll('.router-fx'))
      if (!batched) expect(strips[0]).toBe(firstElement)
      expect(strips.map(strip => strip.getAttribute('aria-label'))).toEqual([
        `Executed by ${firstModel}`, `Currently executing ${replayModel}`,
      ])
      expect(strips.map(strip => strip.querySelector('.router-fx-sr-only')?.textContent)).toEqual([
        `Executed by ${firstModel}`, `Currently executing ${replayModel}`,
      ])
      expect(strips[0]?.querySelector('.router-fx-cell.win .nm')?.getAttribute('aria-label')).toBe(firstModel)
      expect(strips[1]?.querySelector('.router-fx-cell.win')).toBeNull()

      view.messages.value.push({
        role: 'assistant', text: 'Synthetic completion', ts: 3, turnId, messageId: 'synthetic-answer',
        usage: {
          router_model_call_id: '1.0', router_iteration: 1,
          route_plan: { tier: 'c1', model: selected, source: 'classifier', routing_applied: true },
          execution_legs: [{ kind: 'provider_fallback', model: 'terminal-physical-model' }],
        },
      })
      view.isStreaming.value = false
      await nextTick()
      expect(view.cards().map(card => [card.routerExecutionModel, card.routerSettled])).toEqual([
        [firstModel, true], ['terminal-physical-model', true],
      ])
      const settled = Array.from(element.querySelectorAll('.router-fx'))
      expect(settled).toEqual(strips)
      expect(settled.map(strip => strip.getAttribute('aria-label'))).toEqual([
        `Executed by ${firstModel}`, 'Executed by terminal-physical-model',
      ])
      expect(settled[1]?.querySelector('.router-fx-sr-only')?.textContent)
        .toBe('Executed by terminal-physical-model')
    } finally {
      app.unmount()
    }
  })

  it('keeps reused call ids separate during repeated snapshot replay', () => {
    const view = setup()
    const replay = () => {
      view.runtime.resetRouterReplayCursor()
      view.decision(10, firstModel)
      view.runtime.handleRouterControlReplay({ turn_id: turnId }, 20)
      view.decision(21, replayModel)
    }
    replay()
    const keys = view.cards().map(card => card.routerTurnKey)
    replay()
    replay()
    expect(view.cards().map(card => card.routerTurnKey)).toEqual(keys)
    expect(new Set(keys).size).toBe(2)
    expect(view.cards().map(card => [card.routerExecutionModel, card.routerSettled])).toEqual([
      [firstModel, true], [replayModel, false],
    ])
  })
})
