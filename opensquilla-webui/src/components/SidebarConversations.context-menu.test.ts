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
})

async function mountSidebar() {
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
      runStatus: 'idle',
      runLabel: 'Idle',
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
    expect(actions).toEqual(['Pin task', 'Rename', 'Delete'])
  })

  it('leaves the browser context menu untouched on the web build', async () => {
    const root = await mountSidebar()
    const row = root.querySelector<HTMLElement>('.sidebar-history-item')
    expect(row).not.toBeNull()

    const event = dispatchContextMenu(row!)
    await nextTick()

    expect(event.defaultPrevented).toBe(false)
    expect(document.body.querySelector('.sidebar-row-menu')).toBeNull()
  })
})
