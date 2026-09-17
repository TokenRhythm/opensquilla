// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import SidebarConversations, { type SidebarSectionRow } from './SidebarConversations.vue'

const mounted: App[] = []

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

function taskRow(
  key: string,
  taskAttention: SidebarSectionRow['taskAttention'],
): SidebarSectionRow {
  return {
    rowKind: 'session',
    key,
    title: key,
    effectiveAgentId: 'main',
    agentName: 'Main',
    sessionKind: 'chat',
    depth: 0,
    runStatus: taskAttention === 'running' ? 'running' : 'idle',
    runLabel: taskAttention === 'running' ? 'Running' : 'Idle',
    taskAttention,
    updatedAt: Date.now(),
    hasContractGaps: false,
  }
}

async function mountSidebar(rows: SidebarSectionRow[]) {
  i18n.global.locale.value = 'en'
  const root = document.createElement('div')
  document.body.appendChild(root)
  const app = createApp(SidebarConversations, {
    sections: [{ family: 'chats', label: 'Tasks', rows }],
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

describe('SidebarConversations task attention', () => {
  it('renders right-side running, completed, failed, and reserved empty states', async () => {
    const root = await mountSidebar([
      taskRow('running-task', 'running'),
      taskRow('completed-task', 'completed'),
      taskRow('failed-task', 'failed'),
      taskRow('idle-task', 'none'),
    ])
    const indicators = [...root.querySelectorAll<HTMLElement>('[data-testid="sidebar-task-attention"]')]

    expect(indicators).toHaveLength(4)
    expect(indicators[0].classList).toContain('sidebar-task-attention--running')
    expect(indicators[0].getAttribute('aria-label')).toBe('Task running')
    expect(indicators[1].classList).toContain('sidebar-task-attention--completed')
    expect(indicators[1].getAttribute('aria-label')).toBe('Task completed, result not viewed')
    expect(indicators[2].classList).toContain('sidebar-task-attention--failed')
    expect(indicators[2].getAttribute('aria-label')).toBe('Task unfinished, details not viewed')
    expect(indicators[3].classList).toContain('sidebar-task-attention--none')
    expect(indicators[3].getAttribute('aria-hidden')).toBe('true')
    expect(root.querySelector('.sidebar-history-run')).toBeNull()
  })
})
