// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'

import i18n from '@/i18n'
import type {
  ChatStreamTimelineItem,
  ChatToolCallRenderItem,
} from '@/types/chat'
import { projectAssistantActivityTimeline } from '@/utils/chat/assistantActivity'
import AssistantActivityTimeline from './AssistantActivityTimeline.vue'
import timelineSource from './AssistantActivityTimeline.vue?raw'

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
  statusHistory: Array<{ action: string; label: string; at: number }> = [],
  lifecycle: 'working' | 'answering' | 'settled' = 'working',
) {
  const root = document.createElement('div')
  document.body.appendChild(root)
  const projection = projectAssistantActivityTimeline(timelineItems, {
    lifecycle,
    statusHistory,
  })
  const app = createApp({
    render: () => h(AssistantActivityTimeline, {
      class: 'external-activity-class',
      projection,
      timelineItems,
      isToolGroupOpen: () => false,
      isToolItemOpen: () => false,
      toolGroupStatusText: () => 'Done',
      toolStatusText: () => 'Done',
      toolSecondaryText: () => 'private detail',
    }),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(root)
  await nextTick()
  return root
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('AssistantActivityTimeline', () => {
  it('inherits caller attributes on its semantic root without Vue fragment warnings', async () => {
    const root = await mountTimeline([
      group(toolCall('attribute-root', 'read_file')),
    ])

    expect(
      root.querySelector('.assistant-activity-timeline')
        ?.classList.contains('external-activity-class'),
    ).toBe(true)
  })

  it('renders contiguous equivalent calls as one safe semantic group', async () => {
    const root = await mountTimeline([
      group(toolCall('write-secret', 'write_file')),
      group(toolCall('edit-secret', 'edit_file')),
    ])

    expect(root.querySelectorAll('.step-card')).toHaveLength(1)
    expect(root.querySelector('.tool-row__label')?.textContent).toBe('Edited files')
    expect(root.querySelector('.tool-row__arg')?.textContent).toBe('2 files')
    // The footprint carries the count; the raw call-count pill would repeat it.
    expect(root.querySelector('.step-count')).toBeNull()
    expect(root.textContent).not.toContain('2 calls')
    expect(root.textContent).not.toContain('/private/')
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

  it('folds consecutive tool purposes into a readable batch before call details', async () => {
    const root = await mountTimeline([
      group(toolCall('read-one', 'read_file')),
      group(toolCall('run-one', 'execute_code')),
    ])

    const batch = root.querySelector<HTMLDetailsElement>('.assistant-activity-tool-batch')
    expect(batch).not.toBeNull()
    expect(batch?.open).toBe(false)
    expect(batch?.querySelector('.assistant-activity-tool-batch__summary')?.textContent)
      .toContain('Inspected files')
    expect(batch?.querySelector('.assistant-activity-tool-batch__summary')?.textContent)
      .toContain('Ran commands')
    expect(batch?.querySelectorAll('.tool-row')).toHaveLength(2)

    batch?.querySelector<HTMLElement>('summary')?.click()
    expect(batch?.open).toBe(true)
  })

  it('keeps lifecycle phases out of the live action body', async () => {
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

    expect(liveRoot.querySelectorAll('.assistant-activity-status__row')).toHaveLength(0)
    expect(liveRoot.querySelector('.assistant-activity-timeline')).toBeNull()
    expect(liveRoot.textContent).not.toContain('/private/customer')
    expect(liveRoot.textContent).not.toContain('secret')

    const settledRoot = await mountTimeline([], statusHistory, 'settled')
    expect(settledRoot.querySelectorAll('.assistant-activity-status__row')).toHaveLength(2)
    expect(settledRoot.textContent).toContain('Working')
    expect(settledRoot.textContent).toContain('Writing the answer')
    expect(settledRoot.textContent).not.toContain('/private/customer')
    expect(settledRoot.textContent).not.toContain('secret')
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
