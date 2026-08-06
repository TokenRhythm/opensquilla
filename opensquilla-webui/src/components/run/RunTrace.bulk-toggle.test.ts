// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, reactive, ref, type App } from 'vue'

import i18n from '@/i18n'
import type {
  ChatStreamTimelineItem,
  ChatToolCallRenderItem,
} from '@/types/chat'
import { useToolDetailPreference } from '@/composables/useToolDetailPreference'
import RunTrace from './RunTrace.vue'
import runTraceSource from './RunTrace.vue?raw'

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

function groupHeader(el: HTMLElement, groupId: string): HTMLButtonElement | null {
  return el.querySelector<HTMLButtonElement>(`.tool-row--group[data-op="${groupId}"]`)
}

function toolRow(el: HTMLElement, operationKey: string): HTMLButtonElement | null {
  return el.querySelector<HTMLButtonElement>(`.tool-row[data-op="${operationKey}"]`)
}

function ruleBody(selector: string) {
  const selectorStart = runTraceSource.indexOf(selector)
  expect(selectorStart).toBeGreaterThanOrEqual(0)
  const blockStart = runTraceSource.indexOf('{', selectorStart)
  const blockEnd = runTraceSource.indexOf('}', blockStart)
  return runTraceSource.slice(blockStart + 1, blockEnd)
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

describe('RunTrace hierarchy presentation', () => {
  it('renders multi-call members indented under their group header', async () => {
    const { el } = await mountRunTrace([
      group('read-batch', [
        call('search', 'web_search'),
        call('read', 'read_file'),
      ]),
      group('single-group', [call('shell', 'shell')]),
    ], { initialGroupToggles: ['read-batch'] })

    // No bulk toolbar chrome: no call-count summary, no expand/collapse-all.
    expect(el.querySelector('.tool-timeline__toolbar')).toBeNull()
    expect(el.querySelector('[data-testid="run-trace-bulk-toggle"]')).toBeNull()

    // Hierarchy is expressed through indentation: members live in a nested
    // container with a left guide, single calls stay at the top level.
    expect(el.querySelector('.step-group-members')).not.toBeNull()
    expect(el.querySelector('.tool-row--member')).not.toBeNull()
    expect(ruleBody('.step-group-members')).toContain('padding-left: 1.25rem;')
    expect(ruleBody('.tool-timeline--checklist .step-group-members')).toContain(
      'border-left: 1px solid var(--hairline);',
    )
  })

  it('keeps per-row disclosure independent of any bulk state', async () => {
    const { el, onToggleItem, onToggleGroup, itemToggles, groupToggles } = await mountRunTrace([
      group('read-batch', [
        call('search', 'web_search'),
        call('read', 'read_file'),
      ]),
      group('single-group', [call('shell', 'shell')]),
    ])

    // Single-call row: opening it toggles the item only.
    toolRow(el, 'command.run')?.click()
    await nextTick()
    expect(onToggleItem.mock.calls.map(([key]) => key)).toEqual(['shell'])
    expect([...itemToggles]).toEqual(['shell'])

    // Multi-call group: opening the header toggles the group only; members
    // keep their own rows with their own disclosure.
    groupHeader(el, 'read-batch')?.click()
    await nextTick()
    expect(onToggleGroup.mock.calls.map(([key]) => key)).toEqual(['read-batch'])
    expect([...groupToggles]).toEqual(['read-batch'])
    expect(groupHeader(el, 'read-batch')?.getAttribute('aria-expanded')).toBe('true')
    expect(el.querySelectorAll('.tool-row--member')).toHaveLength(2)
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
})
