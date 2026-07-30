// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, reactive, ref, type App } from 'vue'

import i18n from '@/i18n'
import type {
  ChatStreamTimelineItem,
  ChatToolCallRenderItem,
} from '@/types/chat'
import ToolCallTimeline from '@/components/chat/ToolCallTimeline.vue'
import { useToolDetailPreference } from '@/composables/useToolDetailPreference'
import RunTrace from './RunTrace.vue'

type ToolGroupItem = Extract<ChatStreamTimelineItem, { type: 'tool-group' }>

const mountedApps: App[] = []

function call(
  renderKey: string,
  name: string,
  overrides: Partial<ChatToolCallRenderItem> = {},
): ChatToolCallRenderItem {
  return {
    toolId: renderKey,
    renderKey,
    name,
    displayName: name,
    inputRaw: '{}',
    inputPreview: '{}',
    isRunning: false,
    status: 'success',
    isError: false,
    result: 'ok',
    resultPreview: 'ok',
    isOpen: false,
    ...overrides,
  }
}

function group(
  groupId: string,
  calls: ChatToolCallRenderItem[],
): ToolGroupItem {
  const isError = calls.some(entry => entry.isError || entry.status === 'error')
  const isRunning = calls.some(entry => entry.isRunning)
  return {
    type: 'tool-group',
    key: groupId,
    group: {
      groupId,
      operationKey: groupId,
      label: groupId,
      iconName: 'gear',
      calls,
      secondary: '',
      isRunning,
      isError,
      status: isError ? 'error' : (calls.every(entry => entry.status === 'success') ? 'success' : ''),
    },
  }
}

function flip(values: Set<string>, key: string): void {
  if (values.has(key)) values.delete(key)
  else values.add(key)
}

async function mountRunTrace(
  initialItems: ChatStreamTimelineItem[],
  options: {
    showBulkToggle?: boolean
    stateScope?: string
    initialGroupToggles?: string[]
    initialItemToggles?: string[]
  } = {},
) {
  const el = document.createElement('div')
  document.body.appendChild(el)

  const items = ref<ChatStreamTimelineItem[]>(initialItems)
  const groupToggles = reactive(new Set(options.initialGroupToggles ?? []))
  const itemToggles = reactive(new Set(options.initialItemToggles ?? []))
  const onToggleGroup = vi.fn((groupId: string) => flip(groupToggles, groupId))
  const onToggleItem = vi.fn((renderKey: string) => flip(itemToggles, renderKey))

  const Host = defineComponent({
    setup() {
      return () => h(RunTrace, {
        items: items.value,
        ...(options.showBulkToggle === undefined
          ? {}
          : { showBulkToggle: options.showBulkToggle }),
        ...(options.stateScope === undefined
          ? {}
          : { stateScope: options.stateScope }),
        isToolGroupOpen: (groupId: string) => groupToggles.has(groupId),
        isToolItemOpen: (renderKey: string) => itemToggles.has(renderKey),
        onToggleGroup,
        onToggleItem,
      })
    },
  })

  const app = createApp(Host)
  mountedApps.push(app)
  app.use(i18n)
  app.mount(el)
  await nextTick()

  return {
    el,
    items,
    groupToggles,
    itemToggles,
    onToggleGroup,
    onToggleItem,
  }
}

function bulkButton(el: HTMLElement): HTMLButtonElement | null {
  return el.querySelector<HTMLButtonElement>('[data-testid="run-trace-bulk-toggle"]')
}

function bodyCount(el: HTMLElement): number {
  return el.querySelectorAll('.tool-row-body').length
}

function groupHeader(el: HTMLElement, groupId: string): HTMLButtonElement | null {
  return el.querySelector<HTMLButtonElement>(`.tool-row--group[data-op="${groupId}"]`)
}

function toolRow(el: HTMLElement, operationKey: string): HTMLButtonElement | null {
  return el.querySelector<HTMLButtonElement>(`.tool-row[data-op="${operationKey}"]`)
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  useToolDetailPreference().setMode('auto')
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('RunTrace bulk toggle', () => {
  it('is opt-in for chat surfaces', async () => {
    const { el } = await mountRunTrace([
      group('search-group', [call('search', 'web_search')]),
      group('shell-group', [call('shell', 'shell')]),
    ])

    expect(bulkButton(el)).toBeNull()
  })

  it('counts top-level tool groups instead of calls for the visibility threshold', async () => {
    const { el } = await mountRunTrace([
      group('batch', [
        call('search', 'web_search'),
        call('read', 'read_file'),
      ]),
    ], { showBulkToggle: true })

    expect(el.querySelectorAll('.tool-row')).toHaveLength(1)
    expect(bulkButton(el)).toBeNull()
  })

  it('uses actual open state and item events for single-call groups', async () => {
    const { el, onToggleItem, itemToggles } = await mountRunTrace([
      group('search-group', [call('search', 'web_search')]),
      group('shell-group', [call('shell', 'shell')]),
    ], { showBulkToggle: true })

    expect(bodyCount(el)).toBe(1)
    expect(bulkButton(el)?.textContent?.trim()).toBe('Collapse all')
    expect(bulkButton(el)?.hasAttribute('aria-expanded')).toBe(false)

    bulkButton(el)?.click()
    await nextTick()

    expect(onToggleItem.mock.calls.map(([key]) => key)).toEqual(['shell'])
    expect([...itemToggles]).toEqual(['shell'])
    expect(bodyCount(el)).toBe(0)
    expect(bulkButton(el)?.textContent?.trim()).toBe('Expand all')

    onToggleItem.mockClear()
    bulkButton(el)?.click()
    await nextTick()

    expect(onToggleItem.mock.calls.map(([key]) => key)).toEqual(['search', 'shell'])
    expect(new Set(itemToggles)).toEqual(new Set(['search']))
    expect(bodyCount(el)).toBe(2)
    expect(bulkButton(el)?.textContent?.trim()).toBe('Collapse all')
  })

  it('uses group and member events for multi-call groups', async () => {
    const { el, onToggleGroup, onToggleItem } = await mountRunTrace([
      group('read-batch', [
        call('search', 'web_search'),
        call('read', 'read_file'),
      ]),
      group('write-batch', [
        call('shell', 'shell'),
        call('write', 'write_file'),
      ]),
    ], { showBulkToggle: true })

    expect(groupHeader(el, 'read-batch')?.getAttribute('aria-expanded')).toBe('false')
    expect(groupHeader(el, 'write-batch')?.getAttribute('aria-expanded')).toBe('true')

    bulkButton(el)?.click()
    await nextTick()

    expect(onToggleGroup.mock.calls.map(([key]) => key)).toEqual(['write-batch'])
    expect(onToggleItem.mock.calls.map(([key]) => key)).toEqual(['shell', 'write'])
    expect(groupHeader(el, 'read-batch')?.getAttribute('aria-expanded')).toBe('false')
    expect(groupHeader(el, 'write-batch')?.getAttribute('aria-expanded')).toBe('false')
    expect(bodyCount(el)).toBe(0)

    onToggleGroup.mockClear()
    onToggleItem.mockClear()
    bulkButton(el)?.click()
    await nextTick()

    expect(onToggleGroup.mock.calls.map(([key]) => key)).toEqual([
      'read-batch',
      'write-batch',
    ])
    expect(onToggleItem.mock.calls.map(([key]) => key)).toEqual([
      'search',
      'read',
      'shell',
      'write',
    ])
    expect(groupHeader(el, 'read-batch')?.getAttribute('aria-expanded')).toBe('true')
    expect(groupHeader(el, 'write-batch')?.getAttribute('aria-expanded')).toBe('true')
    expect(el.querySelectorAll('.tool-row--member[aria-expanded="true"]')).toHaveLength(4)
    expect(bodyCount(el)).toBe(4)
  })

  it('routes mixed targets through groups, members, and errors', async () => {
    const errorCall = call('error', 'custom_tool', {
      status: 'error',
      isError: true,
      result: 'failed',
      resultPreview: 'failed',
    })
    const { el, itemToggles, onToggleGroup, onToggleItem } = await mountRunTrace([
      group('search-group', [call('single-search', 'web_search')]),
      group('error-group', [errorCall]),
      group('mixed-batch', [
        call('batch-shell', 'shell'),
        call('batch-search', 'web_search'),
      ]),
    ], {
      showBulkToggle: true,
      initialItemToggles: ['batch-search'],
    })

    expect(bodyCount(el)).toBe(3)

    bulkButton(el)?.click()
    await nextTick()

    expect(onToggleItem.mock.calls.map(([key]) => key)).toEqual([
      'error',
      'batch-shell',
      'batch-search',
    ])
    expect(onToggleGroup.mock.calls.map(([key]) => key)).toEqual(['mixed-batch'])
    expect(itemToggles.has('batch-search')).toBe(false)
    expect(bodyCount(el)).toBe(0)

    onToggleGroup.mockClear()
    onToggleItem.mockClear()
    bulkButton(el)?.click()
    await nextTick()

    expect(onToggleItem.mock.calls.map(([key]) => key)).toEqual([
      'single-search',
      'error',
      'batch-shell',
      'batch-search',
    ])
    expect(onToggleGroup.mock.calls.map(([key]) => key)).toEqual(['mixed-batch'])
    expect(itemToggles.has('batch-search')).toBe(true)
    expect(bodyCount(el)).toBe(4)
  })

  it('keeps bulk-expanded live targets open when an error changes their defaults', async () => {
    const { el, items, groupToggles, itemToggles, onToggleGroup, onToggleItem } = await mountRunTrace([
      group('single-group', [call('single-search', 'web_search')]),
      group('search-batch', [
        call('batch-search-1', 'web_search'),
        call('batch-search-2', 'web_search'),
      ]),
    ], { showBulkToggle: true })

    expect(bulkButton(el)?.textContent?.trim()).toBe('Expand all')
    bulkButton(el)?.click()
    await nextTick()

    expect([...itemToggles]).toEqual([
      'single-search',
      'batch-search-1',
      'batch-search-2',
    ])
    expect([...groupToggles]).toEqual(['search-batch'])
    expect(bodyCount(el)).toBe(3)

    onToggleGroup.mockClear()
    onToggleItem.mockClear()
    const singleGroup = items.value[0] as ToolGroupItem
    const searchBatch = items.value[1] as ToolGroupItem
    singleGroup.group.calls[0].status = 'error'
    singleGroup.group.calls[0].isError = true
    searchBatch.group.calls[0].status = 'error'
    searchBatch.group.calls[0].isError = true
    await nextTick()

    expect(onToggleItem.mock.calls.map(([key]) => key)).toEqual([
      'single-search',
      'batch-search-1',
    ])
    expect(onToggleGroup.mock.calls.map(([key]) => key)).toEqual(['search-batch'])
    expect(itemToggles.has('single-search')).toBe(false)
    expect(groupToggles.has('search-batch')).toBe(false)
    expect(groupHeader(el, 'search-batch')?.getAttribute('aria-expanded')).toBe('true')
    expect(bodyCount(el)).toBe(3)
    expect(bulkButton(el)?.textContent?.trim()).toBe('Collapse all')
  })

  it('collapses member details as well as their multi-call group', async () => {
    useToolDetailPreference().setMode('compact')
    const { el } = await mountRunTrace([
      group('first-batch', [
        call('first-search', 'web_search'),
        call('first-read', 'read_file'),
      ]),
      group('second-batch', [
        call('second-search', 'web_search'),
        call('second-read', 'read_file'),
      ]),
    ], { showBulkToggle: true })

    bulkButton(el)?.click()
    await nextTick()

    expect(el.querySelectorAll('.tool-row--member[aria-expanded="true"]')).toHaveLength(4)
    expect(bodyCount(el)).toBe(4)

    bulkButton(el)?.click()
    await nextTick()
    expect(bodyCount(el)).toBe(0)

    groupHeader(el, 'first-batch')?.click()
    await nextTick()
    expect(el.querySelectorAll('.tool-row--member[aria-expanded="false"]')).toHaveLength(2)
    expect(bodyCount(el)).toBe(0)
  })

  it('isolates repeated provider group ids by message scope', async () => {
    const el = document.createElement('div')
    document.body.appendChild(el)
    const groupToggles = reactive(new Set<string>())
    const itemToggles = reactive(new Set<string>())
    const firstItems = [
      group('shared-first', [call('a-1', 'web_search'), call('a-2', 'web_search')]),
      group('shared-second', [call('a-3', 'web_search'), call('a-4', 'web_search')]),
    ]
    const secondItems = [
      group('shared-first', [call('b-1', 'web_search'), call('b-2', 'web_search')]),
      group('shared-second', [call('b-3', 'web_search'), call('b-4', 'web_search')]),
    ]
    const onToggleGroup = vi.fn((id: string) => flip(groupToggles, id))
    const onToggleItem = vi.fn((id: string) => flip(itemToggles, id))
    const trace = (items: ChatStreamTimelineItem[], stateScope: string) => h(RunTrace, {
      items,
      stateScope,
      showBulkToggle: true,
      isToolGroupOpen: (id: string) => groupToggles.has(id),
      isToolItemOpen: (id: string) => itemToggles.has(id),
      onToggleGroup,
      onToggleItem,
    })
    const Host = defineComponent({
      setup() {
        return () => h('div', [
          h('section', { id: 'message-a' }, [trace(firstItems, 'message-a')]),
          h('section', { id: 'message-b' }, [trace(secondItems, 'message-b')]),
        ])
      },
    })
    const app = createApp(Host)
    mountedApps.push(app)
    app.use(i18n)
    app.mount(el)
    await nextTick()

    el.querySelector<HTMLButtonElement>('#message-a [data-testid="run-trace-bulk-toggle"]')?.click()
    await nextTick()

    expect(onToggleGroup.mock.calls.map(([id]) => id)).toEqual([
      'message-a:shared-first',
      'message-a:shared-second',
    ])
    expect(el.querySelectorAll('#message-a .tool-row--group[aria-expanded="true"]')).toHaveLength(2)
    expect(el.querySelectorAll('#message-b .tool-row--group[aria-expanded="false"]')).toHaveLength(2)
  })

  it('isolates repeated provider tool ids by message scope', async () => {
    const el = document.createElement('div')
    document.body.appendChild(el)
    const groupToggles = reactive(new Set<string>())
    const itemToggles = reactive(new Set<string>())
    const sharedItems = [
      group('shared-first', [call('shared-tool-1', 'web_search')]),
      group('shared-second', [call('shared-tool-2', 'web_search')]),
    ]
    const onToggleGroup = vi.fn((id: string) => flip(groupToggles, id))
    const onToggleItem = vi.fn((id: string) => flip(itemToggles, id))
    const trace = (stateScope: string) => h(RunTrace, {
      items: sharedItems,
      stateScope,
      showBulkToggle: true,
      isToolGroupOpen: (id: string) => groupToggles.has(id),
      isToolItemOpen: (id: string) => itemToggles.has(id),
      onToggleGroup,
      onToggleItem,
    })
    const Host = defineComponent({
      setup() {
        return () => h('div', [
          h('section', { id: 'tool-message-a' }, [trace('message-a')]),
          h('section', { id: 'tool-message-b' }, [trace('message-b')]),
        ])
      },
    })
    const app = createApp(Host)
    mountedApps.push(app)
    app.use(i18n)
    app.mount(el)
    await nextTick()

    el.querySelector<HTMLButtonElement>('#tool-message-a [data-testid="run-trace-bulk-toggle"]')?.click()
    await nextTick()

    expect(onToggleItem.mock.calls.map(([id]) => id)).toEqual([
      'message-a:shared-tool-1',
      'message-a:shared-tool-2',
    ])
    expect(el.querySelectorAll('#tool-message-a .tool-row-body')).toHaveLength(2)
    expect(el.querySelectorAll('#tool-message-b .tool-row-body')).toHaveLength(0)
  })

  it('preserves an override when a live single-call row becomes a group', async () => {
    const { el, items, groupToggles, itemToggles, onToggleGroup } = await mountRunTrace([
      group('growing-group', [call('first-search', 'web_search')]),
      group('other-group', [call('other-search', 'web_search')]),
    ], { showBulkToggle: true, stateScope: 'stream' })

    bulkButton(el)?.click()
    await nextTick()
    expect(new Set(itemToggles)).toEqual(new Set([
      'stream:first-search',
      'stream:other-search',
    ]))

    onToggleGroup.mockClear()
    const growingGroup = items.value[0] as ToolGroupItem
    items.value = [
      {
        ...growingGroup,
        group: {
          ...growingGroup.group,
          calls: [
            ...growingGroup.group.calls,
            call('second-search', 'web_search'),
          ],
        },
      },
      items.value[1],
    ]
    await nextTick()

    expect(onToggleGroup.mock.calls.map(([key]) => key)).toEqual(['stream:growing-group'])
    expect(groupToggles.has('stream:growing-group')).toBe(true)
    expect(groupHeader(el, 'growing-group')?.getAttribute('aria-expanded')).toBe('true')
    expect(bulkButton(el)?.textContent?.trim()).toBe('Collapse all')
  })

  it('appears when a second tool group is appended during streaming', async () => {
    const { el, items } = await mountRunTrace([
      { type: 'text', key: 'leading-text', html: '<p>Starting the first tool</p>' },
      group('search-group', [call('search', 'web_search')]),
    ], { showBulkToggle: true })

    expect(bulkButton(el)).toBeNull()

    items.value = [
      ...items.value,
      { type: 'text', key: 'middle-text', html: '<p>Starting the second tool</p>' },
      group('shell-group', [call('shell', 'shell')]),
    ]
    await nextTick()

    expect(bulkButton(el)?.textContent?.trim()).toBe('Collapse all')
    expect(el.querySelectorAll('[data-testid="run-trace-bulk-toolbar"]')).toHaveLength(1)
    expect(el.querySelector('.tool-timeline__summary')?.textContent?.trim()).toBe('2 calls')
    expect(bodyCount(el)).toBe(1)

    const timelineChildren = Array.from(
      el.querySelector('.tool-row-group')?.children ?? [],
    ) as HTMLElement[]
    expect(timelineChildren.map(child => {
      if (child.matches('.msg-ai-text')) return child.textContent?.trim()
      if (child.matches('.tool-timeline__toolbar')) return 'bulk-toolbar'
      return child.querySelector('.tool-row__label')?.textContent?.trim()
    })).toEqual([
      'Starting the first tool',
      'bulk-toolbar',
      'search-group',
      'Starting the second tool',
      'shell-group',
    ])

    bulkButton(el)?.click()
    await nextTick()
    expect(bodyCount(el)).toBe(0)
  })

  it('is enabled by the chat timeline wrapper, including the live checklist variant', async () => {
    const el = document.createElement('div')
    document.body.appendChild(el)
    const itemToggles = reactive(new Set<string>())
    const onToggleItem = vi.fn((renderKey: string) => flip(itemToggles, renderKey))
    const items = [
      group('search-group', [call('search', 'web_search')]),
      group('shell-group', [call('shell', 'shell')]),
    ]
    const Host = defineComponent({
      setup() {
        return () => h(ToolCallTimeline, {
          items,
          variant: 'checklist',
          isToolGroupOpen: () => false,
          isToolItemOpen: (renderKey: string) => itemToggles.has(renderKey),
          toolGroupStatusText: () => '',
          toolStatusText: () => '',
          toolSecondaryText: () => '',
          onToggleItem,
        })
      },
    })

    const app = createApp(Host)
    mountedApps.push(app)
    app.use(i18n)
    app.mount(el)
    await nextTick()

    expect(el.querySelector('.tool-timeline--checklist')).not.toBeNull()
    expect(el.querySelector('.tool-timeline__toolbar')?.nextElementSibling)
      .toBe(el.querySelector('.step-card'))
    expect(bulkButton(el)?.textContent?.trim()).toBe('Collapse all')
    expect(bodyCount(el)).toBe(1)

    bulkButton(el)?.click()
    await nextTick()

    expect(onToggleItem.mock.calls.map(([key]) => key)).toEqual(['shell'])
    expect(bodyCount(el)).toBe(0)
  })
})

describe('RunTrace tool detail preference', () => {
  it('keeps the existing per-tool defaults in Auto mode', async () => {
    const { el } = await mountRunTrace([
      group('search-group', [call('search', 'web_search')]),
      group('command-group', [call('command', 'shell')]),
    ])

    expect(toolRow(el, 'web.search')?.getAttribute('aria-expanded')).toBe('false')
    expect(toolRow(el, 'command.run')?.getAttribute('aria-expanded')).toBe('true')
  })

  it('collapses ordinary tools in Compact mode but still opens errors and error groups', async () => {
    useToolDetailPreference().setMode('compact')
    const failed = call('failed', 'web_search', {
      status: 'error',
      isError: true,
      result: 'failed',
      resultPreview: 'failed',
    })
    const { el } = await mountRunTrace([
      group('command-group', [call('command', 'shell')]),
      group('error-group', [failed]),
      group('mixed-group', [call('read', 'read_file'), failed]),
    ])

    expect(toolRow(el, 'command.run')?.getAttribute('aria-expanded')).toBe('false')
    expect(toolRow(el, 'web.search')?.getAttribute('aria-expanded')).toBe('true')
    expect(groupHeader(el, 'mixed-group')?.getAttribute('aria-expanded')).toBe('true')
  })

  it('opens every ordinary tool in Expanded mode', async () => {
    useToolDetailPreference().setMode('expanded')
    const { el } = await mountRunTrace([
      group('search-group', [call('search', 'web_search')]),
      group('command-group', [call('command', 'shell')]),
    ])

    expect(toolRow(el, 'web.search')?.getAttribute('aria-expanded')).toBe('true')
    expect(toolRow(el, 'command.run')?.getAttribute('aria-expanded')).toBe('true')
  })

  it('keeps a manual override stable when the global default changes', async () => {
    const { el, itemToggles } = await mountRunTrace([
      group('first-search-group', [call('first-search', 'web_search')]),
      group('second-search-group', [call('second-search', 'web_search')]),
    ])
    const rows = el.querySelectorAll<HTMLButtonElement>('.tool-row[data-op="web.search"]')

    rows[0].click()
    await nextTick()
    expect(rows[0].getAttribute('aria-expanded')).toBe('true')
    expect(rows[1].getAttribute('aria-expanded')).toBe('false')
    expect(itemToggles.has('first-search')).toBe(true)

    useToolDetailPreference().setMode('expanded')
    await nextTick()

    expect(rows[0].getAttribute('aria-expanded')).toBe('true')
    expect(rows[1].getAttribute('aria-expanded')).toBe('true')
    expect(itemToggles.has('first-search')).toBe(false)
  })

  it('derives the bulk action from Compact and Expanded defaults', async () => {
    useToolDetailPreference().setMode('compact')
    const compact = await mountRunTrace([
      group('search-group', [call('search', 'web_search')]),
      group('command-group', [call('command', 'shell')]),
    ], { showBulkToggle: true })
    expect(bulkButton(compact.el)?.textContent?.trim()).toBe('Expand all')

    useToolDetailPreference().setMode('expanded')
    await nextTick()
    expect(bulkButton(compact.el)?.textContent?.trim()).toBe('Collapse all')
  })
})
