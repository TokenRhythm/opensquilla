// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import { createPinia } from 'pinia'

import i18n from '@/i18n'
import type { GoalSnapshot } from '@/composables/chat/useChatGoals'
import { useToolDetailPreference } from '@/composables/useToolDetailPreference'
import { clearAssistantActivityExpansionState } from '@/utils/chat/activityDisclosureState'
import type {
  ChatMessageMeta,
  ChatRenderedMessage,
  ChatStreamTimelineItem,
  ChatToolCallRenderItem,
} from '@/types/chat'
import type { ChatPart } from '@/types/parts'
import AssistantMessage from './AssistantMessage.vue'

const mountedApps: App[] = []

function failedCall(): ChatToolCallRenderItem {
  return {
    toolId: 'failed-search',
    renderKey: 'failed-search',
    name: 'web_search',
    displayName: 'Search',
    inputRaw: '{"query":"OpenSquilla"}',
    inputPreview: 'OpenSquilla',
    isRunning: false,
    status: 'error',
    isError: true,
    result: 'Network unavailable',
    resultPreview: 'Network unavailable',
    isOpen: false,
  }
}

function failedDocumentWriterCall(name = 'document_apply'): ChatToolCallRenderItem {
  return {
    ...failedCall(),
    toolId: `failed-${name}`,
    renderKey: `failed-${name}`,
    name,
    displayName: 'Apply document change',
    result: 'Proposal validation failed',
    resultPreview: 'Proposal validation failed',
  }
}

function successfulCall(toolId: string, name: string): ChatToolCallRenderItem {
  return {
    ...failedCall(),
    toolId,
    renderKey: toolId,
    name,
    displayName: name,
    status: 'success',
    isError: false,
    result: 'ok',
    resultPreview: 'ok',
  }
}

function timelineGroup(call: ChatToolCallRenderItem): ChatStreamTimelineItem {
  return {
    type: 'tool-group',
    key: `group-${call.toolId}`,
    group: {
      groupId: `group-${call.toolId}`,
      operationKey: call.name,
      label: call.displayName,
      iconName: 'edit',
      calls: [call],
      secondary: '',
      isRunning: false,
      isError: false,
      status: 'success',
    },
  }
}

function failedTimeline(): ChatStreamTimelineItem[] {
  const call = failedCall()
  return [
    { type: 'text', key: 'draft-prefix', html: 'Draft prefix', rawText: 'Draft prefix' },
    {
      type: 'tool-group',
      key: 'failed-group',
      group: {
        groupId: 'failed-group',
        operationKey: 'web.search',
        label: 'Search',
        iconName: 'search',
        calls: [call],
        secondary: '',
        isRunning: false,
        isError: true,
        status: 'error',
      },
    },
    { type: 'text', key: 'draft-suffix', html: 'Draft suffix', rawText: 'Draft suffix' },
  ]
}

function successfulTimeline(): ChatStreamTimelineItem[] {
  const call = failedCall()
  call.status = 'success'
  call.isError = false
  call.result = 'Found one result'
  call.resultPreview = 'Found one result'
  return failedTimeline().map(item => {
    if (item.type !== 'tool-group') return item
    return {
      ...item,
      group: {
        ...item.group,
        calls: [call],
        isError: false,
        status: 'success' as const,
      },
    }
  })
}

function approvalPart(
  resolution: Extract<ChatPart, { type: 'interrupt' }>['resolution'],
): Extract<ChatPart, { type: 'interrupt' }> {
  return {
    type: 'interrupt',
    key: 'approval-1',
    interruptKind: 'approval',
    approval: {
      approvalId: 'approval-1',
      namespace: 'exec',
      toolName: 'shell',
      command: 'printf ok',
      approvalKind: 'sandbox_path',
      args: null,
      warning: '',
      agent: 'main',
      sessionKey: 'session-a',
      deadline: 0,
    },
    resolution,
    busy: false,
    error: '',
  }
}

function approvalTimelineItem(
  part: Extract<ChatPart, { type: 'interrupt' }>,
): ChatStreamTimelineItem {
  return {
    type: 'interrupt',
    key: part.key,
    approvalId: part.approval?.approvalId || '',
    part,
  }
}

function planPart(): Extract<ChatPart, { type: 'plan' }> {
  return {
    type: 'plan',
    key: 'assistant-1:plan:revision-1',
    plan: {
      revisionId: 'revision-1',
      planId: 'plan-1',
      title: 'A restrained plan',
      markdown: 'Keep the final plan easy to scan.',
      steps: [{ stepId: 'step-1', title: 'Verify the layout' }],
      current: true,
    },
  }
}

function clarifyPart(
  presentation?: string,
): Extract<ChatPart, { type: 'interrupt' }> {
  return {
    type: 'interrupt',
    key: presentation ? 'plan-clarify-1' : 'generic-clarify-1',
    interruptKind: 'clarify',
    clarify: {
      intro: 'Confirm the scope.',
      fields: [{
        name: 'scope',
        prompt: 'Which scope?',
        type: 'enum',
        required: true,
        defaultValue: '',
        choices: ['focused', 'complete'],
      }],
      ...(presentation ? { presentation } : {}),
      requestId: presentation ? 'plan-input-1' : 'generic-input-1',
      runId: 'plan-run-1',
      step: 'confirm_scope',
    },
    resolution: 'replied',
    busy: false,
    error: '',
  }
}

function usageMeta(overrides: Partial<ChatMessageMeta> = {}): ChatMessageMeta {
  return {
    model: 'tokenrhythm/kimi-k2.7-code',
    modelShort: 'kimi-k2.7-code',
    input: 4096,
    output: 128,
    hasTokens: true,
    cachedTokens: 512,
    reasoningTokens: 64,
    costUsd: 0.012345,
    hasSaved: false,
    savedLabel: '',
    ...overrides,
  }
}

function baseMessage(overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage {
  return {
    id: 'assistant-1',
    messageId: 'assistant-1',
    turnKey: 'turn:user-1',
    role: 'assistant',
    displayRole: 'assistant',
    roleLabel: 'Assistant',
    text: 'Canonical answer',
    timeStr: '',
    showHeader: false,
    timelineItems: failedTimeline(),
    parts: [{
      type: 'reasoning',
      key: 'assistant-1:reasoning',
      text: 'Checked the available evidence.',
      seconds: 7,
    }],
    statusHistory: [
      { action: 'search', label: 'Searching', at: 1000 },
      { action: 'write', label: 'Writing', at: 2000 },
    ],
    ...overrides,
  }
}

function mountMessage(
  message: ChatRenderedMessage,
  showTurnOutcome = false,
  extraProps: Record<string, unknown> = {},
): HTMLElement {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp({
    render: () => h(AssistantMessage, {
      message,
      index: 0,
      sessionKey: 'session-a',
      shareMode: false,
      shareSelected: false,
      shareMessageId: 'assistant-1',
      showTurnOutcome,
      ...extraProps,
      renderMarkdown: (text: string) => `<p>${text}</p>`,
      fmtTok: (value: number) => String(value),
      toolCallGroups: () => [],
      isToolGroupOpen: () => false,
      isToolItemOpen: () => false,
      toolGroupStatusText: () => 'Failed',
      toolStatusText: () => 'Failed',
      toolSecondaryText: () => '',
      copyMessage: async () => true,
    }),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.use(createPinia())
  app.mount(el)
  return el
}

function completedGoal(): GoalSnapshot {
  return {
    goalId: 'goal-1',
    sessionKey: 'session-a',
    sessionId: 'session-a',
    epoch: 0,
    objective: 'Finish the visual regression fix',
    status: 'complete',
    stateRevision: 3,
    objectiveRevision: 1,
    progressRevision: 1,
    progress: null,
    continuationSeq: 0,
    activeTaskId: null,
    sourceMessageId: 'user-1',
    terminalTurnId: 'turn-goal',
    executionState: 'idle',
    continuationDeferredReason: null,
    turnsStarted: 2,
    turnsSettled: 2,
    windowTurnsStarted: 2,
    activeTimeMs: 17_000,
    windowActiveTimeMs: 17_000,
    usage: {
      inputTokens: 4096,
      outputTokens: 128,
      reasoningTokens: 64,
      cacheReadTokens: 512,
      cacheWriteTokens: 0,
      totalTokens: 4288,
    },
    pauseReason: null,
    blockedReason: null,
    terminalReason: 'model_complete',
    createdAt: 1,
    updatedAt: 2,
    finishedAt: 2,
  }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  clearAssistantActivityExpansionState()
  useToolDetailPreference().setMode('auto')
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('AssistantMessage activity disclosure', () => {
  it('keeps a plain completion status and restores usage to the compact footer', async () => {
    const el = mountMessage(baseMessage({
      timelineItems: [],
      parts: [],
      statusHistory: [],
      meta: usageMeta(),
      turnOutcome: {
        turnId: 'turn-usage',
        status: 'succeeded',
        kind: 'completed',
        startedAt: 1_725_000_000_000,
        finishedAt: 1_725_000_005_000,
      },
    }), true)
    await nextTick()

    expect(el.querySelector('.assistant-activity')).toBeNull()
    expect(el.querySelector('[data-testid="turn-outcome-completed"]')?.textContent)
      .toContain('Completed')
    const trigger = el.querySelector<HTMLButtonElement>('.msg-meta__more-btn')
    expect(trigger).not.toBeNull()
    trigger?.click()
    await nextTick()
    const usage = el.querySelector('.msg-meta-popover')?.textContent
    expect(usage).toContain('kimi-k2.7-code')
    expect(usage).toContain('4096')
    expect(usage).toContain('128')
    expect(usage).toContain('512')
    expect(usage).toContain('64')
  })

  it('keeps tools and reasoning in activity while usage stays in the footer', async () => {
    const el = mountMessage(baseMessage({
      timelineItems: successfulTimeline(),
      meta: usageMeta(),
      turnOutcome: {
        turnId: 'turn-tools-usage',
        status: 'succeeded',
        kind: 'completed',
      },
    }), true)
    await nextTick()

    const receipts = el.querySelectorAll('.assistant-activity')
    expect(receipts).toHaveLength(1)
    expect(receipts[0]?.querySelector('.turn-usage-details')).toBeNull()
    expect(receipts[0]?.querySelector('.tool-row')).not.toBeNull()
    expect(el.querySelector('.msg-meta__more-btn')).not.toBeNull()
  })

  it('keeps compact usage for Plan, Goal, and Cron without creating activity', async () => {
    const cases = [
      { overrides: { parts: [planPart()] }, goalOutcome: null },
      {
        overrides: { parts: [], turnInputMode: 'system_event', turnRunKind: 'goal' },
        goalOutcome: completedGoal(),
      },
      {
        overrides: { parts: [], provenanceKind: 'cron', provenanceSourceTool: 'cron.run' },
        goalOutcome: null,
      },
    ] satisfies Array<{
      overrides: Partial<ChatRenderedMessage>
      goalOutcome: GoalSnapshot | null
    }>
    for (const { overrides, goalOutcome } of cases) {
      const el = mountMessage(baseMessage({
        timelineItems: [],
        statusHistory: [],
        meta: usageMeta(),
        turnOutcome: {
          turnId: `turn-${String(overrides.turnRunKind || overrides.provenanceKind || 'plan')}`,
          status: 'succeeded',
          kind: 'completed',
        },
        ...overrides,
      }), true, { goalOutcome, goalElapsed: '17s' })
      await nextTick()

      expect(el.querySelectorAll('.assistant-activity')).toHaveLength(0)
      expect(el.querySelector('.turn-usage-details')).toBeNull()
      expect(el.querySelector('.msg-meta__more-btn')).not.toBeNull()
      if (overrides.parts?.some(part => part.type === 'plan')) {
        expect(el.querySelector('.plan-card')).not.toBeNull()
        expect(el.querySelector('.msg-ai-actions')).toBeNull()
      }
      if (goalOutcome) {
        expect(el.querySelector('.goal-outcome')?.textContent).toContain('2 turns')
        expect(el.querySelector('.goal-outcome')?.textContent).toContain('4,288 tokens')
      }
      if (overrides.provenanceKind === 'cron') {
        expect(el.querySelector('.msg-provenance-chip')?.textContent).toContain('Scheduled')
      }
      el.remove()
    }
  })

  it('keeps usage inspectable beside explicit failed and stopped outcomes', async () => {
    for (const outcome of [
      { turnId: 'turn-failed', status: 'failed', kind: 'failed' },
      { turnId: 'turn-stopped', status: 'cancelled', kind: 'cancelled' },
    ]) {
      const el = mountMessage(baseMessage({
        timelineItems: [],
        parts: [],
        statusHistory: [],
        meta: usageMeta(),
        turnOutcome: outcome,
      }), true)
      await nextTick()

      expect(el.querySelector('.assistant-activity')).toBeNull()
      expect(el.querySelector('.turn-outcome')).not.toBeNull()
      expect(el.querySelector('.turn-usage-details')).toBeNull()
      expect(el.querySelector('.msg-meta__more-btn')).not.toBeNull()
      el.remove()
    }
  })

  it('keeps restart recovery guidance visible beside durable activity', async () => {
    const el = mountMessage(baseMessage({
      timelineItems: successfulTimeline(),
      turnOutcome: {
        turnId: 'turn-restart-activity',
        status: 'abandoned',
        kind: 'interrupted',
        reason: 'process_restart',
      },
    }), true)
    await nextTick()

    expect(el.querySelector('.assistant-activity')).not.toBeNull()
    expect(el.querySelector('.turn-outcome--process-restart')?.textContent)
      .toContain("This task won't continue automatically")
  })

  it('keeps restart recovery guidance visible beside a Plan card', async () => {
    const el = mountMessage(baseMessage({
      text: '',
      timelineItems: [],
      parts: [planPart()],
      statusHistory: [],
      turnOutcome: {
        turnId: 'turn-restart-plan',
        status: 'abandoned',
        kind: 'interrupted',
        errorClass: 'process_restart',
      },
    }), true)
    await nextTick()

    expect(el.querySelector('.plan-card')).not.toBeNull()
    expect(el.querySelector('.turn-outcome--process-restart')?.textContent)
      .toContain('Review any existing results or tool activity')
  })

  it('retains the legacy footer usage entry when history has no turn outcome', async () => {
    const el = mountMessage(baseMessage({
      timelineItems: [],
      parts: [],
      statusHistory: [],
      meta: usageMeta(),
      turnOutcome: undefined,
    }))
    await nextTick()

    expect(el.querySelector('.assistant-activity')).toBeNull()
    expect(el.querySelectorAll('.msg-meta__more-btn')).toHaveLength(1)
  })

  it('keeps incomplete unknown-only usage honest in the legacy popover', async () => {
    const el = mountMessage(baseMessage({
      timelineItems: [],
      parts: [],
      statusHistory: [],
      meta: usageMeta({
        input: 0,
        output: 0,
        hasTokens: false,
        costUsd: 0,
        coverageStatus: 'usage_unknown',
        usageUnknown: true,
        unknownUsageEvents: 1,
        hasKnownUsage: false,
      }),
      turnOutcome: undefined,
    }))
    await nextTick()

    el.querySelector<HTMLButtonElement>('.msg-meta__more-btn')?.click()
    await nextTick()
    const coverage = el.querySelector<HTMLElement>('[data-turn-usage-coverage="incomplete"]')
    expect(coverage?.textContent).toContain('exact usage total unavailable')
    expect(coverage?.textContent).toContain('1 provider call has unknown usage')
    expect(el.querySelector('.msg-meta-popover')?.textContent).not.toContain('$0')
  })

  it('keeps numeric ensemble costs without adding an unknown-usage display state', async () => {
    const meta = usageMeta({
      input: 0,
      output: 0,
      hasTokens: false,
      costUsd: 0,
      coverageStatus: 'usage_unknown',
      usageUnknown: true,
      unknownUsageEvents: 1,
      hasKnownUsage: false,
    })
    meta.ensemble = {
      profile: 'ensemble-review',
      modelCount: 1,
      totalCandidates: 1,
      requestCount: 1,
      costUsd: 0,
      fallbackUsed: false,
      fallbackReason: '',
      savedUsd: 0,
      savedPct: 0,
      models: [{
        role: 'proposer',
        label: 'proposer',
        provider: 'test-provider',
        model: 'test/model',
        modelShort: 'model',
        input: 0,
        output: 0,
        costUsd: 0,
      }],
    }
    const el = mountMessage(baseMessage({
      timelineItems: [],
      parts: [],
      statusHistory: [],
      meta,
      turnOutcome: undefined,
    }))
    await nextTick()

    el.querySelector<HTMLButtonElement>('.msg-meta__more-btn')?.click()
    await nextTick()
    const popover = el.querySelector<HTMLElement>('.msg-meta-popover')
    expect(popover?.textContent).toContain('ensemble-review')
    expect(popover?.textContent).not.toContain('exact usage total unavailable')
    expect(popover?.textContent).toContain('$0')
    expect(el.querySelector('.msg-meta-popover__model-cost')?.textContent?.trim()).toBe('$0')
  })

  it('adds compact usage without reordering a canonical-less legacy timeline', async () => {
    const el = mountMessage(baseMessage({
      text: '',
      parts: [],
      statusHistory: [],
      meta: usageMeta(),
      turnOutcome: {
        turnId: 'turn-legacy-timeline',
        status: 'succeeded',
        kind: 'completed',
      },
    }), true)
    await nextTick()

    expect(el.querySelectorAll('.assistant-activity')).toHaveLength(0)
    expect(el.querySelector('.turn-usage-details')).toBeNull()
    expect(el.querySelector('.msg-meta__more-btn')).not.toBeNull()
    const text = el.textContent || ''
    expect(text).toContain('Draft prefix')
    expect(text).toContain('Draft suffix')
    expect(text.indexOf('Draft prefix')).toBeLessThan(text.indexOf('Draft suffix'))
  })

  it('shows completed in the task-status position for a simple successful turn', async () => {
    const el = mountMessage(baseMessage({
      timelineItems: [],
      parts: [],
      statusHistory: [],
      turnOutcome: {
        turnId: 'turn-success',
        status: 'succeeded',
        kind: 'completed',
      },
    }), true)
    await nextTick()

    expect(el.querySelector('[data-testid="turn-outcome-completed"]')?.textContent)
      .toContain('Completed')
  })

  it('keeps the canonical answer outside activity and hides failed tool content', async () => {
    const el = mountMessage(baseMessage())
    await nextTick()

    const activity = el.querySelector<HTMLElement>('.assistant-activity')
    const summary = activity?.querySelector<HTMLButtonElement>('.assistant-activity__summary')
    const answer = [...el.querySelectorAll<HTMLElement>('.msg-ai-text')]
      .find(node => !activity?.contains(node))
    const failedRow = activity?.querySelector<HTMLElement>('.tool-row--error')

    expect(activity).not.toBeNull()
    expect(summary?.getAttribute('aria-expanded')).toBe('false')
    expect(activity?.dataset.shareExpanded).toBe('false')
    const reasoningFold = activity?.querySelector<HTMLDetailsElement>('details.thinking-fold')
    expect(reasoningFold).not.toBeNull()
    expect(reasoningFold?.open).toBe(false)
    expect(activity?.querySelector('.assistant-activity__chevron')).toBeNull()
    expect(activity?.querySelector('.assistant-activity__summary-arrow')).not.toBeNull()
    expect(activity?.textContent).toContain('Checked the available evidence.')
    expect(activity?.textContent).toContain('Searched the web')
    expect(activity?.textContent).not.toContain('1 web action')
    expect(activity?.textContent).not.toContain('failure recovered')
    expect(failedRow).toBeNull()

    expect(answer?.textContent).toBe('Canonical answer')
    expect(activity?.contains(answer ?? null)).toBe(false)
    expect(el.querySelectorAll('.msg-ai-text')).toHaveLength(1)
    expect(activity?.querySelector('.activity-narration')?.textContent).toContain('Draft prefix')
    expect(activity?.textContent).toContain('Draft prefix')
    expect(el.textContent).not.toContain('Draft suffix')
  })

  it('restores routine phase rows and reopens settled reasoning content', async () => {
    const startedAt = Date.parse('2026-01-01T00:00:00.000Z')
    const el = mountMessage(baseMessage({
      ts: '2026-01-01T00:00:07.000Z',
      timelineItems: [],
      statusHistory: [
        { action: 'Sending', label: 'Sending', at: startedAt + 1_000 },
        { action: 'provider:requesting', label: 'Waiting', at: startedAt + 2_000 },
        { action: 'provider:reasoning', label: 'Reasoning', at: startedAt + 3_000 },
        { action: 'write:1', label: 'Writing', at: startedAt + 4_000 },
      ],
    }))
    await nextTick()

    const activity = el.querySelector<HTMLElement>('.assistant-activity')!
    activity.querySelector<HTMLButtonElement>('.assistant-activity__summary')?.click()
    await nextTick()
    expect(activity.textContent).not.toContain('Working')
    expect(activity.textContent).toContain('Model response · 1s')
    expect(activity.textContent).toContain('Thinking deeply · 1s')
    expect(activity.textContent).toContain('Answer composition · 3s')
    const processText = activity.textContent || ''
    expect(processText.indexOf('Model response'))
      .toBeLessThan(processText.indexOf('Thinking deeply'))
    expect(processText.indexOf('Thinking deeply'))
      .toBeLessThan(processText.indexOf('Answer composition'))

    const reasoning = activity.querySelector<HTMLDetailsElement>('.thinking-fold')!
    expect(reasoning.open).toBe(false)
    reasoning.querySelector<HTMLElement>('.thinking-fold__summary')?.click()
    await nextTick()
    expect(reasoning.open).toBe(true)
    expect(reasoning.querySelector('.thinking-fold__body')?.textContent)
      .toBe('Checked the available evidence.')
  })

  it('retains separate completed reasoning blocks and lets each reopen', async () => {
    const el = mountMessage(baseMessage({
      timelineItems: [],
      reasoningBlocks: [
        {
          id: 'reasoning-1',
          index: 0,
          text: 'First physical call.',
          status: 'completed',
          startedAt: 1_000,
          endedAt: 3_000,
          contentKind: 'reasoning',
        },
        {
          id: 'reasoning-2',
          index: 1,
          text: 'Second physical call.',
          status: 'completed',
          startedAt: 4_000,
          endedAt: 7_000,
          contentKind: 'reasoning',
        },
      ],
    }))
    await nextTick()

    const folds = el.querySelectorAll<HTMLDetailsElement>('.reasoning-timeline details')
    expect(folds).toHaveLength(2)
    expect([...folds].every(fold => !fold.open)).toBe(true)
    expect(folds[0]?.textContent).toContain('Thinking deeply · 2s')
    expect(folds[1]?.textContent).toContain('Thinking deeply · 3s')

    folds[0]?.querySelector<HTMLElement>('summary')?.click()
    folds[1]?.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    expect(folds[0]?.open).toBe(true)
    expect(folds[1]?.open).toBe(true)
    expect(folds[0]?.textContent).toContain('First physical call.')
    expect(folds[1]?.textContent).toContain('Second physical call.')
  })

  it('continues an unfinished coarse reveal across the live-to-settled remount', async () => {
    const callbacks: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      callbacks.push(callback)
      return callbacks.length
    })
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    const fullText = 'coarse terminal reasoning '.repeat(80)
    const el = mountMessage(baseMessage({
      timelineItems: [],
      reasoningPresentationPending: true,
      reasoningBlocks: [{
        id: 'reasoning-1',
        index: 0,
        text: fullText,
        status: 'completed',
        startedAt: 1_000,
        endedAt: 3_000,
        contentKind: 'reasoning',
      }],
    }))
    await nextTick()

    const activitySummary = el.querySelector<HTMLButtonElement>('.assistant-activity__summary')
    const body = () => el.querySelector<HTMLElement>('.thinking-fold__body')?.textContent || ''
    expect(activitySummary?.getAttribute('aria-expanded')).toBe('true')
    expect(body().length).toBeGreaterThan(0)
    expect(body().length).toBeLessThan(fullText.length)

    while (callbacks.length) {
      callbacks.shift()?.(performance.now())
      await nextTick()
    }
    expect(body()).toBe(fullText)
    expect(activitySummary?.getAttribute('aria-expanded')).toBe('false')
  })

  it('uses the status timeline clock for the parent total when outcome timing is shorter', async () => {
    const startedAt = Date.parse('2026-01-01T00:00:00.000Z')
    const el = mountMessage(baseMessage({
      ts: '2026-01-01T00:00:07.000Z',
      timelineItems: [],
      statusHistory: [
        { action: 'Sending', label: 'Sending', at: startedAt + 1_000 },
        { action: 'provider:requesting', label: 'Waiting', at: startedAt + 2_000 },
        { action: 'provider:reasoning', label: 'Reasoning', at: startedAt + 3_000 },
        { action: 'write:1', label: 'Writing', at: startedAt + 4_000 },
      ],
      turnOutcome: {
        turnId: 'turn-short-server-clock',
        status: 'succeeded',
        kind: 'completed',
        startedAt: startedAt + 2_000,
        finishedAt: startedAt + 5_000,
      },
    }))
    await nextTick()

    // The status phases occupy five seconds and the complete client-observed
    // turn occupies six. The three-second server outcome is a fallback only,
    // otherwise the child phase sum can exceed its parent total.
    expect(el.querySelector('.assistant-activity__summary')?.textContent)
      .toContain('Completed · 6s')
    expect(el.querySelector('.assistant-activity__detail')).toBeNull()
  })

  it('never shows a parent total shorter than the rendered phase durations', async () => {
    const startedAt = Date.parse('2026-01-01T00:00:00.000Z')
    const el = mountMessage(baseMessage({
      ts: '2026-01-01T00:00:04.000Z',
      timelineItems: [],
      parts: [{
        type: 'reasoning',
        key: 'assistant-1:reasoning',
        text: 'Checked the available evidence.',
        seconds: 2,
      }],
      statusHistory: [
        { action: 'provider:requesting', label: 'Waiting', at: startedAt + 1_000 },
        // A block-qualified action still projects a one-second reasoning
        // phase, while the rendered reasoning part owns its two-second
        // provider duration.
        { action: 'provider:reasoning:block-1', label: 'Reasoning', at: startedAt + 2_000 },
        { action: 'write:1', label: 'Writing', at: startedAt + 3_000 },
      ],
    }))
    await nextTick()

    const activity = el.querySelector<HTMLElement>('.assistant-activity')!
    activity.querySelector<HTMLButtonElement>('.assistant-activity__summary')?.click()
    await nextTick()
    expect(activity.textContent).toContain('Model response · 1s')
    expect(activity.textContent).toContain('Thinking deeply · 2s')
    expect(activity.textContent).toContain('Answer composition · 1s')
    expect(activity.querySelector('.assistant-activity__summary')?.textContent)
      .toContain('Completed · 4s')
  })

  it('defaults successful activity to collapsed', async () => {
    const el = mountMessage(baseMessage({ timelineItems: successfulTimeline() }))
    await nextTick()

    const summary = el.querySelector('.assistant-activity__summary')
    expect(summary?.getAttribute('aria-expanded')).toBe('false')
    expect(summary?.textContent).toContain('Completed · 7s')
    expect(summary?.textContent).not.toContain('item')
    expect(el.querySelector('.assistant-activity__detail')?.textContent).toContain('1 web action')
    expect(summary?.textContent).not.toContain('Activity ·')
    expect(el.querySelector('.assistant-activity')?.getAttribute('data-share-expanded')).toBe('false')
    expect(el.querySelector('.tool-row')).not.toBeNull()
    const answer = el.querySelector<HTMLElement>('.assistant-answer')
    const activity = el.querySelector<HTMLElement>('.assistant-activity')
    expect(el.querySelector('.assistant-answer--separated')).not.toBeNull()
    expect(
      Boolean(
        (activity?.compareDocumentPosition(answer!) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING,
      ),
    ).toBe(true)
  })

  it('does not add an answer divider when the message has no activity', async () => {
    const el = mountMessage(baseMessage({
      text: 'Direct answer',
      timelineItems: [],
      toolCalls: [],
      parts: [],
      statusHistory: [],
    }))
    await nextTick()

    expect(el.querySelector('.assistant-answer')).not.toBeNull()
    expect(el.querySelector('.assistant-answer--separated')).toBeNull()
  })

  it('places a single planning-process disclosure before the Plan card', async () => {
    const reasoning: Extract<ChatPart, { type: 'reasoning' }> = {
      type: 'reasoning',
      key: 'assistant-1:reasoning',
      text: 'Checked constraints and compatibility.',
      seconds: 7,
    }
    const el = mountMessage(baseMessage({
      text: 'The plan is ready.',
      timelineItems: successfulTimeline(),
      parts: [reasoning, planPart()],
      statusHistory: [],
    }))
    await nextTick()

    const main = el.querySelector<HTMLElement>('.msg-ai-main')
    const intro = main?.querySelector<HTMLElement>('.plan-message-intro')
    const activity = main?.querySelector<HTMLElement>('.assistant-activity')
    const card = main?.querySelector<HTMLElement>('.plan-card')
    const children = Array.from(main?.children ?? [])

    expect(intro?.textContent).toContain('The plan is ready.')
    expect(children.indexOf(activity as HTMLElement))
      .toBeLessThan(children.indexOf(intro as HTMLElement))
    expect(children.indexOf(intro as HTMLElement))
      .toBeLessThan(children.indexOf(card as HTMLElement))
    expect(activity?.querySelector('.assistant-activity__label')?.textContent)
      .toBe('Planning process · 7s')
    expect(activity?.querySelector('.assistant-activity__detail')).toBeNull()
    expect(activity?.querySelector('.thinking-fold')).toBeNull()
    expect(activity?.querySelector('.thinking-block__header')).toBeNull()
    expect(activity?.querySelector('.thinking-block__body')?.textContent)
      .toBe('Checked constraints and compatibility.')
  })

  it('does not add a generic completed receipt below a Plan card', async () => {
    const el = mountMessage(baseMessage({
      text: '',
      timelineItems: [],
      parts: [planPart()],
      statusHistory: [],
      turnOutcome: {
        turnId: 'turn-plan',
        status: 'succeeded',
        kind: 'completed',
      },
    }), true)
    await nextTick()

    expect(el.querySelector('.plan-card')).not.toBeNull()
    expect(el.querySelector('[data-testid="turn-outcome-completed"]')).toBeNull()
  })

  it('removes the standalone Plan questionnaire receipt once the Plan card exists', async () => {
    const el = mountMessage(baseMessage({
      text: '',
      timelineItems: [],
      parts: [clarifyPart('plan_questionnaire_v1'), planPart()],
      statusHistory: [],
    }))
    await nextTick()

    expect(el.querySelector('.plan-card')).not.toBeNull()
    expect(el.querySelector('.clarify-outcome--plan')).toBeNull()
  })

  it('does not suppress a generic clarify receipt when a Plan card exists', async () => {
    const el = mountMessage(baseMessage({
      text: '',
      timelineItems: [],
      parts: [clarifyPart(), planPart()],
      statusHistory: [],
    }))
    await nextTick()

    expect(el.querySelector('.plan-card')).not.toBeNull()
    expect(el.querySelector('.clarify-outcome')).not.toBeNull()
    expect(el.querySelector('.clarify-outcome--plan')).toBeNull()
  })

  it('keeps intermediate candidate narration inside activity and the final answer outside once', async () => {
    const el = mountMessage(baseMessage({
      text: 'Final verified answer.',
      timelineItems: [
        timelineGroup(successfulCall('inspect', 'read_source')),
        {
          type: 'text',
          key: 'draft-candidate',
          html: '<p>Draft candidate.</p>',
          rawText: 'Draft candidate.',
        },
        timelineGroup(successfulCall('verify', 'execute_code')),
        {
          type: 'text',
          key: 'final-snapshot',
          html: '<p>Final verified answer.</p>',
          rawText: 'Final verified answer.',
        },
      ],
      parts: [],
      statusHistory: [],
    }))
    await nextTick()

    const activity = el.querySelector<HTMLElement>('.assistant-activity')
    const answer = [...el.querySelectorAll<HTMLElement>('.msg-ai-text')]
      .find(node => !activity?.contains(node))
    expect(activity?.textContent).toContain('Draft candidate.')
    expect(activity?.textContent).not.toContain('Final verified answer.')
    expect(answer?.textContent).toBe('Final verified answer.')
    expect((el.textContent?.match(/Final verified answer\./g) ?? [])).toHaveLength(1)
  })

  it('keeps aggregated narration around one tool inside the collapsed activity', async () => {
    const el = mountMessage(baseMessage({
      text: 'Inspecting first.\nChecking the result.\nFinal answer.',
      timelineItems: [
        {
          type: 'text',
          key: 'opening',
          html: '<p>Inspecting first.</p>',
          rawText: 'Inspecting first.\n',
        },
        {
          type: 'text',
          key: 'middle',
          html: '<p>Checking the result.</p>',
          rawText: 'Checking the result.\n',
        },
        timelineGroup(successfulCall('verify', 'http_request')),
        {
          type: 'text',
          key: 'answer',
          html: '<p>Final answer.</p>',
          rawText: 'Final answer.',
        },
      ],
      parts: [],
      statusHistory: [],
    }))
    await nextTick()

    const activity = el.querySelector<HTMLElement>('.assistant-activity')
    const answer = [...el.querySelectorAll<HTMLElement>('.msg-ai-text')]
      .find(node => !activity?.contains(node))

    expect(activity?.querySelector('.assistant-activity__summary')?.getAttribute('aria-expanded'))
      .toBe('false')
    expect(activity?.textContent).toContain('Inspecting first.')
    expect(activity?.textContent).toContain('Checking the result.')
    expect(activity?.textContent).not.toContain('Final answer.')
    expect(answer?.textContent).toBe('Final answer.')
  })

  it('collapses PlanRun narration and leaves only the terminal delivery outside', async () => {
    const checkpoint = successfulCall('checkpoint', 'plan_run_checkpoint')
    const el = mountMessage(baseMessage({
      text: 'Inspecting files.\n\nImplementation complete.',
      timelineItems: [
        {
          type: 'text',
          key: 'work',
          html: '<p>Inspecting files.</p>',
          rawText: 'Inspecting files.\n\n',
        },
        timelineGroup(successfulCall('inspect', 'read_source')),
        {
          type: 'text',
          key: 'delivery',
          html: '<p>Implementation complete.</p>',
          rawText: 'Implementation complete.',
        },
        timelineGroup(checkpoint),
      ],
      parts: [],
      statusHistory: [],
    }))
    await nextTick()

    const activity = el.querySelector<HTMLElement>('.assistant-activity')
    const answer = [...el.querySelectorAll<HTMLElement>('.msg-ai-text')]
      .find(node => !activity?.contains(node))

    expect(activity?.querySelector('.assistant-activity__summary')?.getAttribute('aria-expanded'))
      .toBe('false')
    expect(activity?.textContent).toContain('Inspecting files.')
    expect(activity?.textContent).not.toContain('plan_run_checkpoint')
    expect(answer?.textContent).toBe('Implementation complete.')
    expect(el.textContent).not.toContain('Inspecting files.Implementation complete.')
  })

  it('does not render an activity disclosure containing only a failed tool', async () => {
    const timelineItems = failedTimeline().filter(item => item.type === 'tool-group')
    const el = mountMessage(baseMessage({
      text: '',
      timelineItems,
      toolCalls: [failedCall()],
      parts: [],
      statusHistory: [],
    }))
    await nextTick()

    const activity = el.querySelector('.assistant-activity')
    expect(activity).toBeNull()
    expect(el.querySelector('.tool-row--error')).toBeNull()
  })

  it.each(['document_apply', 'document_patch'])(
    'keeps a failed %s page update visible and uses the unified successful summary',
    async (writerName) => {
      const failedWriter = failedDocumentWriterCall(writerName)
      const failedApply = timelineGroup(failedWriter)
      if (failedApply.type !== 'tool-group') throw new Error('expected tool group')
      failedApply.group.isError = true
      failedApply.group.status = 'error'
      const successfulApply = timelineGroup(successfulCall('applied-document', writerName))
      const el = mountMessage(baseMessage({
        timelineItems: [failedApply, successfulApply],
        toolCalls: [failedWriter, successfulCall('applied-document', writerName)],
        parts: [],
        statusHistory: [],
        turnOutcome: {
          turnId: 'turn-document-apply',
          status: 'succeeded',
          documentMutationOutcome: {
            version: 1,
            status: 'applied',
            corrected: true,
            proposalAttempts: 2,
          },
        },
      }))
      await nextTick()

      const activity = el.querySelector<HTMLElement>('.assistant-activity')
      expect(
        activity?.querySelector('.assistant-activity__summary')?.getAttribute('aria-expanded'),
      ).toBe('false')
      expect(activity?.querySelector('.assistant-activity__summary')?.textContent)
        .toContain('Page updated')
      expect(activity?.querySelector('.assistant-activity__summary')?.textContent)
        .not.toMatch(/receipt|reconciliation|revision|document_(?:apply|patch)/i)
      expect(activity?.querySelector('.tool-row--error')).not.toBeNull()
      expect(activity?.textContent).not.toContain('Proposal validation failed')
    },
  )

  it('hides restored failures whose error state only survived on the group', async () => {
    const staleFailure = timelineGroup(successfulCall('stale-failure', 'execute_code'))
    if (staleFailure.type !== 'tool-group') throw new Error('expected tool group')
    staleFailure.group.isError = true
    staleFailure.group.status = 'error'
    const el = mountMessage(baseMessage({
      timelineItems: [staleFailure],
      parts: [],
      statusHistory: [],
    }))
    await nextTick()

    expect(el.querySelector('.assistant-activity')).toBeNull()
    expect(el.querySelector('.tool-row')).toBeNull()
    expect(el.textContent).not.toContain('Failed')
  })

  it('keeps a restored document writer failure with a canonical operation key', async () => {
    const restoredFailure = timelineGroup(successfulCall('restored-writer', 'document_patch'))
    if (restoredFailure.type !== 'tool-group') throw new Error('expected tool group')
    restoredFailure.group.operationKey = 'document.update'
    restoredFailure.group.isError = true
    restoredFailure.group.status = 'error'
    const el = mountMessage(baseMessage({
      timelineItems: [restoredFailure],
      parts: [],
      statusHistory: [],
    }))
    await nextTick()

    expect(el.querySelector('.assistant-activity')).not.toBeNull()
    expect(el.textContent).toContain('Failed')
  })

  it('keeps successful calls while removing failed calls from a mixed group', async () => {
    const success = successfulCall('successful-command', 'execute_code')
    const mixed = timelineGroup(success)
    if (mixed.type !== 'tool-group') throw new Error('expected tool group')
    mixed.group.calls = [success, failedCall()]
    mixed.group.isError = true
    mixed.group.status = 'error'
    const el = mountMessage(baseMessage({
      timelineItems: [mixed],
      parts: [],
      statusHistory: [],
    }))
    await nextTick()

    expect(el.querySelectorAll('.tool-row')).toHaveLength(1)
    expect(el.querySelector('.tool-row--error')).toBeNull()
    expect(el.textContent).not.toContain('Network unavailable')
  })

  it('keeps interrupted activity collapsed while leaving the answer outside', async () => {
    const el = mountMessage(baseMessage({
      interrupted: true,
      timelineItems: successfulTimeline(),
    }))
    await nextTick()

    const activity = el.querySelector('.assistant-activity')
    const answer = [...el.querySelectorAll<HTMLElement>('.msg-ai-text')]
      .find(node => !activity?.contains(node))
    expect(activity?.classList.contains('assistant-activity--interrupted')).toBe(true)
    expect(activity?.querySelector('.assistant-activity__summary')?.getAttribute('aria-expanded'))
      .toBe('false')
    expect(activity?.querySelector('.assistant-activity__summary')?.textContent)
      .not.toContain('Completed ·')
    expect(answer?.textContent).toBe('Canonical answer')
    expect(activity?.contains(answer ?? null)).toBe(false)
  })

  it('does not claim completion while approval is unresolved', async () => {
    const pending = approvalPart(null)
    const el = mountMessage(baseMessage({
      timelineItems: [...successfulTimeline(), approvalTimelineItem(pending)],
      parts: [pending],
    }))
    await nextTick()

    const summary = el.querySelector('.assistant-activity__summary')
    const card = el.querySelector<HTMLElement>('.approval-card')
    expect(summary?.textContent).toContain('1 web action')
    expect(summary?.textContent).not.toContain('Completed ·')
    expect(card).not.toBeNull()
    expect(el.querySelectorAll('.approval-card')).toHaveLength(1)
    expect(el.querySelector('.assistant-activity')?.contains(card ?? null)).toBe(false)
    expect(el.querySelector('.msg-ai-main')?.lastElementChild).toBe(card)
  })

  it('moves a resolved approval outcome into its chronological activity position', async () => {
    const approved = approvalPart('approved')
    const el = mountMessage(baseMessage({
      timelineItems: [...successfulTimeline(), approvalTimelineItem(approved)],
      parts: [approved],
    }))
    await nextTick()

    const activity = el.querySelector('.assistant-activity')
    const outcome = el.querySelector<HTMLElement>('.approval-outcome')
    expect(outcome).not.toBeNull()
    expect(el.querySelectorAll('.approval-outcome')).toHaveLength(1)
    expect(activity?.contains(outcome ?? null)).toBe(true)
  })

  it('does not claim completion after an approval is denied', async () => {
    // No tool footprint: the summary falls back to the lifecycle label, which
    // must not claim completion while the approval outcome is a denial.
    const el = mountMessage(baseMessage({
      timelineItems: [],
      parts: [approvalPart('denied')],
    }))
    await nextTick()

    const summary = el.querySelector('.assistant-activity__summary')
    expect(summary?.textContent).toContain('Activity ·')
    expect(summary?.textContent).not.toContain('Completed ·')
  })

  it('uses the completed summary after approval and a settled answer', async () => {
    const el = mountMessage(baseMessage({
      timelineItems: [],
      parts: [approvalPart('approved')],
    }))
    await nextTick()

    expect(el.querySelector('.assistant-activity__summary')?.textContent)
      .toBe('Completed')
  })

  it('uses an exact local duration when the live status snapshot provides one', async () => {
    const el = mountMessage(baseMessage({
      ts: 1_725_000_022,
      statusHistory: [
        { action: 'inspect', label: 'Inspecting', at: 1_725_000_001_000 },
        { action: 'write', label: 'Writing', at: 1_725_000_018_000 },
      ],
      timelineItems: successfulTimeline(),
    }))
    await nextTick()

    expect(el.querySelector('.assistant-activity__summary')?.textContent)
      .toContain('Completed · 21s')
    expect(el.querySelector('.assistant-activity__detail')?.textContent).not.toContain('Worked for')
  })

  it('keeps the exact duration when same-session history replaces the local row', async () => {
    const local = mountMessage(baseMessage({
      ts: '2024-08-30T06:40:22.000Z',
      statusHistory: [{
        action: 'inspect',
        label: 'Inspecting',
        at: 1_725_000_001_000,
      }],
      timelineItems: successfulTimeline(),
    }))
    await nextTick()
    expect(local.querySelector('.assistant-activity__summary')?.textContent).toContain('Completed · 21s')

    const restored = mountMessage(baseMessage({
      id: 'server-assistant',
      messageId: 'server-assistant',
      statusHistory: [],
      timelineItems: successfulTimeline(),
    }))
    await nextTick()
    expect(restored.querySelector('.assistant-activity__summary')?.textContent).toContain('Completed · 21s')
  })

  it('keeps the collapsed row compact and moves footprint and elapsed time into details', async () => {
    const el = mountMessage(baseMessage({
      ts: 1_725_000_022,
      statusHistory: [
        { action: 'search', label: 'Searching', at: 1_725_000_001_000 },
      ],
      timelineItems: [
        timelineGroup(successfulCall('search-1', 'web_search')),
        timelineGroup(successfulCall('run-1', 'bash_exec')),
        timelineGroup(successfulCall('artifact-1', 'publish_artifact')),
        timelineGroup(successfulCall('recall-1', 'memory_search')),
      ],
    }))
    await nextTick()

    expect(el.querySelector('.assistant-activity__label')?.textContent)
      .toBe('Completed · 21s')
    // The expanded detail preserves only concrete work footprints; the parent
    // completion label is the single owner of total elapsed time.
    expect(el.querySelector('.assistant-activity__label')?.textContent)
      .not.toContain('item')
    expect(el.querySelector('.assistant-activity__detail')?.textContent)
      .toBe('1 web action · 1 command · 2 more')
  })

  it('persists a measured duration from a watcher even when no disclosure reads it', async () => {
    // A legacy row (timeline text, no canonical answer) renders no activity
    // disclosure, so nothing ever evaluates the duration computed. The write
    // lives in a watcher, not the computed, so the turn duration is still
    // recorded and survives into the restored separable row.
    const legacy = mountMessage(baseMessage({
      text: '',
      ts: 1_725_000_022,
      statusHistory: [
        { action: 'inspect', label: 'Inspecting', at: 1_725_000_001_000 },
      ],
    }))
    await nextTick()
    expect(legacy.querySelector('.assistant-activity')).toBeNull()

    const restored = mountMessage(baseMessage({
      id: 'server-assistant',
      messageId: 'server-assistant',
      statusHistory: [],
      timelineItems: successfulTimeline(),
    }))
    await nextTick()

    expect(restored.querySelector('.assistant-activity__summary')?.textContent)
      .toContain('Completed · 21s')
  })

  it('expands streaming work and automatically folds it when settled', async () => {
    const message = reactive(baseMessage({
      isStreaming: true,
      timelineItems: successfulTimeline(),
      meta: usageMeta(),
    }))
    const el = mountMessage(message)
    await nextTick()

    expect(el.querySelector('.assistant-activity__live-head')?.getAttribute('aria-expanded'))
      .toBe('true')
    expect(el.querySelector('.assistant-activity__body')?.getAttribute('aria-hidden'))
      .toBe('false')
    const liveAnswer = el.querySelector<HTMLElement>('.assistant-answer')
    const liveActivity = el.querySelector<HTMLElement>('.assistant-activity')
    expect(
      Boolean(
        (liveActivity?.compareDocumentPosition(liveAnswer!) ?? 0)
        & Node.DOCUMENT_POSITION_FOLLOWING,
      ),
    ).toBe(true)

    message.isStreaming = false
    await nextTick()

    expect(el.querySelector('.assistant-activity__summary')?.getAttribute('aria-expanded'))
      .toBe('false')
    expect(el.querySelector('.assistant-activity__body')?.getAttribute('aria-hidden'))
      .toBe('true')
    const settledAnswer = el.querySelector<HTMLElement>('.assistant-answer')
    const settledActivity = el.querySelector<HTMLElement>('.assistant-activity')
    expect(settledActivity).toBe(liveActivity)
    expect(
      Boolean(
        (settledActivity?.compareDocumentPosition(settledAnswer!) ?? 0)
        & Node.DOCUMENT_POSITION_FOLLOWING,
      ),
    ).toBe(true)
  })

  it('does not let the tool-detail preference force the outer activity open', async () => {
    useToolDetailPreference().setMode('expanded')
    const el = mountMessage(baseMessage({ timelineItems: successfulTimeline() }))
    await nextTick()

    expect(el.querySelector('.assistant-activity__summary')?.getAttribute('aria-expanded')).toBe('false')
  })

  it('does not apply the tool-detail preference to reasoning-only activity', async () => {
    useToolDetailPreference().setMode('expanded')
    const el = mountMessage(baseMessage({
      timelineItems: [],
      statusHistory: [],
    }))
    await nextTick()

    expect(el.querySelector('.assistant-activity__summary')?.getAttribute('aria-expanded')).toBe('false')
    expect(el.querySelector<HTMLDetailsElement>('.thinking-fold')?.open).toBe(false)
  })

  it('expands the settled activity from the whole summary row with a hover affordance', async () => {
    const el = mountMessage(baseMessage({ timelineItems: successfulTimeline() }))
    await nextTick()

    const activity = el.querySelector<HTMLElement>('.assistant-activity')
    const summary = activity?.querySelector<HTMLButtonElement>('.assistant-activity__summary')
    expect(summary?.querySelector('.assistant-activity__summary-arrow')).not.toBeNull()
    expect(summary?.querySelector('.assistant-activity__chevron')).toBeNull()
    summary?.click()
    await nextTick()

    expect(summary?.getAttribute('aria-expanded')).toBe('true')
    expect(activity?.dataset.shareExpanded).toBe('true')
    expect(activity?.querySelector('.assistant-activity__body')?.getAttribute('aria-hidden'))
      .toBe('false')
  })

  it('keeps user expansion through a same-session history replacement', async () => {
    const local = mountMessage(baseMessage({ timelineItems: successfulTimeline() }))
    await nextTick()
    local.querySelector<HTMLButtonElement>('.assistant-activity__summary')?.click()
    await nextTick()

    const restored = mountMessage(baseMessage({
      id: 'server-assistant',
      messageId: 'server-assistant',
      statusHistory: [],
      timelineItems: successfulTimeline(),
    }))
    await nextTick()

    expect(restored.querySelector('.assistant-activity__summary')?.getAttribute('aria-expanded')).toBe('true')
  })

  it('does not share expansion or duration with another turn that reused tool ids', async () => {
    const first = mountMessage(baseMessage({
      ts: '2024-08-30T06:40:22.000Z',
      turnKey: 'turn:user-1',
      statusHistory: [{
        action: 'inspect',
        label: 'Inspecting',
        at: 1_725_000_001_000,
      }],
      timelineItems: successfulTimeline(),
    }))
    await nextTick()
    first.querySelector<HTMLButtonElement>('.assistant-activity__summary')?.click()
    await nextTick()

    const second = mountMessage(baseMessage({
      id: 'assistant-2',
      messageId: 'assistant-2',
      turnKey: 'turn:user-2',
      ts: null,
      statusHistory: [],
      timelineItems: successfulTimeline(),
    }))
    await nextTick()

    const summary = second.querySelector('.assistant-activity__summary')
    expect(summary?.getAttribute('aria-expanded')).toBe('false')
    expect(summary?.textContent).not.toContain('Worked for 21s')
  })

  it('keeps partial output activity collapsed when the turn ends with a terminal failure', async () => {
    const el = mountMessage(baseMessage({
      text: 'Partial answer before failure.',
      terminalFailure: true,
      timelineItems: successfulTimeline(),
    }))
    await nextTick()

    const activity = el.querySelector('.assistant-activity')
    expect(activity?.classList.contains('assistant-activity--failed')).toBe(true)
    expect(activity?.querySelector('.assistant-activity__summary')?.getAttribute('aria-expanded'))
      .toBe('false')
    expect(el.textContent).toContain('Partial answer before failure.')
  })

  it('preserves legacy narration but removes failed tool rows when no canonical answer exists', async () => {
    const el = mountMessage(baseMessage({
      text: '   ',
      parts: [],
      statusHistory: [],
    }))
    await nextTick()

    const text = el.textContent || ''
    expect(el.querySelector('.assistant-activity')).toBeNull()
    expect(text).toContain('Draft prefix')
    expect(text).toContain('Draft suffix')
    expect(text).not.toContain('Search')
    expect(text).not.toContain('Network unavailable')
    expect(text.indexOf('Draft prefix')).toBeLessThan(text.indexOf('Draft suffix'))
  })

  it('keeps artifacts outside the activity disclosure and actionable', async () => {
    const el = mountMessage(baseMessage({
      artifacts: [{
        id: 'artifact-1',
        name: 'study-notes.md',
        mime: 'text/markdown',
        download_url: '/api/v1/artifacts/artifact-1',
      }],
    }))
    await nextTick()

    const activity = el.querySelector('.assistant-activity')
    const artifacts = el.querySelector<HTMLElement>('.msg-artifacts')
    const ending = el.querySelector<HTMLElement>('[data-testid="done-block"]')
    const footer = el.querySelector<HTMLElement>('.msg-ai-footer')
    expect(activity).not.toBeNull()
    expect(artifacts).not.toBeNull()
    expect(activity?.contains(artifacts ?? null)).toBe(false)
    expect(artifacts?.textContent).toContain('study-notes.md')
    expect(artifacts?.querySelector('button')).not.toBeNull()
    expect(ending?.contains(footer ?? null)).toBe(false)
    expect(ending?.nextElementSibling).toBe(footer)
  })

  it('does not render an empty disclosure for a plain canonical answer', async () => {
    const el = mountMessage(baseMessage({
      timelineItems: [],
      parts: [],
      statusHistory: [],
    }))
    await nextTick()

    expect(el.querySelector('.assistant-activity')).toBeNull()
    expect(el.querySelector('.msg-ai-text')?.textContent).toBe('Canonical answer')
  })
})
