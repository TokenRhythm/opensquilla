// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import SidebarConversations, { type SidebarSection } from './SidebarConversations.vue'

const platformState = vi.hoisted(() => ({ isDesktop: false }))

vi.mock('@/platform', () => ({
  usePlatform: () => ({
    capabilities: { isDesktop: platformState.isDesktop },
  }),
}))

const mounted: App[] = []

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  platformState.isDesktop = false
  vi.useRealTimers()
  vi.restoreAllMocks()
  localStorage.clear()
})

async function mountSidebar(options: {
  onSelect?: (key: string) => void
  onCopySessionLink?: (key: string) => void
  runStatus?: string
  runLabel?: string
} = {}) {
  i18n.global.locale.value = 'en'
  const root = document.createElement('div')
  document.body.appendChild(root)
  const sections: SidebarSection[] = [{
    family: 'chats',
    label: 'Tasks',
    rows: [{
      rowKind: 'session',
      key: 'session-1',
      title: 'First task',
      effectiveAgentId: 'main',
      agentName: 'Main',
      sessionKind: 'chat',
      depth: 0,
      runStatus: options.runStatus ?? 'idle',
      runLabel: options.runLabel ?? 'Idle',
      taskAttention: 'none',
      updatedAt: Date.now(),
      hasContractGaps: false,
    }],
  }]
  const app = createApp(SidebarConversations, {
    sections,
    error: false,
    loading: false,
    currentKey: '',
    contractDebugEnabled: false,
    searchHint: '⌘K',
    onSelect: options.onSelect,
    onCopySessionLink: options.onCopySessionLink,
  })
  app.use(i18n)
  app.mount(root)
  mounted.push(app)
  await nextTick()
  return root
}

function dispatchContextMenu(target: Element): MouseEvent {
  const event = new MouseEvent('contextmenu', {
    bubbles: true,
    cancelable: true,
    clientX: 120,
    clientY: 180,
  })
  target.dispatchEvent(event)
  return event
}

describe('SidebarConversations session context menu', () => {
  it('opens the existing row actions at the pointer in the desktop client', async () => {
    platformState.isDesktop = true
    const root = await mountSidebar()
    const row = root.querySelector<HTMLElement>('.sidebar-history-item')
    expect(row).not.toBeNull()

    const event = dispatchContextMenu(row!)
    await nextTick()

    expect(event.defaultPrevented).toBe(true)
    const menu = document.body.querySelector<HTMLElement>('.sidebar-row-menu')
    expect(menu?.style.left).toBe('120px')
    expect(menu?.style.top).toBe('180px')
    const actions = Array.from(
      menu?.querySelectorAll<HTMLElement>('.sidebar-row-menu__item') ?? [],
    ).map(item => item.textContent?.trim())
    expect(actions).toEqual(['Pin task', 'Rename', 'Copy session link', 'Delete'])
  })

  it('opens the same actions on WebUI without selecting the target session', async () => {
    const onSelect = vi.fn()
    const root = await mountSidebar({ onSelect })
    const row = root.querySelector<HTMLElement>('.sidebar-history-item')
    expect(row).not.toBeNull()

    const event = dispatchContextMenu(row!)
    await nextTick()

    expect(event.defaultPrevented).toBe(true)
    expect(document.body.querySelector('.sidebar-row-menu')).not.toBeNull()
    expect(onSelect).not.toHaveBeenCalled()
    const fromContext = document.querySelector('.sidebar-row-menu')?.textContent
    document.body.click()
    await nextTick()
    root.querySelector<HTMLButtonElement>('.sidebar-row-menu-btn')?.click()
    await nextTick()
    expect(document.querySelector('.sidebar-row-menu')?.textContent).toBe(fromContext)
  })

  it('leaves the native context menu outside session rows untouched', async () => {
    const root = await mountSidebar()
    const event = dispatchContextMenu(root)
    await nextTick()
    expect(event.defaultPrevented).toBe(false)
    expect(document.querySelector('.sidebar-row-menu')).toBeNull()
  })

  it.each([
    { key: 'F10', shiftKey: true },
    { key: 'ContextMenu', shiftKey: false },
  ])('supports $key and returns focus on Escape without selecting a session', async (key) => {
    vi.useFakeTimers()
    const onSelect = vi.fn()
    const root = await mountSidebar({ onSelect })
    const row = root.querySelector<HTMLButtonElement>('.sidebar-history-item')!
    row.focus()
    const event = new KeyboardEvent('keydown', { ...key, bubbles: true, cancelable: true })
    row.dispatchEvent(event)
    await nextTick()
    await nextTick()
    expect(event.defaultPrevented).toBe(true)
    const menu = document.querySelector<HTMLElement>('.sidebar-row-menu')!
    const items = menu.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')
    expect(document.activeElement).toBe(items[0])
    items[0]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }))
    expect(document.activeElement).toBe(items[items.length - 1])
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    await vi.advanceTimersByTimeAsync(500)
    expect(document.activeElement).toBe(row)
    expect(document.querySelector('.sidebar-row-menu')).toBeNull()
    expect(document.querySelector('.sidebar-session-preview')).toBeNull()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('copies the context target through one action without navigating to it', async () => {
    vi.useFakeTimers()
    const onCopySessionLink = vi.fn()
    const onSelect = vi.fn()
    const root = await mountSidebar({ onCopySessionLink, onSelect })
    dispatchContextMenu(root.querySelector('.sidebar-history-item')!)
    await nextTick()
    expect(document.querySelectorAll('[data-session-action="copy-link"]')).toHaveLength(1)
    const copyAction = document.querySelector<HTMLButtonElement>('[data-session-action="copy-link"]')!
    copyAction.focus()
    copyAction.click()
    await nextTick()
    await vi.advanceTimersByTimeAsync(500)
    expect(onCopySessionLink).toHaveBeenCalledExactlyOnceWith('session-1')
    expect(onSelect).not.toHaveBeenCalled()
    expect(document.querySelector('.sidebar-row-menu')).toBeNull()
    expect(document.activeElement).toBe(root.querySelector('.sidebar-history-item'))
    expect(document.querySelector('.sidebar-session-preview')).toBeNull()
  })

  it('fits the actual menu size within the viewport for long labels and optional actions', async () => {
    const root = await mountSidebar()
    const original = HTMLElement.prototype.getBoundingClientRect
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (!this.classList.contains('sidebar-row-menu')) return original.call(this)
      return { left: 120, top: window.innerHeight - 100, width: 260, height: 280 } as DOMRect
    })
    dispatchContextMenu(root.querySelector('.sidebar-history-item')!)
    await nextTick()
    await nextTick()
    const menu = document.querySelector<HTMLElement>('.sidebar-row-menu')!
    expect(menu.style.top).toBe(`${window.innerHeight - 288}px`)
    expect(menu.style.transform).toBe('none')
  })
})

describe('SidebarConversations restrained hover preview', () => {
  it('waits 400ms before opening and cancels a passing hover', async () => {
    vi.useFakeTimers()
    const root = await mountSidebar()
    const row = root.querySelector<HTMLElement>('.sidebar-history-row')!
    row.dispatchEvent(new MouseEvent('mouseenter'))
    await vi.advanceTimersByTimeAsync(399)
    expect(document.querySelector('.sidebar-session-preview')).toBeNull()
    row.dispatchEvent(new MouseEvent('mouseleave'))
    await vi.advanceTimersByTimeAsync(500)
    expect(document.querySelector('.sidebar-session-preview')).toBeNull()
    row.dispatchEvent(new MouseEvent('mouseenter'))
    await vi.advanceTimersByTimeAsync(400)
    expect(document.querySelector('.sidebar-session-preview')?.textContent).toContain('First task')
    expect(document.querySelector('.sidebar-session-preview__status')).toBeNull()
  })

  it('keeps a preview readable under the pointer and dismisses it with Escape', async () => {
    vi.useFakeTimers()
    const root = await mountSidebar({ runStatus: 'queued', runLabel: 'Queued' })
    const row = root.querySelector<HTMLElement>('.sidebar-history-row')!
    row.dispatchEvent(new MouseEvent('mouseenter'))
    await vi.advanceTimersByTimeAsync(400)
    const preview = document.querySelector<HTMLElement>('.sidebar-session-preview')!
    expect(preview.querySelector('.sidebar-session-preview__status')?.textContent).toBe('Queued')
    row.dispatchEvent(new MouseEvent('mouseleave'))
    preview.dispatchEvent(new MouseEvent('mouseenter'))
    await vi.advanceTimersByTimeAsync(500)
    expect(document.querySelector('.sidebar-session-preview')).toBe(preview)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(document.querySelector('.sidebar-session-preview')).toBeNull()
  })

  it('cancels a pending preview when the row menu opens or the component unmounts', async () => {
    vi.useFakeTimers()
    const root = await mountSidebar()
    const row = root.querySelector<HTMLElement>('.sidebar-history-row')!
    row.dispatchEvent(new MouseEvent('mouseenter'))
    await vi.advanceTimersByTimeAsync(200)
    dispatchContextMenu(root.querySelector('.sidebar-history-item')!)
    await vi.advanceTimersByTimeAsync(500)
    expect(document.querySelector('.sidebar-session-preview')).toBeNull()
    document.body.click()
    await nextTick()
    row.dispatchEvent(new MouseEvent('mouseenter'))
    mounted.pop()!.unmount()
    await vi.advanceTimersByTimeAsync(500)
    expect(document.querySelector('.sidebar-session-preview')).toBeNull()
  })
})
