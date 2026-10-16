// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref } from 'vue'
import RouterFxStrip from '@/components/chat/RouterFxStrip.vue'
import { useChatFeatureToggles } from './useChatFeatureToggles'
import { useChatRenderedMessages } from './useChatRenderedMessages'
import { useChatRouterDecisionRuntime } from './useChatRouterDecisionRuntime'
import i18n from '@/i18n'
import type { AppSettings, SettingsObject } from '@/modules/appSettings'
import type { ModelRouting, ModelRoutingSnapshot } from '@/modules/providerConfiguration'
import type { ChatMessage } from '@/types/chat'
import type { RouterTierSnapshotV1 } from '@/utils/chat/routerTierSnapshot'

const previousModels = ['zulu-before', 'delta-before', 'gamma-before', 'beta-before']
const savedModels = ['zulu-after', 'delta-after', 'gamma-after', 'beta-after']
const imageTurn = 'synthetic-generated-image-turn'
const followupTurn = 'synthetic-followup-turn'

function routerConfig(models: string[]): SettingsObject {
  return {
    squilla_router: {
      enabled: true,
      rollout_phase: 'full',
      visual_mode: 'real_candidates',
      tiers: Object.fromEntries([3, 1, 0, 2].map(index => [
        `c${index}`, { model: models[index]!, supports_image: false },
      ])),
    },
  }
}

function snapshot(models: string[], requestKind: 'text' | 'image'): RouterTierSnapshotV1 {
  return {
    version: 1,
    request_kind: requestKind,
    tiers: models.map((model, index) => ({
      tier: `c${index}`, model, execution_kind: 'single_model',
    })),
  }
}

function imageHistory(): ChatMessage[] {
  return [
    { role: 'user', text: 'Create a geometric illustration.', ts: 1, turnId: imageTurn },
    {
      role: 'assistant', text: 'Illustration ready.', ts: 2, turnId: imageTurn,
      restoredFromHistory: true,
      tool_calls: [
        { type: 'tool_use', tool_use_id: 'synthetic-generate', name: 'image_generate',
          input: { prompt: 'A blue circle.', filename: 'circle.png' } },
        { type: 'tool_result', tool_use_id: 'synthetic-generate', name: 'image_generate',
          result: { status: 'ok', path: 'circle.png', mime_type: 'image/png' } },
      ],
      usage: {
        route_plan: {
          tier: 'c1', model: previousModels[1], source: 'classifier', routing_applied: true,
          router_tier_snapshot: snapshot(previousModels, 'text'),
        },
      },
    },
  ]
}

describe('router cards after saving settings with generated image history', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    localStorage.clear()
    vi.stubGlobal('matchMedia', vi.fn(() => ({
      matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })))
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  it.each([true, false])('shows saved C0–C3 candidates through streaming and history (snapshot: %s)', async hasSnapshot => {
    let config = routerConfig(previousModels)
    let onRoutingChanged: ((value: ModelRoutingSnapshot) => void) | undefined
    const readAll = vi.fn(async () => config)
    const appSettings: AppSettings = {
      readAll,
      read: vi.fn(async () => null),
      readEffective: vi.fn(async () => ({ fields: {} })),
      patch: vi.fn(async () => ({})),
      patchSafe: vi.fn(async () => ({})),
      merge: vi.fn(async () => ({})),
      setTelemetryConsent: vi.fn(async (scope, enabled) => ({
        scope, enabled, noticeVersion: null, consentedAtUtc: null,
      })),
    }
    const modelRouting: ModelRouting = {
      get: vi.fn(async (): Promise<ModelRoutingSnapshot> => ({ mode: 'router' })),
      setRouting: vi.fn(async mode => ({ mode })),
      subscribeChanged(listener) {
        onRoutingChanged = listener
        return { close() { onRoutingChanged = undefined } }
      },
    }
    const features = useChatFeatureToggles({
      appSettings, modelRouting,
      setGlobalElevatedMode: vi.fn(), loadCurrentSessionUsage: vi.fn(),
    })
    await features.loadFeatureToggles()
    const cleanupRefresh = features.bindFeatureRefresh()
    const messages = ref(imageHistory())
    const sessionKey = ref('agent:main:webchat:synthetic-settings')
    const isStreaming = ref(true)
    const runtime = useChatRouterDecisionRuntime({
      messages, sessionKey, isStreaming, autoScroll: ref(false),
      activeTurnUsesEnsemble: ref(false), activeTurnId: ref(followupTurn),
      streamBubble: ref(true), streamHasVisibleOutput: ref(false),
      startStreaming: vi.fn(), resetStreamForRouterReplay: vi.fn(),
      resetStreamIdleTimer: vi.fn(), setStreamActivity: vi.fn(), scrollToBottom: vi.fn(),
    })
    const { renderedMessages } = useChatRenderedMessages({
      messages, sessionKey, isStreaming,
      routerSlots: features.routerSlots,
      routerModels: features.routerModels,
      routerTierConfigs: features.routerTierConfigs,
      routerVisualMode: features.routerVisualMode,
      routerVisualEffectsEnabled: ref(true),
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
    const assertCards = async () => {
      await nextTick()
      expect(cards().map(card => card.gridCells?.map(cell => cell.model)))
        .toEqual([previousModels, savedModels])
      const strips = Array.from(element.querySelectorAll('.router-fx'))
      expect(strips.map(strip => Array.from(strip.querySelectorAll('.nm'))
        .map(name => name.getAttribute('aria-label')))).toEqual([previousModels, savedModels])
    }
    try {
      // The mounted chat stays alive while settings save a different candidate pool.
      config = routerConfig(savedModels)
      onRoutingChanged!({ mode: 'router', source: 'onboarding.router.configure' })
      messages.value.push({
        role: 'user', text: 'Describe the composition.', ts: 3, turnId: followupTurn,
      })
      const route = {
        tier: 'c1', model: savedModels[1]!, source: 'image_route', routing_applied: true,
        ...(hasSnapshot ? { router_tier_snapshot: snapshot(savedModels, 'image') } : {}),
      }
      runtime.queueRouterDecision({ ...route, turn_id: followupTurn, stream_seq: 1 })
      runtime.bindRouterDecisionToModelCall('1.0', 1, followupTurn)
      runtime.updateRouterExecutionModel(savedModels[1]!, followupTurn)
      if (hasSnapshot) await assertCards()

      await vi.advanceTimersByTimeAsync(120)
      expect(readAll).toHaveBeenCalledTimes(2)
      expect(features.routerModels.value).toEqual(Object.fromEntries(
        savedModels.map((model, index) => [`c${index}`, model]),
      ))
      await assertCards()

      const answer: ChatMessage = {
        role: 'assistant', text: 'The circle is centered.', ts: 4, turnId: followupTurn,
        messageId: 'synthetic-followup-answer',
        usage: {
          router_model_call_id: '1.0', router_iteration: 1,
          image_route_reason: 'history_context', route_plan: route,
          execution_legs: [{ kind: 'primary', model: savedModels[1] }],
        },
      }
      messages.value.push(answer)
      isStreaming.value = false
      await assertCards()
      expect(cards()[1]?.routerSettled).toBe(true)

      messages.value = [...imageHistory(), messages.value.find(message => message.role === 'user'
        && message.turnId === followupTurn)!, { ...answer, restoredFromHistory: true }]
      await assertCards()
      expect(cards().every(card => card.routerStatic)).toBe(true)
    } finally {
      app.unmount()
      cleanupRefresh()
    }
  })
})
