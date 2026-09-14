// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref, type App } from 'vue'
import i18n, { loadLocaleMessages } from '@/i18n'
import { projectConversationContent } from '@/adapters/gateway/conversationContentV4'
import { requestV4SessionHistory } from '@/adapters/gateway/sessionHistoryV4'
import AssistantMessage from '@/components/chat/AssistantMessage.vue'
import RouterFxStrip from '@/components/chat/RouterFxStrip.vue'
import type { ChatMessage } from '@/types/chat'
import { useChatRenderedMessages } from './useChatRenderedMessages'

const apps: App[] = []

beforeEach(async () => {
  await loadLocaleMessages('zh-Hans')
  i18n.global.locale.value = 'zh-Hans'
  vi.stubGlobal('matchMedia', vi.fn((media: string) => ({
    matches: media === '(prefers-reduced-motion: reduce)',
    media,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })))
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

function ordinaryUsage(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    model: 'test/fast', routed_model: 'test/fast', routed_tier: 'c0',
    routing_source: 'squilla_router', router_model_call_id: '1.0',
    input_tokens: 10, output_tokens: 5, cost_usd: 0.001,
    decision_id: 'test-decision',
    model_usage_breakdown: [{
      role: 'member', provider: 'test', model: 'test/fast',
      input_tokens: 10, output_tokens: 5, cost_usd: 0.001, request_count: 4,
    }],
    ...overrides,
  }
}

function assistant(usage: Record<string, unknown>, overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    role: 'assistant', text: 'Test answer.', ts: 3, turnId: 'turn-one',
    messageId: 'test-answer', usage, ...overrides,
  }
}

function render(messages: ChatMessage[], c3Enabled = true, fixed = false) {
  const isStreaming = ref(false)
  const source = ref(messages)
  const api = useChatRenderedMessages({
    messages: source, sessionKey: ref('test-session'), isStreaming,
    routerSlots: ref(['c0', 'c1', 'c2', 'c3']), routerModels: ref({}),
    routerTierConfigs: ref({
      c0: { model: 'test/fast', imageOnly: false },
      c1: { model: 'test/medium', imageOnly: false },
      c2: { model: 'test/strong', imageOnly: false },
      c3: { model: 'test/aggregator', imageOnly: false, ensembleEnabled: c3Enabled },
    }),
    modelRoutingMode: ref(fixed ? 'off' : 'squilla_router'),
    routerVisualEffectsEnabled: ref(true), routerVisualMode: ref('real_candidates'),
    renderMarkdown: text => text, stripGeneratedArtifactMarkers: text => text,
    stripTimePrefix: text => text, isSubagentCompletionMessage: () => false,
  })
  return {
    ...api, messages: source, isStreaming,
    strips: () => api.renderedMessages.value.filter(message => message.isRouterStrip),
    answer: () => api.renderedMessages.value.find(message => message.displayRole === 'assistant')!,
  }
}

async function mountConversation(api: ReturnType<typeof render>) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp({
    render: () => h('div', api.renderedMessages.value.map((message, index) => {
      if (message.isRouterStrip) return h(RouterFxStrip, { message, key: index })
      if (message.displayRole !== 'assistant') return null
      return h(AssistantMessage, {
        message, index, key: index, shareMode: false, shareSelected: false,
        shareMessageId: message.messageId || 'test-answer',
        renderMarkdown: (text: string) => text, fmtTok: (value: number) => String(value),
        toolCallGroups: () => [], isToolGroupOpen: () => false, isToolItemOpen: () => false,
        toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '',
        copyMessage: async () => true,
      })
    })),
  })
  app.use(i18n)
  apps.push(app)
  app.mount(el)
  await nextTick()
  return el
}

async function projectedAnswer(usage: Record<string, unknown>, restored: boolean) {
  if (!restored) {
    const content = projectConversationContent({ text: 'Test answer.', usage }, 'turn-completed')
    return assistant(content.usage!)
  }
  const page = await requestV4SessionHistory({
    async request<T>(): Promise<T> {
      return {
        messages: [{ id: 'test-answer', role: 'assistant', text: 'Test answer.', turn_usage: usage }],
        has_more: false, oldest_cursor: null, newest_cursor: null,
        history_scope: 'complete', loaded_count: 1, page_size: 100,
        canonical_available: true, canonical_complete: true,
        compaction_summaries: [], turn_outcomes: [],
      } as T
    },
  }, 'test-session', { direction: 'latest', limit: 100, signal: new AbortController().signal }, {
    includeSummaries: true, policy: { concurrentHistoryReads: () => true },
    contractError: message => new Error(message),
  })
  return assistant(page.messages[0]!.usage!, { restoredFromHistory: true })
}

describe('ordinary usage is not ensemble evidence', () => {
  it.each([
    ['one model with repeated requests', [{ role: 'member', model: 'test/fast', request_count: 4 }]],
    ['legacy row without a role', [{ model: 'test/fast' }]],
    ['provider fallback', [{ role: 'member', model: 'test/primary' }, { role: 'member', model: 'test/fallback' }]],
    ['image auxiliary call', [{ role: 'member', model: 'test/fast' }, { role: 'member', model: 'test/vision' }]],
    ['legacy child usage', [{ role: 'subagent', model: 'test/child' }]],
    ['child fusion usage', [
      { role: 'proposer', model: 'test/child-proposer', profile: 'child-fusion' },
      { role: 'aggregator', model: 'test/child-aggregator', profile: 'child-fusion' },
    ]],
  ])('keeps %s as ordinary billing', (_, rows) => {
    const usage = ordinaryUsage({ model_usage_breakdown: rows })
    const api = render([assistant(usage)])
    expect(api.strips()).toHaveLength(1)
    expect(api.strips()[0]?.routerPanel).toBe('real-candidates')
    expect(api.strips()[0]?.ensemble).toBeUndefined()
    expect(api.answer().meta).toMatchObject({ model: 'test/fast', input: 10, output: 5, costUsd: 0.001 })
    expect(api.answer().meta?.ensemble).toBeUndefined()
    expect(usage.model_usage_breakdown).toEqual(rows)
  })

  describe.each(['ensemble_trace', 'ensembleTrace'])('%s', traceField => {
    it.each([undefined, null, {}, [], 'fusion', { profile: 1 }, { mode: true }, { profile: {} }, { profile: ' \t', mode: '' }])(
      'rejects missing or malformed identity %j', trace => {
        const api = render([assistant(ordinaryUsage({ [traceField]: trace }))])
        expect(api.answer().meta?.ensemble).toBeUndefined()
        expect(api.strips()[0]?.routerPanel).toBe('real-candidates')
      },
    )

    it.each([{ profile: 'legacy-fusion' }, { mode: 'b5_fusion' }])('accepts explicit identity %j', identity => {
      const api = render([assistant(ordinaryUsage({
        model_usage_breakdown: undefined,
        modelUsageBreakdown: [{ role: 'fallback_single', provider: 'test', model: 'test/fixed' }],
        [traceField]: { ...identity, total_candidates: 1, fallback_used: true, fallback_reason: 'quorum unavailable' },
      }))])
      expect(api.answer().meta?.ensemble).toMatchObject({
        totalCandidates: 1, fallbackUsed: true, fallbackReason: 'quorum unavailable',
        models: [{ role: 'fallback', model: 'test/fixed' }],
      })
      expect(api.strips()[0]?.routerPanel).toBe('router-ensemble-sequence')
    })
  })

  it.each([
    [false, true], [true, true], [false, false], [true, false],
  ])('renders ordinary completion/history (%s) with C3 fusion enabled=%s', async (restored, c3Enabled) => {
    const usage = ordinaryUsage()
    const message = await projectedAnswer(usage, restored)
    expect(message.usage?.model_usage_breakdown).toEqual(usage.model_usage_breakdown)
    const api = render([message], c3Enabled)
    const el = await mountConversation(api)
    expect(el.querySelector('.router-fx-cell.win')?.textContent).toContain('fast')
    expect(el.querySelector('[data-testid="router-ensemble-stage"]')).toBeNull()
    expect(el.querySelector('[data-testid="router-ensemble-handoff"]')).toBeNull()
    expect(api.answer().meta?.ensemble).toBeUndefined()

    el.querySelector<HTMLButtonElement>('.msg-meta__more-btn')!.click()
    await nextTick()
    const details = el.querySelector('.msg-meta-popover')!.textContent
    expect(details).toContain('fast')
    expect(details).toContain('$0.001')
    expect(details).toContain('↑10 ↓5')
    expect(el.querySelector('.msg-action--vote')?.getAttribute('title')).toBe(i18n.global.t('chat.routeFeedback.up'))
    expect(el.querySelector('.msg-action--vote')?.getAttribute('title')).not.toBe(i18n.global.t('chat.routeFeedback.upEnsemble'))
  })

  it.each([false, true])('does not invent a strip in fixed mode (history=%s)', async restored => {
    const message = await projectedAnswer(ordinaryUsage({ routing_source: 'none' }), restored)
    const api = render([message], false, true)
    const el = await mountConversation(api)
    expect(api.strips()).toHaveLength(0)
    expect(api.answer().meta?.ensemble).toBeUndefined()
    expect(el.querySelector('[data-testid="router-ensemble-stage"]')).toBeNull()
  })
})

function progress(callId: string | undefined = '1.0'): ChatMessage {
  return {
    role: 'router', text: '', ts: 2, turnId: 'turn-one', messageId: 'test-router',
    routerModelCallId: callId, routerSettled: false,
    routerDecision: { tier: 'c3', model: 'test/aggregator', source: 'squilla_router' },
    ensemble: {
      profile: 'test-fusion', modelCount: 1, totalCandidates: 1, requestCount: 1,
      fallbackUsed: true, fallbackReason: 'aggregator failed', costUsd: 0, savedUsd: 0, savedPct: 0,
      models: [{
        role: 'proposer', label: 'proposer', provider: 'test', model: 'test/proposer',
        modelShort: 'proposer', input: 0, output: 0, costUsd: 0, status: 'done',
      }],
    },
  }
}

describe('trace-less ensemble settlement', () => {
  it('retains only the matching progress and waits until the turn finishes', async () => {
    const api = render([progress()])
    api.isStreaming.value = true
    const original = api.strips()[0]!
    const el = await mountConversation(api)
    api.messages.value.push(assistant(ordinaryUsage({ routed_tier: 'c3' })))
    await nextTick()

    expect(api.strips()).toHaveLength(1)
    expect(api.strips()[0]?.ensemble).toEqual(original.ensemble)
    expect(api.strips()[0]?.routerTurnKey).toBe(original.routerTurnKey)
    expect(api.strips()[0]?.routerSettled).toBe(false)
    expect(api.answer().meta?.ensemble).toBeUndefined()
    expect(el.textContent).not.toContain('已融合')

    api.isStreaming.value = false
    await nextTick()
    expect(api.strips()[0]?.routerSettled).toBe(true)
    expect(api.strips()[0]?.ensemble?.models.map(row => row.model)).toEqual(['test/proposer'])
    expect(el.textContent).toContain('已融合 1 个候选')
  })

  it('prefers a completed trace to earlier progress', () => {
    const api = render([progress(), assistant(ordinaryUsage({
      routed_tier: 'c3',
      model_usage_breakdown: [{ role: 'aggregator', model: 'test/final' }],
      ensemble_trace: { profile: 'final-fusion', total_candidates: 1 },
    }))])
    expect(api.strips()[0]?.ensemble?.profile).toBe('final-fusion')
    expect(api.strips()[0]?.ensemble?.models.map(row => row.model)).toEqual(['test/final'])
  })

  it.each(['different call', 'missing receipt call', 'missing progress call', 'different turn'])(
    'does not inherit progress for %s', boundary => {
      const previous = progress()
      if (boundary === 'missing progress call') previous.routerModelCallId = undefined
      const usage = ordinaryUsage({
        router_model_call_id: boundary === 'different call' ? '2.0'
          : boundary === 'missing receipt call' ? undefined : '1.0',
      })
      const api = render([previous, assistant(usage, {
        turnId: boundary === 'different turn' ? 'turn-two' : 'turn-one',
      })])
      const strips = api.strips()
      const last = strips[strips.length - 1]!
      expect(last.routerPanel).toBe('real-candidates')
      expect(last.ensemble).toBeUndefined()
      expect(api.answer().meta?.ensemble).toBeUndefined()
    },
  )

  it('keeps explicit historical fusion identity without inventing missing members', () => {
    const api = render([assistant(ordinaryUsage(), {
      restoredFromHistory: true,
      turnOutcome: { turnId: 'turn-one', status: 'succeeded', acceptedRoutingMode: 'ensemble' },
    })])
    expect(api.strips()[0]?.routerPanel).toBe('llm-ensemble')
    expect(api.strips()[0]?.ensemble).toBeUndefined()
    expect(api.answer().meta?.ensemble).toBeUndefined()
  })
})
