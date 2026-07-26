import { describe, expect, it } from 'vitest'
import { ref } from 'vue'

import { useChatRenderedMessages } from './useChatRenderedMessages'
import type { ChatMessage, ChatRouterTierConfig } from '@/types/chat'
import type { ModelRoutingMode } from '@/types/modelRouting'
import type { ChatPart, InterruptViewState } from '@/types/parts'

function renderedMessagesForRouterVisualMode(
  visualMode: 'real_candidates' | 'legacy_grid',
  modelRoutingMode: ModelRoutingMode = 'squilla_router',
) {
  const configs: Record<string, ChatRouterTierConfig> = {
    fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
    balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
    strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
  }
  const options = {
    messages: ref<ChatMessage[]>([]),
    sessionKey: ref('router-visual-test'),
    routerSlots: ref(['fast', 'balanced', 'strong']),
    routerModels: ref({}),
    routerTierConfigs: ref(configs),
    routerVisualEffectsEnabled: ref(true),
    routerVisualMode: ref(visualMode),
    renderMarkdown: (text: string) => text,
    stripGeneratedArtifactMarkers: (text: string) => text,
    stripTimePrefix: (text: string) => text,
    isSubagentCompletionMessage: () => false,
    modelRoutingMode: ref(modelRoutingMode),
  }
  return useChatRenderedMessages(options)
}

function renderedMessagesFor(
  messages: ChatMessage[],
  interruptState = ref<ReadonlyMap<string, InterruptViewState>>(new Map()),
  routerVisualEffectsEnabled = false,
) {
  return useChatRenderedMessages({
    messages: ref<ChatMessage[]>(messages),
    interruptState,
    sessionKey: ref('agent:main:webchat:test'),
    routerSlots: ref([]),
    routerModels: ref({}),
    routerTierConfigs: ref({}),
    routerVisualEffectsEnabled: ref(routerVisualEffectsEnabled),
    routerVisualMode: ref('real_candidates'),
    renderMarkdown: text => text,
    stripGeneratedArtifactMarkers: text => text,
    stripTimePrefix: text => text,
    isSubagentCompletionMessage: () => false,
  })
}

describe('useChatRenderedMessages router visual mode', () => {
  it('keeps real-candidates mode limited to callable router tiers', () => {
    const api = renderedMessagesForRouterVisualMode('real_candidates')

    const cells = api.routerDecisionCells({
      tier: 'balanced',
      model: 'anthropic/claude-sonnet-4.6',
    })

    expect(cells).toHaveLength(3)
    expect(cells.every(cell => cell.kind === 'real')).toBe(true)
  })

  it('renders legacy-grid mode as a 15-cell visual panel without moving the real winner', () => {
    const api = renderedMessagesForRouterVisualMode('legacy_grid')

    const cells = api.routerDecisionCells({
      tier: 'balanced',
      model: 'anthropic/claude-sonnet-4.6',
    })
    const winnerIdx = api.routerWinnerCellIndex(cells, 'balanced')

    expect(cells).toHaveLength(15)
    expect(cells.some(cell => cell.kind === 'decoy')).toBe(true)
    expect(cells.filter(cell => cell.kind === 'real')).toHaveLength(3)
    expect(cells.map(cell => cell.displayName)).toEqual(
      expect.arrayContaining([
        'gpt-5.5',
        'gemini-3.5-flash',
        'qwen3-coder-plus',
        'grok-4.3',
        'gpt-5.4-mini',
        'kimi-k2.6',
      ]),
    )
    expect(cells.map(cell => cell.displayName)).not.toEqual(
      expect.arrayContaining([
        'gpt-4.1-mini',
        'gpt-4o-mini',
        'o4-mini',
        'deepseek-chat',
        'mistral-medium',
        'grok-code-fast',
        'qwen3-coder',
      ]),
    )
    expect(winnerIdx).toBeGreaterThanOrEqual(0)
    expect(cells[winnerIdx].kind).toBe('real')
    expect(cells[winnerIdx].tiers).toContain('balanced')
  })

  it('keeps a restored single-model turn as the grid while ensemble mode is on', () => {
    // A restored history (squilla_router) turn must render as the normal candidate
    // grid even while the global LLM-ensemble toggle happens to be on — the active
    // mode only tags the live turn, never restored history.
    const withMessages = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hard question', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          restoredFromHistory: true,
          routerDecision: {
            tier: 'balanced',
            model: 'anthropic/claude-sonnet-4.6',
            source: 'squilla_router',
          },
        },
      ]),
      sessionKey: ref('router-ensemble-active-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
        strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('llm_ensemble'),
    })

    const strip = withMessages.renderedMessages.value.find(message => message.isRouterStrip)
    expect(strip?.routerPanel).toBe('real-candidates')
    expect((strip?.gridCells || []).length).toBeGreaterThan(1)
  })

  it('shows the ensemble strip for the live turn while ensemble mode is on', () => {
    // Live (non-history) squilla_router decision + active ensemble mode → the
    // ensemble panel immediately, not the tier grid.
    const withMessages = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hard question', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          routerDecision: {
            tier: 'balanced',
            model: 'anthropic/claude-sonnet-4.6',
            source: 'squilla_router',
          },
        },
      ]),
      sessionKey: ref('router-ensemble-live-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
        strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('llm_ensemble'),
    })

    const strip = withMessages.renderedMessages.value.find(message => message.isRouterStrip)
    expect(strip?.routerPanel).toBe('llm-ensemble')
    expect(strip?.gridCells || []).toHaveLength(0)
  })

  it('renders the ensemble strip when the decision source is ensemble (per-message)', () => {
    const withMessages = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hard question', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          routerDecision: {
            tier: 'balanced',
            model: 'anthropic/claude-sonnet-4.6',
            source: 'llm_ensemble',
          },
        },
      ]),
      sessionKey: ref('router-ensemble-source-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
        strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('off'),
    })

    const strip = withMessages.renderedMessages.value.find(message => message.isRouterStrip)
    expect(strip?.routerPanel).toBe('llm-ensemble')
    expect(strip?.gridCells || []).toHaveLength(0)
  })

  it('keeps the live ensemble strip while its own turn is still streaming', () => {
    // A tool-using ensemble turn emits its breakdown mid-turn; the strip (and any
    // open trace inspector) must survive until the whole turn settles.
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hello', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          routerDecision: {
            tier: 'balanced',
            model: 'anthropic/claude-sonnet-4.6',
            source: 'llm_ensemble',
          },
        },
        {
          role: 'assistant',
          text: 'Working on it…',
          ts: 2,
          messageId: 'assistant-ensemble-live',
          usage: {
            model: 'z-ai/glm-5.2',
            model_usage_breakdown: [
              { role: 'anchor', provider: 'openrouter', model: 'qwen/qwen3.7-plus' },
              { role: 'critic', provider: 'openrouter', model: 'z-ai/glm-5.2' },
            ],
            ensemble_trace: {
              profile: 'default',
              mode: 'router_dynamic',
              llm_request_count: 2,
              total_candidates: 3,
              fallback_used: false,
            },
          },
        },
      ]),
      sessionKey: ref('router-ensemble-live-stream-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
        strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('llm_ensemble'),
      isStreaming: ref(true),
    })

    const strip = api.renderedMessages.value.find(message => message.isRouterStrip)
    expect(strip).toBeTruthy()
    expect(strip?.routerPanel).toBe('llm-ensemble')
    expect(strip?.ensemble?.modelCount).toBe(2)
    expect(strip?.ensemble?.models.map(model => model.modelShort)).toEqual([
      'qwen3.7-plus',
      'glm-5.2',
    ])
  })

  it('keeps the ensemble strip as a settled trace panel once the assistant answer completes', () => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hello', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          routerDecision: {
            tier: 'balanced',
            model: 'anthropic/claude-sonnet-4.6',
            source: 'llm_ensemble',
          },
        },
        {
          role: 'assistant',
          text: 'Hi there.',
          ts: 2,
          messageId: 'assistant-ensemble',
          usage: {
            model: 'z-ai/glm-5.2',
            model_usage_breakdown: [
              { role: 'anchor', provider: 'openrouter', model: 'qwen/qwen3.7-plus' },
              { role: 'critic', provider: 'openrouter', model: 'z-ai/glm-5.2' },
            ],
            ensemble_trace: {
              profile: 'default',
              mode: 'router_dynamic',
              llm_request_count: 2,
              total_candidates: 3,
              fallback_used: false,
            },
          },
        },
      ]),
      sessionKey: ref('router-ensemble-completed-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
        strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('llm_ensemble'),
    })

    const rendered = api.renderedMessages.value
    const assistantIndex = rendered.findIndex(message => message.displayRole === 'assistant')
    const stripIndex = rendered.findIndex(message => message.isRouterStrip)
    const strip = rendered[stripIndex]

    expect(assistantIndex).toBeGreaterThan(-1)
    expect(stripIndex).toBeGreaterThan(-1)
    expect(stripIndex).toBeLessThan(assistantIndex)
    expect(strip?.routerPanel).toBe('llm-ensemble')
    expect(strip?.routerSettled).toBe(true)
    expect(strip?.ensemble?.modelCount).toBe(2)
    expect(strip?.ensemble?.models.map(model => model.modelShort)).toEqual([
      'qwen3.7-plus',
      'glm-5.2',
    ])
    expect(rendered[assistantIndex]?.meta?.ensemble?.modelCount).toBe(2)
  })

  it('replaces an empty live handoff strip with completed assistant usage trace', () => {
    const api = useChatRenderedMessages({
      messages: ref<ChatMessage[]>([
        { role: 'user', text: 'hello', ts: 0 },
        {
          role: 'router',
          text: '',
          ts: 1,
          provenanceKind: 'router_decision',
          routerState: 'handoff',
          routerDecision: {
            tier: 'c1',
            model: '',
            source: 'llm_ensemble',
          },
        },
        {
          role: 'assistant',
          text: 'Done.',
          ts: 2,
          messageId: 'assistant-ensemble-handoff-complete',
          usage: {
            model: 'z-ai/glm-5.2',
            model_usage_breakdown: [
              { role: 'proposer', provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' },
              { role: 'aggregator', provider: 'openrouter', model: 'z-ai/glm-5.2' },
            ],
            ensemble_trace: {
              profile: 'static_openrouter_b5',
              llm_request_count: 5,
              total_candidates: 5,
              fallback_used: false,
            },
          },
        },
      ]),
      sessionKey: ref('router-ensemble-handoff-complete-test'),
      routerSlots: ref(['fast', 'balanced', 'strong']),
      routerModels: ref({}),
      routerTierConfigs: ref({
        fast: { model: 'openai/gpt-5.4-mini', supportsImage: false, imageOnly: false },
        balanced: { model: 'anthropic/claude-sonnet-4.6', supportsImage: false, imageOnly: false },
        strong: { model: 'openai/gpt-5.5', supportsImage: false, imageOnly: false },
      }),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('llm_ensemble'),
      isStreaming: ref(false),
    })

    const rendered = api.renderedMessages.value
    const strip = rendered.find(message => message.isRouterStrip)
    const assistantIndex = rendered.findIndex(message => message.displayRole === 'assistant')
    const stripIndex = rendered.findIndex(message => message.isRouterStrip)

    expect(stripIndex).toBeGreaterThan(-1)
    expect(assistantIndex).toBeGreaterThan(-1)
    expect(stripIndex).toBeLessThan(assistantIndex)
    expect(strip?.routerPanel).toBe('llm-ensemble')
    expect(strip?.routerSettled).toBe(true)
    expect(strip?.routerState).toBe('settled')
    expect(strip?.ensemble?.modelCount).toBe(2)
    expect(strip?.ensemble?.models.map(model => model.modelShort)).toEqual([
      'deepseek-v4-pro',
      'glm-5.2',
    ])
  })

  it('keeps one router render key when a live strip becomes a settled trace', () => {
    const messages = ref<ChatMessage[]>([
      {
        role: 'user',
        text: 'compare candidates',
        ts: 1,
        clientId: 'local-user-turn',
        messageId: 'server-user-turn',
      },
      {
        role: 'router',
        text: '',
        ts: 2,
        messageId: 'router-live-event',
        provenanceKind: 'router_decision',
        routerDecision: {
          tier: 'c1',
          model: 'qwen/qwen3.7-plus',
          source: 'llm_ensemble',
        },
      },
    ])
    const isStreaming = ref(true)
    const api = useChatRenderedMessages({
      messages,
      sessionKey: ref('router-stable-key-test'),
      routerSlots: ref([]),
      routerModels: ref({}),
      routerTierConfigs: ref({}),
      routerVisualEffectsEnabled: ref(true),
      routerVisualMode: ref('real_candidates'),
      renderMarkdown: text => text,
      stripGeneratedArtifactMarkers: text => text,
      stripTimePrefix: text => text,
      isSubagentCompletionMessage: () => false,
      modelRoutingMode: ref('llm_ensemble'),
      isStreaming,
    })

    const liveKey = api.renderedMessages.value.find(message => message.isRouterStrip)?.routerTurnKey

    messages.value.push({
      role: 'assistant',
      text: 'Settled answer.',
      ts: 3,
      messageId: 'assistant-settled',
      usage: {
        model_usage_breakdown: [
          { role: 'proposer', provider: 'openrouter', model: 'qwen/qwen3.7-plus' },
          { role: 'aggregator', provider: 'openrouter', model: 'z-ai/glm-5.2' },
        ],
        ensemble_trace: {
          profile: 'default',
          total_candidates: 1,
          llm_request_count: 2,
        },
      },
    })
    isStreaming.value = false

    const settledKey = api.renderedMessages.value.find(message => message.isRouterStrip)?.routerTurnKey
    expect(liveKey).toBe('router-turn:local-user-turn')
    expect(settledKey).toBe(liveKey)
  })
})

describe('useChatRenderedMessages per-turn usage', () => {
  it('marks partial assistant output when a following error terminates the turn', () => {
    const api = renderedMessagesFor([
      {
        role: 'user',
        text: 'run the task',
        ts: 1,
        messageId: 'user-1',
      },
      {
        role: 'assistant',
        text: 'Partial result',
        ts: 2,
        messageId: 'assistant-partial',
        tool_calls: [{
          id: 'read-1',
          name: 'read_file',
          input: { path: '/repo/file.ts' },
          result: 'content',
        }],
      },
      {
        role: 'error',
        text: 'Provider failed',
        ts: 3,
        messageId: 'error-1',
      },
    ])

    const assistant = api.renderedMessages.value.find(
      message => message.displayRole === 'assistant',
    )
    expect(assistant?.terminalFailure).toBe(true)
    expect(assistant?.text).toBe('Partial result')
  })

  it('marks partial assistant output failed after a durable terminal system row is restored', () => {
    const api = renderedMessagesFor([
      {
        role: 'user',
        text: 'run the task',
        ts: 1,
        messageId: 'user-1',
        restoredFromHistory: true,
      },
      {
        role: 'assistant',
        text: 'Partial result',
        ts: 2,
        messageId: 'assistant-partial',
        restoredFromHistory: true,
        turnId: 'turn-1',
      },
      {
        role: 'system',
        text: 'localized durable terminal detail',
        ts: 3,
        messageId: 'system-terminal-1',
        restoredFromHistory: true,
        turnId: 'turn-1',
      },
    ])

    const assistant = api.renderedMessages.value.find(
      message => message.displayRole === 'assistant',
    )
    expect(assistant?.terminalFailure).toBe(true)
  })

  it('does not treat ordinary local or provenance-tagged system rows as terminal failures', () => {
    const local = renderedMessagesFor([
      {
        role: 'user',
        text: 'run the task',
        ts: 1,
        messageId: 'user-1',
      },
      {
        role: 'assistant',
        text: 'Completed result',
        ts: 2,
        messageId: 'assistant-complete',
      },
      {
        role: 'system',
        text: 'ordinary local notice',
        ts: 3,
      },
    ])
    const durableCron = renderedMessagesFor([
      {
        role: 'user',
        text: 'run the task',
        ts: 1,
        messageId: 'user-2',
        restoredFromHistory: true,
      },
      {
        role: 'assistant',
        text: 'Completed result',
        ts: 2,
        messageId: 'assistant-complete-2',
        restoredFromHistory: true,
      },
      {
        role: 'system',
        text: 'ordinary durable notice',
        ts: 3,
        messageId: 'system-cron-1',
        restoredFromHistory: true,
        provenanceKind: 'cron',
      },
    ])

    const localAssistant = local.renderedMessages.value.find(
      message => message.displayRole === 'assistant',
    )
    const cronAssistant = durableCron.renderedMessages.value.find(
      message => message.displayRole === 'assistant',
    )
    expect(localAssistant?.terminalFailure).toBeUndefined()
    expect(cronAssistant?.terminalFailure).toBeUndefined()
  })

  it('does not treat an unprovenanced durable system row without matching turn identity as terminal', () => {
    const api = renderedMessagesFor([
      {
        role: 'user',
        text: 'run the task',
        ts: 1,
        messageId: 'user-3',
        restoredFromHistory: true,
        turnId: 'turn-3',
      },
      {
        role: 'assistant',
        text: 'Completed result',
        ts: 2,
        messageId: 'assistant-complete-3',
        restoredFromHistory: true,
        turnId: 'turn-3',
      },
      {
        role: 'system',
        text: 'ordinary injected durable notice',
        ts: 3,
        messageId: 'system-injected-1',
        restoredFromHistory: true,
      },
    ])

    const assistant = api.renderedMessages.value.find(
      message => message.displayRole === 'assistant',
    )
    expect(assistant?.terminalFailure).toBeUndefined()
  })

  it('keeps each assistant message token counts independent', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'first answer',
        ts: 1,
        messageId: 'assistant-1',
        usage: { input_tokens: 11, output_tokens: 3 },
      },
      {
        role: 'user',
        text: 'next question',
        ts: 2,
        messageId: 'user-2',
      },
      {
        role: 'assistant',
        text: 'second answer',
        ts: 3,
        messageId: 'assistant-3',
        usage: { input_tokens: 22, output_tokens: 5 },
      },
    ])

    const assistantMessages = api.renderedMessages.value.filter(
      message => message.displayRole === 'assistant',
    )
    expect(assistantMessages.map(message => message.meta?.input)).toEqual([11, 22])
    expect(assistantMessages.map(message => message.meta?.output)).toEqual([3, 5])
  })
})

describe('useChatRenderedMessages clarify history recovery', () => {
  it('restores a clarify interrupt from persisted meta-step tool input', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'Please reply with these fields.',
        ts: 0,
        messageId: 'm-clarify',
        tool_calls: [
          {
            type: 'tool_use',
            tool_use_id: 'meta_step_project_clarify',
            name: 'meta-step:project_clarify',
            input: {
              kind: 'user_input',
              paused: true,
              step: 'project_clarify',
              run_id: 'run-1',
              clarify_schema: {
                mode: 'form',
                intro: 'A few details.',
                fields: [
                  {
                    name: 'topic',
                    type: 'string',
                    required: true,
                    prompt: 'Project topic',
                  },
                  {
                    name: 'age_band',
                    type: 'enum',
                    required: true,
                    prompt: 'Child age band',
                    choices: ['PRE_K', 'EARLY_GRADE'],
                  },
                ],
              },
            },
          },
          {
            type: 'tool_result',
            tool_use_id: 'meta_step_project_clarify',
            name: 'meta-step:project_clarify',
            result: "paused: awaiting user input (step 'project_clarify')",
          },
        ],
      },
    ])

    const [message] = api.renderedMessages.value
    const clarify = message.parts?.find((part): part is ChatPart & {
      type: 'interrupt'
      interruptKind: 'clarify'
    } => part.type === 'interrupt' && part.interruptKind === 'clarify')

    expect(clarify).toBeTruthy()
    expect(clarify?.key).toBe('m-clarify:interrupt:run-1|project_clarify')
    expect(clarify?.clarify?.intro).toBe('A few details.')
    expect(clarify?.clarify?.fields.map(field => field.name)).toEqual([
      'topic',
      'age_band',
    ])
    expect(clarify?.clarify?.fields[1].choices).toEqual(['PRE_K', 'EARLY_GRADE'])
  })

  it('applies clarify submit state to recovered historical interrupt cards', () => {
    const interruptState = ref<ReadonlyMap<string, InterruptViewState>>(new Map([
      ['run-1|project_clarify', {
        resolution: 'replied',
        busy: true,
        error: '',
      }],
    ]))
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'Please reply with these fields.',
        ts: 0,
        messageId: 'm-clarify',
        tool_calls: [
          {
            type: 'tool_use',
            tool_use_id: 'meta_step_project_clarify',
            name: 'meta-step:project_clarify',
            input: {
              kind: 'user_input',
              paused: true,
              step: 'project_clarify',
              run_id: 'run-1',
              clarify_schema: {
                mode: 'form',
                fields: [
                  {
                    name: 'topic',
                    type: 'string',
                    required: true,
                  },
                ],
              },
            },
          },
        ],
      },
    ], interruptState)

    const [message] = api.renderedMessages.value
    const clarify = message.parts?.find((part): part is ChatPart & {
      type: 'interrupt'
      interruptKind: 'clarify'
    } => part.type === 'interrupt' && part.interruptKind === 'clarify')

    expect(clarify?.resolution).toBe('replied')
    expect(clarify?.busy).toBe(true)
  })
})

describe('useChatRenderedMessages ensemble metadata', () => {
  it('reconstructs a settled ensemble strip from completed assistant usage', () => {
    const api = renderedMessagesFor([
      {
        role: 'user',
        text: 'Compare multiple recent AI policy updates.',
        ts: 0,
      },
      {
        role: 'assistant',
        text: 'fused answer',
        ts: 1,
        messageId: 'm-ensemble-strip',
        usage: {
          model_usage_breakdown: [
            { role: 'anchor', provider: 'openrouter', model: 'qwen/qwen3.7-plus' },
            { role: 'research', provider: 'openrouter', model: 'moonshotai/kimi-k2.6' },
            { role: 'critic', provider: 'openrouter', model: 'z-ai/glm-5.2' },
          ],
          ensemble_trace: {
            profile: 'default',
            llm_request_count: 3,
            total_candidates: 8,
            fallback_used: false,
          },
        },
      },
    ], undefined, true)

    const strip = api.renderedMessages.value.find(message => message.isRouterStrip)
    const assistant = api.renderedMessages.value.find(message => message.displayRole === 'assistant')

    expect(strip).toBeTruthy()
    expect(strip?.routerPanel).toBe('llm-ensemble')
    expect(strip?.routerSettled).toBe(true)
    expect(strip?.ensemble?.modelCount).toBe(3)
    expect(assistant?.meta?.ensemble?.modelCount).toBe(3)
    expect(assistant?.meta?.ensemble?.models.map(model => model.modelShort)).toEqual([
      'qwen3.7-plus',
      'kimi-k2.6',
      'glm-5.2',
    ])
  })

  it('normalizes ensemble model breakdown, cost, and savings into assistant meta', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'fused answer',
        ts: 0,
        messageId: 'm-ensemble',
        usage: {
          model: 'z-ai/glm-5.2',
          cost_usd: 0.123456,
          total_savings_usd: 0.045,
          total_savings_pct: 26,
          model_usage_breakdown: [
            {
              role: 'proposer',
              label: 'Proposer 1',
              provider: 'openrouter',
              model: 'deepseek/deepseek-v4-pro',
              input_tokens: 10,
              output_tokens: 2,
              billed_cost: 0.01,
              elapsed_ms: 105_000,
            },
            {
              role: 'aggregator',
              label: 'aggregator',
              provider: 'openrouter',
              model: 'z-ai/glm-5.2',
              input_tokens: 20,
              output_tokens: 8,
              billed_cost: 0.02,
              elapsed_ms: 12_000,
            },
          ],
          ensemble_trace: {
            profile: 'default',
            llm_request_count: 2,
            fallback_used: false,
          },
        },
      },
    ])

    const [message] = api.renderedMessages.value
    expect(message.meta?.ensemble).toMatchObject({
      profile: 'default',
      modelCount: 2,
      requestCount: 2,
      costUsd: 0.123456,
      savedUsd: 0.045,
      savedPct: 26,
      fallbackUsed: false,
    })
    expect(message.meta?.ensemble?.models.map(model => model.model)).toEqual([
      'deepseek/deepseek-v4-pro',
      'z-ai/glm-5.2',
    ])
    expect(message.meta?.ensemble?.models.map(model => model.elapsedMs)).toEqual([
      105_000,
      12_000,
    ])
  })

  it('preserves candidate terminal status from the ensemble trace after settlement', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'fused answer',
        ts: 0,
        usage: {
          model_usage_breakdown: [
            {
              role: 'proposer',
              label: 'proposer_1',
              provider: 'openrouter',
              model: 'deepseek/deepseek-v4-pro',
              sample_index: 0,
              input_tokens: 10,
              output_tokens: 2,
              elapsed_ms: 5_700,
            },
            {
              role: 'aggregator',
              label: 'aggregator',
              provider: 'openrouter',
              model: 'z-ai/glm-5.2',
              input_tokens: 20,
              output_tokens: 8,
              elapsed_ms: 12_000,
            },
          ],
          ensemble_trace: {
            profile: 'default',
            total_candidates: 3,
            candidates: [
              {
                label: 'proposer_1',
                provider: 'openrouter',
                model: 'deepseek/deepseek-v4-pro',
                sample_index: 0,
                ok: true,
                elapsed_ms: 5_700,
              },
              {
                label: 'proposer_2',
                provider: 'openrouter',
                model: 'z-ai/glm-5.2',
                sample_index: 0,
                ok: false,
                elapsed_ms: 21_000,
                error: 'proposer cancelled after 5s ensemble quorum grace',
                error_code: 'quorum_cancelled',
              },
              {
                label: 'proposer_3',
                provider: 'openrouter',
                model: 'moonshotai/kimi-k2.7',
                sample_index: 0,
                ok: false,
                elapsed_ms: 7_000,
                error: 'provider timed out',
                error_code: 'timeout',
              },
            ],
          },
        },
      },
    ])

    const models = api.renderedMessages.value[0].meta?.ensemble?.models
    expect(models).toHaveLength(4)
    expect(models?.[0]).toMatchObject({
      model: 'deepseek/deepseek-v4-pro',
      input: 10,
      output: 2,
      status: 'done',
    })
    expect(models?.[1]).toMatchObject({
      model: 'z-ai/glm-5.2',
      status: 'skipped',
      errorCode: 'quorum_cancelled',
      elapsedMs: 21_000,
    })
    expect(models?.[2]).toMatchObject({
      model: 'moonshotai/kimi-k2.7',
      status: 'failed',
      errorCode: 'timeout',
    })
    expect(models?.[3]).toMatchObject({
      role: 'aggregator',
      model: 'z-ai/glm-5.2',
      input: 20,
      output: 8,
    })
    expect(models?.[3]?.status).toBeUndefined()
  })

  it('does not undercount requests when a turn has multiple ensemble breakdown rows', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'fused answer',
        ts: 0,
        usage: {
          model_usage_breakdown: [
            { role: 'proposer', provider: 'openrouter', model: 'p1' },
            { role: 'aggregator', provider: 'openrouter', model: 'a1' },
            { role: 'proposer', provider: 'openrouter', model: 'p2' },
            { role: 'aggregator', provider: 'openrouter', model: 'a2' },
          ],
          ensemble_trace: {
            profile: 'default',
            llm_request_count: 2,
          },
        },
      },
    ])

    expect(api.renderedMessages.value[0].meta?.ensemble?.requestCount).toBe(4)
  })

  it('does not count an unavailable trace candidate as a physical request', () => {
    const api = renderedMessagesFor([
      {
        role: 'assistant',
        text: 'fused answer',
        ts: 0,
        usage: {
          model_usage_breakdown: [
            {
              role: 'proposer',
              label: 'proposer_1',
              provider: 'openrouter',
              model: 'p1',
            },
            {
              role: 'proposer',
              label: 'proposer_2',
              provider: 'openrouter',
              model: 'p2',
            },
            {
              role: 'proposer',
              label: 'proposer_3',
              provider: 'openrouter',
              model: 'p3',
            },
            {
              role: 'aggregator',
              label: 'aggregator',
              provider: 'openrouter',
              model: 'agg',
            },
          ],
          ensemble_trace: {
            profile: 'default',
            total_candidates: 4,
            llm_request_count: 4,
            candidates: [
              {
                label: 'proposer_1',
                provider: 'openrouter',
                model: 'p1',
                request_started: true,
                ok: true,
              },
              {
                label: 'proposer_2',
                provider: 'openrouter',
                model: 'p2',
                request_started: true,
                ok: true,
              },
              {
                label: 'proposer_3',
                provider: 'openrouter',
                model: 'p3',
                request_started: true,
                ok: true,
              },
              {
                label: 'proposer_4',
                provider: 'openrouter',
                model: 'unavailable',
                request_started: false,
                ok: false,
                error: 'proposer deployment is not ready',
                error_code: 'deployment_unavailable',
              },
            ],
          },
        },
      },
    ])

    const ensemble = api.renderedMessages.value[0].meta?.ensemble
    expect(ensemble?.models).toHaveLength(5)
    expect(ensemble?.requestCount).toBe(4)
  })
})
