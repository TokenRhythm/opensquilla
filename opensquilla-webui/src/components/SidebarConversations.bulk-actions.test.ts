// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import SidebarConversations, { type SidebarSection } from './SidebarConversations.vue'

const mounted: App[] = []

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

async function mountSidebar(options: {
  sections?: SidebarSection[]
  error?: boolean
} = {}) {
  i18n.global.locale.value = 'en'
  const root = document.createElement('div')
  document.body.appendChild(root)
  const sections: SidebarSection[] = options.sections ?? [{
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
    error: options.error ?? false,
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

describe('SidebarConversations bulk actions', () => {
  it('offers rename and delete actions for automation run records', async () => {
    await mountSidebar({
      sections: [{
        family: 'automations',
        label: 'Automations',
        rows: [{
          rowKind: 'session',
          key: 'cron-session-1',
          title: 'Cron isolated run',
          effectiveAgentId: 'main',
          agentName: 'Main',
          sessionKind: 'cron',
          depth: 0,
          runStatus: 'idle',
          runLabel: 'Idle',
          taskAttention: 'none',
          updatedAt: Date.now(),
          hasContractGaps: false,
        }],
      }],
    })

    const menuButton = document.body.querySelector<HTMLButtonElement>(
      '[aria-label="Actions for Cron isolated run"]',
    )
    expect(menuButton).not.toBeNull()
    menuButton?.click()
    await nextTick()

    const menuText = document.body.querySelector('.sidebar-row-menu')?.textContent
    expect(menuText).toContain('Rename')
    expect(menuText).toContain('Delete')
  })

  it('does not render the conversations region until a session exists', async () => {
    const root = await mountSidebar({ sections: [] })

    expect(root.querySelector('.sidebar-history')).toBeNull()
    expect(root.textContent).not.toContain('No tasks yet')
    expect(root.textContent).not.toContain('Start a task')
  })

  it('keeps the retry state visible when loading sessions fails', async () => {
    const root = await mountSidebar({ sections: [], error: true })

    expect(root.querySelector('.sidebar-history')).toBeTruthy()
    expect(root.textContent).toContain('Unable to load tasks')
  })

  it('uses a disabled trash action until a task is selected', async () => {
    const root = await mountSidebar()
    const manage = root.querySelector<HTMLButtonElement>('[aria-label="Manage tasks"]')
    manage?.click()
    await nextTick()

    const emptyDelete = root.querySelector<HTMLButtonElement>('[aria-label="Delete 0 selected"]')
    expect(emptyDelete?.disabled).toBe(true)
    expect(emptyDelete?.innerHTML).toContain('M19 6v14')
    expect(root.querySelector('[aria-label="Exit selection"]')).not.toBeNull()

    root.querySelector<HTMLButtonElement>('.sidebar-history-item')?.click()
    await nextTick()

    const selectedDelete = root.querySelector<HTMLButtonElement>('[aria-label="Delete 1 selected"]')
    expect(selectedDelete?.disabled).toBe(false)
  })

  it('exits selection mode and clears the current selection', async () => {
    const root = await mountSidebar()
    root.querySelector<HTMLButtonElement>('[aria-label="Manage tasks"]')?.click()
    await nextTick()

    root.querySelector<HTMLButtonElement>('.sidebar-history-item')?.click()
    await nextTick()
    expect(root.querySelector('[aria-label="Delete 1 selected"]')).not.toBeNull()

    const exit = root.querySelector<HTMLButtonElement>('[aria-label="Exit selection"]')
    expect(exit?.textContent?.trim()).toBe('Done')
    exit?.click()
    await nextTick()

    expect(root.querySelector('[aria-label="Exit selection"]')).toBeNull()
    expect(root.querySelector('[aria-label="Manage tasks"]')).not.toBeNull()

    root.querySelector<HTMLButtonElement>('[aria-label="Manage tasks"]')?.click()
    await nextTick()
    expect(root.querySelector('[aria-label="Delete 0 selected"]')).not.toBeNull()
  })
})
