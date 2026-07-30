// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'
import { createPinia } from 'pinia'

import i18n from '@/i18n'
import { useToolDetailPreference } from '@/composables/useToolDetailPreference'
import { clearAssistantActivityExpansionState } from '@/utils/chat/activityDisclosureState'
import type {
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

beforeEach(() => {
  i18n.global.locale.value = 'en'
  clearAssistantActivityExpansionState()
  useToolDetailPreference().setMode('auto')
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('AssistantMessage activity disclosure', () => {
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
    expect(el.querySelector('.assistant-activity__detail')?.textContent).toContain('Worked for 21s')
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
    expect(local.querySelector('.assistant-activity__detail')?.textContent).toContain('Worked for 21s')

    const restored = mountMessage(baseMessage({
      id: 'server-assistant',
      messageId: 'server-assistant',
      statusHistory: [],
      timelineItems: successfulTimeline(),
    }))
    await nextTick()
    expect(restored.querySelector('.assistant-activity__detail')?.textContent).toContain('Worked for 21s')
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
    // The expanded detail preserves the exact footprint and elapsed metadata.
    expect(el.querySelector('.assistant-activity__label')?.textContent)
      .not.toContain('item')
    expect(el.querySelector('.assistant-activity__detail')?.textContent)
      .toBe('1 web action · 1 command · 2 more · Worked for 21s')
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

    expect(restored.querySelector('.assistant-activity__detail')?.textContent)
      .toContain('Worked for 21s')
  })

  it('keeps streaming work collapsed until the user expands it', async () => {
    const el = mountMessage(baseMessage({
      isStreaming: true,
      timelineItems: successfulTimeline(),
    }))
    await nextTick()

    const summary = el.querySelector<HTMLButtonElement>('.assistant-activity__live-head')
    const body = el.querySelector<HTMLElement>('.assistant-activity__body')
    expect(summary?.getAttribute('aria-expanded')).toBe('false')
    expect(summary?.textContent).toContain('Working')
    expect(body?.getAttribute('aria-hidden')).toBe('true')
    expect(body?.classList.contains('is-open')).toBe(false)
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
    expect(activity?.querySelector('.assistant-activity__body')?.classList.contains('is-open'))
      .toBe(true)
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
