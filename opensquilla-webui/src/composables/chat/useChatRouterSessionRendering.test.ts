// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, effectScope, h, nextTick, ref } from 'vue'
import RouterFxStrip from '@/components/chat/RouterFxStrip.vue'
import i18n from '@/i18n'
import type { ChatMessage } from '@/types/chat'
import { useChatRenderedMessages } from './useChatRenderedMessages'
import { useChatSessionViewCache } from './useChatSessionViewCache'

const models = ['first-model', 'second-model', 'third-model', 'fourth-model']
const user: ChatMessage = { role: 'user', text: 'Count the sides.', ts: 1, turnId: 'turn-a' }
function decision(sequence = 1, replayed = false): ChatMessage {
  return {
    role: 'router', text: '', ts: sequence + 1, turnId: 'turn-a',
    messageId: `router-a-${sequence}`, provenanceKind: 'router_decision',
    routerModelCallId: `${sequence}.0`, routerReplayed: replayed,
    routerDecision: {
      tier: `c${sequence}`, model: models[sequence]!, source: 'classifier',
      router_tier_snapshot: {
        version: 1, request_kind: 'text',
        tiers: models.map((model, index) => ({ tier: `c${index}`, model, execution_kind: 'single_model' })),
      },
    },
  }
}

describe('router presentation while returning to a running session', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.stubGlobal('matchMedia', vi.fn(() => ({
      matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })))
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('keeps the cached winner static through history and live restoration, then animates a new decision', async () => {
    const scope = effectScope()
    const sessionKey = ref('A')
    const messages = ref<ChatMessage[]>([user, decision()])
    const cache = scope.run(() => useChatSessionViewCache({
      messages, sessionKey, deliveryIdentity: ref('gateway:owner'), waitForLive: true,
    }))!
    const { renderedMessages } = useChatRenderedMessages({
      messages: cache.messages, sessionKey, isStreaming: ref(true),
      isPreview: cache.showingPreview,
      routerSlots: ref([]), routerModels: ref({}), routerTierConfigs: ref({}),
      routerVisualEffectsEnabled: ref(true), routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text, stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text, isSubagentCompletionMessage: () => false,
    })
    const cards = () => renderedMessages.value.filter(message => message.isRouterStrip)
    const element = document.createElement('div')
    const app = createApp({
      render: () => h('div', cards().map(message => h(RouterFxStrip, {
        key: message.routerTurnKey, message,
      }))),
    })
    app.use(i18n)
    app.mount(element)
    try {
      cache.historyInstalled('A')
      cache.liveInstalled('A')
      await nextTick()
      expect(element.querySelector('.router-fx')?.getAttribute('data-phase')).toBe('scanning')
      await vi.advanceTimersByTimeAsync(600)
      expect(element.querySelector('.router-fx')?.getAttribute('data-phase')).toBe('locked')

      sessionKey.value = 'B'
      messages.value = []
      await nextTick()
      cache.historyInstalled('B')
      cache.liveInstalled('B')
      sessionKey.value = 'A'
      await nextTick()
      const cachedStrip = element.querySelector('.router-fx')
      expect(cache.showingPreview.value).toBe(true)
      expect(cachedStrip?.getAttribute('data-phase')).toBe('static')
      expect(cachedStrip?.querySelector('.router-fx-cell.win .nm')?.getAttribute('aria-label')).toBe(models[1])
      expect(cachedStrip?.querySelector('.router-fx-sr-only')?.textContent).toBe('')
      // The cached presentation must not rewrite the actual runtime or claim completion.
      expect(messages.value).toEqual([])
      expect(cache.messages.value[1]?.restoredFromHistory).toBeUndefined()
      expect(cards()[0]?.routerSettled).toBe(false)

      messages.value = [user]
      cache.historyInstalled('A')
      await nextTick()
      expect(element.querySelector('.router-fx')).toBe(cachedStrip)
      messages.value.push(decision(1, true))
      cache.liveInstalled('A')
      await nextTick()
      expect(cache.showingPreview.value).toBe(false)
      expect(element.querySelector('.router-fx')).toBe(cachedStrip)
      expect(cachedStrip?.getAttribute('data-phase')).toBe('static')
      expect(cachedStrip?.querySelector('.router-fx-selector')).toBeNull()

      messages.value.push(decision(2))
      await nextTick()
      const strips = [...element.querySelectorAll('.router-fx')]
      expect(strips).toHaveLength(2)
      expect(strips[0]).toBe(cachedStrip)
      expect(strips[0]?.getAttribute('data-phase')).toBe('static')
      expect(strips[1]?.getAttribute('data-phase')).toBe('scanning')
      await vi.advanceTimersByTimeAsync(600)
      expect(strips[1]?.getAttribute('data-phase')).toBe('locked')
      expect(strips[1]?.querySelector('.router-fx-cell.win .nm')?.getAttribute('aria-label')).toBe(models[2])
    } finally {
      app.unmount()
      scope.stop()
    }
  })
})
