// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Fragment, createApp, createCommentVNode, h, nextTick, shallowRef, type App, type VNode } from 'vue'

import i18n from '@/i18n'
import type {
  ChatStreamTimelineItem,
  ChatToolCallRenderItem,
} from '@/types/chat'
import type { StatusPart } from '@/types/parts'
import { projectAssistantActivityTimeline, type AssistantActivityLifecycle } from '@/utils/chat/assistantActivity'
import AssistantActivityTimeline from './AssistantActivityTimeline.vue'
import timelineSource from './AssistantActivityTimeline.vue?raw'
import { useToolDetailPreference } from '@/composables/useToolDetailPreference'
import { provideActivityDisclosureVisibility } from '@/composables/chat/useActivityDisclosureVisibility'
import { createPinia } from 'pinia'

const mountedApps: App[] = []

function ruleBody(selector: string) {
  const selectorStart = timelineSource.indexOf(selector)
  expect(selectorStart).toBeGreaterThanOrEqual(0)

  const blockStart = timelineSource.indexOf('{', selectorStart)
  const blockEnd = timelineSource.indexOf('}', blockStart)
  return timelineSource.slice(blockStart + 1, blockEnd)
}

function toolCall(id: string, name: string): ChatToolCallRenderItem {
  return {
    toolId: id,
    renderKey: id,
    name,
    displayName: `Private ${id}`,
    inputRaw: `{"path":"/private/${id}"}`,
    inputPreview: `/private/${id}`,
    isRunning: false,
    status: 'success',
    isError: false,
    result: 'ok',
    resultPreview: 'ok',
    isOpen: false,
  }
}

function group(call: ChatToolCallRenderItem): ChatStreamTimelineItem {
  return {
    type: 'tool-group',
    key: `group-${call.toolId}`,
    group: {
      groupId: `group-${call.toolId}`,
      operationKey: 'file.edit',
      label: call.displayName,
      iconName: 'edit',
      calls: [call],
      secondary: call.inputPreview,
      isRunning: false,
      isError: false,
      status: 'success',
    },
  }
}

async function mountTimeline(
  timelineItems: ChatStreamTimelineItem[],
  statusHistory: StatusPart[] = [],
  lifecycle: AssistantActivityLifecycle = 'working',
  interruptSlot?: (props: { part: Extract<import('@/types/parts').ChatPart, { type: 'interrupt' }> }) => VNode[],
) {
  const root = document.createElement('div')
  document.body.appendChild(root)
  const projection = projectAssistantActivityTimeline(timelineItems, {
    lifecycle,
    statusHistory,
    endedAt: (statusHistory[statusHistory.length - 1]?.at ?? 0) + 3_000,
  })
  const app = createApp({
    render: () => h(AssistantActivityTimeline, {
      class: 'external-activity-class',
      projection,
      timelineItems,
      isToolGroupOpen: () => undefined,
      isToolItemOpen: () => undefined,
      toolGroupStatusText: () => 'Done',
      toolStatusText: () => 'Done',
      toolSecondaryText: () => 'private detail',
    }, interruptSlot ? { interrupt: interruptSlot } : undefined),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.use(createPinia())
  app.mount(root)
  await nextTick()
  return root
}

beforeEach(() => {
  useToolDetailPreference().setMode('collapsed')
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('AssistantActivityTimeline', () => {
  it.each([
    ['provider:requesting', 'Waiting for model'],
    ['Preparing tool call', 'Preparing tool call'],
  ])('gives %s quiet text feedback without a dot or second timer', async (action, label) => {
    const executing = shallowRef(true)
    const visible = shallowRef(true)
    const history: StatusPart[] = [{ action, label: action, at: 1_000 }]
    const projection = shallowRef(projectAssistantActivityTimeline([], {
      lifecycle: 'working', statusHistory: history, endedAt: 3_000,
    }))
    const root = document.createElement('div')
    document.body.appendChild(root)
    const app = createApp({ setup() {
      provideActivityDisclosureVisibility(visible)
      return () => h(AssistantActivityTimeline, {
        projection: projection.value, executing: executing.value,
        isToolGroupOpen: () => undefined, isToolItemOpen: () => undefined,
        toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '',
      })
    } })
    mountedApps.push(app)
    app.use(i18n)
    app.use(createPinia())
    app.mount(root)
    await nextTick()
    expect(root.textContent).toContain(label)
    expect(root.querySelectorAll('.activity-motion--active')).toHaveLength(1)
    expect(root.querySelector('.assistant-activity-status__dot')).toBeNull()
    expect(root.querySelector('.assistant-activity-status__duration')).toBeNull()

    visible.value = false
    await nextTick()
    expect(root.querySelector('.activity-motion--active')).toBeNull()
    visible.value = true
    executing.value = false
    await nextTick()
    expect(root.querySelector('.activity-motion--active')).toBeNull()

    // An authoritative tool start takes over the preparing/waiting row.
    executing.value = true
    const tool = toolCall('running-write', 'write_file')
    tool.isRunning = true
    tool.status = ''
    projection.value = projectAssistantActivityTimeline([group(tool)], {
      lifecycle: 'working', statusHistory: history, endedAt: 4_000,
    })
    await nextTick()
    expect(root.querySelector('.assistant-activity-status__row--routine')).toBeNull()
    expect(root.textContent).not.toContain(label)

    projection.value = projectAssistantActivityTimeline([], {
      lifecycle: 'interrupted', statusHistory: history, endedAt: 5_000,
    })
    await nextTick()
    expect(root.querySelector('.assistant-activity-status__row')).toBeNull()
    expect(root.querySelector('.activity-motion--active')).toBeNull()
  })

  it.each(['absent', 'comment', 'fragment'] as const)(
    'omits consecutive %s interrupt slots without breaking visible reading boundaries',
    async kind => {
      const interrupt = (key: string, activityOrder: number): ChatStreamTimelineItem => ({
        type: 'interrupt', key, activityOrder, approvalId: key,
        part: { type: 'interrupt', key, interruptKind: 'approval', resolution: null, busy: false, error: '' },
      })
      const slot = kind === 'absent' ? undefined : vi.fn(() => kind === 'comment'
        ? [createCommentVNode('v-if')]
        : [h(Fragment, null, [createCommentVNode('v-if')])])
      const root = await mountTimeline([
        { type: 'text', key: 'before-tools', html: '<p>Before tools.</p>', activityOrder: 1 },
        interrupt('empty-1', 2), interrupt('empty-2', 3),
        { ...group(toolCall('after-empty', 'read_file')), activityOrder: 4 },
        { type: 'text', key: 'before-prose', html: '<p>Before prose.</p>', activityOrder: 5 },
        interrupt('empty-3', 6), interrupt('empty-4', 7),
        { type: 'text', key: 'after-prose', html: '<p>After prose.</p>', activityOrder: 8 },
      ], [], 'settled', slot)

      expect(root.querySelectorAll('.run-trace__interrupt')).toHaveLength(0)
      expect(root.querySelectorAll('.activity-narration + .tool-timeline .step-card')).toHaveLength(1)
      expect(root.querySelectorAll('.activity-narration + .activity-narration')).toHaveLength(1)
      expect([...root.querySelector('.assistant-activity-timeline')!.children].map(node =>
        node.classList.contains('activity-narration') ? node.textContent : 'tool',
      )).toEqual(['Before tools.', 'tool', 'Before prose.', 'After prose.'])
      if (slot) expect(slot).toHaveBeenCalledTimes(4)
    },
  )

  it('evaluates a visible interrupt slot once and preserves its activity identity and order', async () => {
    const slot = vi.fn(({ part }: { part: Extract<import('@/types/parts').ChatPart, { type: 'interrupt' }> }) =>
      [h(Fragment, null, [createCommentVNode('leading comment'), h('button', { type: 'button' }, part.key)])])
    const root = await mountTimeline([
      { type: 'text', key: 'before', html: '<p>Before approval.</p>', activityOrder: 1 },
      {
        type: 'interrupt', key: 'approval-slot', approvalId: 'approval-slot', activityOrder: 2,
        part: { type: 'interrupt', key: 'approved', interruptKind: 'approval', resolution: 'approved', busy: false, error: '' },
      },
      { type: 'text', key: 'after', html: '<p>After approval.</p>', activityOrder: 3 },
    ], [], 'settled', slot)

    const interrupt = root.querySelector('.run-trace__interrupt')
    expect(interrupt?.getAttribute('data-activity-id')).toBe('approval-slot')
    expect(interrupt?.getAttribute('data-activity-order')).toBe('2')
    expect(interrupt?.querySelector('button')?.textContent).toBe('approved')
    expect([...root.querySelectorAll('[data-activity-entry]')].map(node => node.getAttribute('data-activity-order')))
      .toEqual(['1', '2', '3'])
    expect(slot).toHaveBeenCalledTimes(1)
  })

  it('retains text-only Fragment slot content', async () => {
    const root = await mountTimeline([{
      type: 'interrupt', key: 'text-interrupt', approvalId: 'text-interrupt', activityOrder: 1,
      part: { type: 'interrupt', key: 'approved', interruptKind: 'approval', resolution: 'approved', busy: false, error: '' },
    }], [], 'settled', () => [h(Fragment, null, ['Approval recorded'])])
    expect(root.querySelector('.run-trace__interrupt')?.textContent).toBe('Approval recorded')
  })

  it('runs compaction feedback only while executing inside a visible activity', async () => {
    const executing = shallowRef(true)
    const visible = shallowRef(true)
    const projection = shallowRef(projectAssistantActivityTimeline([], {
      lifecycle: 'working',
      statusHistory: [{
        action: 'context_compaction', label: '', at: 1_000,
        id: 'cmp-running', category: 'maintenance', state: 'running',
        source: 'automatic',
      }],
      endedAt: 2_000,
    }))
    const root = document.createElement('div')
    document.body.appendChild(root)
    const app = createApp({
      setup() {
        provideActivityDisclosureVisibility(visible)
        return () => h(AssistantActivityTimeline, {
          projection: projection.value,
          executing: executing.value,
          isToolGroupOpen: () => undefined,
          isToolItemOpen: () => undefined,
          toolGroupStatusText: () => '',
          toolStatusText: () => '',
          toolSecondaryText: () => '',
        })
      },
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(root)
    await nextTick()
    const isAnimating = () => !!root.querySelector('.assistant-activity-status__row--animating')
    expect(isAnimating()).toBe(true)

    executing.value = false
    await nextTick()
    expect(isAnimating()).toBe(false)
    expect(root.querySelector('[data-testid="compaction-event"]')).not.toBeNull()

    visible.value = false
    executing.value = true
    await nextTick()
    expect(isAnimating()).toBe(false)

    visible.value = true
    await nextTick()
    expect(isAnimating()).toBe(true)

    projection.value = projectAssistantActivityTimeline([], {
      lifecycle: 'settled',
      statusHistory: [{
        action: 'context_compaction', label: '', at: 1_000,
        id: 'cmp-running', category: 'maintenance', state: 'completed',
        source: 'automatic', durability: 'durable',
      }],
      endedAt: 2_000,
    })
    await nextTick()
    expect(isAnimating()).toBe(false)
    expect(root.textContent).toContain('Context organized')
  })

  it.each(['working', 'settled'] as const)('distinguishes temporary reductions from saved summaries while %s', async lifecycle => {
    const root = await mountTimeline([], [
      {
        action: 'context_compaction', label: '', at: 1_000,
        id: 'cmp-temporary', category: 'maintenance', state: 'completed',
        source: 'automatic', durability: 'request_scoped',
      },
      {
        action: 'context_compaction', label: '', at: 2_000,
        id: 'cmp-saved', category: 'maintenance', state: 'completed',
        source: 'automatic', durability: 'durable',
      },
    ], lifecycle)

    const events = root.querySelectorAll<HTMLElement>('[data-testid="compaction-event"]')
    expect(events).toHaveLength(2)
    expect(events[0]?.textContent).not.toContain('History temporarily reduced; continuing')
    expect(events[0]?.textContent).toContain('Context organized')
    expect(events[0]?.querySelector('button')?.getAttribute('aria-label')).toBe('History temporarily reduced; continuing')
    expect(events[0]?.dataset.status).toBe('completed')
    expect(events[0]?.dataset.durability).toBe('request_scoped')
    expect(events[1]?.textContent).toContain('Context organized')
    for (const event of events) {
      expect(event.classList.contains('assistant-activity-status__row--failed')).toBe(false)
      expect(event.querySelector('.assistant-activity-status__dot, .compaction-recovery-marker__ring')).not.toBeNull()
      expect(event.querySelector('.assistant-activity-status__duration')).toBeNull()
      expect(event.textContent).not.toContain('session history was not rewritten')
    }
    expect(root.querySelector('.assistant-activity-status__row--current')).toBeNull()
  })

  it.each(['working', 'settled'] as const)('does not announce an automatic failed attempt as a terminal chat error while %s', async lifecycle => {
    const root = await mountTimeline([], [{
      action: 'context_compaction', label: '', at: 1_000,
      id: 'cmp-failed-attempt', category: 'maintenance', state: 'failed',
      source: 'automatic', failureKind: 'rate_limited', httpStatus: 429,
    }], lifecycle)
    const event = root.querySelector<HTMLElement>('[data-testid="compaction-event"]')!
    expect(event.dataset.status).toBe('failed')
    expect(event.textContent?.trim()).toBe(i18n.global.t('chat.compact.incomplete'))
    expect(event.classList.contains('assistant-activity-status__row--failed')).toBe(false)
    expect(event.querySelector('[role="alert"]')).toBeNull()
    expect(event.querySelector('[role="status"]')).not.toBeNull()
    expect(event.querySelector('.assistant-activity-status__duration')).toBeNull()
  })

  it('inherits caller attributes on its semantic root without Vue fragment warnings', async () => {
    const root = await mountTimeline([
      group(toolCall('attribute-root', 'read_file')),
    ])

    expect(
      root.querySelector('.assistant-activity-timeline')
        ?.classList.contains('external-activity-class'),
    ).toBe(true)
  })

  it('renders equivalent calls individually with their real targets and no count group', async () => {
    const root = await mountTimeline([
      group(toolCall('write-secret', 'write_file')),
      group(toolCall('edit-secret', 'edit_file')),
    ])

    expect(root.querySelectorAll('.step-card')).toHaveLength(2)
    expect([...root.querySelectorAll('.tool-row__label')].map(row => row.textContent))
      .toEqual(['Written', 'Edited'])
    expect([...root.querySelectorAll('.tool-row__arg')].map(row => row.textContent))
      .toEqual(['…/write-secret', '…/edit-secret'])
    expect(root.querySelector('.step-count')).toBeNull()
    expect(root.querySelector('.tool-row--group')).toBeNull()
    expect(root.textContent).not.toContain('2 calls')
    expect(root.textContent).not.toContain('2 files')
    expect(root.textContent).not.toContain('{"path"')
    expect(root.textContent).not.toContain('Private write-secret')
  })

  it('renders the present-tense purpose the projection emits for a running cluster', async () => {
    const running = toolCall('running-search', 'web_search')
    running.isRunning = true
    running.status = ''
    const root = await mountTimeline([group(running)], [], 'working')

    // The projection emits chat.activity.purposeRunning.* while a cluster is
    // current; the timeline must render that code untouched — no cached or
    // re-derived past-tense label may shadow it.
    expect(root.querySelector('.tool-row__label')?.textContent).toBe('Searching the web')
    expect(root.textContent).not.toContain('Searched the web')
  })

  it('centers phase dots in the marker column shared with tool-row icons', () => {
    // 0.125rem row padding + 0.875rem marker column + 0.625rem gap = 1.625rem,
    // the same left origin the tool-row labels use.
    expect(ruleBody('.assistant-activity-status__row {')).toContain('gap: 0.625rem;')
    expect(ruleBody('.assistant-activity-status__dot {')).toContain('margin: 0 0.25rem;')
  })

  it('omits empty rendered narration between tools without changing source items', async () => {
    const items: ChatStreamTimelineItem[] = [
      group(toolCall('before-blank', 'read_file')),
      { type: 'text', key: 'blank', html: '', rawText: '\n\n', activityOrder: 2 },
      group(toolCall('after-blank', 'execute_code')),
    ]
    const root = await mountTimeline(items)
    expect(root.querySelector('.activity-narration')).toBeNull()
    expect(root.querySelectorAll('.tool-timeline')).toHaveLength(1)
    expect([...root.querySelectorAll('.tool-row__label')].map(row => row.textContent))
      .toEqual(['Read', 'Ran'])
    expect(items[1]).toMatchObject({ rawText: '\n\n', activityOrder: 2 })
  })

  it('does not leave an empty activity batch for whitespace-only rendered content', async () => {
    const root = await mountTimeline([
      { type: 'text', key: 'blank', html: ' \n ', rawText: '\n' },
    ], [], 'settled')
    expect(root.querySelector('.assistant-activity-timeline')).toBeNull()
  })

  it('keeps narration as a semantic grouping boundary', async () => {
    const root = await mountTimeline([
      group(toolCall('write-before', 'write_file')),
      {
        type: 'text',
        key: 'narration',
        html: '<p>Checking the first change.</p>',
        rawText: 'Checking the first change.',
      },
      group(toolCall('edit-after', 'edit_file')),
    ])

    expect(root.querySelectorAll('.step-card')).toHaveLength(2)
    expect(root.querySelector('.assistant-activity-tool-batch')).toBeNull()
    expect(root.textContent).toContain('Checking the first change.')
  })

  it('renders consecutive mixed tools without an extra disclosure level', async () => {
    const root = await mountTimeline([
      group(toolCall('read-one', 'read_file')),
      group(toolCall('run-one', 'execute_code')),
    ])

    expect(root.querySelector('.assistant-activity-tool-batch')).toBeNull()
    expect(root.querySelector('.tool-row--group')).toBeNull()
    expect(root.querySelectorAll('.tool-row')).toHaveLength(2)
    expect([...root.querySelectorAll('.tool-row__label')].map(row => row.textContent))
      .toEqual(['Read', 'Ran'])
  })

  it('shows only the current routine phase live and removes it from settled history', async () => {
    const statusHistory = [
      {
        action: 'Sending',
        label: 'Sending /private/customer/secret.txt',
        at: 1_000,
      },
      {
        action: 'write:1',
        label: 'Writing /private/customer/secret.txt',
        at: 2_000,
      },
    ]
    const liveRoot = await mountTimeline([], statusHistory, 'answering')

    expect(liveRoot.querySelectorAll('.assistant-activity-status__row')).toHaveLength(1)
    expect(liveRoot.textContent).toContain('Writing the answer')
    expect(liveRoot.querySelector('.assistant-activity-status__duration')).toBeNull()
    expect(liveRoot.textContent).not.toContain('/private/customer')
    expect(liveRoot.textContent).not.toContain('secret')

    const settledRoot = await mountTimeline([], statusHistory, 'settled')
    expect(settledRoot.querySelector('.assistant-activity-timeline')).toBeNull()
    expect(settledRoot.textContent).not.toContain('Working')
    expect(settledRoot.textContent).not.toContain('Answer composition')
    expect(settledRoot.textContent).not.toContain('/private/customer')
    expect(settledRoot.textContent).not.toContain('secret')
  })

  it('updates the live phase in place and removes it after terminal handoff', async () => {
    const statusHistory: StatusPart[] = [
      { action: 'provider:requesting', label: 'Waiting', at: 1_000 },
    ]
    const projection = shallowRef(projectAssistantActivityTimeline([], {
      lifecycle: 'working', statusHistory, endedAt: 2_000,
    }))
    const root = document.createElement('div')
    document.body.appendChild(root)
    const app = createApp({
      render: () => h(AssistantActivityTimeline, {
        projection: projection.value,
        isToolGroupOpen: () => undefined,
        isToolItemOpen: () => undefined,
        toolGroupStatusText: () => '',
        toolStatusText: () => '',
        toolSecondaryText: () => '',
      }),
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(root)
    await nextTick()
    expect(root.textContent).toContain('Waiting for model')

    statusHistory.push({ action: 'write:1', label: 'Writing', at: 2_000 })
    projection.value = projectAssistantActivityTimeline([], {
      lifecycle: 'answering', statusHistory, endedAt: 3_000,
    })
    await nextTick()
    await vi.waitFor(() => {
      expect(root.querySelectorAll('.assistant-activity-status__row--current')).toHaveLength(1)
    })
    expect(root.textContent).toContain('Writing the answer')
    expect(root.textContent).not.toContain('Waiting for model')

    projection.value = projectAssistantActivityTimeline([], {
      lifecycle: 'settled', statusHistory, endedAt: 4_000,
    })
    await nextTick()
    expect(root.querySelector('.assistant-activity-timeline')).toBeNull()
    expect(projection.value.statusSteps.map(step => step.durationSeconds)).toEqual([1, 2])
    expect(statusHistory).toHaveLength(2)
  })

  it.each(['settled', 'failed', 'interrupted'] as const)('retains exceptional provider transitions without routine rows when %s', async lifecycle => {
    const root = await mountTimeline([], [
      { action: 'provider:requesting', label: 'Waiting', at: 1_000 },
      { action: 'provider:reasoning', label: 'Reasoning', at: 2_000 },
      { action: 'provider:fallback', label: 'Fallback', at: 3_000 },
      { action: 'provider:retrying:2:3', label: 'Retry', at: 4_000 },
      { action: 'write:1', label: 'Writing', at: 5_000 },
    ], lifecycle)

    const rows = root.querySelectorAll('.assistant-activity-status__row')
    expect(rows).toHaveLength(2)
    expect(root.textContent).not.toContain('Model response')
    expect(root.textContent).not.toContain('Waiting for model')
    expect(root.textContent).not.toContain('Answer composition')
    expect(root.textContent).not.toContain('Writing the answer')
    expect(root.textContent).not.toContain('Thinking')
    expect(root.textContent).toContain('Tried a backup model')
    expect(rows[1]?.textContent).toContain('Retried the model request')
  })

  it('shows prior semantic context but leaves the current action to the live header', async () => {
    const root = await mountTimeline([], [
      {
        action: 'inspect',
        label: 'Inspecting /private/customer/secret.txt',
        at: 1_000,
      },
      {
        action: 'change',
        label: 'Editing /private/customer/secret.txt',
        at: 2_000,
      },
    ])

    const rows = root.querySelectorAll('.assistant-activity-status__row')
    expect(rows).toHaveLength(1)
    expect(rows[0]?.textContent).toContain('Inspected files')
    expect(root.textContent).not.toContain('Edited files')
    expect(root.textContent).not.toContain('/private/customer')
  })
})
