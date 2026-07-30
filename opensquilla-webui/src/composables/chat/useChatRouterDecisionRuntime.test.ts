import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import type { ChatMessage } from '@/types/chat'
import { useChatRouterDecisionRuntime } from '@/composables/chat/useChatRouterDecisionRuntime'
import type { ModelRoutingMode } from '@/types/modelRouting'

function makeRuntime(
  messages: ChatMessage[] = [],
  isStreaming = true,
  modelRoutingMode: ModelRoutingMode = 'llm_ensemble',
  autoScroll = true,
) {
  const messagesRef = ref<ChatMessage[]>(messages)
  const scrollToBottom = vi.fn()
  const runtime = useChatRouterDecisionRuntime({
    messages: messagesRef,
    sessionKey: ref('sess'),
    isStreaming: ref(isStreaming),
    autoScroll: ref(autoScroll),
    modelRoutingMode: ref(modelRoutingMode),
    streamBubble: ref(true),
    streamHasVisibleOutput: ref(false),
    startStreaming: vi.fn(),
    resetStreamForRouterReplay: vi.fn(),
    resetStreamIdleTimer: vi.fn(),
    setStreamActivity: vi.fn(),
    scrollToBottom,
  })
  return { runtime, messagesRef, scrollToBottom }
}

describe('appendEnsembleProgress', () => {
  it('synthesizes a router message and reveals members with running → done status', () => {
    const { runtime, messagesRef } = makeRuntime([{ role: 'user', text: 'q', ts: 0 }])

    runtime.appendEnsembleProgress({
      event_type: 'proposer_start',
      proposer_label: 'anchor',
      proposer_provider: 'openrouter',
      proposer_model: 'qwen/qwen3.7-plus',
    })

    const router = messagesRef.value.find(m => m.role === 'router')
    expect(router).toBeTruthy()
    expect(router?.provenanceKind).toBe('router_decision')
    expect(router?.routerDecision?.source).toBe('llm_ensemble')
    expect(router?.ensemble?.models).toHaveLength(1)
    expect(router?.ensemble?.models[0].modelShort).toBe('qwen3.7-plus')
    expect(router?.ensemble?.models[0].status).toBe('running')
    expect(router?.ensemble?.modelCount).toBe(1)

    // The finish delta upserts the SAME row (no duplicate) and flips to done.
    runtime.appendEnsembleProgress({
      event_type: 'proposer_finish',
      proposer_label: 'anchor',
      proposer_provider: 'openrouter',
      proposer_model: 'qwen/qwen3.7-plus',
      input_tokens: 100,
      output_tokens: 20,
      elapsed_ms: 105_000,
    })
    expect(router?.ensemble?.models).toHaveLength(1)
    expect(router?.ensemble?.models[0].status).toBe('done')
    expect(router?.ensemble?.models[0].input).toBe(100)
    expect(router?.ensemble?.models[0].elapsedMs).toBe(105_000)

    // A second proposer grows the revealed count.
    runtime.appendEnsembleProgress({
      event_type: 'proposer_start',
      proposer_label: 'critic',
      proposer_provider: 'openrouter',
      proposer_model: 'z-ai/glm-5.2',
    })
    expect(router?.ensemble?.models).toHaveLength(2)
    expect(router?.ensemble?.modelCount).toBe(2)
  })

  it('tracks failed candidates and the aggregator as independent lifecycle rows', () => {
    const { runtime, messagesRef } = makeRuntime([{ role: 'user', text: 'q', ts: 0 }])

    runtime.appendEnsembleProgress({
      event_type: 'proposer_start',
      proposer_index: 1,
      proposer_label: 'critic',
      proposer_provider: 'openrouter',
      proposer_model: 'z-ai/glm-5.2',
    })
    runtime.appendEnsembleProgress({
      event_type: 'proposer_finish',
      proposer_index: 1,
      proposer_label: 'critic',
      proposer_provider: 'openrouter',
      proposer_model: 'z-ai/glm-5.2',
      elapsed_ms: 118_000,
      error: 'provider timed out',
    })
    runtime.appendEnsembleProgress({
      event_type: 'aggregator_start',
      proposer_index: -1,
      proposer_label: 'aggregator',
      proposer_provider: 'openrouter',
      proposer_model: 'anthropic/claude-sonnet',
    })

    const models = messagesRef.value.find(message => message.role === 'router')?.ensemble?.models
    expect(models).toHaveLength(2)
    expect(models?.[0]).toMatchObject({
      role: 'critic',
      label: 'critic',
      status: 'failed',
      elapsedMs: 118_000,
      error: 'provider timed out',
    })
    expect(models?.[1]).toMatchObject({ role: 'aggregator', status: 'running' })

    runtime.appendEnsembleProgress({
      event_type: 'aggregator_finish',
      proposer_index: -1,
      proposer_label: 'aggregator',
      proposer_provider: 'openrouter',
      proposer_model: 'anthropic/claude-sonnet',
      input_tokens: 200,
      output_tokens: 40,
      elapsed_ms: 12_000,
    })
    expect(models?.[1]).toMatchObject({
      role: 'aggregator',
      status: 'done',
      input: 200,
      output: 40,
      elapsedMs: 12_000,
    })
  })

  it('classifies a quorum-cancelled candidate as skipped instead of failed', () => {
    const { runtime, messagesRef } = makeRuntime([{ role: 'user', text: 'q', ts: 0 }])

    runtime.appendEnsembleProgress({
      event_type: 'proposer_finish',
      proposer_index: 3,
      proposer_label: 'critic',
      proposer_provider: 'openrouter',
      proposer_model: 'z-ai/glm-5.2',
      elapsed_ms: 21_000,
      error: 'proposer cancelled after ensemble quorum grace',
      error_code: 'quorum_cancelled',
    })

    const model = messagesRef.value.find(message => message.role === 'router')?.ensemble?.models[0]
    expect(model).toMatchObject({
      role: 'critic',
      status: 'skipped',
      elapsedMs: 21_000,
      errorCode: 'quorum_cancelled',
    })
  })

  it('narrowly recognizes the legacy quorum-grace message without matching embedded text', () => {
    const { runtime, messagesRef } = makeRuntime([{ role: 'user', text: 'q', ts: 0 }])

    runtime.appendEnsembleProgress({
      event_type: 'proposer_finish',
      proposer_index: 2,
      proposer_label: 'legacy',
      proposer_provider: 'openrouter',
      proposer_model: 'legacy-model',
      error: 'proposer cancelled after 5.5s ensemble quorum grace',
    })
    runtime.appendEnsembleProgress({
      event_type: 'proposer_finish',
      proposer_index: 3,
      proposer_label: 'upstream',
      proposer_provider: 'openrouter',
      proposer_model: 'upstream-model',
      error: 'upstream said: proposer cancelled after 5.5s ensemble quorum grace',
    })

    const models = messagesRef.value.find(message => message.role === 'router')?.ensemble?.models
    expect(models?.[0]).toMatchObject({
      model: 'legacy-model',
      status: 'skipped',
      errorCode: 'quorum_cancelled',
    })
    expect(models?.[1]).toMatchObject({
      model: 'upstream-model',
      status: 'failed',
    })
    expect(models?.[1]?.errorCode).toBeUndefined()
  })

  it('attaches members to the existing live router message instead of duplicating it', () => {
    const existing: ChatMessage = {
      role: 'router',
      text: '',
      ts: 1,
      provenanceKind: 'router_decision',
      routerDecision: { tier: 'c1', model: 'x', source: 'squilla_router' },
    }
    const { runtime, messagesRef } = makeRuntime([{ role: 'user', text: 'q', ts: 0 }, existing])

    runtime.appendEnsembleProgress({
      event_type: 'proposer_finish',
      proposer_label: 'anchor',
      proposer_provider: 'openrouter',
      proposer_model: 'qwen/qwen3.7-plus',
    })

    const routers = messagesRef.value.filter(m => m.role === 'router')
    expect(routers).toHaveLength(1)
    expect(routers[0].ensemble?.models).toHaveLength(1)
    // The strip is upgraded onto the ensemble branch.
    expect(routers[0].routerDecision?.source).toBe('llm_ensemble')
  })

  it('ignores deltas with no model and no aggregator role', () => {
    const { runtime, messagesRef } = makeRuntime([{ role: 'user', text: 'q', ts: 0 }])
    runtime.appendEnsembleProgress({ event_type: 'proposer_start', proposer_model: '' })
    expect(messagesRef.value.some(m => m.role === 'router')).toBe(false)
  })

  it('updates ensemble state without re-pinning a reader who scrolled up', () => {
    const { runtime, messagesRef, scrollToBottom } = makeRuntime(
      [{ role: 'user', text: 'q', ts: 0 }],
      true,
      'llm_ensemble',
      false,
    )

    runtime.appendEnsembleProgress({
      event_type: 'proposer_start',
      proposer_label: 'anchor',
      proposer_provider: 'openrouter',
      proposer_model: 'qwen/qwen3.7-plus',
    })

    expect(messagesRef.value.find(message => message.role === 'router')?.ensemble?.models).toHaveLength(1)
    expect(scrollToBottom).not.toHaveBeenCalled()
  })

  it('keeps following ensemble progress while the reader remains at the live edge', () => {
    const { runtime, scrollToBottom } = makeRuntime([{ role: 'user', text: 'q', ts: 0 }])

    runtime.appendEnsembleProgress({
      event_type: 'proposer_start',
      proposer_label: 'anchor',
      proposer_provider: 'openrouter',
      proposer_model: 'qwen/qwen3.7-plus',
    })

    expect(scrollToBottom).toHaveBeenCalledTimes(1)
  })
})

describe('markEnsembleHandoff', () => {
  it('synthesizes a handoff router message when only the reserve strip exists', () => {
    const { runtime, messagesRef } = makeRuntime([{ role: 'user', text: 'q', ts: 0 }])

    runtime.markEnsembleHandoff()

    const router = messagesRef.value.find(message => message.role === 'router')
    expect(router?.routerDecision?.source).toBe('llm_ensemble')
    expect(router?.routerState).toBe('handoff')
  })

  it('marks the live empty ensemble router as handed off once agent activity starts', () => {
    const router: ChatMessage = {
      role: 'router',
      text: '',
      ts: 1,
      provenanceKind: 'router_decision',
      routerDecision: { tier: 'c1', model: 'deepseek/deepseek-v4-pro', source: 'llm_ensemble' },
    }
    const { runtime } = makeRuntime([{ role: 'user', text: 'q', ts: 0 }, router])

    runtime.markEnsembleHandoff()

    expect(router.routerState).toBe('handoff')
  })

  it('keeps revealed ensemble candidates intact when marking handoff', () => {
    const router: ChatMessage = {
      role: 'router',
      text: '',
      ts: 1,
      provenanceKind: 'router_decision',
      routerDecision: { tier: 'c1', model: 'deepseek/deepseek-v4-pro', source: 'llm_ensemble' },
      ensemble: {
        profile: 'llm_ensemble',
        modelCount: 1,
        totalCandidates: 1,
        requestCount: 1,
        fallbackUsed: false,
        fallbackReason: '',
        costUsd: 0,
        savedUsd: 0,
        savedPct: 0,
        models: [{
          role: 'proposer',
          label: 'proposer',
          provider: 'openrouter',
          model: 'z-ai/glm-5.2',
          modelShort: 'glm-5.2',
          input: 0,
          output: 0,
          costUsd: 0,
          status: 'running',
        }],
      },
    }
    const { runtime } = makeRuntime([{ role: 'user', text: 'q', ts: 0 }, router])

    runtime.markEnsembleHandoff()

    expect(router.ensemble?.models).toHaveLength(1)
    expect(router.ensemble?.models[0].model).toBe('z-ai/glm-5.2')
    expect(router.routerState).toBe('handoff')
  })

  it('marks a live router row as handoff when ensemble mode owns the current turn', () => {
    const router: ChatMessage = {
      role: 'router',
      text: '',
      ts: 1,
      provenanceKind: 'router_decision',
      routerDecision: { tier: 'c1', model: 'deepseek/deepseek-v4-pro', source: 'squilla_router' },
    }
    const { runtime } = makeRuntime([{ role: 'user', text: 'q', ts: 0 }, router], true, 'llm_ensemble')

    runtime.markEnsembleHandoff()

    expect(router.routerState).toBe('handoff')
  })

  it('does not mark non-ensemble router messages', () => {
    const router: ChatMessage = {
      role: 'router',
      text: '',
      ts: 1,
      provenanceKind: 'router_decision',
      routerDecision: { tier: 'c1', model: 'deepseek/deepseek-v4-pro', source: 'squilla_router' },
    }
    const { runtime } = makeRuntime([{ role: 'user', text: 'q', ts: 0 }, router], true, 'squilla_router')

    runtime.markEnsembleHandoff()

    expect(router.routerState).toBeUndefined()
  })

  it('marks the ensemble handoff without re-pinning a reader who scrolled up', () => {
    const { runtime, messagesRef, scrollToBottom } = makeRuntime(
      [{ role: 'user', text: 'q', ts: 0 }],
      true,
      'llm_ensemble',
      false,
    )

    runtime.markEnsembleHandoff()

    expect(messagesRef.value.find(message => message.role === 'router')?.routerState).toBe('handoff')
    expect(scrollToBottom).not.toHaveBeenCalled()
  })
})
