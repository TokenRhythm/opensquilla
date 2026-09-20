// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import { createI18n } from 'vue-i18n'
import SidebarConversations, { COLLAPSE_STORAGE_KEY } from './SidebarConversations.vue'
import type { SidebarSectionRow } from '@/composables/useSessions'

const mounted: App[] = []

function row(key: string, overrides: Partial<SidebarSectionRow> = {}): SidebarSectionRow {
  return {
    key, title: key, rowKind: 'session', sessionKind: 'chat', effectiveAgentId: 'main',
    agentName: 'Main', depth: 0, runStatus: 'idle', runLabel: 'Idle', taskAttention: 'none',
    updatedAt: 1, hasContractGaps: false, ...overrides,
  }
}

function child(key: string, parentKey: string, overrides: Partial<SidebarSectionRow> = {}) {
  return row(key, { parentKey, sessionKind: 'task', depth: 1, ...overrides })
}

async function mountSidebar(rows: SidebarSectionRow[], currentKey = '') {
  const state = reactive({ rows, currentKey })
  const onReorder = vi.fn()
  const onSelect = vi.fn()
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp(() => h(SidebarConversations, {
    sections: [{ family: 'chats', label: 'Tasks', rows: state.rows }],
    error: false, loading: false, currentKey: state.currentKey,
    contractDebugEnabled: false, searchHint: 'Ctrl+K', onReorder, onSelect,
  }))
  app.use(createI18n({
    legacy: false, locale: 'en', missingWarn: false, fallbackWarn: false,
    messages: { en: { shared: { sidebar: {
      subtaskCount: '{count} subtasks', subtasksRunning: '{count} running',
      subtasksAttention: '{count} need attention', expandSubtasks: 'Show subtasks for {title}',
      collapseSubtasks: 'Hide subtasks for {title}', moveUp: 'Move up', moveDown: 'Move down',
    } } } },
  }))
  app.mount(host)
  mounted.push(app)
  await nextTick()
  const find = (key: string) => host.querySelector<HTMLElement>(`[data-session-key="${key}"]`)
  const toggle = async (key: string) => {
    find(key)?.querySelector<HTMLButtonElement>('.sidebar-task-disclosure')?.click()
    await nextTick()
    await vi.waitFor(() => expect(host.querySelector('.sidebar-row-leave-active')).toBeNull())
  }
  return { host, state, find, toggle, onReorder, onSelect }
}

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('SidebarConversations task hierarchy', () => {
  it('collapses completed descendants by default and remembers explicit expansion', async () => {
    const rows = [row('parent'), child('child', 'parent'), child('grandchild', 'child'), row('other')]
    const sidebar = await mountSidebar(rows)
    expect(sidebar.find('child')).toBeNull()
    expect(sidebar.find('grandchild')).toBeNull()
    expect(sidebar.find('parent')?.textContent).toContain('2 subtasks')
    expect(sidebar.find('other')).not.toBeNull()

    await sidebar.toggle('parent')
    expect(sidebar.find('child')).not.toBeNull()
    expect(sidebar.find('grandchild')).toBeNull()
    await sidebar.toggle('child')
    expect(sidebar.find('grandchild')).not.toBeNull()
    expect(sidebar.onSelect).not.toHaveBeenCalled()

    const restored = await mountSidebar(rows)
    expect(restored.find('grandchild')).not.toBeNull()
  })

  it('keeps running or failed descendants visible and summarizes them after manual collapse', async () => {
    const sidebar = await mountSidebar([
      row('parent'),
      child('running', 'parent', { runStatus: 'running', taskAttention: 'running' }),
      child('failed', 'parent', { runStatus: 'failed' }),
      child('completed', 'parent', { taskAttention: 'completed' }),
    ])
    expect(sidebar.find('running')).not.toBeNull()
    expect(sidebar.find('failed')).not.toBeNull()
    expect(sidebar.find('parent')?.querySelector('.sidebar-subtask-summary')?.getAttribute('title')).toBe('3 subtasks · 1 running · 1 need attention')
    expect(sidebar.find('parent')?.querySelector('[aria-label="1 need attention"]')).not.toBeNull()
    await sidebar.toggle('parent')
    expect(sidebar.find('running')).toBeNull()
    expect(sidebar.find('failed')).toBeNull()
    expect(sidebar.find('parent')?.querySelector('.sidebar-subtask-summary.has-attention')).not.toBeNull()
  })

  it('reveals selected descendants across persisted nested collapses and capped depths', async () => {
    localStorage.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify({ 'task:parent': true, 'task:child': true }))
    const sidebar = await mountSidebar([
      row('parent'), child('child', 'parent', { depth: 3 }),
      child('grandchild', 'child', { depth: 3 }), row('other'),
    ], 'grandchild')
    expect(sidebar.find('grandchild')?.querySelector('[aria-current="page"]')).not.toBeNull()
    expect(sidebar.find('parent')?.querySelector('.sidebar-task-disclosure')?.getAttribute('aria-expanded')).toBe('true')
    await sidebar.toggle('parent')
    expect(sidebar.find('grandchild')).toBeNull()
    sidebar.state.currentKey = 'other'
    await nextTick()
    sidebar.state.currentKey = 'grandchild'
    await nextTick()
    expect(sidebar.find('grandchild')).not.toBeNull()
  })

  it('preserves a pinned parent relationship without hiding unrelated adjacent tasks', async () => {
    const sidebar = await mountSidebar([
      row('parent', { pinned: true }), child('child', 'parent'),
      child('orphan', 'missing'), row('other'),
    ])
    expect(sidebar.find('parent')?.dataset.sidebarZone).toBe('pinned')
    expect(sidebar.find('child')).toBeNull()
    expect(sidebar.find('orphan')).not.toBeNull()
    expect(sidebar.find('other')).not.toBeNull()
    await sidebar.toggle('parent')
    expect(sidebar.find('child')).not.toBeNull()
  })

  it('keeps collapsed subtasks accessible during bulk selection', async () => {
    const sidebar = await mountSidebar([row('parent'), child('child', 'parent')])
    expect(sidebar.find('child')).toBeNull()
    sidebar.host.querySelector<HTMLButtonElement>('.sidebar-bulk-mode-btn')!.click()
    await nextTick()

    expect(sidebar.find('parent')?.querySelector('.sidebar-task-disclosure')).not.toBeNull()
    await sidebar.toggle('parent')
    sidebar.find('child')!.querySelector<HTMLButtonElement>('.sidebar-history-item')!.click()
    await nextTick()
    expect(sidebar.find('child')?.classList.contains('is-selected')).toBe(true)
    expect(sidebar.onSelect).not.toHaveBeenCalled()

    await sidebar.toggle('parent')
    await sidebar.toggle('parent')
    expect(sidebar.find('child')?.classList.contains('is-selected')).toBe(false)
  })

  it('reveals the selected task when its project lineage arrives in a later page', async () => {
    localStorage.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify({ 'project:project-a': true, 'task:parent': true }))
    const sidebar = await mountSidebar([
      row('workspace:project-a', { rowKind: 'workspace', sessionKind: 'workspace', workspaceId: 'project-a' }),
      row('parent', { workspaceId: 'project-a', depth: 1 }),
    ], 'child')
    expect(sidebar.find('parent')).toBeNull()
    sidebar.state.rows.push(child('child', 'parent', { workspaceId: 'project-a', depth: 2 }))
    await nextTick()
    expect(sidebar.find('parent')).not.toBeNull()
    expect(sidebar.find('child')?.querySelector('[aria-current="page"]')).not.toBeNull()
  })

  it('does not initiate dragging from the disclosure control', async () => {
    const sidebar = await mountSidebar([row('parent'), child('child', 'parent')])
    const disclosure = sidebar.find('parent')!.querySelector<HTMLButtonElement>('.sidebar-task-disclosure')!
    disclosure.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 1, button: 0, pointerType: 'mouse', clientX: 20, clientY: 20 }))
    document.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, pointerId: 1, clientX: 20, clientY: 80 }))
    await nextTick()
    expect(document.querySelector('.sidebar-session-drag-preview')).toBeNull()
    expect(sidebar.onReorder).not.toHaveBeenCalled()
  })

  it('moves tasks from the keyboard menu within their ordering scope and restores focus', async () => {
    const sidebar = await mountSidebar([
      row('pinned', { pinned: true }), row('first'), child('child', 'first'), row('second'),
    ])
    const trigger = sidebar.find('first')!.querySelector<HTMLButtonElement>('.sidebar-row-menu-btn')!
    trigger.click()
    await nextTick()
    await nextTick()
    const down = document.querySelector<HTMLButtonElement>('[data-session-action="move-down"]')!
    expect(document.querySelector('[data-session-action="move-up"]')).toBeNull()
    const menu = document.querySelector<HTMLElement>('.sidebar-row-menu')!
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    expect(document.activeElement).toBe(down)
    down.click()
    await nextTick()
    expect(sidebar.onReorder).toHaveBeenCalledExactlyOnceWith({ draggedKey: 'first', targetKey: 'second', position: 'after' })
    expect(document.activeElement).toBe(trigger)
    expect(sidebar.onSelect).not.toHaveBeenCalled()
  })
})
