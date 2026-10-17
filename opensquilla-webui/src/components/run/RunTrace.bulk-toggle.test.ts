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
import { createPinia } from 'pinia'

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

async function mountRunTrace(
  initialItems: ChatStreamTimelineItem[],
  options: {
    presentation?: 'activity'
    showBulkToggle?: boolean
    stateScope?: string
    groupChoices?: Map<string, boolean>
    itemChoices?: Map<string, boolean>
  } = {},
) {
  const el = document.createElement('div')
  document.body.appendChild(el)

  const items = ref<ChatStreamTimelineItem[]>(initialItems)
  const groupToggles = reactive(options.groupChoices ?? new Map<string, boolean>())
  const itemToggles = reactive(options.itemChoices ?? new Map<string, boolean>())
  const onToggleGroup = vi.fn((groupId: string, open: boolean) => groupToggles.set(groupId, open))
  const onToggleItem = vi.fn((renderKey: string, open: boolean) => itemToggles.set(renderKey, open))

  const Host = defineComponent({
    setup() {
      return () => h(RunTrace, {
        items: items.value,
        presentation: options.presentation,
        ...(options.showBulkToggle === undefined
          ? {}
          : { showBulkToggle: options.showBulkToggle }),
        ...(options.stateScope === undefined
          ? {}
          : { stateScope: options.stateScope }),
        isToolGroupOpen: (groupId: string) => groupToggles.get(groupId),
        isToolItemOpen: (renderKey: string) => itemToggles.get(renderKey),
        onToggleGroup,
        onToggleItem,
      })
    },
  })

  const app = createApp(Host)
  mountedApps.push(app)
  app.use(i18n)
  app.use(createPinia())
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
  useToolDetailPreference().setMode('collapsed')
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('RunTrace bulk toggle', () => {
  it('is opt-in and counts top-level groups rather than members', async () => {
    const one = await mountRunTrace([group('batch', [call('a', 'shell'), call('b', 'read_file')])], { showBulkToggle: true })
    expect(bulkButton(one.el)).toBeNull()
    const two = await mountRunTrace([group('a', [call('a', 'shell')]), group('b', [call('b', 'read_file')])])
    expect(bulkButton(two.el)).toBeNull()
  })

  it('expands and collapses every single-call detail with explicit choices', async () => {
    const { el, itemToggles, onToggleItem } = await mountRunTrace([
      group('search', [call('search', 'web_search')]),
      group('shell', [call('shell', 'shell')]),
    ], { showBulkToggle: true })
    expect(bodyCount(el)).toBe(0)
    expect(bulkButton(el)?.textContent?.trim()).toBe('Expand all')
    expect(bulkButton(el)?.hasAttribute('aria-expanded')).toBe(false)
    bulkButton(el)?.click()
    await nextTick()
    expect(bodyCount(el)).toBe(2)
    expect(onToggleItem.mock.calls).toEqual([['search', true], ['shell', true]])
    expect(bulkButton(el)?.textContent?.trim()).toBe('Collapse all')
    bulkButton(el)?.click()
    await nextTick()
    expect(bodyCount(el)).toBe(0)
    expect([...itemToggles]).toEqual([['search', false], ['shell', false]])
  })

  it('applies bulk choices to groups and all members, including rows already matching', async () => {
    const { el, groupToggles, itemToggles } = await mountRunTrace([
      group('reads', [call('search', 'web_search'), call('read', 'read_file')]),
      group('writes', [call('shell', 'shell'), call('write', 'write_file')]),
    ], { showBulkToggle: true })
    expect(bodyCount(el)).toBe(0)
    bulkButton(el)?.click()
    await nextTick()
    expect(bodyCount(el)).toBe(4)
    expect(groupHeader(el, 'reads')?.getAttribute('aria-expanded')).toBe('true')
    expect(el.querySelectorAll('.tool-row--member[aria-expanded="true"]')).toHaveLength(4)
    toolRow(el, 'file.inspect')?.click()
    await nextTick()
    expect(itemToggles.get('read')).toBe(false)
    bulkButton(el)?.click()
    await nextTick()
    expect(bodyCount(el)).toBe(0)
    expect([...groupToggles.values()]).toEqual([false, false])
    expect([...itemToggles.values()]).toEqual([false, false, false, false])
    useToolDetailPreference().setMode('expanded')
    await nextTick()
    expect(bodyCount(el)).toBe(0)
  })

  it('keeps a bulk choice through new defaults and late failures', async () => {
    const { el, items, onToggleItem } = await mountRunTrace([
      group('single', [call('single', 'web_search')]),
      group('batch', [call('first', 'web_search'), call('second', 'web_search')]),
    ], { showBulkToggle: true })
    bulkButton(el)?.click()
    await nextTick()
    onToggleItem.mockClear()
    const batch = items.value[1] as ToolGroupItem
    batch.group.calls[0].isError = true
    batch.group.calls[0].status = 'error'
    useToolDetailPreference().setMode('expanded')
    await nextTick()
    useToolDetailPreference().setMode('collapsed')
    await nextTick()
    expect(bodyCount(el)).toBe(3)
    expect(onToggleItem).not.toHaveBeenCalled()
  })

  it.each(['group', 'item'] as const)('isolates repeated %s ids by message scope', async kind => {
    const groupChoices = reactive(new Map<string, boolean>())
    const itemChoices = reactive(new Map<string, boolean>())
    const entries = kind === 'group'
      ? [group('shared', [call('one', 'shell'), call('two', 'read_file')])]
      : [group('shared', [call('one', 'shell')])]
    const a = await mountRunTrace(entries, { stateScope: 'session:turn-a', groupChoices, itemChoices })
    const b = await mountRunTrace(entries, { stateScope: 'session:turn-b', groupChoices, itemChoices })
    const selector = kind === 'group' ? '.tool-row--group' : '.tool-row'
    a.el.querySelector<HTMLButtonElement>(selector)?.click()
    await nextTick()
    expect(a.el.querySelector(selector)?.getAttribute('aria-expanded')).toBe('true')
    expect(b.el.querySelector(selector)?.getAttribute('aria-expanded')).toBe('false')
  })

  it.each([true, false])('carries a manual %s from a single row to its growing group', async choice => {
    useToolDetailPreference().setMode(choice ? 'collapsed' : 'expanded')
    const { el, items, groupToggles } = await mountRunTrace([
      group('growing', [call('first', 'web_search')]),
    ], { stateScope: 'turn' })
    toolRow(el, 'web.search')?.click()
    await nextTick()
    items.value = [group('growing', [call('first', 'web_search'), call('second', 'web_search')])]
    await nextTick()
    expect(groupToggles.get('turn:growing')).toBe(choice)
    expect(groupHeader(el, 'growing')?.getAttribute('aria-expanded')).toBe(String(choice))
    useToolDetailPreference().setMode(choice ? 'expanded' : 'collapsed')
    await nextTick()
    expect(groupHeader(el, 'growing')?.getAttribute('aria-expanded')).toBe(String(choice))
  })

  it('does not overwrite an existing group choice when a batch grows', async () => {
    const { items, groupToggles } = await mountRunTrace([
      group('growing', [call('first', 'web_search')]),
    ], { groupChoices: new Map([['growing', false]]), itemChoices: new Map([['first', true]]) })
    items.value = [group('growing', [call('first', 'web_search'), call('second', 'web_search')])]
    await nextTick()
    expect(groupToggles.get('growing')).toBe(false)
  })

  it('inserts the bulk action next to new tools without moving narration', async () => {
    const { el, items } = await mountRunTrace([
      { type: 'text', key: 'before', html: '<p>First progress</p>' },
      group('search-group', [call('search', 'web_search')]),
    ], { showBulkToggle: true })
    expect(bulkButton(el)).toBeNull()
    items.value = [...items.value,
      { type: 'text', key: 'between', html: '<p>Next progress</p>' },
      group('shell-group', [call('shell', 'shell')]),
    ]
    await nextTick()
    expect(el.querySelectorAll('[data-testid="run-trace-bulk-toolbar"]')).toHaveLength(1)
    expect(el.querySelector('.tool-timeline__summary')?.textContent?.trim()).toBe('2 calls')
    const children = Array.from(el.querySelector('.tool-row-group')?.children ?? [])
    expect(children.map(child => child.matches('.msg-ai-text') ? child.textContent?.trim()
      : child.matches('.tool-timeline__toolbar') ? 'bulk-toolbar'
      : child.querySelector('.tool-row__label')?.textContent?.trim())).toEqual([
      'First progress', 'bulk-toolbar', 'search-group', 'Next progress', 'shell-group',
    ])
  })

  it.each([undefined, 'activity'] as const)('forwards explicit choices through the chat wrapper (%s)', async presentation => {
    const el = document.createElement('div')
    document.body.appendChild(el)
    const choices = reactive(new Map<string, boolean>())
    const onToggleItem = vi.fn((id: string, open: boolean) => choices.set(id, open))
    const app = createApp(defineComponent({ setup: () => () => h(ToolCallTimeline, {
      items: [group('shell', [call('shell', 'shell', { inputRaw: '{"command":"npm test"}' })]), group('read', [call('read', 'read_file')])],
      presentation, variant: 'checklist',
      isToolGroupOpen: () => undefined, isToolItemOpen: (id: string) => choices.get(id),
      toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '', onToggleItem,
    }) }))
    mountedApps.push(app)
    app.use(i18n).use(createPinia()).mount(el)
    await nextTick()
    expect(Boolean(bulkButton(el))).toBe(presentation !== 'activity')
    toolRow(el, 'command.run')?.click()
    await nextTick()
    expect(onToggleItem).toHaveBeenLastCalledWith('shell', true)
    expect(toolRow(el, 'command.run')?.getAttribute('aria-expanded')).toBe('true')
    toolRow(el, 'command.run')?.click()
    await nextTick()
    expect(onToggleItem).toHaveBeenLastCalledWith('shell', false)
    expect(toolRow(el, 'command.run')?.getAttribute('aria-expanded')).toBe('false')
  })
})

describe('RunTrace tool detail defaults', () => {
  it.each(['collapsed', 'expanded'] as const)('uses %s for every tool type including failures', async mode => {
    useToolDetailPreference().setMode(mode)
    const { el } = await mountRunTrace([group('mixed', [
      call('read', 'read_file', { inputRaw: '{"path":"src/a.ts"}', result: 'source' }),
      call('command', 'shell', { inputRaw: '{"command":"npm test"}' }),
      call('write', 'write_file', { inputRaw: '{"path":"src/a.ts","content":"source"}', result: 'Written 6 bytes' }),
      call('failed', 'custom_tool', { status: 'error', isError: true, result: 'Network unavailable' }),
    ])], { presentation: 'activity' })
    const expected = String(mode === 'expanded')
    expect(el.querySelector('.tool-row--group')).toBeNull()
    expect(el.querySelectorAll('.tool-row')).toHaveLength(4)
    for (const row of el.querySelectorAll('.tool-row')) expect(row.getAttribute('aria-expanded')).toBe(expected)
    expect(el.querySelector('.tool-row--error')?.textContent).not.toContain(i18n.global.t('shared.runTrace.activityNotCompleted'))
    expect(el.querySelector('.tool-row--error .tool-row__status-dot')?.getAttribute('aria-label'))
      .toContain(i18n.global.t('shared.runTrace.activityNotCompleted'))
    expect(el.textContent?.includes('Network unavailable')).toBe(mode === 'expanded')
  })

  it.each([true, false])('retains manual %s through a complete mode round trip while untouched rows follow defaults', async choice => {
    useToolDetailPreference().setMode(choice ? 'collapsed' : 'expanded')
    const { el, itemToggles } = await mountRunTrace([
      group('read', [call('read', 'read_file', { inputRaw: '{"path":"src/a.ts"}', result: 'source' })]),
      group('shell', [call('shell', 'shell', { inputRaw: '{"command":"npm test"}' })]),
    ], { presentation: 'activity' })
    toolRow(el, 'file.inspect')?.click()
    await nextTick()
    for (const mode of ['collapsed', 'expanded', 'collapsed', 'expanded'] as const) {
      useToolDetailPreference().setMode(mode)
      await nextTick()
      expect(toolRow(el, 'file.inspect')?.getAttribute('aria-expanded')).toBe(String(choice))
      expect(toolRow(el, 'command.run')?.getAttribute('aria-expanded')).toBe(String(mode === 'expanded'))
    }
    expect(itemToggles.get('read')).toBe(choice)
  })

  it('keeps an explicitly closed tool closed as its batch grows and execution fails', async () => {
    useToolDetailPreference().setMode('expanded')
    const first = call('first', 'shell', { inputRaw: '{"command":"npm test"}', isRunning: true })
    const { el, items } = await mountRunTrace([group('batch', [first])], { presentation: 'activity' })
    toolRow(el, 'command.run')?.click()
    await nextTick()
    items.value = [group('batch', [
      { ...first, isRunning: false, status: 'error', isError: true, result: 'Validation failed' },
      call('next', 'read_file', { inputRaw: '{"path":"src/a.ts"}', result: 'source' }),
    ])]
    await nextTick()
    expect(toolRow(el, 'command.run')?.getAttribute('aria-expanded')).toBe('false')
    expect(toolRow(el, 'file.inspect')?.getAttribute('aria-expanded')).toBe('true')
    expect(el.querySelector('.tool-row__error-summary')).toBeNull()
    expect(el.textContent).not.toContain('Validation failed')
    expect(toolRow(el, 'command.run')?.querySelector('.tool-row__status-dot')).not.toBeNull()
  })

  it('retains choices through remounts with a different render key for the same tool', async () => {
    const choices = reactive(new Map<string, boolean>())
    const live = await mountRunTrace([group('live', [call('live-key', 'shell', { toolId: 'same-call' })])], {
      stateScope: 'session:turn', itemChoices: choices,
    })
    toolRow(live.el, 'command.run')?.click()
    await nextTick()
    expect(choices.get('session:turn:same-call')).toBe(true)
    const history = await mountRunTrace([group('history', [call('history-key', 'shell', { toolId: 'same-call' })])], {
      stateScope: 'session:turn', itemChoices: choices,
    })
    expect(toolRow(history.el, 'command.run')?.getAttribute('aria-expanded')).toBe('true')
    toolRow(history.el, 'command.run')?.click()
    await nextTick()
    expect(toolRow(live.el, 'command.run')?.getAttribute('aria-expanded')).toBe('false')
  })

  it('keeps absent detail bodies non-expandable in expanded mode', async () => {
    useToolDetailPreference().setMode('expanded')
    const { el } = await mountRunTrace([group('empty', [call('empty', 'custom_tool', {
      inputRaw: '', inputPreview: '', result: '', resultPreview: '',
    })])])
    expect(el.querySelector('.tool-row')?.hasAttribute('aria-expanded')).toBe(false)
    expect(bodyCount(el)).toBe(0)
  })
})
